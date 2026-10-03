#!/opt/harness/bin/python
"""Persistent tenant agent daemon and reconnectable NDJSON task client.

Input: one JSON line containing task_id, prompt, session_id, proxy_url,
proxy_token, and model. --daemon keeps one SDK runtime and its PTYs alive;
the default/--client mode only submits a task and tails its durable event log.
--health probes the daemon. Launch with /opt/harness/bin/python -I. The host
worker owns task deadlines and cancellation by restarting the tenant cgroup.
"""

from __future__ import annotations

import base64
from collections import deque
import ctypes
import fcntl
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import queue
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
from urllib.parse import quote, urlsplit


WORKSPACE = Path("/workspace")
AGENT_HOME = Path("/home/agent")
HARNESS_HOME = AGENT_HOME / ".dsh"
CONTROL_HOME = AGENT_HOME / ".agent-cloud"
MAX_INPUT_BYTES = 256 * 1024
MAX_LOG_CHARS = 12_000
MAX_MODEL_BYTES = 12 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SECRET_KEYS = re.compile(r"(?:^|[_-])(?:token|api[_-]?key|authorization|password|secret)(?:$|[_-])", re.I)
_redactions: list[str] = []
_recent_secrets = deque(maxlen=128)
_permanent_secrets: list[str] = []
_redaction_lock = threading.Lock()
_cancel_signal: int | None = None
_cleaning_up = False
DAEMON_PROTOCOL = 2
MODEL_OUTPUT_TOKENS = 49_152


class TaskCancelled(BaseException):
    pass


def remember_secret(secret, permanent=False):
    global _redactions
    with _redaction_lock:
        if permanent:
            _permanent_secrets.append(secret)
        else:
            _recent_secrets.append(secret)
        values = set()
        for token in (*_permanent_secrets, *_recent_secrets):
            values.update((token, quote(token, safe=""), base64.b64encode(token.encode()).decode()))
        _redactions = sorted(values, key=len, reverse=True)


