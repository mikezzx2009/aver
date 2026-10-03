#!/usr/bin/env python3
"""Small durable web app; task execution runs in a separate supervised worker."""
import base64
import contextlib
import hashlib
import heapq
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from common import DATA, ROOT, ProxyTokens, config, save_config
from server_stats import HostStatsSampler
from publisher import PublicationError, list_publications
from store import Store, StoreError, NotFound, ValidationError, Conflict

STORE = None
TOKENS = None
AUTH_LOCK = threading.Lock()
RATE_LOCK = threading.Lock()
RATE = {}
MAX_BODY = 12 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MODEL_OUTPUT_TOKENS = 49_152

class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound accepted connections, including idle HTTP keep-alive clients."""
    daemon_threads = True
    block_on_close = False

    def __init__(self, server_address, handler_class, *, max_workers=32, request_timeout=30):
        if not isinstance(max_workers, int) or max_workers < 1 or request_timeout <= 0:
            raise ValueError('Server concurrency and socket timeout must be positive')
        self._request_slots = threading.BoundedSemaphore(max_workers)
        self.request_timeout = float(request_timeout)
        self._stats_lock = threading.Lock()
        self._stats_sampler = None
        self._stats_closed = False
        super().__init__(server_address, handler_class)

    def server_stats(self):
        with self._stats_lock:
            if self._stats_closed:
                raise RuntimeError('Server is closing')
            if self._stats_sampler is None:
                self._stats_sampler = HostStatsSampler(DATA)
            sampler = self._stats_sampler
        return sampler.snapshot()

    def server_close(self):
        with self._stats_lock:
            self._stats_closed = True
            sampler = self._stats_sampler
        if sampler is not None:
            sampler.close()
        super().server_close()

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            # Never queue an accepted connection or spend a worker on rejection.
            # The short response fits the socket send buffer; a client that cannot
            # receive it is simply closed without blocking the accept loop.
            try:
                request.setblocking(False)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                b'Connection: close\r\nRetry-After: 1\r\n'
                                b'Content-Length: 0\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            request.settimeout(self.request_timeout)
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

def public_user(user):
    return {k: user[k] for k in ('id', 'username', 'is_admin')}

def public_config():
    c = config()
    return {'configured': bool(c['api_key']), 'model': c['model'],
            'signup_enabled': bool(c['signup_enabled']),
            'needs_bootstrap': not bool(STORE.list_all_users()), 'domain': c['domain'],
            'site_base_url': c.get('site_base_url', '')}

def csrf(token):
    return hashlib.sha256(('csrf:' + token).encode()).hexdigest()

def message_view(user_id, messages):
    """Show canonical website links while preserving the original message log."""
    task_ids = list(dict.fromkeys(message.get('task_id') for message in messages
                                 if message.get('role') == 'assistant' and message.get('task_id')))
    replacements = {}
    for offset in range(0, len(task_ids), 200):
        try:
            records = list_publications(DATA, user_id, task_ids[offset:offset + 200])
        except (OSError, PublicationError):
            continue  # File metadata must not prevent reading the conversation.
        for record in records:
            parsed = urllib.parse.urlsplit(record['url'])
            if re.fullmatch(r'/[a-z0-9]{4,10}/', parsed.path):
                old_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
                                                  '/sites/' + record['id'] + '/', '', ''))
                replacements[old_url] = record['url']
    result = []
    for message in messages:
        if message.get('role') != 'assistant':
            result.append(message)
            continue
        content = message['content']
        for old, canonical in replacements.items():
            content = content.replace(old, canonical)
        result.append({**message, 'content': content})
    return result

@contextlib.contextmanager
def workspace_fd(user_id, path='', *, parent=False, conversation_id=None):
    """Resolve every directory using openat + O_NOFOLLOW, including under races."""
    if not isinstance(path, str):
        raise ValidationError('文件路径必须是文字')
    parts = PurePosixPath(path).parts
    if path.startswith('/') or any(p in ('..', '.') for p in parts) or '\x00' in path:
        raise ValidationError('文件路径无效')
    scope = (STORE.open_workspace(user_id) if conversation_id is None else
             STORE.open_conversation_workspace(user_id, conversation_id))
    with scope as root_fd:
        fd = os.dup(root_fd)
        try:
            for part in (parts[:-1] if parent else parts):
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            yield fd, parts[-1] if parts else ''
        finally:
            os.close(fd)

def file_page(directory_fd, *, cursor=None, limit=200):
    """Page a directory without retaining all names or following any symlinks."""
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValidationError('文件页大小必须在 1 到 500 之间')
    boundary = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                raise ValueError('Invalid cursor')
            value = json.loads(base64.b64decode(cursor+'='*(-len(cursor)%4), altchars=b'-_', validate=True))
            if (not isinstance(value, list) or len(value) != 2 or type(value[0]) is not int or value[0] not in (0, 1)
                    or not isinstance(value[1], str) or not value[1] or '/' in value[1] or '\x00' in value[1]):
                raise ValueError('Invalid cursor')
            boundary = tuple(value)
        except (ValueError, TypeError, UnicodeDecodeError):
            raise ValidationError('文件历史游标无效')

    def entries():
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # Agent may remove an entry while listing.
                item = {'name': entry.name, 'is_dir': stat.S_ISDIR(info.st_mode),
                        'is_file': stat.S_ISREG(info.st_mode), 'size': info.st_size}
                key = (0 if item['is_dir'] else 1, item['name'])
                if boundary is None or key > boundary:
                    yield key, item

    # nsmallest maintains a bounded heap while scanning the directory.
    selected = heapq.nsmallest(limit+1, entries(), key=lambda item: item[0])
    has_more, selected = len(selected) > limit, selected[:limit]
    next_cursor = None
    if has_more:
        next_cursor = base64.urlsafe_b64encode(json.dumps(selected[-1][0], ensure_ascii=True).encode()).decode().rstrip('=')
    return {'items': [item for _, item in selected], 'has_more': has_more, 'next_cursor': next_cursor}

class Handler(BaseHTTPRequestHandler):
    server_version = 'AgentCloud'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Never log query strings (bootstrap tokens) or model headers/content.
        print(json.dumps({'http': self.command, 'path': urllib.parse.urlsplit(self.path).path,
                          'status': args[1] if len(args) > 1 else ''}), flush=True)

    def headers_common(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def json(self, payload, status=200, extra=None):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.headers_common()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ValidationError('请求长度无效')
        if not 0 < length <= MAX_BODY:
            raise ValidationError('请求为空或过大')
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValidationError('请求格式无效')
        return body

    def token(self):
        try:
            cookie = cookies.SimpleCookie(self.headers.get('Cookie', ''))
            return cookie['agent_session'].value if 'agent_session' in cookie else ''
        except cookies.CookieError:
            return ''

    def user(self):
        user = STORE.get_session(self.token())
        if not user:
            raise PermissionError('请先登录')
        return user

    def check_origin(self):
        domain = config()['domain']
        port = self.server.server_port
        allowed = {f'http://localhost:{port}', f'http://127.0.0.1:{port}', f'https://{domain}'}
        if self.headers.get('Origin') not in allowed:
            raise PermissionError('请求来源无效')

    def check_csrf(self):
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf(self.token())):
            raise PermissionError('登录状态已变化，请刷新网页')

    def session_cookie(self, token, expired=False):
        secure = self.headers.get('X-Forwarded-Proto') == 'https' and self.client_address[0] == '127.0.0.1'
        return 'agent_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + ('0' if expired else '604800') + ('; Secure' if secure else '')

    def rate_check(self):
        ip = self.client_address[0]
        if ip == '127.0.0.1' and self.headers.get('X-Real-IP'):
            try:
                ip = str(ipaddress.ip_address(self.headers['X-Real-IP']))
            except ValueError:
                pass
        with RATE_LOCK:
            now = time.time()
            times = [x for x in RATE.get(ip, []) if now-x < 300]
            if len(times) >= 20:
                raise Conflict('尝试次数过多，请稍后再试')
            times.append(now)
            RATE[ip] = times
            if len(RATE) > 5000:
                for key in list(RATE):
                    if not RATE[key] or now-RATE[key][-1] > 300:
                        del RATE[key]

    def do_GET(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            q = urllib.parse.parse_qs(parsed.query)
            # An explicitly empty scope is invalid, never the legacy workspace.
            file_conversation = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get('conversation', [None])[0]
            if path == '/health':
                return self.json({'ok': True})
            if path == '/api/status':
                return self.json(public_config())
            if path == '/api/me':
                return self.json({'user': public_user(self.user()), 'csrf': csrf(self.token()), 'config': public_config()})
            if path.startswith('/api/'):
                user = self.user()
                uid = user['id']
                if path == '/api/server-stats':
                    return self.json(self.server.server_stats())
                if path == '/api/conversations':
                    return self.json(STORE.list_conversations(uid))
                if path == '/api/messages':
                    if q.get('page', [''])[0] == '1':
                        page = STORE.list_messages_page(uid, q.get('conversation', [''])[0],
                            before=int(q['before'][0]) if 'before' in q else None,
                            after=int(q['after'][0]) if 'after' in q else None,
                            limit=int(q.get('limit', ['100'])[0]))
                        return self.json({**page, 'items': message_view(uid, page['items'])})
                    return self.json(message_view(uid, STORE.list_messages(uid,
                        q.get('conversation', [''])[0], after=int(q.get('after', ['0'])[0]))))
                if path == '/api/tasks':
                    if q.get('page', [''])[0] == '1':
                        if 'ids' in q:
                            if 'cursor' in q:
                                raise ValidationError('任务刷新不能同时指定历史游标')
                            return self.json({'items': STORE.get_tasks(uid, q['ids'][0].split(','),
                                conversation_id=q.get('conversation', [None])[0]), 'has_more': False, 'next_cursor': None})
                        return self.json(STORE.list_tasks_page(uid, conversation_id=q.get('conversation', [None])[0],
                            cursor=q.get('cursor', [None])[0], limit=int(q.get('limit', ['50'])[0])))
                    return self.json(STORE.list_tasks(uid, conversation_id=q.get('conversation', [None])[0], limit=200, before=float(q['before'][0]) if 'before' in q else None))
                if path == '/api/events':
                    if q.get('page', [''])[0] == '1':
                        return self.json(STORE.list_events_page(uid, task_id=q.get('task', [None])[0],
                            before=int(q['before'][0]) if 'before' in q else None,
                            after=int(q['after'][0]) if 'after' in q else None,
                            limit=int(q.get('limit', ['100'])[0])))
                    return self.json(STORE.list_events(uid, after=int(q.get('after', ['0'])[0]), task_id=q.get('task', [None])[0]))
                if path == '/api/files':
                    if q.get('page', [''])[0] == '1':
                        with workspace_fd(uid, q.get('path', [''])[0], conversation_id=file_conversation) as (fd, _):
                            return self.json(file_page(fd, cursor=q.get('cursor', [None])[0],
                                                       limit=int(q.get('limit', ['200'])[0])))
                    items = []
                    with workspace_fd(uid, q.get('path', [''])[0], conversation_id=file_conversation) as (fd, _):
                        with os.scandir(fd) as entries:
                            for entry in entries:
                                info = entry.stat(follow_symlinks=False)
                                items.append({'name': entry.name, 'is_dir': stat.S_ISDIR(info.st_mode),
                                              'is_file': stat.S_ISREG(info.st_mode), 'size': info.st_size})
                                if len(items) >= 2000:
                                    break
                    return self.json(sorted(items, key=lambda x: (not x['is_dir'], x['name'])))
                if path == '/api/file':
                    relative = q.get('path', [''])[0]
                    with workspace_fd(uid, relative, parent=True, conversation_id=file_conversation) as (parent, name):
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                        with os.fdopen(fd, 'rb') as file:
                            info = os.fstat(file.fileno())
                            if not stat.S_ISREG(info.st_mode):
                                raise ValidationError('只支持下载普通文件')
                            self.send_response(200)
                            self.headers_common()
                            self.send_header('Content-Type', 'application/octet-stream')
                            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
                            self.send_header('Content-Length', str(info.st_size))
                            self.send_header('Cache-Control', 'no-store')
                            self.end_headers()
                            remaining = info.st_size
                            try:
                                while remaining:
                                    chunk = file.read(min(64 * 1024, remaining))
                                    if not chunk:
                                        self.close_connection = True  # File shortened after fstat.
                                        break
                                    self.wfile.write(chunk)
                                    remaining -= len(chunk)
                            except (OSError, TimeoutError):
                                self.close_connection = True
                    return
                if path == '/api/settings' and user['is_admin']:
                    c = config()
                    return self.json({'model': c['model'], 'configured': bool(c['api_key']), 'signup_enabled': c['signup_enabled'],
                                      'users': [public_user(u) for u in STORE.list_all_users()], 'queue': STORE.queue_counts()})
                raise NotFound('页面不存在')
            assets = {'/':'index.html', '/index.html':'index.html', '/style.css':'style.css', '/app.js':'app.js', '/favicon.svg':'favicon.svg'}
            if path not in assets:
                raise NotFound('页面不存在')
            name = assets[path]
            data = (ROOT / 'static' / name).read_bytes()
            self.send_response(200)
            self.headers_common()
            self.send_header('Content-Type', (mimetypes.guess_type(name)[0] or 'application/octet-stream') + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except BrokenPipeError:
            pass
        except Exception as e:
            self.error(e)

    def do_POST(self):
        try:
            self.check_origin()
            path = urllib.parse.urlsplit(self.path).path
            body = self.body()
            for field in ('username', 'password', 'bootstrap_token', 'title', 'conversation_id', 'prompt', 'idempotency_key', 'task_id', 'filename', 'content', 'path', 'model', 'api_key'):
                if field in body and not isinstance(body[field], str):
                    raise ValidationError('请求字段格式无效：'+field)
            if path in ('/api/login', '/api/signup'):
                self.rate_check()
                with AUTH_LOCK:
                    if path == '/api/signup':
                        c = config()
                        first = not bool(STORE.list_all_users())
                        if first:
                            if not c['bootstrap_enabled'] or not hmac.compare_digest(str(body.get('bootstrap_token', '')), str(c.get('bootstrap_token', secrets.token_hex(32)))):
                                raise PermissionError('请输入初始化口令以创建管理员')
                        elif not c['signup_enabled']:
                            raise PermissionError('当前仅开放已有测试账户，请联系管理员')
                        user = STORE.create_user(body.get('username', ''), body.get('password', ''), is_admin=first)
                        if first:
                            save_config({'bootstrap_enabled': False, 'bootstrap_token': ''})
                    else:
                        user = STORE.authenticate_user(body.get('username', ''), body.get('password', ''))
                        if not user:
                            raise PermissionError('用户名或密码错误')
                    token = STORE.create_session(user['id'])
                return self.json({'user': public_user(user), 'csrf': csrf(token), 'config': public_config()}, extra={'Set-Cookie': self.session_cookie(token)})
            user = self.user()
            self.check_csrf()
            uid = user['id']
            if path == '/api/logout':
                STORE.revoke_session(self.token())
                return self.json({'ok': True}, extra={'Set-Cookie': self.session_cookie('', True)})
            if path == '/api/conversations':
                return self.json(STORE.create_conversation(uid, str(body.get('title', '新对话'))[:80]), 201)
            if path == '/api/tasks':
                if len(str(body.get('prompt', ''))) > 32000:
                    raise ValidationError('单个任务描述最长 32000 字符')
                return self.json(STORE.submit_task(uid, body.get('conversation_id', ''), body.get('prompt', ''), body.get('idempotency_key', '')), 202)
            if path == '/api/cancel':
                return self.json(STORE.cancel_task(uid, body.get('task_id', '')))
            if path == '/api/retry':
                return self.json(STORE.retry_task(uid, body.get('task_id', ''), body.get('idempotency_key', '')))
            if path == '/api/files':
                filename = body.get('filename', '')
                if not isinstance(filename, str) or not filename or filename in ('.', '..') or '/' in filename or '\\' in filename or '\x00' in filename:
                    raise ValidationError('文件名无效')
                data = base64.b64decode(body.get('content', ''), validate=True)
                if len(data) > MAX_FILE:
                    raise ValidationError('上传文件最大 8 MB')
                with workspace_fd(uid, body.get('path', ''), conversation_id=body.get('conversation_id')) as (parent, _):
                    temp = '.upload-' + secrets.token_hex(12)
                    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    try:
                        with os.fdopen(fd, 'wb') as f:
                            f.write(data)
                            f.flush()
                            os.fsync(f.fileno())
                            if os.geteuid() == 0:
                                os.fchown(f.fileno(), 10001, 10001)
                        os.link(temp, filename, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    except FileExistsError:
                        raise Conflict('同名文件已存在，请修改文件名')
                    finally:
                        os.unlink(temp, dir_fd=parent)
                return self.json({'ok': True}, 201)
            if path == '/api/settings' and user['is_admin']:
                model = body.get('model', config()['model'])
                if model not in ('deepseek-v4-flash', 'deepseek-v4-pro'):
                    raise ValidationError('模型名称无效')
                changes = {'model': model}
                if isinstance(body.get('signup_enabled'), bool):
                    changes['signup_enabled'] = body['signup_enabled']
                if body.get('api_key'):
                    key = body['api_key']
                    if not isinstance(key, str) or not 10 <= len(key.strip()) <= 256:
                        raise ValidationError('API Key 格式无效')
                    # Validate before storing, without submitting a billable model task.
                    req = urllib.request.Request('https://api.deepseek.com/models', headers={'Authorization': 'Bearer ' + key.strip()})
                    try:
                        with urllib.request.urlopen(req, timeout=20) as response:
                            models = json.load(response).get('data', [])
                        if models and model not in {m['id'] for m in models}:
                            raise ValidationError('此 API Key 尚不支持所选模型，请选择可用模型')
                    except urllib.error.HTTPError:
                        raise ValidationError('DeepSeek 未接受该 API Key，请检查后重试')
                    except urllib.error.URLError:
                        raise ValidationError('暂时无法连接 DeepSeek，请稍后重试')
                    changes['api_key'] = key.strip()
                save_config(changes)
                if config()['api_key']:
                    STORE.requeue_waiting_tasks()
                return self.json({'ok': True, 'config': public_config()})
            raise NotFound('操作不存在')
        except BrokenPipeError:
            pass
        except Exception as e:
            self.error(e)

    def error(self, e):
        if isinstance(e, PermissionError):
            code = 401 if str(e) == '请先登录' else 403
        elif isinstance(e, (NotFound, FileNotFoundError)):
            code = 404
        elif isinstance(e, Conflict):
            code = 409
        elif isinstance(e, (ValidationError, ValueError, OSError)):
            code = 400
        else:
            code = 500
        message = str(e) if isinstance(e, (StoreError, PermissionError)) else ('请求格式无效' if code < 500 else '服务暂时遇到问题')
        self.json({'error': message}, code)

class ProxyHandler(BaseHTTPRequestHandler):
    """Only exposes model inference. Never exposes host credentials or web APIs."""
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_):
        pass
    def do_GET(self):
        self.send_error(405)
    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != '/v1/chat/completions':
            return self.send_error(404)
        token = self.headers.get('Authorization', '')
        record = TOKENS.lookup(token[7:] if token.startswith('Bearer ') else '')
        if not record:
            return self.send_error(403)
        try:
            task = STORE.get_task(record['user_id'], record['task_id'])
            if task['status'] != 'running' or task['cancel_requested'] or not task['lease_expires_at'] or task['lease_expires_at'] <= time.time():
                return self.send_error(403)
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_BODY:
                return self.send_error(413)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                return self.send_error(400)
            c = config()
            if not c['api_key']:
                return self.send_error(503)
            body['model'] = c['model']
            # The host owns the per-request budget. This also upgrades already
            # running tenant SDKs without replacing their persistent processes.
            body['max_tokens'] = MODEL_OUTPUT_TOKENS
            req = urllib.request.Request('https://api.deepseek.com/chat/completions',
                data=json.dumps(body).encode(), headers={'Authorization': 'Bearer '+c['api_key'], 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=300) as upstream:
                self.send_response(upstream.status)
                self.send_header('Content-Type', upstream.headers.get('Content-Type', 'application/json'))
                self.send_header('Transfer-Encoding', 'chunked')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                while True:
                    chunk = upstream.read1(8192)
                    if not chunk:
                        break
                    self.wfile.write(('%X\r\n' % len(chunk)).encode()+chunk+b'\r\n')
                    self.wfile.flush()
                self.wfile.write(b'0\r\n\r\n')
        except urllib.error.HTTPError as e:
            # Provider errors may reflect headers; return a bounded generic error.
            self.send_error(e.code, 'Model provider rejected request')
        except BrokenPipeError:
            pass
        except Exception:
            self.send_error(502, 'Model request failed')

def main():
    global STORE, TOKENS
    os.umask(0o077)
    STORE = Store(DATA / 'data')
    TOKENS = ProxyTokens(DATA / 'data')
    if not STORE.list_all_users() and not config().get('bootstrap_token'):
        save_config({'bootstrap_token': secrets.token_urlsafe(24)})
    host = os.environ.get('AGENT_CLOUD_HOST', '127.0.0.1')
    port = int(os.environ.get('AGENT_CLOUD_PORT', '8080'))
    proxy_host = os.environ.get('AGENT_CLOUD_PROXY_HOST', '127.0.0.1')
    proxy_port = int(os.environ.get('AGENT_CLOUD_PROXY_PORT', '8090'))
    proxy = BoundedHTTPServer((proxy_host, proxy_port), ProxyHandler, max_workers=8, request_timeout=330)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    server = None
    try:
        server = BoundedHTTPServer((host, port), Handler, max_workers=32, request_timeout=30)
        print(json.dumps({'service': 'web', 'host': host, 'port': port}), flush=True)
        server.serve_forever()
    finally:
        if server is not None:
            server.server_close()
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=2)

if __name__ == '__main__':
    main()
