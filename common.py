"""Configuration and short-lived sandbox credentials, never shared with agents."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('AGENT_CLOUD_DATA', ROOT / 'var'))
CONFIG_PATH = Path(os.environ.get('AGENT_CLOUD_CONFIG', DATA / 'config.json'))
DEFAULTS = {'model': 'deepseek-v4-flash', 'api_key': '',
            'api_base': 'https://api.deepseek.com', 'signup_enabled': False,
            # Public origins are deployment settings, never a bundled address.
            # Leave publication unconfigured until the operator enables Nginx.
            'bootstrap_enabled': True, 'domain': '', 'site_base_url': '',
            'task_timeout_seconds': 0, 'max_concurrency': 1,
            'sandbox_image': 'agent-cloud-sandbox:0.1',
            'proxy_url': 'http://172.29.0.1:8090/v1'}

def config():
    result = dict(DEFAULTS)
    try:
        result.update(json.loads(CONFIG_PATH.read_text()))
    except FileNotFoundError:
        pass
    return result

def save_config(changes):
    current = config()
    current.update(changes)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_name(CONFIG_PATH.name + '.' + secrets.token_hex(6))
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(current, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, CONFIG_PATH)
    return current

class ProxyTokens:
    def __init__(self, data_dir=DATA):
        self.path = Path(data_dir) / 'proxy.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS tokens (digest TEXT PRIMARY KEY, task_id TEXT, user_id TEXT, expires REAL)')
        os.chmod(self.path, 0o600)

    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        return c

    def issue(self, task_id, user_id, ttl=3600):
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as c:
            c.execute('DELETE FROM tokens WHERE expires < ?', (time.time(),))
            c.execute('INSERT INTO tokens VALUES (?,?,?,?)', (digest, str(task_id), str(user_id), time.time()+ttl))
        return token

    def lookup(self, token):
        if not token or len(token) > 200:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as c:
            row = c.execute('SELECT * FROM tokens WHERE digest=? AND expires>?', (digest, time.time())).fetchone()
        return dict(row) if row else None

    def revoke(self, task_id):
        with self.connect() as c:
            c.execute('DELETE FROM tokens WHERE task_id=?', (str(task_id),))

    def extend(self, task_id, ttl=3600):
        with self.connect() as c:
            c.execute('UPDATE tokens SET expires=? WHERE task_id=?', (time.time()+ttl, str(task_id)))

    def revoke_all(self):
        with self.connect() as c:
            c.execute('DELETE FROM tokens')