def redact(value):
    if isinstance(value, str):
        for secret in _redactions:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {
            redact(str(key)): "[REDACTED]" if SECRET_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def emit(event_type: str, data: dict) -> None:
    print(json.dumps({"type": event_type, "data": redact(data)}, ensure_ascii=False), flush=True)


def diagnostic(message: str) -> None:
    print(redact(message)[:MAX_LOG_CHARS], file=sys.stderr, flush=True)


def bounded_detail(value):
    value = redact(value)
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= MAX_LOG_CHARS:
        return value
    return {"text": encoded[:MAX_LOG_CHARS], "truncated": True}


def handle_signal(signum, _frame) -> None:
    global _cancel_signal
    _cancel_signal = _cancel_signal or signum
    # Unwind SDK locks first; close() in a signal handler can deadlock.
    if not _cleaning_up:
        raise TaskCancelled()


def validate_task(task) -> dict:
    if not isinstance(task, dict):
        raise ValueError("Task input must be a JSON object")
    for field in ("task_id", "session_id"):
        if not isinstance(task.get(field), str) or not IDENTIFIER.fullmatch(task[field]):
            raise ValueError(f"Invalid {field}; use 1–128 letters, numbers, underscores, or hyphens")
    if not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
        raise ValueError("prompt must contain text")
    token = task.get("proxy_token")
    if not isinstance(token, str) or not token or len(token) > 4096 or token != token.strip():
        raise ValueError("Invalid proxy_token")
    remember_secret(token)
    proxy = task.get("proxy_url")
    if not isinstance(proxy, str):
        raise ValueError("proxy_url must be an HTTP(S) base URL")
    parsed = urlsplit(proxy)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("proxy_url must be an HTTP(S) base URL without credentials, query, or fragment")
    _ = parsed.port  # Validate a malformed port before starting a runtime.
    model = task.get("model")
    if not isinstance(model, str) or not model or len(model) > 128 or any(c.isspace() for c in model):
        raise ValueError("Invalid model")
    task["proxy_url"] = proxy.rstrip("/")
    return task


def read_task() -> dict:
    raw = sys.stdin.buffer.readline(MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Task input is empty or exceeds 256 KiB")
    return validate_task(json.loads(raw))


def isolate_launch_environment() -> None:
    # SDK 0.1.5rc1 merges env into os.environ; replace the parent environment
    # before constructing it. No host credentials or arbitrary DSH_* survive.
    os.environ.clear()
    os.environ.update({
        "HOME": str(AGENT_HOME), "USER": "agent", "LOGNAME": "agent",
        "PATH": "/opt/harness/bin:/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "xterm-256color",
        "TMPDIR": "/tmp", "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
        "DSH_SYSTEM_PROMPT": (
            "You are a general-purpose task agent. Use your shell tool to carry out the user's "
            "task in /workspace, inspect the real results, and report clearly what was done. "
            "Never claim to have created a file or executed a command unless a tool actually "
            "did it. Keep deliverables in /workspace. Treat credentials as confidential."
        ),
    })


def establish_process_group() -> None:
    try:
        os.setsid()
    except PermissionError:
        if os.getpgrp() != os.getpid():
            raise
    # Adopt orphaned PTY descendants for cleanup after the SDK runtime exits.
    # The container/cgroup is still the final boundary for forced cancellation.
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "Cannot establish child subreaper")


def process_tree() -> dict[int, int]:
    records: dict[int, tuple[int, int]] = {}
    for entry in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = entry.read_text()
            fields = text[text.rfind(")") + 2:].split()
            records[int(entry.parent.name)] = (int(fields[1]), int(fields[19]))
        except (OSError, ValueError, IndexError):
            continue
    owned = {os.getpid()}
    while True:
        additions = {pid for pid, (parent, _) in records.items() if parent in owned}
        if additions <= owned:
            break
        owned.update(additions)
    return {pid: records[pid][1] for pid in owned if pid != os.getpid() and pid in records}


def same_process(pid: int, started: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return int(text[text.rfind(")") + 2:].split()[19]) == started
    except (OSError, ValueError, IndexError):
        return False


def cleanup_children(children: dict[int, int]) -> None:
    children.update(process_tree())
    for signum in (signal.SIGTERM, signal.SIGKILL):
        for pid, started in children.items():
            if same_process(pid, started):
                try:
                    os.kill(pid, signum)
                except ProcessLookupError:
                    pass
        if signum == signal.SIGTERM and children:
            time.sleep(0.2)
            children.update(process_tree())
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except ChildProcessError:
            break


def create_pid_file(task_id: str) -> Path:
    CONTROL_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = CONTROL_HOME / f"{task_id}.pid"
    record = {"task_id": task_id, "pid": os.getpid(), "pgid": os.getpgrp(), "started_at": time.time()}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            previous = json.loads(path.read_text())
            previous_pid = int(previous["pid"])
            if previous_pid <= 1:
                raise ValueError("Invalid previous PID")
            os.kill(previous_pid, 0)
        except ProcessLookupError:
            path.unlink()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Invalid existing PID record; supervisor must reconcile it") from exc
        else:
            raise RuntimeError("A runner for this task is already active")
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def notification_callback(notification, output=emit) -> None:
    payload = notification.payload
    session_id = payload.get("sessionId")
    if notification.method == "session.status":
        output("status", {"session_id": session_id, "phase": payload.get("status")})
        return
    if notification.method != "session.event":
        return
    event = payload.get("event", {})
    event_type = event.get("type", "")
    data = event.get("data", {})
    common = {"session_id": session_id, "event": event_type, "seq": event.get("seq")}
    if event_type.startswith("tool/"):
        output("tool", {**common, "detail": bounded_detail(data)})
    elif event_type in {"turn/start", "turn/end", "step/start", "step/end"}:
        output("log", {**common, "detail": bounded_detail(data)})
    elif event_type == "assistant/message":
        message = data.get("message", data) if isinstance(data, dict) else {}
        content = message.get("content", []) if isinstance(message, dict) else []
        text = redact("".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"))
        if text:
            output("assistant", {**common, "text": text[:MAX_LOG_CHARS], "truncated": len(text) > MAX_LOG_CHARS})


class TaskJournal:
    """The execution thread writes; any number of clients can replay/tail it."""
    def __init__(self, task_id):
        self.task_id = task_id
        self.path = CONTROL_HOME / (task_id + ".events.jsonl")
        self.condition = threading.Condition()
        self.done = False
        self.success = False
        existed = self.path.exists()
        if existed:
            # Replaying a completed task is idempotent. An unfinished journal
            # from a previous daemon is never silently executed a second time.
            with self.path.open("rb") as stream:
                stream.seek(max(0, self.path.stat().st_size - MAX_MODEL_BYTES))
                tail = stream.read().splitlines()
            if tail:
                try:
                    event = json.loads(tail[-1])
                    self.done = event["type"] in {"result", "error", "cancelled"}
                    self.success = event["type"] == "result" and bool(event["data"].get("success"))
                except (ValueError, KeyError, TypeError):
                    pass
        self.stream = None if self.done else self.path.open("ab", buffering=0)
        os.chmod(self.path, 0o600)
        if existed and not self.done:
            self.write("error", {"task_id": task_id, "message": "Agent daemon restarted before task completion; inspect saved files before continuing"})

    def write(self, kind, data):
        if kind in {"result", "error", "cancelled"}:
            data = {**data, "journal_terminal": True}
        event = {"type": kind, "data": redact(data)}
        encoded = json.dumps(event, ensure_ascii=False, default=str).encode() + b"\n"
        if len(encoded) > MAX_MODEL_BYTES:
            encoded = json.dumps({"type": kind, "data": {"task_id": self.task_id,
                "message": "Output exceeds event limit; read the result files", "success": False}}).encode() + b"\n"
        with self.condition:
            self.stream.write(encoded)
            os.fsync(self.stream.fileno())
            if kind in {"result", "error", "cancelled"}:
                self.done = True
                self.success = kind == "result" and bool(event["data"].get("success"))
                self.stream.close()
            self.condition.notify_all()

    def follow(self, output):
        with self.path.open("rb") as stream:
            while True:
                with self.condition:
                    line = stream.readline()
                    if not line:
                        if self.done:
                            return
                        self.condition.wait(timeout=1)
                        continue
                output.write(line)
                output.flush()


class ModelRelay:
    """A runtime credential only works on loopback during an active task.

    SDK 0.1.5rc1 fixes its API key at process launch. Keep that local key
    stable while swapping only the revocable host-proxy token per task.
    No proxy token is placed in the SDK's environment or persisted to disk.
    """
    def __init__(self):
        self.key = secrets.token_urlsafe(32)
        remember_secret(self.key, permanent=True)
        self.lock = threading.Lock()
        self.active = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_error(405)

            def do_POST(self):
                self.close_connection = True
                connection = None
                response_started = False
                try:
                    if self.path != "/v1/chat/completions":
                        return self.send_error(404)
                    if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.key):
                        return self.send_error(403)
                    with owner.lock:
                        active = owner.active
                    if active is None:
                        return self.send_error(403, "No active task")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_MODEL_BYTES:
                        return self.send_error(413)
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        return self.send_error(400)
                    body["model"] = active["model"]
                    url = urlsplit(active["proxy_url"])
                    transport = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
                    # http.client never follows redirects or forwards a token
                    # to a location supplied by an upstream response.
                    connection = transport(url.hostname, url.port, timeout=300)
                    connection.request("POST", url.path.rstrip("/") + "/chat/completions",
                        body=json.dumps(body).encode(), headers={
                            "Authorization": "Bearer " + active["proxy_token"],
                            "Content-Type": "application/json", "Connection": "close"})
                    upstream = connection.getresponse()
                    if upstream.status != 200:
                        return self.send_error(upstream.status if 400 <= upstream.status < 600 else 502,
                                               "Model proxy rejected request")
                    self.send_response(200)
                    self.send_header("Content-Type", upstream.getheader("Content-Type", "application/json"))
                    self.send_header("Connection", "close")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    response_started = True
                    while True:
                        with owner.lock:
                            if owner.active is not active:
                                break
                        chunk = upstream.read1(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    if not response_started:
                        self.send_error(502, "Model relay request failed")
                finally:
                    if connection is not None:
                        connection.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def activate(self, task):
        with self.lock:
            self.active = {key: task[key] for key in ("proxy_url", "proxy_token", "model")}

    def deactivate(self):
        with self.lock:
            self.active = None

    def close(self):
        self.deactivate()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class AgentDaemon:
    def __init__(self):
        self.jobs = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.journals = {}
        self.harness = None
        self.degraded = False
        self.relay = ModelRelay()
        self.stopping = False

    def submit(self, task):
        with self.lock:
            if self.stopping:
                raise RuntimeError("Agent daemon is stopping")
            task_id = task["task_id"]
            if task_id in self.journals:
                return self.journals[task_id]
            # Bound the in-memory cache; completed journals remain on disk.
            for old in list(self.journals):
                if len(self.journals) < 32:
                    break
                if self.journals[old].done:
                    del self.journals[old]
            if self.jobs.full():
                raise RuntimeError("Agent already has a queued task")
            journal = TaskJournal(task_id)
            self.journals[task_id] = journal
            if not journal.done:
                journal.write("accepted", {"task_id": task_id, "session_id": task["session_id"],
                    "daemon_pid": os.getpid(), "protocol": DAEMON_PROTOCOL})
                self.jobs.put_nowait((task, journal))
            return journal

    def execute(self, task, journal):
        pid_file = None
        running_sdk = False
        try:
            if self.degraded:
                raise RuntimeError("Agent transport needs an explicit sandbox restart")
            if task["model"] not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
                raise ValueError("Only the verified DeepSeek v4 flash/pro model family is supported")
            pid_file = create_pid_file(task["task_id"])
            self.relay.activate(task)
            if self.harness is None:
                from deepseek_harness import DeepSeekHarness
                self.harness = DeepSeekHarness(
                    cwd=str(WORKSPACE), runtime_cwd=str(WORKSPACE), dsh_home=str(HARNESS_HOME),
                    profile="sdk-minimal", provider="deepseek-official", model=task["model"],
                    base_url=self.relay.base_url, api_key=self.relay.key, max_tokens=MODEL_OUTPUT_TOKENS,
                    initialize_timeout_seconds=60.0, request_timeout_seconds=60.0,
                    shutdown_timeout_seconds=3.0,
                )
            journal.write("session", {"task_id": task["task_id"], "session_id": task["session_id"],
                "pid": os.getpid(), "pgid": os.getpgrp(), "model": task["model"]})
            running_sdk = True
            result = self.harness.run(task["prompt"], session_id=task["session_id"],
                on_notification=lambda event: notification_callback(event, journal.write))
            running_sdk = False
            terminal = {"task_id": task["task_id"], "session_id": result.session_id,
                        "response": result.final_response, "finish_reason": result.finish_reason,
                        "success": result.finish_reason == "completed"}
            if not terminal["success"]:
                terminal["error"] = (
                    "本轮模型输出达到长度上限，任务尚未完成；已生成的文件仍保留，可发送新消息让 Agent 继续"
                    if result.finish_reason == "max-tokens" else
                    "Agent 本轮未完成：" + str(result.finish_reason or "未知原因")[:160]
                )
            # Release the SDK's per-run events/notifications; never dispose the
            # live agent or PTY at a normal task boundary.
            del result
            self.relay.deactivate()
            journal.write("result", terminal)
        except TaskCancelled:
            self.relay.deactivate()
            journal.write("cancelled", {"task_id": task["task_id"], "signal": _cancel_signal})
            raise
        except Exception as exc:
            self.relay.deactivate()
            # An RPC/notification exception can leave an accepted prompt still
            # executing. Do not start another job until the cgroup is restarted.
            self.degraded = self.degraded or running_sdk
            message = redact(f"{type(exc).__name__}: {exc}")
            diagnostic(message)
            journal.write("error", {"task_id": task["task_id"], "message": message[:MAX_LOG_CHARS],
                                    "requires_restart": self.degraded})
        finally:
            self.relay.deactivate()
            if pid_file is not None:
                pid_file.unlink(missing_ok=True)
            # Forget the short-lived upstream credential once a job ends.
            task.pop("proxy_token", None)

    def close(self):
        self.stopping = True
        self.relay.deactivate()
        children = process_tree()
        if self.harness is not None:
            try:
                self.harness.close()
            except Exception as exc:
                diagnostic(f"Runtime shutdown failed: {type(exc).__name__}: {exc}")
        cleanup_children(children)
        self.relay.close()


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


def serve_daemon() -> int:
    global _cleaning_up
    isolate_launch_environment()
    establish_process_group()
    if not WORKSPACE.is_dir():
        raise RuntimeError("/workspace must be mounted before starting the daemon")
    HARNESS_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    CONTROL_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    from importlib.metadata import version
    if version("deepseek-harness-sdk") != "0.1.5rc1":
        raise RuntimeError("This runner requires deepseek-harness-sdk==0.1.5rc1")
    lock = (CONTROL_HOME / "daemon.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    socket_path = CONTROL_HOME / "daemon.sock"
    socket_path.unlink(missing_ok=True)
    daemon = AgentDaemon()
    server = None

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                self.connection.settimeout(10)
                raw = self.rfile.readline(MAX_INPUT_BYTES + 1025)
                if not raw or len(raw) > MAX_INPUT_BYTES + 1024:
                    raise ValueError("Invalid control request size")
                request = json.loads(raw)
                if request.get("op") == "health":
                    self.wfile.write(json.dumps({"type": "health", "data": {
                        "ready": not daemon.stopping, "pid": os.getpid(), "protocol": DAEMON_PROTOCOL}}).encode() + b"\n")
                    return
                if request.get("op") != "run":
                    raise ValueError("Invalid control operation")
                task = validate_task(request.get("task"))
                journal = daemon.submit(task)
                self.connection.settimeout(30)
                journal.follow(self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                # A lost subscriber must never terminate the queued/running job.
                pass
            except Exception as exc:
                try:
                    message = redact(f"{type(exc).__name__}: {exc}")[:MAX_LOG_CHARS]
                    self.wfile.write(json.dumps({"type": "error", "data": {
                        "message": message, "journal_terminal": False}}).encode() + b"\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

    try:
        server = ControlServer(str(socket_path), Handler)
        os.chmod(socket_path, 0o600)
        (CONTROL_HOME / "daemon.pid").write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        while True:
            task, journal = daemon.jobs.get()
            daemon.execute(task, journal)
    except TaskCancelled:
        return 128 + (_cancel_signal or signal.SIGTERM)
    finally:
        _cleaning_up = True
        daemon.stopping = True
        if server is not None:
            server.shutdown()
            server.server_close()
        daemon.close()
        socket_path.unlink(missing_ok=True)
        (CONTROL_HOME / "daemon.pid").unlink(missing_ok=True)
        lock.close()


def client(health=False) -> int:
    task = None if health else read_task()
    request = {"op": "health"} if health else {"op": "run", "task": task}
    success = False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(CONTROL_HOME / "daemon.sock"))
        connection.sendall(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        connection.settimeout(None)
        with connection.makefile("rb") as stream:
            for line in stream:
                event = json.loads(line)
                emit(event["type"], event["data"])
                if health:
                    return 0 if event["data"].get("ready") and event["data"].get("protocol") == DAEMON_PROTOCOL else 1
                if event["type"] == "result":
                    success = bool(event["data"].get("success"))
    return 0 if success else 1


def main() -> int:
    os.umask(0o077)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, handle_signal)
    try:
        if sys.argv[1:] == ["--daemon"]:
            return serve_daemon()
        if sys.argv[1:] in ([], ["--client"], ["--health"]):
            return client(health=sys.argv[1:] == ["--health"])
        raise ValueError("Expected --daemon, --client, or --health")
    except TaskCancelled:
        return 128 + (_cancel_signal or signal.SIGTERM)
    except BrokenPipeError:
        # The daemon and its background jobs continue after this client exits.
        return 1
    except Exception as exc:
        message = redact(f"{type(exc).__name__}: {exc}")[:MAX_LOG_CHARS]
        diagnostic(message)
        if sys.argv[1:] != ["--health"]:
            emit("error", {"message": message})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
