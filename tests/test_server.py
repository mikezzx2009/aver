"""HTTP boundary tests: sessions, tenant isolation, file escape and bootstrap."""
import base64
import concurrent.futures
import contextlib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
import io
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

import common
import server
from store import Store


@dataclass
class Response:
    status: int
    payload: object
    headers: object
    raw: bytes


class ServerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.bootstrap = "bootstrap-test-token-private"
        self.model_secret = "model-test-secret-private-value"
        self.store = Store(self.data_dir / "data")
        for target, name, value in (
            (common, "CONFIG_PATH", self.data_dir / "config.json"),
            (common, "DATA", self.data_dir),
            (server, "DATA", self.data_dir),
            (server, "STORE", self.store),
            (server, "TOKENS", common.ProxyTokens(self.data_dir / "data")),
            (server, "RATE", {}),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        common.save_config({"bootstrap_token": self.bootstrap, "bootstrap_enabled": True,
                            "api_key": "", "signup_enabled": False})
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.http = server.BoundedHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=5)

    def request(self, path, *, method="GET", body=None, raw=None, auth=None,
                origin="default", csrf="default", headers=None, base=None):
        request_headers = dict(headers or {})
        if origin == "default":
            request_headers["Origin"] = self.base
        elif origin is not None:
            request_headers["Origin"] = origin
        if auth:
            request_headers["Cookie"] = auth["cookie"]
            if csrf == "default":
                request_headers["X-CSRF-Token"] = auth["csrf"]
            elif csrf is not None:
                request_headers["X-CSRF-Token"] = csrf
        if raw is None and body is not None:
            raw = json.dumps(body).encode("utf-8")
        if raw is not None:
            request_headers["Content-Type"] = "application/json"
        req = urllib.request.Request((base or self.base) + path, data=raw, method=method, headers=request_headers)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            try:
                payload = json.loads(content)
            except (ValueError, UnicodeDecodeError):
                payload = None
            return Response(response.status, payload, response.headers, content)

    @staticmethod
    def authentication(response):
        return {"cookie": response.headers["Set-Cookie"].split(";", 1)[0],
                "csrf": response.payload["csrf"], "user": response.payload["user"]}

    def admin(self):
        response = self.request("/api/signup", method="POST", body={"username": "alice", "password": "private-test-password",
                                                                    "bootstrap_token": self.bootstrap})
        self.assertEqual(200, response.status, response.raw)
        return self.authentication(response)

    def bob(self):
        self.store.create_user("bob", "bob-private-password")
        response = self.request("/api/login", method="POST", body={"username": "bob", "password": "bob-private-password"})
        self.assertEqual(200, response.status)
        return self.authentication(response)

    def conversation(self, auth):
        response = self.request("/api/conversations", method="POST", body={"title": "Test conversation"}, auth=auth)
        self.assertEqual(201, response.status, response.raw)
        return response.payload

    def task(self, auth, conversation, key="a-task"):
        response = self.request("/api/tasks", method="POST", body={"conversation_id": conversation["id"],
                    "prompt": "Write a durable file after I close the webpage", "idempotency_key": key}, auth=auth)
        self.assertEqual(202, response.status, response.raw)
        return response.payload

    def test_historical_website_links_display_canonical_urls_without_changing_history(self):
        alice = self.admin()
        conversation = self.conversation(alice)
        task = self.task(alice, conversation)
        original = 'http://203.0.113.10/sites/' + 'a' * 32 + '/'
        canonical = 'http://203.0.113.10/ruler/'
        message = self.store.add_message(alice['user']['id'], conversation['id'], 'assistant',
            '网站：' + original + '\n资源：' + original + 'assets/test.svg', task_id=task['id'])
        self.store.add_message(alice['user']['id'], conversation['id'], 'user', original)
        with mock.patch.object(server, 'list_publications', return_value=[
                {'id': 'a' * 32, 'url': canonical}]) as lookup:
            for path in ('/api/messages?conversation=', '/api/messages?page=1&conversation='):
                response = self.request(path + conversation['id'], auth=alice)
                self.assertEqual(response.status, 200)
                items = response.payload.get('items') if isinstance(response.payload, dict) else response.payload
                rendered = next(item for item in items if item['id'] == message['id'])
                self.assertEqual(rendered['content'], '网站：' + canonical + '\n资源：' + canonical + 'assets/test.svg')
                self.assertEqual(items[-1]['content'], original)
            self.assertEqual(lookup.call_args.args[1:], (alice['user']['id'], [task['id']]))
            bob = self.bob()
            self.assertEqual(self.request('/api/messages?page=1&conversation=' + conversation['id'], auth=bob).status, 404)
        stored = next(item for item in self.store.list_messages(alice['user']['id'], conversation['id'])
                      if item['id'] == message['id'])
        self.assertEqual(stored['content'], message['content'])

    def test_server_stats_requires_login_and_exposes_only_shared_aggregates(self):
        response = self.request('/api/server-stats')
        self.assertEqual(401,response.status)
        self.assertIsNone(self.http._stats_sampler)
        alice = self.admin()
        bob = self.bob()
        bob_response = self.request('/api/server-stats',auth=bob)
        self.assertEqual(200,bob_response.status,bob_response.raw)
        self.assertFalse(bob['user']['is_admin'])
        data = bob_response.payload
        self.assertEqual({'cpu_percent','cpu_count','memory','disk','load_average','collected_at','sample_seconds'},set(data))
        self.assertIsNone(data['cpu_percent'])
        self.assertIsNone(data['sample_seconds'])
        for resource in ('memory','disk'):
            self.assertEqual({'used_bytes','total_bytes','percent'},set(data[resource]))
        self.assertIsInstance(data['collected_at'],(int,float))
        self.assertEqual('no-store',bob_response.headers['Cache-Control'])
        # All authenticated users see the same cached host-level snapshot.
        self.assertEqual(data,self.request('/api/server-stats',auth=alice).payload)
        self.assertNotIn(str(self.data_dir),bob_response.raw.decode())
        thread = self.http._stats_sampler._thread
        self.stop_server()
        self.assertFalse(thread.is_alive())

    def test_first_admin_requires_bootstrap_and_cannot_be_recreated(self):
        for supplied in (None, "wrong-token"):
            body = {"username": "alice", "password": "private-test-password"}
            if supplied:
                body["bootstrap_token"] = supplied
            response = self.request("/api/signup", method="POST", body=body)
            self.assertEqual(403, response.status)
            self.assertEqual([], self.store.list_all_users())
        auth = self.admin()
        self.assertTrue(auth["user"]["is_admin"])
        self.assertFalse(common.config()["bootstrap_enabled"])
        self.assertEqual("", common.config()["bootstrap_token"])
        response = self.request("/api/signup", method="POST", body={"username": "mallory", "password": "a secure password",
                    "bootstrap_token": self.bootstrap, "is_admin": True})
        self.assertEqual(403, response.status)
        common.save_config({"signup_enabled": True})
        response = self.request("/api/signup", method="POST", body={"username": "mallory", "password": "a secure password", "is_admin": True})
        self.assertEqual(200, response.status)
        self.assertFalse(response.payload["user"]["is_admin"])

    def test_login_origin_csrf_session_and_cookie_security(self):
        auth = self.admin()
        login_body = {"username": "alice", "password": "private-test-password"}
        for origin in (None, "https://evil.example", "null"):
            response = self.request("/api/login", method="POST", body=login_body, origin=origin)
            self.assertEqual(403, response.status)
        response = self.request("/api/login", method="POST", body=login_body,
                                headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(200, response.status)
        cookie = response.headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
            self.assertIn(flag, cookie)
        fresh = self.authentication(response)
        for token in (None, "wrong-csrf-token", auth["csrf"]):
            response = self.request("/api/conversations", method="POST", body={"title": "attack"}, auth=fresh, csrf=token)
            self.assertEqual(403, response.status)
        response = self.request("/api/conversations", method="POST", body={"title": "attack"}, auth=fresh, origin="https://evil.example")
        self.assertEqual(403, response.status)
        self.assertEqual([], self.store.list_conversations(fresh["user"]["id"]))
        me = self.request("/api/me", auth=fresh)
        self.assertEqual(200, me.status)
        self.assertEqual(fresh["csrf"], me.payload["csrf"])
        self.assertEqual("no-store", me.headers["Cache-Control"])
        self.assertEqual("DENY", me.headers["X-Frame-Options"])
        self.assertEqual(200, self.request("/api/logout", method="POST", body={}, auth=fresh).status)
        self.assertEqual(401, self.request("/api/me", auth=fresh).status)

    def test_task_message_event_and_settings_tenant_boundary(self):
        alice = self.admin()
        bob = self.bob()
        conversation = self.conversation(alice)
        task = self.task(alice, conversation)
        attacks = (
            ("/api/messages?conversation=" + conversation["id"], "GET", None),
            ("/api/tasks?conversation=" + conversation["id"], "GET", None),
            ("/api/events?task=" + task["id"], "GET", None),
            ("/api/cancel", "POST", {"task_id": task["id"]}),
            ("/api/retry", "POST", {"task_id": task["id"], "idempotency_key": "attack"}),
            ("/api/tasks", "POST", {"conversation_id": conversation["id"], "prompt": "attack", "idempotency_key": "attack"}),
            ("/api/settings", "GET", None),
            ("/api/settings", "POST", {"signup_enabled": True}),
        )
        for path, method, body in attacks:
            with self.subTest(path=path, method=method):
                self.assertEqual(404, self.request(path, method=method, body=body, auth=bob).status)
        self.assertEqual([], self.request("/api/events", auth=bob).payload)
        self.assertEqual([], self.request("/api/tasks", auth=bob).payload)
        self.assertEqual("queued", self.store.get_task(alice["user"]["id"], task["id"])["status"])
        self.assertFalse(common.config()["signup_enabled"])

    def test_no_key_submission_is_durable_idempotent_and_independent_of_browser(self):
        auth = self.admin()
        conversation = self.conversation(auth)
        first = self.task(auth, conversation)
        second = self.task(auth, conversation)
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["created"])
        # Login state can end while the authoritative task remains in the queue.
        self.request("/api/logout", method="POST", body={}, auth=auth)
        reopened = Store(self.data_dir / "data")
        self.assertEqual("queued", reopened.get_task(auth["user"]["id"], first["id"])["status"])
        self.assertEqual(1, len(reopened.list_messages(auth["user"]["id"], conversation["id"])))
        claimed = reopened.claim_task("worker-after-browser-close")
        self.assertEqual(first["id"], claimed["id"])
        reopened.mark_waiting_config(claimed["id"], "worker-after-browser-close")
        self.assertEqual("waiting_config", reopened.get_task(auth["user"]["id"], first["id"])["status"])

    def test_file_download_upload_and_cross_tenant_escape_boundaries(self):
        alice = self.admin()
        bob = self.bob()
        upload = self.request("/api/files", method="POST", body={"path": "files", "filename": "alice.txt",
                                "content": base64.b64encode(b"alice-private-content").decode()}, auth=alice)
        self.assertEqual(201, upload.status)
        own = self.request("/api/file?path=files/alice.txt", auth=alice)
        self.assertEqual(200, own.status)
        self.assertEqual(b"alice-private-content", own.raw)
        self.assertEqual(404, self.request("/api/file?path=files/alice.txt", auth=bob).status)
        root = self.store.workspace_path(alice["user"]["id"])
        host = self.data_dir / "host-secrets"
        host.mkdir()
        sentinel = "host-private-secret-should-never-appear"
        (host / "secret.txt").write_text(sentinel)
        (root / "files" / "secret-link").symlink_to(host / "secret.txt")
        (root / "files" / "host-dir-link").symlink_to(host, target_is_directory=True)
        for relative in ("files/secret-link", "files/host-dir-link/secret.txt", "../host-secrets/secret.txt", str(host / "secret.txt")):
            response = self.request("/api/file?path=" + urllib.parse.quote(relative), auth=alice)
            self.assertIn(response.status, (400, 404))
            self.assertNotIn(sentinel.encode(), response.raw)
        response = self.request("/api/files?path=files/host-dir-link", auth=alice)
        self.assertIn(response.status, (400, 404))
        response = self.request("/api/files", method="POST", body={"path": "files/host-dir-link", "filename": "attack.txt",
                                "content": base64.b64encode(b"attack").decode()}, auth=alice)
        self.assertIn(response.status, (400, 404))
        self.assertFalse((host / "attack.txt").exists())
        response = self.request("/api/files", method="POST", body={"path": "files", "filename": "secret-link",
                                "content": base64.b64encode(b"overwrite attack").decode()}, auth=alice)
        self.assertEqual(409, response.status)
        self.assertEqual(sentinel, (host / "secret.txt").read_text())
        if hasattr(os, "mkfifo"):
            os.mkfifo(root / "files" / "pipe")
            self.assertEqual(400, self.request("/api/file?path=files/pipe", auth=alice).status)

    def test_conversation_files_keep_identical_names_separate_and_require_ownership(self):
        alice, bob = self.admin(), self.bob()
        uid = alice['user']['id']
        first = self.store.create_conversation(uid, 'First project')['id']
        second = self.store.create_conversation(uid, 'Second project')['id']
        bob_conversation = self.store.create_conversation(bob['user']['id'], 'Bob project')['id']
        for cid, content in ((first, b'first project'), (second, b'second project')):
            uploaded = self.request('/api/files', method='POST', auth=alice,
                                    body={'conversation_id': cid, 'path': '', 'filename': 'report.txt',
                                          'content': base64.b64encode(content).decode()})
            self.assertEqual(201, uploaded.status, uploaded.raw)
            downloaded = self.request('/api/file?conversation='+cid+'&path=report.txt', auth=alice)
            self.assertEqual(200, downloaded.status, downloaded.raw)
            self.assertEqual(content, downloaded.raw)
            listed = self.request('/api/files?page=1&conversation='+cid, auth=alice)
            self.assertEqual(['report.txt'], [item['name'] for item in listed.payload['items']])
        # Files stay relative to the selected conversation, not the tenant root.
        self.assertEqual(404, self.request('/api/file?path=report.txt', auth=alice).status)
        for cid, auth in ((first, bob), (bob_conversation, alice)):
            self.assertEqual(404, self.request('/api/files?conversation='+cid, auth=auth).status)
            self.assertEqual(404, self.request('/api/file?conversation='+cid+'&path=report.txt', auth=auth).status)
            uploaded = self.request('/api/files', method='POST', auth=auth,
                                    body={'conversation_id': cid, 'filename': 'foreign.txt', 'content': ''})
            self.assertEqual(404, uploaded.status, uploaded.raw)
        self.assertFalse((self.store.conversation_workspace_path(uid, first) / 'foreign.txt').exists())

    def test_conversation_files_reject_empty_scope_traversal_and_symlinks(self):
        auth = self.admin()
        uid = auth['user']['id']
        cid = self.store.create_conversation(uid)['id']
        root = self.store.prepare_conversation_workspace(uid, cid)
        sibling = self.store.create_conversation(uid)['id']
        other = self.store.prepare_conversation_workspace(uid, sibling)
        (other / 'private.txt').write_text('other conversation content')
        host = self.data_dir / 'host-private'
        host.mkdir()
        (host / 'secret.txt').write_text('host sentinel')
        (root / 'host-link').symlink_to(host, target_is_directory=True)
        (root / 'file-link').symlink_to(host / 'secret.txt')
        for relative in ('../'+sibling+'/private.txt', str(host / 'secret.txt'),
                         'host-link/secret.txt', 'file-link'):
            response = self.request('/api/file?conversation='+cid+'&path='+urllib.parse.quote(relative), auth=auth)
            self.assertIn(response.status, (400, 404), response.raw)
            self.assertNotIn(b'host sentinel', response.raw)
            self.assertNotIn(b'other conversation content', response.raw)
        for directory in ('../'+sibling, 'host-link'):
            response = self.request('/api/files', method='POST', auth=auth,
                                    body={'conversation_id': cid, 'path': directory,
                                          'filename': 'attack.txt', 'content': ''})
            self.assertIn(response.status, (400, 404), response.raw)
        self.assertFalse((host / 'attack.txt').exists())
        self.assertFalse((other / 'attack.txt').exists())
        self.assertEqual(404, self.request('/api/files?conversation=', auth=auth).status)
        self.assertEqual(404, self.request('/api/file?conversation=&path=files/any.txt', auth=auth).status)
        empty_scope = self.request('/api/files', method='POST', auth=auth,
                                   body={'conversation_id': '', 'filename': 'attack.txt', 'content': ''})
        self.assertEqual(404, empty_scope.status)

    def test_conversation_file_api_rejects_scope_ancestor_symlink(self):
        auth = self.admin()
        uid = auth['user']['id']
        cid = self.store.create_conversation(uid)['id']
        root = self.store.prepare_conversation_workspace(uid, cid)
        root.rmdir()
        outside = self.data_dir / 'outside-conversation'
        outside.mkdir()
        (outside / 'private.txt').write_text('outside sentinel')
        root.symlink_to(outside, target_is_directory=True)
        for endpoint in ('/api/files?conversation='+cid,
                         '/api/file?conversation='+cid+'&path=private.txt'):
            response = self.request(endpoint, auth=auth)
            self.assertEqual(400, response.status, response.raw)
            self.assertNotIn(b'outside sentinel', response.raw)
        response = self.request('/api/files', method='POST', auth=auth,
                                body={'conversation_id': cid, 'filename': 'attack.txt', 'content': ''})
        self.assertEqual(400, response.status)
        self.assertFalse((outside / 'attack.txt').exists())

    def test_conversation_upload_uses_held_parent_when_ancestor_is_replaced(self):
        auth = self.admin()
        uid = auth['user']['id']
        cid = self.store.create_conversation(uid)['id']
        root = self.store.prepare_conversation_workspace(uid, cid)
        conversations = root.parent
        held = conversations.with_name('conversations-held')
        outside = self.data_dir / 'outside-conversations'
        (outside / cid).mkdir(parents=True)
        original_open = os.open
        replaced = False

        def swap_after_open(name, flags, *args, **kwargs):
            nonlocal replaced
            descriptor = original_open(name, flags, *args, **kwargs)
            if name == 'conversations' and 'dir_fd' in kwargs and not replaced:
                conversations.rename(held)
                conversations.symlink_to(outside, target_is_directory=True)
                replaced = True
            return descriptor

        with mock.patch('store.os.open', side_effect=swap_after_open):
            response = self.request('/api/files', method='POST', auth=auth,
                                    body={'conversation_id': cid, 'filename': 'safe.txt',
                                          'content': base64.b64encode(b'anchored upload').decode()})
        self.assertEqual(201, response.status, response.raw)
        self.assertTrue(replaced)
        self.assertEqual(b'anchored upload', (held / cid / 'safe.txt').read_bytes())
        self.assertFalse((outside / cid / 'safe.txt').exists())
        # A new request cannot follow the now-replaced scope ancestor.
        self.assertEqual(400, self.request('/api/files?conversation='+cid, auth=auth).status)

    def test_model_key_and_bootstrap_token_are_not_returned_or_logged(self):
        common.save_config({"api_key": self.model_secret})
        status = self.request("/api/status")
        self.assertTrue(status.payload["configured"])
        auth = self.admin()
        responses = [status, self.request("/api/me", auth=auth), self.request("/api/settings", auth=auth),
                     self.request("/health?bootstrap_token=" + self.bootstrap)]
        for response in responses:
            self.assertEqual(200, response.status)
            self.assertNotIn(self.model_secret.encode(), response.raw)
            self.assertNotIn(self.bootstrap.encode(), response.raw)
        self.assertNotIn(self.model_secret, self.output.getvalue())
        self.assertNotIn(self.bootstrap, self.output.getvalue())
        self.assertEqual(0o600, common.CONFIG_PATH.stat().st_mode & 0o777)

    def test_invalid_json_and_field_shapes_are_client_errors(self):
        auth = self.admin()
        for raw in (b'{"broken":', b'[]', b'null', b'"string"', b'\xff'):
            with self.subTest(raw=raw):
                response = self.request("/api/conversations", method="POST", raw=raw, auth=auth)
                self.assertEqual(400, response.status, response.raw)
        malformed = (
            ("/api/files", {"path": [], "filename": "attack.txt", "content": ""}),
            ("/api/files", {"path": "files", "filename": "attack.txt", "content": []}),
            ("/api/tasks", {"conversation_id": {}, "prompt": "attack", "idempotency_key": "shape"}),
        )
        for path, body in malformed:
            with self.subTest(path=path, body=body):
                response = self.request(path, method="POST", body=body, auth=auth)
                self.assertEqual(400, response.status, response.raw)
        self.assertEqual(200, self.request("/health").status)

    def test_model_proxy_token_requires_live_uncancelled_task_lease(self):
        auth = self.admin()
        conversation = self.conversation(auth)
        task = self.task(auth, conversation)
        proxy = server.BoundedHTTPServer(("127.0.0.1", 0), server.ProxyHandler, max_workers=8, request_timeout=330)
        proxy.daemon_threads = True
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            proxy_base = f"http://127.0.0.1:{proxy.server_port}"
            token = server.TOKENS.issue(task["id"], auth["user"]["id"])
            body = {"messages": [{"role": "user", "content": "test"}]}

            def invoke(value=token):
                return self.request("/v1/chat/completions", method="POST", body=body, base=proxy_base,
                                    headers={"Authorization": "Bearer " + value})

            self.assertEqual(403, invoke("invalid-token").status)
            self.assertEqual(403, invoke().status)  # A queued task has no model access.
            self.store.claim_task("worker")
            self.assertEqual(503, invoke().status)  # Authorized, but no model key.
            common.save_config({'api_key': self.model_secret})
            forwarded = []
            original_open = urllib.request.urlopen

            class Upstream(io.BytesIO):
                status = 200
                headers = {'Content-Type': 'text/event-stream'}

            def route_upstream(request, *args, **kwargs):
                if request.full_url == 'https://api.deepseek.com/chat/completions':
                    forwarded.append(json.loads(request.data))
                    return Upstream(b'data: [DONE]\n\n')
                return original_open(request, *args, **kwargs)

            # Existing tenant daemons may still request the old 8192 cap;
            # the authoritative host budget upgrades them without a restart.
            with mock.patch.object(server.urllib.request, 'urlopen', side_effect=route_upstream):
                for requested in (8192, 999999):
                    body['max_tokens'] = requested
                    body['model'] = 'tenant-supplied-model'
                    self.assertEqual(200, invoke().status)
            self.assertEqual([49_152, 49_152], [item['max_tokens'] for item in forwarded])
            self.assertTrue(all(item['model'] == common.config()['model'] for item in forwarded))
            # A lease can expire before the queue sweeper changes the status.
            with sqlite3.connect(self.store.db_path) as connection:
                connection.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (time.time() - 1, task["id"]))
            self.assertEqual(403, invoke().status)
            # Restore a live lease to test cancellation as a separate boundary.
            with sqlite3.connect(self.store.db_path) as connection:
                connection.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (time.time() + 60, task["id"]))
            self.store.cancel_task(auth["user"]["id"], task["id"])
            self.assertEqual(403, invoke().status)
            server.TOKENS.revoke(task["id"])
            self.assertEqual(403, invoke().status)
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=5)

    def test_paginated_history_contract_and_loaded_task_refresh(self):
        auth = self.admin()
        conversation = self.conversation(auth)
        submitted = [self.task(auth, conversation, key=f"page-{number}") for number in range(3)]
        query = "?page=1&conversation=" + conversation["id"]
        messages = self.request("/api/messages" + query + "&limit=2", auth=auth)
        self.assertEqual(200, messages.status)
        self.assertTrue(messages.payload["has_more"])
        self.assertEqual(2, len(messages.payload["items"]))
        older = self.request("/api/messages" + query + "&before=" + str(messages.payload["next_cursor"]), auth=auth)
        self.assertEqual(200, older.status)
        self.assertEqual(1, len(older.payload["items"]))
        new = self.store.add_message(auth["user"]["id"], conversation["id"], "assistant", "Latest response")
        live = self.request("/api/messages" + query + "&after=" + str(messages.payload["items"][-1]["id"]), auth=auth)
        self.assertEqual([new["id"]], [item["id"] for item in live.payload["items"]])

        tasks = self.request("/api/tasks" + query + "&limit=2", auth=auth)
        self.assertEqual(200, tasks.status)
        self.assertTrue(tasks.payload["has_more"])
        older = self.request("/api/tasks" + query + "&cursor=" + urllib.parse.quote(tasks.payload["next_cursor"]), auth=auth)
        self.assertEqual([submitted[0]["id"]], [item["id"] for item in older.payload["items"]])
        claimed = self.store.claim_task("worker")
        self.store.finish_task(claimed["id"], "worker")
        refreshed = self.request("/api/tasks" + query + "&ids=" + claimed["id"], auth=auth)
        self.assertEqual("succeeded", refreshed.payload["items"][0]["status"])
        self.assertEqual(400, self.request("/api/tasks" + query + "&cursor=bad!", auth=auth).status)

        event_query = "?page=1&task=" + submitted[0]["id"]
        events = self.request("/api/events" + event_query + "&limit=2", auth=auth)
        self.assertEqual(200, events.status)
        self.assertTrue(events.payload["has_more"])
        older = self.request("/api/events" + event_query + "&before=" + str(events.payload["next_cursor"]), auth=auth)
        self.assertGreater(len(older.payload["items"]), 0)
        last = events.payload["items"][-1]["id"]
        new_cursor = self.store.append_event(auth["user"]["id"], submitted[0]["id"], "output", {"text": "live tail"})
        live = self.request("/api/events" + event_query + "&after=" + str(last), auth=auth)
        self.assertEqual([new_cursor], [item["id"] for item in live.payload["items"]])
        bob = self.bob()
        for endpoint in ("/api/messages" + query, "/api/tasks" + query, "/api/events" + event_query,
                         "/api/tasks?page=1&ids=" + claimed["id"]):
            self.assertEqual(404, self.request(endpoint, auth=bob).status)
        self.assertEqual(400, self.request("/api/settings", method="POST", body={"model": "deepseek-chat"}, auth=auth).status)

    def test_directory_beyond_2000_entries_and_large_file_streaming(self):
        auth = self.admin()
        root = self.store.workspace_path(auth["user"]["id"]) / "files"
        for number in range(2105):
            (root / f"f-{number:04}.txt").touch()
        (root / "folder").mkdir()
        outside = self.data_dir / "host-private.txt"
        outside.write_text("host-private")
        (root / "host-link").symlink_to(outside)
        (root / "host-directory").symlink_to(self.data_dir, target_is_directory=True)
        items, cursor = [], None
        while True:
            path = "/api/files?page=1&path=files&limit=500" + ("&cursor=" + urllib.parse.quote(cursor) if cursor else "")
            page = self.request(path, auth=auth)
            self.assertEqual(200, page.status, page.raw)
            items.extend(page.payload["items"])
            if not page.payload["has_more"]:
                break
            cursor = page.payload["next_cursor"]
        self.assertEqual(2108, len(items))
        self.assertEqual(2108, len({item["name"] for item in items}))
        self.assertEqual("folder", items[0]["name"])
        self.assertFalse(next(item for item in items if item["name"] == "host-link")["is_file"])
        self.assertEqual(400, self.request("/api/files?page=1&path=files/host-directory", auth=auth).status)
        self.assertEqual(400, self.request("/api/files?page=1&path=files&cursor=bad!", auth=auth).status)

        size, block = 9 * 1024 * 1024 + 123, bytes(range(256)) * 256
        digest = hashlib.sha256()
        with (root / "large.bin").open("wb") as file:
            remaining = size
            while remaining:
                chunk = block[:min(len(block), remaining)]
                file.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        download = self.request("/api/file?path=files/large.bin", auth=auth)
        self.assertEqual(200, download.status)
        self.assertEqual(str(size), download.headers["Content-Length"])
        self.assertEqual(size, len(download.raw))
        self.assertEqual(digest.hexdigest(), hashlib.sha256(download.raw).hexdigest())
        bob = self.bob()
        self.assertEqual([], self.request("/api/files?page=1&path=files", auth=bob).payload["items"])
        self.assertEqual(404, self.request("/api/file?path=files/large.bin", auth=bob).status)


