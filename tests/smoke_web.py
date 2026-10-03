#!/usr/bin/env python3
"""Run the installed web app against disposable state and loopback-only ports.

Usage: python3 /tmp/remote-web-smoke.py --app-dir /opt/agent-cloud
No production user, data directory, model key, worker or service is changed.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


SMOKE_SITE_ID = "0123456789abcdef0123456789abcdef"
SMOKE_SITE_SLUG = "smoke123"


@dataclass
class Response:
    status: int
    json: object
    body: bytes
    headers: object


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Smoke:
    def __init__(self, app_dir, python, data_dir, *, initialize=True):
        self.app_dir, self.python, self.data_dir = app_dir, python, data_dir
        self.web_port = free_port()
        self.proxy_port = free_port()
        while self.proxy_port == self.web_port:
            self.proxy_port = free_port()
        self.base = f"http://127.0.0.1:{self.web_port}"
        self.bootstrap = secrets.token_urlsafe(24)
        self.alice_password = secrets.token_urlsafe(24)
        self.bob_password = secrets.token_urlsafe(24)
        self.process = None
        self.log_path = data_dir / "server.log"
        self.log = self.log_path.open("ab")
        self.checks = []
        self.env = dict(os.environ)
        self.env.update({"AGENT_CLOUD_DATA": str(data_dir), "AGENT_CLOUD_CONFIG": str(data_dir / "config.json"),
                         "AGENT_CLOUD_HOST": "127.0.0.1", "AGENT_CLOUD_PORT": str(self.web_port),
                         "AGENT_CLOUD_PROXY_HOST": "127.0.0.1", "AGENT_CLOUD_PROXY_PORT": str(self.proxy_port),
                         "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        if initialize:
            config = {"bootstrap_enabled": True, "bootstrap_token": self.bootstrap,
                      "signup_enabled": False, "api_key": "", "model": "deepseek-v4-flash", "domain": "smoke.invalid"}
            config_path = data_dir / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            config_path.chmod(0o600)

    def request(self, path, *, data=None, auth=None, method=None):
        headers = {"Origin": self.base}
        if auth:
            headers["Cookie"] = auth["cookie"]
            headers["X-CSRF-Token"] = auth["csrf"]
        encoded = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            encoded = json.dumps(data).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=encoded, method=method, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                payload = None
            return Response(response.status, payload, body, response.headers)

    def start(self):
        require(self.process is None, "Test web process is already running")
        self.process = subprocess.Popen([self.python, str(self.app_dir / "server.py")], cwd=self.app_dir,
                                        env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            require(self.process.poll() is None, "Disposable web process exited during startup")
            try:
                if self.request("/health").status == 200:
                    return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        raise AssertionError("Disposable web process did not become ready")

    def stop(self):
        if self.process is not None:
            process, self.process = self.process, None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def close(self):
        self.stop()
        self.log.close()

    @staticmethod
    def auth(response):
        require(response.status == 200, f"Authentication failed with HTTP {response.status}")
        cookie = response.headers.get("Set-Cookie", "")
        require("HttpOnly" in cookie and "SameSite=Strict" in cookie, "Session cookie protection is missing")
        require("agent_session=" in cookie, "Session cookie was not issued")
        return {"cookie": cookie.split(";", 1)[0], "csrf": response.json["csrf"], "user": response.json["user"]}

    def login(self, username, password):
        return self.auth(self.request("/api/login", data={"username": username, "password": password}))

    def conversation(self, auth, title):
        response = self.request("/api/conversations", auth=auth, data={"title": title})
        require(response.status == 201, "Creating a conversation failed")
        return response.json

    def task(self, auth, conversation, key):
        response = self.request("/api/tasks", auth=auth, data={"conversation_id": conversation["id"],
            "prompt": "Disposable smoke test: retain this task after the webpage and web process close.",
            "idempotency_key": key})
        require(response.status == 202, "Submitting a durable task failed")
        require(response.json["status"] == "queued", "Task should wait in the isolated queue")
        return response.json

    def upload(self, auth, content):
        response = self.request("/api/files", auth=auth, data={"path": "files", "filename": "smoke.txt",
            "content": base64.b64encode(content).decode("ascii")})
        require(response.status == 201, "Workspace upload failed")

    def download(self, auth, content):
        response = self.request("/api/file?path=files/smoke.txt", auth=auth)
        require(response.status == 200 and response.body == content, "Workspace download content differed")
        require(int(response.headers["Content-Length"]) == len(content), "Download length differed")

    def publication_fixture(self):
        # Exercise the backup with copied public assets and private publication
        # records, without actually enabling a vhost or publishing a user task.
        site = self.data_dir / "published" / SMOKE_SITE_ID
        site.mkdir(parents=True, mode=0o755)
        (site / "index.html").write_text("<h1>Durable publication fixture</h1>\n")
        short = self.data_dir / "published-short"
        short.mkdir(mode=0o755)
        (short / SMOKE_SITE_SLUG).symlink_to("../published/" + SMOKE_SITE_ID, target_is_directory=True)
        records = self.data_dir / "publication-records"
        records.mkdir(mode=0o700)
        record = records / (SMOKE_SITE_ID + ".json")
        record.write_text(json.dumps({"site_id": SMOKE_SITE_ID, "source": "fixture-only"}))
        record.chmod(0o600)

    @staticmethod
    def verify_publication_fixture(directory):
        site = directory / "published" / SMOKE_SITE_ID / "index.html"
        record = directory / "publication-records" / (SMOKE_SITE_ID + ".json")
        short = directory / "published-short" / SMOKE_SITE_SLUG
        require(site.read_text() == "<h1>Durable publication fixture</h1>\n",
                "Published static asset did not survive backup/restore")
        require(json.loads(record.read_text())["site_id"] == SMOKE_SITE_ID,
                "Publication record did not survive backup/restore")
        require(record.stat().st_mode & 0o777 == 0o600,
                "Restored publication record permissions exposed private metadata")
        require(short.is_symlink() and os.readlink(short) == "../published/" + SMOKE_SITE_ID,
                "Stable publication alias lost its relative symbolic link during restore")
        require((short / "index.html").read_bytes() == site.read_bytes(),
                "Restored stable URL alias no longer resolves to its published snapshot")

    def restore_and_verify(self, archive_path, alice, bob, alice_conversation, bob_conversation,
                           alice_task, bob_task, alice_content, bob_content):
        """Restore the actual archive into an empty directory, then boot the app."""
        self.stop()  # Recovery does not depend on the original web process.
        with tempfile.TemporaryDirectory(prefix="agent-cloud-restore-") as temp:
            restored_dir = Path(temp).resolve()
            with tarfile.open(archive_path, "r:gz") as archive:
                selected = [member for member in archive.getmembers()
                            if member.name in ("databases/agent-cloud.sqlite3", "databases/proxy.sqlite3", "private/config.json", "data/workspaces")
                            or member.name.startswith(("data/workspaces/", "published/", "published-short/", "publication-records/"))
                            or member.name in ("published", "published-short", "publication-records")]
                names = {member.name for member in selected}
                require("databases/agent-cloud.sqlite3" in names and "private/config.json" in names,
                        "Archive lacks the database or config required for a real restore")
                # Python 3.12's data filter rejects path escapes and unsafe links.
                # Never extract the archive's source tree into the installed app.
                archive.extractall(restored_dir, members=selected, filter="data")
            self.verify_publication_fixture(restored_dir)
            (restored_dir / "data").mkdir(mode=0o700, exist_ok=True)
            for database in ("agent-cloud.sqlite3", "proxy.sqlite3"):
                source = restored_dir / "databases" / database
                if source.exists():
                    destination = restored_dir / "data" / database
                    shutil.copyfile(source, destination)
                    destination.chmod(0o600)
            shutil.copyfile(restored_dir / "private" / "config.json", restored_dir / "config.json")
            (restored_dir / "config.json").chmod(0o600)
            require(not any((restored_dir / "data").glob("*-wal")) and not any((restored_dir / "data").glob("*-shm")),
                    "Restore included live database sidecars")
            recovered_config = json.loads((restored_dir / "config.json").read_text())
            require(not recovered_config["api_key"] and not recovered_config["bootstrap_enabled"],
                    "Restore altered the model-key or completed-bootstrap configuration")

            restored = Smoke(self.app_dir, self.python, restored_dir, initialize=False)
            try:
                restored.start()
                status = restored.request("/api/status")
                require(status.status == 200 and not status.json["needs_bootstrap"] and not status.json["configured"],
                        "Restored installation did not recognize the existing accounts and empty model key")
                # Existing hashed sessions and password records must both work.
                require(restored.request("/api/me", auth=bob).status == 200, "Restored session could not authenticate")
                recovered_alice = restored.login("smoke_alice", self.alice_password)
                recovered_bob = restored.login("smoke_bob", self.bob_password)
                require(recovered_alice["user"]["id"] == alice["user"]["id"] and recovered_alice["user"]["is_admin"],
                        "Restore changed Alice's identity or administrator role")
                require(recovered_bob["user"]["id"] == bob["user"]["id"] and not recovered_bob["user"]["is_admin"],
                        "Restore changed Bob's identity or permissions")
                for auth, conversation, task, content in (
                    (recovered_alice, alice_conversation, alice_task, alice_content),
                    (recovered_bob, bob_conversation, bob_task, bob_content),
                ):
                    conversations = restored.request("/api/conversations", auth=auth)
                    require(conversations.status == 200 and [row["id"] for row in conversations.json] == [conversation["id"]],
                            "Restored conversation identity or ownership differed")
                    history = restored.request("/api/messages?page=1&conversation=" + conversation["id"], auth=auth)
                    require(history.status == 200 and len(history.json["items"]) == 1
                            and history.json["items"][0]["task_id"] == task["id"]
                            and history.json["items"][0]["content"] == task["prompt"],
                            "Restored message history did not match the archived task")
                    tasks = restored.request("/api/tasks?page=1&conversation=" + conversation["id"], auth=auth)
                    require(tasks.status == 200 and len(tasks.json["items"]) == 1
                            and tasks.json["items"][0]["id"] == task["id"]
                            and tasks.json["items"][0]["status"] == "queued"
                            and tasks.json["items"][0]["attempt_count"] == 0,
                            "Restored queued task changed identity or was executed unexpectedly")
                    restored.download(auth, content)
                    listing = restored.request("/api/files?page=1&path=files", auth=auth)
                    require(listing.status == 200 and [entry["name"] for entry in listing.json["items"]] == ["smoke.txt"],
                            "Restored workspace contents differed")
                for path in ("/api/messages?page=1&conversation=" + alice_conversation["id"],
                             "/api/tasks?page=1&conversation=" + alice_conversation["id"],
                             "/api/events?page=1&task=" + alice_task["id"],
                             "/api/tasks?page=1&ids=" + alice_task["id"], "/api/settings"):
                    require(restored.request(path, auth=recovered_bob).status == 404,
                            "Restore broke tenant isolation")
                require(restored.request("/api/cancel", auth=recovered_bob, data={"task_id": alice_task["id"]}).status == 404,
                        "Restored Bob account could cancel Alice's task")
                require(restored.request("/api/file?path=..%2F..%2Fconfig.json", auth=recovered_bob).status == 400,
                        "Restored workspace allowed traversal into private configuration")
            finally:
                restored.close()
        self.checks.append("archive restored into empty data directory; accounts, roles, sessions, history, queued work, tenant files and publications recovered")

    def run(self):
        self.start()
        status = self.request("/api/status")
        require(status.status == 200 and status.json["needs_bootstrap"] and not status.json["configured"],
                "Disposable installation must start without users or a model key")
        rejected = self.request("/api/signup", data={"username": "smoke_alice", "password": self.alice_password,
                                                      "bootstrap_token": "incorrect"})
        require(rejected.status == 403, "Admin bootstrap accepted an incorrect initialization token")
        alice = self.auth(self.request("/api/signup", data={"username": "smoke_alice", "password": self.alice_password,
                                                            "bootstrap_token": self.bootstrap}))
        require(alice["user"]["is_admin"], "First authenticated bootstrap user did not become admin")
        alice = self.login("smoke_alice", self.alice_password)
        closed = self.request("/api/signup", data={"username": "smoke_bob", "password": self.bob_password})
        require(closed.status == 403, "Signups unexpectedly open in disposable default configuration")
        enabled = self.request("/api/settings", auth=alice, data={"signup_enabled": True, "model": "deepseek-v4-flash"})
        require(enabled.status == 200 and not enabled.json["config"]["configured"], "Opening isolated signup failed")
        bob = self.auth(self.request("/api/signup", data={"username": "smoke_bob", "password": self.bob_password,
                                                          "is_admin": True}))
        require(not bob["user"]["is_admin"], "Signup payload escalated a test user to admin")
        self.checks.append("admin bootstrap and password login")

        alice_conversation = self.conversation(alice, "Alice disposable test")
        bob_conversation = self.conversation(bob, "Bob disposable test")
        alice_task = self.task(alice, alice_conversation, "smoke-alice-task")
        repeated = self.task(alice, alice_conversation, "smoke-alice-task")
        require(repeated["id"] == alice_task["id"] and not repeated["created"], "A repeated submission duplicated the task")
        bob_task = self.task(bob, bob_conversation, "smoke-bob-task")
        for path in ("/api/messages?page=1&conversation=" + alice_conversation["id"],
                     "/api/tasks?page=1&conversation=" + alice_conversation["id"],
                     "/api/events?page=1&task=" + alice_task["id"],
                     "/api/tasks?page=1&ids=" + alice_task["id"], "/api/settings"):
            require(self.request(path, auth=bob).status == 404, "Second user reached another tenant's private resource")
        require(self.request("/api/cancel", auth=bob, data={"task_id": alice_task["id"]}).status == 404,
                "Second user could cancel another tenant's task")
        require([task["id"] for task in self.request("/api/tasks", auth=bob).json] == [bob_task["id"]],
                "Second user's task list included another tenant")
        self.checks.append("two-user task/history isolation and submission idempotency")

        alice_content, bob_content = b"Alice persistent workspace\n", b"Bob isolated workspace\n"
        self.upload(alice, alice_content)
        require(self.request("/api/file?path=files/smoke.txt", auth=bob).status == 404, "Bob downloaded Alice's file")
        self.upload(bob, bob_content)
        self.download(alice, alice_content)
        self.download(bob, bob_content)
        repeated_upload = self.request("/api/files", auth=alice, data={"path": "files", "filename": "smoke.txt",
            "content": base64.b64encode(b"overwrite").decode("ascii")})
        require(repeated_upload.status == 409, "An existing workspace file was silently overwritten")
        self.download(alice, alice_content)
        require(self.request("/api/file?path=..%2F..%2Fconfig.json", auth=alice).status == 400,
                "Workspace traversal reached host data")
        listing = self.request("/api/files?page=1&path=files", auth=alice)
        require(listing.status == 200 and [entry["name"] for entry in listing.json["items"]] == ["smoke.txt"],
                "Workspace listing did not return the expected isolated file")
        self.checks.append("isolated upload, read-only download and traversal denial")

        require(self.request("/api/logout", auth=alice, data={}).status == 200, "Logout failed")
        self.stop()
        self.start()
        persisted_session = self.request("/api/me", auth=bob)
        require(persisted_session.status == 200 and persisted_session.json["user"]["id"] == bob["user"]["id"],
                "Session did not survive the web-process restart")
        alice = self.login("smoke_alice", self.alice_password)
        tasks = self.request("/api/tasks?page=1&conversation=" + alice_conversation["id"], auth=alice)
        require(tasks.status == 200 and len(tasks.json["items"]) == 1
                and tasks.json["items"][0]["id"] == alice_task["id"] and tasks.json["items"][0]["status"] == "queued",
                "Queued work did not survive logout and web-process restart")
        messages = self.request("/api/messages?page=1&conversation=" + alice_conversation["id"], auth=alice)
        require(messages.status == 200 and len(messages.json["items"]) == 1, "Restart lost or duplicated user history")
        self.download(alice, alice_content)
        self.download(bob, bob_content)
        self.checks.append("queue, history, sessions and files survive web-process restart")

        self.publication_fixture()

        backup = subprocess.run([self.python, str(self.app_dir / "backup.py")], cwd=self.app_dir, env=self.env,
                                capture_output=True, text=True, timeout=60)
        require(backup.returncode == 0, "Installed backup.py failed: " + backup.stderr[-1000:])
        report = json.loads(backup.stdout.strip())
        archive_path = Path(report["backup"]).resolve()
        require(archive_path.is_relative_to((self.data_dir / "backups").resolve()), "Backup escaped the disposable data directory")
        with tarfile.open(archive_path, "r:gz") as archive:
            require(archive.getmember("source/publisher.py").isfile(), "Backup omitted the publisher source dependency")
            require(archive.getmember("published/" + SMOKE_SITE_ID + "/index.html").isfile(), "Backup omitted published static assets")
            short_alias = archive.getmember("published-short/" + SMOKE_SITE_SLUG)
            require(short_alias.issym() and short_alias.linkname == "../published/" + SMOKE_SITE_ID,
                    "Backup omitted or dereferenced the stable publication alias")
            require(archive.getmember("publication-records/" + SMOKE_SITE_ID + ".json").mode == 0o600,
                    "Backup omitted private publication records or changed their permissions")
            database_file = archive.extractfile("databases/agent-cloud.sqlite3")
            require(database_file is not None, "Backup omitted the durable database")
            snapshot_path = self.data_dir / "backup-integrity.sqlite3"
            with database_file, snapshot_path.open("wb") as target:
                while block := database_file.read(64 * 1024):
                    target.write(block)
            for auth, content in ((alice, alice_content), (bob, bob_content)):
                member = archive.extractfile("data/workspaces/" + auth["user"]["id"] + "/files/smoke.txt")
                require(member is not None, "Backup omitted a persistent user file")
                with member:
                    require(member.read() == content, "Backup user-file content differed")
        with sqlite3.connect(snapshot_path) as database:
            require(database.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Backup database failed integrity check")
            require(database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2, "Backup omitted disposable users")
            require(database.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0] == 2,
                    "Backup omitted durable queued tasks")
            require(database.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2, "Backup history differs from live state")
        self.checks.append("installed online backup has integral SQLite state and both workspaces")
        self.restore_and_verify(archive_path, alice, bob, alice_conversation, bob_conversation,
                                alice_task, bob_task, alice_content, bob_content)
        return self.checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=Path("/opt/agent-cloud"))
    parser.add_argument("--python", default=sys.executable, help="Python used to launch the installed server and backup")
    args = parser.parse_args()
    app_dir = args.app_dir.resolve()
    require((app_dir / "server.py").is_file() and (app_dir / "backup.py").is_file(), "Installed application directory is missing")

    def interrupted(*_):
        raise KeyboardInterrupt("Smoke test interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agent-cloud-smoke-") as temp:
        smoke = Smoke(app_dir, args.python, Path(temp).resolve())
        try:
            checks = smoke.run()
            print(json.dumps({"ok": True, "checks": checks, "production_modified": False,
                              "duration_seconds": round(time.monotonic() - started, 2)}, ensure_ascii=False))
        except (Exception, KeyboardInterrupt) as error:
            smoke.log.flush()
            tail = smoke.log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
            print(json.dumps({"ok": False, "error": str(error), "server_log_tail": tail,
                              "production_modified": False}, ensure_ascii=False), file=sys.stderr)
            return 1
        finally:
            smoke.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
