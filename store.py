"""Durable, tenant-scoped storage and a SQLite-backed agent task queue.

Every operation opens its own connection, making Store safe to share between
HTTP threads and independent worker processes. SQLite is the queue authority;
the browser connection never owns a task's lifetime.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import errno
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator


STATUSES = frozenset({"queued", "running", "waiting_config", "succeeded", "failed", "interrupted", "cancelled"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "interrupted", "cancelled"})
MAX_PROMPT_BYTES = 1024 * 1024
AGENT_UID = 10001


def prepare_agent_directory(path: Path, *, subdirectories: tuple[str, ...] = ()) -> Path:
    """Make host-created tenant directories usable by the container's UID.

    The parent is host-managed; child entries are tenant-controlled. Open each
    directory without following symlinks and change the opened inode only.
    Do not recursively take ownership of arbitrary tenant files or link targets.
    Non-root local previews keep their existing filesystem identity.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in subdirectories:
            if not name or name in ('.', '..') or '/' in name:
                raise ValueError('Expected a single directory name')
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    # Preserve a tenant's replacement entry without touching
                    # its target. The web file API also rejects these links.
                    continue
                raise
            try:
                if os.geteuid() == 0:
                    os.fchown(child, AGENT_UID, AGENT_UID)
            finally:
                os.close(child)
        if os.geteuid() == 0:
            os.fchown(descriptor, AGENT_UID, AGENT_UID)
    finally:
        os.close(descriptor)
    return path


class StoreError(Exception):
    pass


class ValidationError(StoreError):
    pass


class NotFound(StoreError):
    pass


class Conflict(StoreError):
    pass