class BoundedServerTests(unittest.TestCase):
    def start_server(self, handler_class, *, max_workers=2, request_timeout=2):
        http = server.BoundedHTTPServer(("127.0.0.1", 0), handler_class,
                                       max_workers=max_workers, request_timeout=request_timeout)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()

        def stop():
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        return f"http://127.0.0.1:{http.server_port}", http

    @staticmethod
    def fetch(url):
        try:
            response = urllib.request.urlopen(url, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            response.read()
            return response.status

    def test_saturation_rejects_immediately_and_never_exceeds_worker_limit(self):
        release = threading.Event()
        self.addCleanup(release.set)
        full = threading.Event()
        lock = threading.Lock()
        counters = {"active": 0, "maximum": 0}

        class HeldHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                with lock:
                    counters["active"] += 1
                    counters["maximum"] = max(counters["maximum"], counters["active"])
                    if counters["active"] == 2:
                        full.set()
                try:
                    if self.path == "/hold":
                        release.wait(5)
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                finally:
                    with lock:
                        counters["active"] -= 1

        base, _ = self.start_server(HeldHandler, max_workers=2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            held = [pool.submit(self.fetch, base + "/hold") for _ in range(2)]
            try:
                self.assertTrue(full.wait(2), "Both execution slots should become occupied")
                rejected = [pool.submit(self.fetch, base + "/fast") for _ in range(6)]
                self.assertEqual([503] * 6, [result.result(timeout=2) for result in rejected])
                self.assertFalse(any(result.done() for result in held))
                with lock:
                    self.assertEqual(2, counters["active"])
                    self.assertEqual(2, counters["maximum"])
            finally:
                release.set()
            self.assertEqual([200, 200], [result.result(timeout=2) for result in held])
        self.assertEqual(200, self.fetch(base + "/fast"))
        self.assertEqual(2, counters["maximum"])

    def test_partial_headers_timeout_releases_connection_slot(self):
        accepted = threading.Event()
        observed = []

        class TimeoutHandler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                observed.append(self.connection.gettimeout())
                accepted.set()

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        base, http = self.start_server(TimeoutHandler, max_workers=1, request_timeout=0.4)
        with socket.create_connection(http.server_address, timeout=2) as slow:
            slow.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
            self.assertTrue(accepted.wait(2))
            self.assertEqual(0.4, observed[0])
            self.assertEqual(503, self.fetch(base + "/"))
            self.assertEqual(b"", slow.recv(1))  # Incomplete headers timed out.
        self.assertEqual(200, self.fetch(base + "/"))


if __name__ == "__main__":
    unittest.main()
