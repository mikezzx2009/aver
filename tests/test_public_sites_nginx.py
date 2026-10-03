"""Static release guard checks; deployment still validates with nginx -t."""
from pathlib import Path
import http.client
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest


TEMPLATE = Path(__file__).resolve().parents[1] / 'deploy' / 'agent-cloud-sites.nginx.conf'
RESERVED = ('api', 'sites', 'static', 'assets', 'private', 'admin', 'settings', 'workspace',
            'workspaces', 'data', 'backups', 'runs', 'sandboxes', 'health', 'login', 'logout',
            'favicon', 'signup', 'status', 'config', 'published')


class PublicSitesNginxTests(unittest.TestCase):
    def test_only_complete_publication_ids_enter_the_published_tree(self):
        template = TEMPLATE.read_text()
        guard = re.search(r'if\s*\(\$uri\s*!~\s*"([^"]+)"\)\s*\{\s*return\s+404;', template)
        self.assertIsNotNone(guard, 'Public alias needs a quoted fail-closed URI guard')
        allowed = re.compile(guard.group(1))
        site_id = '0123456789abcdef' * 2
        for uri in (f'/sites/{site_id}', f'/sites/{site_id}/',
                    f'/sites/{site_id}/index.html', f'/sites/{site_id}/data/chart.json'):
            self.assertIsNotNone(allowed.search(uri), uri)
        for uri in ('/sites/', '/sites/.staging/index.html', '/sites/arbitrary/index.html',
                    f'/sites/{site_id[:-1]}/index.html', f'/sites/{site_id}0/index.html',
                    f'/sites/{site_id.upper()}/index.html', '/api/settings', '/data/config.json'):
            self.assertIsNone(allowed.search(uri), uri)

    def test_public_origin_never_forwards_to_private_services(self):
        # Ignore comments so explanatory prose cannot satisfy a directive check.
        template = '\n'.join(line.split('#', 1)[0] for line in TEMPLATE.read_text().splitlines())
        self.assertNotRegex(template, r'\b(?:proxy_pass|fastcgi_pass|uwsgi_pass|scgi_pass)\b')
        self.assertRegex(template, r'location\s+/\s*\{\s*return\s+404;')
        self.assertRegex(template, r'\bdisable_symlinks\s+on;')
        self.assertRegex(template, r'\bautoindex\s+off;')

    def test_short_alias_uses_named_captures_and_excludes_reserved_routes(self):
        template = TEMPLATE.read_text()
        location = re.search(r'location\s+~\s+"([^"]*\(\?<short_slug>[^\"]*)"\s*\{([^}]*)\}', template)
        self.assertIsNotNone(location)
        # Translate PCRE named groups to Python while retaining its path guard.
        allowed = re.compile(re.sub(r'\(\?<([a-z_]+)>', r'(?P<\1>', location.group(1)))
        for uri in ('/ruler/', '/ruler/index.html', '/ruler/assets/style.css', '/abcd/', '/abc1234567/'):
            matched = allowed.fullmatch(uri)
            self.assertIsNotNone(matched, uri)
            self.assertEqual(uri.split('/')[1], matched.group('short_slug'))
        for uri in ('/ruler', '/abc/', '/abcdefghijk/', '/RUler/', '/ruler-one/',
                    '/sites/'+'a'*32+'/', '/api/status', *('/'+name+'/' for name in RESERVED)):
            self.assertIsNone(allowed.fullmatch(uri), uri)
        body = location.group(2)
        self.assertIn('alias /srv/agent-cloud/published-short/$short_slug/$short_asset;', body)
        self.assertIn('disable_symlinks if_not_owner;', body)
        self.assertIn('autoindex off;', body)
        self.assertNotRegex(body, r'\$[1-9]')

    def test_stable_alias_disables_stale_conditional_responses_and_preserves_headers(self):
        template = TEMPLATE.read_text()
        body = re.search(r'location\s+~\s+"[^\"]*\(\?<short_slug>[^\"]*"\s*\{([^}]*)\}', template).group(1)
        for directive in ('etag off;', 'if_modified_since off;',
                          'add_header Cache-Control no-cache always;',
                          'add_header X-Content-Type-Options nosniff always;',
                          'add_header Referrer-Policy no-referrer always;'):
            self.assertIn(directive, body)
        self.assertIn('return 308 /$short_redirect/$is_args$args;', template)