class AuthError(StoreError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _username(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("Username must be a string")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,31}", value):
        raise ValidationError("Username must be 3–32 letters, digits, underscores or hyphens")
    return value


def _content(value: str, *, name: str = "Content", allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"{name} must be a nonempty string")
    if len(value.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValidationError(f"{name} is too large (maximum {MAX_PROMPT_BYTES} bytes)")
    return value


class Store:
    def __init__(self, data_dir: str | Path = "/srv/agent-cloud/data", *, lease_seconds: float = 90):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace_root = self.data_dir / "workspaces"
        self.workspace_root.mkdir(mode=0o700, exist_ok=True)
        self.db_path = self.data_dir / "agent-cloud.sqlite3"
        self.lease_seconds = float(lease_seconds)
        if self.lease_seconds <= 0:
            raise ValidationError("Lease duration must be positive")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    permissions_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(id, user_id)
                );
                CREATE INDEX IF NOT EXISTS conversations_user ON conversations(user_id, updated_at);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    conversation_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','running','waiting_config','succeeded','failed','interrupted','cancelled')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    worker_id TEXT,
                    lease_expires_at REAL,
                    heartbeat_at REAL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result TEXT,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(user_id, idempotency_key),
                    UNIQUE(id, user_id),
                    FOREIGN KEY(conversation_id, user_id) REFERENCES conversations(id, user_id)
                );
                CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS tasks_user ON tasks(user_id, created_at);
                CREATE INDEX IF NOT EXISTS tasks_lease ON tasks(status, lease_expires_at);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    conversation_id TEXT NOT NULL,
                    task_id TEXT,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','system','tool')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(conversation_id, user_id) REFERENCES conversations(id, user_id),
                    FOREIGN KEY(task_id, user_id) REFERENCES tasks(id, user_id)
                );
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(user_id, conversation_id, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(task_id, user_id) REFERENCES tasks(id, user_id)
                );
                CREATE INDEX IF NOT EXISTS events_user_cursor ON events(user_id, id);
                CREATE INDEX IF NOT EXISTS events_task_cursor ON events(task_id, id);
                CREATE TABLE IF NOT EXISTS settings (
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(scope,key)
                );
                PRAGMA user_version = 2;
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
            if "is_admin" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            if "permissions_json" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN permissions_json TEXT NOT NULL DEFAULT '{}'")
        # New database files contain password hashes, sessions and user content.
        os.chmod(self.db_path, 0o600)

    def workspace_path(self, user_id: str) -> Path:
        # IDs are generated here and never taken from a filesystem path supplied
        # by the browser. Requiring a real user also prevents arbitrary paths.
        self.get_user(user_id)
        return self.workspace_root / user_id

    def prepare_workspace(self, user_id: str) -> Path:
        return prepare_agent_directory(self.workspace_path(user_id),
                                       subdirectories=('home', 'files'))

    @contextmanager
    def open_workspace(self, user_id: str) -> Iterator[int]:
        """Anchor a real user's workspace below the host-managed root.

        Tenant-controlled directory entries are opened relative to an existing
        directory descriptor. Replacing an ancestor with a link cannot redirect
        a later operation to another tenant or a host directory.
        """
        self.get_user(user_id)
        if not isinstance(user_id, str) or not re.fullmatch(r'[0-9a-f]{32}', user_id):
            raise NotFound('User not found')
        root = os.open(self.workspace_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = None
        try:
            created = False
            try:
                os.mkdir(user_id, mode=0o700, dir_fd=root)
                created = True
            except FileExistsError:
                pass
            descriptor = os.open(user_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=root)
            if created and os.geteuid() == 0:
                os.fchown(descriptor, AGENT_UID, AGENT_UID)
            yield descriptor
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(root)

    def conversation_workspace_path(self, user_id: str, conversation_id: str) -> Path:
        """Return the filesystem location only after validating ownership.

        This path is useful for container working directories and display; HTTP
        file operations use open_conversation_workspace instead of reopening it.
        """
        if not isinstance(conversation_id, str) or not re.fullmatch(r'[0-9a-f]{32}', conversation_id):
            raise NotFound('Conversation not found')
        self.get_conversation(user_id, conversation_id)
        return self.workspace_path(user_id) / 'conversations' / conversation_id

    @contextmanager
    def open_conversation_workspace(self, user_id: str, conversation_id: str) -> Iterator[int]:
        """Prepare and open an owned conversation scope without following links."""
        self.conversation_workspace_path(user_id, conversation_id)
        with self.open_workspace(user_id) as workspace:
            descriptor = os.dup(workspace)
            try:
                for name in ('conversations', conversation_id):
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                    os.fchmod(descriptor, 0o700)
                    if os.geteuid() == 0:
                        os.fchown(descriptor, AGENT_UID, AGENT_UID)
                yield descriptor
            finally:
                os.close(descriptor)

    def prepare_conversation_workspace(self, user_id: str, conversation_id: str) -> Path:
        path = self.conversation_workspace_path(user_id, conversation_id)
        with self.open_conversation_workspace(user_id, conversation_id):
            pass
        return path

    def _public_user(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "username": row["username"], "created_at": row["created_at"],
                "workspace_path": str(self.workspace_root / row["id"]),
                "is_admin": bool(row["is_admin"]), "permissions": json.loads(row["permissions_json"])}

    @staticmethod
    def _require_user(connection: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise NotFound("User not found")
        return row

    @staticmethod
    def _require_conversation(connection: sqlite3.Connection, user_id: str, conversation_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                                 (conversation_id, user_id)).fetchone()
        if row is None:
            raise NotFound("Conversation not found")
        return row

    @staticmethod
    def _require_task(connection: sqlite3.Connection, user_id: str, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)).fetchone()
        if row is None:
            raise NotFound("Task not found")
        return row

    @staticmethod
    def _task(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["metadata"] = json.loads(task.pop("metadata_json"))
        task.pop("request_hash", None)
        task["cancel_requested"] = bool(task["cancel_requested"])
        return task

    @staticmethod
    def _emit(connection: sqlite3.Connection, user_id: str, task_id: str | None,
              event_type: str, payload: Any, now: float) -> int:
        cursor = connection.execute(
            "INSERT INTO events(user_id,task_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?)",
            (user_id, task_id, event_type, _json(payload), now))
        return cursor.lastrowid

    def create_user(self, username: str, password: str, is_admin: bool = False) -> dict[str, Any]:
        username = _username(username)
        if not isinstance(password, str) or len(password) < 8 or len(password.encode("utf-8")) > 4096:
            raise ValidationError("Password must be at least 8 characters and at most 4096 bytes")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
        user_id, now = uuid.uuid4().hex, time.time()
        with self._transaction() as connection:
            try:
                connection.execute("INSERT INTO users(id,username,password_salt,password_hash,created_at,is_admin) VALUES(?,?,?,?,?,?)",
                                   (user_id, username, salt, digest, now, int(bool(is_admin))))
            except sqlite3.IntegrityError as exc:
                raise Conflict("Username is already registered") from exc
            prepare_agent_directory(self.workspace_root / user_id,
                                    subdirectories=('home', 'files'))
            return self._public_user(self._require_user(connection, user_id))

    def get_user(self, user_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            return self._public_user(self._require_user(connection, user_id))

    def list_all_users(self) -> list[dict[str, Any]]:
        """Trusted operator/worker interface; server must gate any HTTP exposure."""
        with self._connection() as connection:
            return [self._public_user(row) for row in connection.execute("SELECT * FROM users ORDER BY created_at,id")]

    def user_permissions(self, user_id: str) -> dict[str, Any]:
        return self.get_user(user_id)["permissions"]

    def set_user_permissions(self, user_id: str, permissions: dict[str, Any]) -> dict[str, Any]:
        """Trusted operator interface. Application code must authorize its caller."""
        if not isinstance(permissions, dict):
            raise ValidationError("Permissions must be an object")
        with self._transaction() as connection:
            self._require_user(connection, user_id)
            connection.execute("UPDATE users SET permissions_json=? WHERE id=?", (_json(permissions), user_id))
            return self._public_user(self._require_user(connection, user_id))

    @staticmethod
    def _setting_key(key: str) -> str:
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValidationError("Setting key must be 1–200 characters")
        return key

    def get_setting(self, key: str, default: Any = None, *, user_id: str | None = None) -> Any:
        key = self._setting_key(key)
        with self._connection() as connection:
            if user_id is not None:
                self._require_user(connection, user_id)
            row = connection.execute("SELECT value_json FROM settings WHERE scope=? AND key=?", (user_id or "", key)).fetchone()
            return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value: Any, *, user_id: str | None = None) -> None:
        key = self._setting_key(key)
        with self._transaction() as connection:
            if user_id is not None:
                self._require_user(connection, user_id)
            connection.execute("""INSERT INTO settings(scope,key,value_json,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(scope,key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (user_id or "", key, _json(value), time.time()))

    def list_settings(self, *, user_id: str | None = None) -> dict[str, Any]:
        with self._connection() as connection:
            if user_id is not None:
                self._require_user(connection, user_id)
            return {row["key"]: json.loads(row["value_json"]) for row in connection.execute(
                "SELECT key,value_json FROM settings WHERE scope=? ORDER BY key", (user_id or "",))}

    def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        try:
            username = _username(username)
        except ValidationError:
            return None
        if not isinstance(password, str) or len(password.encode("utf-8")) > 4096:
            return None
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        # A dummy derivation reduces the username-enumeration timing difference.
        salt = row["password_salt"] if row else b"\0" * 16
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
        if row is None or not hmac.compare_digest(digest, row["password_hash"]):
            return None
        return self._public_user(row)

    def create_session(self, user_id: str, *, ttl: float = 7 * 24 * 3600) -> str:
        if ttl <= 0:
            raise ValidationError("Session lifetime must be positive")
        token, now = secrets.token_urlsafe(32), time.time()
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._transaction() as connection:
            self._require_user(connection, user_id)
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest, user_id, now, now + ttl))
        return token

    def get_session(self, token: str | None, *, now: float | None = None) -> dict[str, Any] | None:
        if not isinstance(token, str) or len(token) > 256:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                (digest, time.time() if now is None else now)).fetchone()
            return self._public_user(row) if row else None

    def revoke_session(self, token: str) -> None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))

    def create_conversation(self, user_id: str, title: str = "New conversation") -> dict[str, Any]:
        title = _content(title, name="Title")
        if len(title) > 200:
            raise ValidationError("Title must be at most 200 characters")
        conversation_id, now = uuid.uuid4().hex, time.time()
        with self._transaction() as connection:
            self._require_user(connection, user_id)
            connection.execute("INSERT INTO conversations VALUES(?,?,?,?,?)", (conversation_id, user_id, title, now, now))
            return dict(self._require_conversation(connection, user_id, conversation_id))

    def get_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            return dict(self._require_conversation(connection, user_id, conversation_id))

    def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._require_user(connection, user_id)
            return [dict(row) for row in connection.execute(
                "SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC,id", (user_id,))]

    def add_message(self, user_id: str, conversation_id: str, role: str, content: str,
                    *, task_id: str | None = None) -> dict[str, Any]:
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValidationError("Invalid message role")
        content = _content(content, allow_empty=True)
        now = time.time()
        with self._transaction() as connection:
            self._require_conversation(connection, user_id, conversation_id)
            if task_id:
                task = self._require_task(connection, user_id, task_id)
                if task["conversation_id"] != conversation_id:
                    raise NotFound("Task not found in this conversation")
            cursor = connection.execute(
                "INSERT INTO messages(user_id,conversation_id,task_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, conversation_id, task_id, role, content, now))
            connection.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            message = dict(connection.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone())
            self._emit(connection, user_id, task_id, "message", message, now)
            return message

    def list_messages(self, user_id: str, conversation_id: str, *, after: int = 0,
                      limit: int = 1000) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._require_conversation(connection, user_id, conversation_id)
            return [dict(row) for row in connection.execute(
                "SELECT * FROM messages WHERE user_id=? AND conversation_id=? AND id>? ORDER BY id LIMIT ?",
                (user_id, conversation_id, max(0, int(after)), max(1, min(int(limit), 1000))))]

    @staticmethod
    def _page_limit(limit: int) -> int:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("Page size must be between 1 and 200")
        return limit

    @staticmethod
    def _record_cursor(value: int | None) -> int | None:
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ValidationError("Invalid record cursor")
        return value

    def list_messages_page(self, user_id: str, conversation_id: str, *, before: int | None = None,
                           after: int | None = None, limit: int = 100) -> dict[str, Any]:
        """Latest messages by default; before loads history, after catches up live."""
        limit = self._page_limit(limit)
        before, after = self._record_cursor(before), self._record_cursor(after)
        if before is not None and after is not None:
            raise ValidationError("Use either before or after, not both")
        with self._connection() as connection:
            self._require_conversation(connection, user_id, conversation_id)
            condition, arguments = "user_id=? AND conversation_id=?", [user_id, conversation_id]
            if before is not None:
                condition += " AND id<?"
                arguments.append(before)
            elif after is not None:
                condition += " AND id>?"
                arguments.append(after)
            direction = "ASC" if after is not None else "DESC"
            arguments.append(limit + 1)
            rows = connection.execute("SELECT * FROM messages WHERE " + condition + " ORDER BY id " + direction + " LIMIT ?", arguments).fetchall()
            has_more = len(rows) > limit
            items = [dict(row) for row in rows[:limit]]
            if after is None:
                items.reverse()
            next_cursor = (items[-1 if after is not None else 0]["id"] if has_more and items else None)
            return {"items": items, "has_more": has_more, "next_cursor": next_cursor}

    def submit_task(self, user_id: str, conversation_id: str, prompt: str,
                    idempotency_key: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        prompt = _content(prompt, name="Prompt")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise ValidationError("An idempotency key of 1–200 characters is required")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError("Metadata must be an object")
        encoded = _json(metadata or {})
        request_hash = hashlib.sha256(_json([conversation_id, prompt, metadata or {}]).encode("utf-8")).hexdigest()
        task_id, now = uuid.uuid4().hex, time.time()
        with self._transaction() as connection:
            self._require_conversation(connection, user_id, conversation_id)
            existing = connection.execute("SELECT * FROM tasks WHERE user_id=? AND idempotency_key=?",
                                          (user_id, idempotency_key)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise Conflict("Idempotency key was already used for a different request")
                task = self._task(existing)
                task["created"] = False
                return task
            connection.execute("""INSERT INTO tasks
                (id,user_id,conversation_id,prompt,idempotency_key,request_hash,status,created_at,updated_at,metadata_json)
                VALUES(?,?,?,?,?,?,'queued',?,?,?)""",
                (task_id, user_id, conversation_id, prompt, idempotency_key, request_hash, now, now, encoded))
            cursor = connection.execute(
                "INSERT INTO messages(user_id,conversation_id,task_id,role,content,created_at) VALUES(?,?,?,'user',?,?)",
                (user_id, conversation_id, task_id, prompt, now))
            connection.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            self._emit(connection, user_id, task_id, "message",
                       dict(connection.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone()), now)
            self._emit(connection, user_id, task_id, "task_submitted", {"status": "queued", "conversation_id": conversation_id}, now)
            task = self._task(self._require_task(connection, user_id, task_id))
            task["created"] = True
            return task

    def get_task(self, user_id: str, task_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            return self._task(self._require_task(connection, user_id, task_id))

    def list_tasks(self, user_id: str, *, conversation_id: str | None = None,
                   limit: int = 1000, before: float | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._require_user(connection, user_id)
            filters, arguments = ["user_id=?"], [user_id]
            if conversation_id is not None:
                self._require_conversation(connection, user_id, conversation_id)
                filters.append("conversation_id=?")
                arguments.append(conversation_id)
            if before is not None:
                filters.append("created_at<?")
                arguments.append(float(before))
            arguments.append(max(1, min(int(limit), 1000)))
            return [self._task(row) for row in connection.execute(
                "SELECT * FROM tasks WHERE " + " AND ".join(filters) + " ORDER BY created_at DESC,id LIMIT ?", arguments)]

    @staticmethod
    def _task_page_cursor(task: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(_json([task["created_at"], task["id"]]).encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_task_cursor(cursor: str) -> tuple[float, str]:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
            raise ValidationError("Invalid task cursor")
        try:
            decoded = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            if (not isinstance(decoded, list) or len(decoded) != 2 or not isinstance(decoded[0], (int, float))
                    or isinstance(decoded[0], bool) or not math.isfinite(decoded[0]) or not isinstance(decoded[1], str)
                    or not re.fullmatch(r"[a-f0-9]{32}", decoded[1])):
                raise ValueError("Invalid cursor values")
            return float(decoded[0]), decoded[1]
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise ValidationError("Invalid task cursor") from exc

    def list_tasks_page(self, user_id: str, *, conversation_id: str | None = None,
                        cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Stable tuple cursor prevents losing records with equal timestamps."""
        limit = self._page_limit(limit)
        with self._connection() as connection:
            self._require_user(connection, user_id)
            filters, arguments = ["user_id=?"], [user_id]
            if conversation_id is not None:
                self._require_conversation(connection, user_id, conversation_id)
                filters.append("conversation_id=?")
                arguments.append(conversation_id)
            if cursor is not None:
                created_at, task_id = self._decode_task_cursor(cursor)
                filters.append("(created_at<? OR (created_at=? AND id<?))")
                arguments.extend((created_at, created_at, task_id))
            arguments.append(limit + 1)
            rows = connection.execute("SELECT * FROM tasks WHERE " + " AND ".join(filters)
                                      + " ORDER BY created_at DESC,id DESC LIMIT ?", arguments).fetchall()
            items = [self._task(row) for row in rows[:limit]]
            has_more = len(rows) > limit
            return {"items": items, "has_more": has_more,
                    "next_cursor": self._task_page_cursor(items[-1]) if has_more and items else None}

    def get_tasks(self, user_id: str, task_ids: list[str], *, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """Refresh loaded active history without fetching every historical row."""
        if (not isinstance(task_ids, list) or len(task_ids) > 200
                or any(not isinstance(value, str) or not value for value in task_ids)):
            raise ValidationError("Provide at most 200 task IDs")
        unique = list(dict.fromkeys(task_ids))
        with self._connection() as connection:
            self._require_user(connection, user_id)
            if conversation_id is not None:
                self._require_conversation(connection, user_id, conversation_id)
            if not unique:
                return []
            condition = "user_id=? AND id IN (" + ",".join("?" for _ in unique) + ")"
            arguments = [user_id, *unique]
            if conversation_id is not None:
                condition += " AND conversation_id=?"
                arguments.append(conversation_id)
            found = {row["id"]: self._task(row) for row in connection.execute("SELECT * FROM tasks WHERE " + condition, arguments)}
            if len(found) != len(unique):
                raise NotFound("Task not found")
            return [found[task_id] for task_id in unique]

    def list_running_tasks(self) -> list[dict[str, Any]]:
        """Trusted worker interface for process/container recovery after restart."""
        with self._connection() as connection:
            return [self._task(row) for row in connection.execute("SELECT * FROM tasks WHERE status='running' ORDER BY created_at,id")]

    def append_event(self, user_id: str, task_id: str | None, event_type: str,
                     payload: Any, *, worker_id: str | None = None) -> int:
        if not isinstance(event_type, str) or not 1 <= len(event_type) <= 80:
            raise ValidationError("Invalid event type")
        with self._transaction() as connection:
            self._require_user(connection, user_id)
            if task_id:
                task = self._require_task(connection, user_id, task_id)
                if worker_id is not None and (task["status"] != "running" or task["worker_id"] != worker_id
                                               or task["lease_expires_at"] <= time.time()):
                    raise Conflict("Worker no longer owns this task")
            return self._emit(connection, user_id, task_id, event_type, payload, time.time())

    def list_events(self, user_id: str, *, after: int = 0, task_id: str | None = None,
                    limit: int = 500) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._require_user(connection, user_id)
            arguments = [user_id, max(0, int(after))]
            condition = "user_id=? AND id>?"
            if task_id is not None:
                self._require_task(connection, user_id, task_id)
                condition += " AND task_id=?"
                arguments.append(task_id)
            arguments.append(max(1, min(int(limit), 1000)))
            result = []
            for row in connection.execute("SELECT * FROM events WHERE " + condition + " ORDER BY id LIMIT ?", arguments):
                event = dict(row)
                event["cursor"] = event["id"]
                event["type"] = event.pop("event_type")
                event["payload"] = json.loads(event.pop("payload_json"))
                result.append(event)
            return result

    def list_events_page(self, user_id: str, *, before: int | None = None, after: int | None = None,
                         task_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Latest durable events by default, with backward and forward cursors."""
        limit = self._page_limit(limit)
        before, after = self._record_cursor(before), self._record_cursor(after)
        if before is not None and after is not None:
            raise ValidationError("Use either before or after, not both")
        with self._connection() as connection:
            self._require_user(connection, user_id)
            condition, arguments = "user_id=?", [user_id]
            if task_id is not None:
                self._require_task(connection, user_id, task_id)
                condition += " AND task_id=?"
                arguments.append(task_id)
            if before is not None:
                condition += " AND id<?"
                arguments.append(before)
            elif after is not None:
                condition += " AND id>?"
                arguments.append(after)
            direction = "ASC" if after is not None else "DESC"
            arguments.append(limit + 1)
            rows = connection.execute("SELECT * FROM events WHERE " + condition + " ORDER BY id " + direction + " LIMIT ?", arguments).fetchall()
            has_more, items = len(rows) > limit, []
            for row in rows[:limit]:
                event = dict(row)
                event["cursor"] = event["id"]
                event["type"] = event.pop("event_type")
                event["payload"] = json.loads(event.pop("payload_json"))
                items.append(event)
            if after is None:
                items.reverse()
            return {"items": items, "has_more": has_more,
                    "next_cursor": items[-1 if after is not None else 0]["id"] if has_more and items else None}

    def _recover_stale(self, connection: sqlite3.Connection, now: float) -> int:
        rows = connection.execute("SELECT * FROM tasks WHERE status='running' AND lease_expires_at<=?", (now,)).fetchall()
        for row in rows:
            error = "Worker lease expired; inspect persisted outputs before explicitly retrying"
            connection.execute("""UPDATE tasks SET status='interrupted',updated_at=?,finished_at=?,
                lease_expires_at=NULL,error=? WHERE id=?""", (now, now, error, row["id"]))
            self._emit(connection, row["user_id"], row["id"], "task_status",
                       {"status": "interrupted", "error": error, "reason": "lease_expired"}, now)
        return len(rows)

    def recover_stale_tasks(self, *, now: float | None = None) -> int:
        with self._transaction() as connection:
            return self._recover_stale(connection, time.time() if now is None else now)

    def claim_task(self, worker_id: str, *, max_concurrency: int = 1,
                    lease_seconds: float | None = None) -> dict[str, Any] | None:
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 200:
            raise ValidationError("Worker ID is required")
        if max_concurrency < 1:
            raise ValidationError("Concurrency must be at least one")
        duration = self.lease_seconds if lease_seconds is None else float(lease_seconds)
        if duration <= 0:
            raise ValidationError("Lease duration must be positive")
        with self._transaction() as connection:
            now = time.time()
            self._recover_stale(connection, now)
            running = connection.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
            if running >= max_concurrency:
                return None
            # A user's persistent computer executes tasks serially even when the
            # server's global concurrency is raised in a later deployment.
            row = connection.execute("""SELECT t.* FROM tasks t WHERE t.status='queued'
                AND NOT EXISTS(SELECT 1 FROM tasks active WHERE active.user_id=t.user_id AND active.status='running')
                ORDER BY t.created_at,t.id LIMIT 1""").fetchone()
            if row is None:
                return None
            connection.execute("""UPDATE tasks SET status='running',worker_id=?,lease_expires_at=?,
                heartbeat_at=?,started_at=COALESCE(started_at,?),updated_at=?,attempt_count=attempt_count+1 WHERE id=?""",
                (worker_id, now + duration, now, now, now, row["id"]))
            self._emit(connection, row["user_id"], row["id"], "task_status", {"status": "running"}, now)
            return self._task(self._require_task(connection, row["user_id"], row["id"]))

    def heartbeat(self, task_id: str, worker_id: str, *, lease_seconds: float | None = None) -> bool:
        duration = self.lease_seconds if lease_seconds is None else float(lease_seconds)
        if duration <= 0:
            raise ValidationError("Lease duration must be positive")
        with self._transaction() as connection:
            now = time.time()
            cursor = connection.execute("""UPDATE tasks SET heartbeat_at=?,lease_expires_at=?
                WHERE id=? AND worker_id=? AND status='running' AND lease_expires_at>? AND cancel_requested=0""",
                (now, now + duration, task_id, worker_id, now))
            return cursor.rowcount == 1

    def finish_task(self, task_id: str, worker_id: str, status: str = "succeeded", *,
                     result: str | None = None, error: str | None = None) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "interrupted", "cancelled", "waiting_config"}:
            raise ValidationError("Invalid completion status")
        with self._transaction() as connection:
            now = time.time()
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise NotFound("Task not found")
            if row["status"] != "running" or row["worker_id"] != worker_id or row["lease_expires_at"] <= now:
                raise Conflict("Worker no longer owns this task")
            # Cancellation can arrive between the worker's last check and its
            # completion transaction. Honor the request while retaining result
            # text as evidence of any work that already completed.
            if row["cancel_requested"]:
                status = "cancelled"
            finished = now if status in TERMINAL_STATUSES else None
            connection.execute("""UPDATE tasks SET status=?,result=?,error=?,updated_at=?,finished_at=?,
                lease_expires_at=NULL WHERE id=?""", (status, result, error, now, finished, task_id))
            self._emit(connection, row["user_id"], task_id, "task_status", {"status": status, "error": error}, now)
            return self._task(self._require_task(connection, row["user_id"], task_id))

    def mark_waiting_config(self, task_id: str, worker_id: str, reason: str = "Model API key is not configured") -> dict[str, Any]:
        return self.finish_task(task_id, worker_id, "waiting_config", error=reason)

    def requeue_waiting_tasks(self, user_id: str | None = None) -> int:
        """Called by trusted server code after configuration becomes available."""
        now = time.time()
        with self._transaction() as connection:
            if user_id is not None:
                self._require_user(connection, user_id)
                rows = connection.execute("SELECT * FROM tasks WHERE status='waiting_config' AND user_id=?", (user_id,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM tasks WHERE status='waiting_config'").fetchall()
            for row in rows:
                connection.execute("UPDATE tasks SET status='queued',worker_id=NULL,error=NULL,updated_at=? WHERE id=?", (now, row["id"]))
                self._emit(connection, row["user_id"], row["id"], "task_status", {"status": "queued", "reason": "configuration_available"}, now)
            return len(rows)

    def cancel_task(self, user_id: str, task_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            now = time.time()
            row = self._require_task(connection, user_id, task_id)
            if row["status"] in TERMINAL_STATUSES:
                return self._task(row)
            if row["status"] == "running":
                # Keep the execution slot occupied until the worker has actually
                # stopped its subprocess and acknowledges cancellation.
                if not row["cancel_requested"]:
                    connection.execute("UPDATE tasks SET cancel_requested=1,updated_at=? WHERE id=?", (now, task_id))
                    self._emit(connection, user_id, task_id, "cancel_requested", {"status": "running", "cancel_requested": True}, now)
                return self._task(self._require_task(connection, user_id, task_id))
            connection.execute("""UPDATE tasks SET status='cancelled',cancel_requested=1,updated_at=?,
                finished_at=?,lease_expires_at=NULL WHERE id=?""", (now, now, task_id))
            self._emit(connection, user_id, task_id, "task_status", {"status": "cancelled"}, now)
            return self._task(self._require_task(connection, user_id, task_id))

    def retry_task(self, user_id: str, task_id: str, idempotency_key: str) -> dict[str, Any]:
        """Explicit user retry creates a new task; an interrupted run is preserved."""
        task = self.get_task(user_id, task_id)
        if task["status"] not in {"failed", "interrupted", "cancelled"}:
            raise Conflict("Only failed, interrupted or cancelled tasks can be retried")
        metadata = dict(task["metadata"])
        metadata["retry_of"] = task_id
        return self.submit_task(user_id, task["conversation_id"], task["prompt"], idempotency_key, metadata=metadata)

    def queue_counts(self) -> dict[str, int]:
        """Global metrics for a trusted operator, never an unscoped browser endpoint."""
        result = {status: 0 for status in STATUSES}
        with self._connection() as connection:
            for row in connection.execute("SELECT status,COUNT(*) AS count FROM tasks GROUP BY status"):
                result[row["status"]] = row["count"]
        return result

    def backup(self, destination: str | Path) -> Path:
        """Consistent online SQLite backup; caller also backs up workspace files."""
        destination = Path(destination).resolve()
        if destination == self.db_path:
            raise ValidationError("Backup destination must differ from the live database")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        os.chmod(destination, 0o600)
        return destination