@unittest.skipUnless(shutil.which('nginx'), 'Actual Nginx binary is required for the alias integration test')
class PublicSitesNginxIntegrationTests(unittest.TestCase):
    """Start an isolated loopback server; never reload the production daemon."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aver-nginx-sites-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o755)
        self.site_id = '0123456789abcdef' * 2
        self.next_id = 'fedcba9876543210' * 2
        for site_id, content in ((self.site_id, '<h1>Version one</h1>'),
                                 (self.next_id, '<h1>Version two</h1>')):
            site = self.root / 'published' / site_id
            (site / 'assets').mkdir(parents=True)
            for path in (site.parent, site, site / 'assets'):
                path.chmod(0o755)
            for name, text in (('index.html', content), ('assets/style.css', 'body{color:red}'),
                               ('.env', 'private env sentinel'), ('server.py', 'private source sentinel')):
                path = site / name
                path.write_text(text)
                path.chmod(0o644)
        self.aliases = self.root / 'published-short'
        self.aliases.mkdir(mode=0o755)
        self.aliases.chmod(0o755)
        (self.aliases / 'ruler').symlink_to('../published/'+self.site_id, target_is_directory=True)
        # Even existing aliases cannot make service routes public.
        for name in RESERVED:
            (self.aliases / name).symlink_to('../published/'+self.site_id, target_is_directory=True)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            self.port = reservation.getsockname()[1]
        server = TEMPLATE.read_text().replace('listen 80;', f'listen 127.0.0.1:{self.port};')
        server = server.replace('listen [::]:80;', '').replace('/srv/agent-cloud/', str(self.root)+'/')
        config = self.root / 'nginx.conf'
        config.write_text('worker_processes 1;\npid '+str(self.root / 'nginx.pid')+';\n'
                          'error_log stderr warn;\nevents { worker_connections 32; }\n'
                          'http { access_log off; default_type application/octet-stream;\n'
                          'types { text/html html; text/css css; }\n'+server+'\n}\n')
        executable = shutil.which('nginx')
        checked = subprocess.run([executable, '-p', str(self.root)+'/', '-c', str(config), '-t'],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(0, checked.returncode, checked.stderr)
        self.process = subprocess.Popen([executable, '-p', str(self.root)+'/', '-c', str(config),
                                         '-g', 'daemon off; master_process off;'],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_nginx)
        deadline = time.monotonic() + 5
        while True:
            try:
                self.request('/')
                break
            except OSError:
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    self.stop_nginx()
                    self.fail('Isolated Nginx did not start; inspect its disposable configuration')
                time.sleep(0.02)

    def stop_nginx(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=5)

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            connection.request('GET', path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    def test_real_nginx_serves_short_index_assets_redirect_updates_and_legacy_ids(self):
        for path, expected in (('/ruler/', b'<h1>Version one</h1>'),
                               ('/ruler/index.html', b'<h1>Version one</h1>'),
                               ('/ruler/assets/style.css', b'body{color:red}'),
                               ('/sites/'+self.site_id+'/', b'<h1>Version one</h1>')):
            status, headers, body = self.request(path)
            self.assertEqual(200, status, (path, body))
            self.assertEqual(expected, body)
            self.assertEqual('nosniff', headers.get('X-Content-Type-Options'))
            self.assertEqual('no-referrer', headers.get('Referrer-Policy'))
            if path.startswith('/ruler/'):
                self.assertEqual('no-cache', headers.get('Cache-Control'))
                self.assertNotIn('ETag', headers)
        status, headers, _ = self.request('/ruler?view=1')
        self.assertEqual(308, status)
        self.assertTrue(headers['Location'].endswith('/ruler/?view=1'), headers['Location'])

        for path in ('/', '/api/status', '/ruler/.env', '/ruler/%2eenv', '/ruler/server.py',
                     '/ruler/../api/status', '/abc/', '/abcdefghijkl/', '/RUler/', '/ruler-one/',
                     '/notthere/', *('/'+name+'/' for name in RESERVED)):
            status, _, body = self.request(path)
            self.assertEqual(404, status, (path, body))
            self.assertNotIn(b'private ', body)
        self.assertIn(self.request('/ruler/assets/')[0], (403, 404))

        # A same-second, same-length release must not return 304 or stale HTML.
        original = self.root / 'published' / self.site_id / 'index.html'
        replacement = self.root / 'published' / self.next_id / 'index.html'
        os.utime(replacement, ns=(original.stat().st_atime_ns, original.stat().st_mtime_ns))
        next_link = self.aliases / '.next-ruler'
        next_link.symlink_to('../published/'+self.next_id, target_is_directory=True)
        os.replace(next_link, self.aliases / 'ruler')
        status, _, body = self.request('/ruler/', {'If-Modified-Since': 'Thu, 31 Dec 2099 23:59:59 GMT'})
        self.assertEqual(200, status)
        self.assertEqual(b'<h1>Version two</h1>', body)
        self.assertEqual(b'<h1>Version one</h1>', self.request('/sites/'+self.site_id+'/')[2])

        # Legacy snapshots retain strict no-symlink access, even for same-owner links.
        legacy_link = original.parent / 'assets' / 'linked.css'
        legacy_link.symlink_to('style.css')
        self.assertIn(self.request('/sites/'+self.site_id+'/assets/linked.css')[0], (403, 404))
        if os.geteuid() == 0:
            unsafe = self.aliases / 'unsafe'
            unsafe.symlink_to('../published/'+self.site_id, target_is_directory=True)
            os.chown(unsafe, 10001, 10001, follow_symlinks=False)
            self.assertIn(self.request('/unsafe/')[0], (403, 404))


if __name__ == '__main__':
    unittest.main()
