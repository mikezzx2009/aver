import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / 'deploy' / 'update-web.py'
spec = importlib.util.spec_from_file_location('agent_cloud_update_web', SCRIPT)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class UpdateWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stage, self.app, self.data = (self.root / name for name in ('stage', 'app', 'data'))
        self.backups = self.root / 'backups'
        for path in (self.stage, self.app, self.data / 'data', self.backups):
            path.mkdir(parents=True)
        for name in updater.REQUIRED + updater.WORKER_SOURCES:
            for directory, value in ((self.stage, 'new'), (self.app, 'old')):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                content = f"VERSION = {value!r}\n" if path.suffix == '.py' else value + '\n'
                path.write_text(content)
        for directory in (self.app, self.stage):
            (directory / 'common.py').write_text('VALUE = "do not publish"\n')
        self.database = self.data / 'data' / 'agent-cloud.sqlite3'
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE tasks (status TEXT NOT NULL)')
        self.worker_active = True
        self.commands = []

    def state(self, unit):
        active = unit == updater.WEB or self.worker_active
        return {'active': 'active' if active else 'inactive', 'pid': 123 if active else 0}

    def systemctl(self, *arguments, **options):
        self.commands.append(arguments)
        if arguments == ('stop', updater.WORKER):
            self.worker_active = False
        elif arguments == ('start', updater.WORKER):
            self.worker_active = True
        return mock.Mock(returncode=0, stderr='', stdout='')

    def run_update(self, include_worker=True, health=None):
        argv = ['update-web.py', '--stage', str(self.stage), '--app-dir', str(self.app),
                '--data-dir', str(self.data)]
        if include_worker:
            argv.append('--include-worker')
        output = io.StringIO()
        with mock.patch.object(updater.sys, 'argv', argv), \
                mock.patch.object(updater.os, 'geteuid', return_value=0), \
                mock.patch.object(updater.os, 'umask'), \
                mock.patch.object(updater, 'BACKUP_ROOT', self.backups), \
                mock.patch.object(updater, 'service_state', side_effect=self.state), \
                mock.patch.object(updater, 'systemctl', side_effect=self.systemctl), \
                mock.patch.object(updater, 'wait_health', side_effect=health), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = updater.main()
        return result, output.getvalue()

    def test_worker_sources_are_explicitly_opt_in_and_required_when_opted_in(self):
        default = updater.release_files(self.stage, self.app)
        self.assertTrue(set(updater.WORKER_SOURCES).isdisjoint(default))
        opted_in = updater.release_files(self.stage, self.app, include_worker=True)
        self.assertTrue(set(updater.WORKER_SOURCES).issubset(opted_in))
        self.assertNotIn('common.py', opted_in)
        (self.stage / 'worker.py').unlink()
        updater.release_files(self.stage, self.app)
        with self.assertRaisesRegex(updater.UpdateError, 'worker.py'):
            updater.release_files(self.stage, self.app, include_worker=True)

    def test_publisher_dependency_is_required_and_nginx_template_is_source_only(self):
        template = self.stage / 'deploy' / 'agent-cloud-sites.nginx.conf'
        template.parent.mkdir(exist_ok=True)
        template.write_text('server { listen 80; }\n')
        names = updater.release_files(self.stage, self.app)
        self.assertIn('publisher.py', names)
        self.assertIn('deploy/agent-cloud-sites.nginx.conf', names)
        result, output = self.run_update(include_worker=False)
        self.assertEqual(result, 0)
        self.assertEqual((self.app / 'deploy' / template.name).read_text(), template.read_text())
        self.assertEqual(self.commands, [('stop', updater.WORKER), ('restart', updater.WEB),
                                         ('start', updater.WORKER)])
        # Copying this optional template never runs Nginx or alters its enabled sites.
        self.assertIn('deploy/agent-cloud-sites.nginx.conf', json.loads(output)['updated'])
        (self.stage / 'publisher.py').unlink()
        with self.assertRaisesRegex(updater.UpdateError, 'publisher.py'):
            updater.release_files(self.stage, self.app)

    def test_binary_appledouble_test_metadata_is_ignored(self):
        tests = self.stage / 'tests'
        tests.mkdir()
        (tests / 'test_real.py').write_text('VALUE = 1\n')
        (tests / '._test_real.py').write_bytes(b'\x00\x05\x16\x07\xffmetadata')
        hidden = tests / '._metadata'
        hidden.mkdir()
        (hidden / 'invalid.py').write_bytes(b'\xffinvalid')
        names = updater.release_files(self.stage, self.app, include_worker=True)
        self.assertIn('tests/test_real.py', names)
        self.assertFalse(any(Path(name).name.startswith('._') for name in names))
        self.assertNotIn('tests/._metadata/invalid.py', names)

    def test_worker_release_copies_only_declared_sources_and_resumes_idle_worker(self):
        untouched = self.app / 'tenant-runtime-state'
        untouched.write_text('background process state')
        result, output = self.run_update()
        self.assertEqual(result, 0)
        report = json.loads(output)
        self.assertTrue(set(updater.WORKER_SOURCES).issubset(report['updated']))
        self.assertEqual(self.commands, [('stop', updater.WORKER), ('restart', updater.WEB),
                                         ('start', updater.WORKER)])
        self.assertEqual((self.app / 'worker.py').read_text(), "VERSION = 'new'\n")
        self.assertEqual(untouched.read_text(), 'background process state')
        self.assertEqual((self.app / 'common.py').read_text(), 'VALUE = "do not publish"\n')
        manifest = json.loads((Path(report['code_backup']) / 'update-manifest.json').read_text())
        self.assertTrue(manifest['include_worker'])
        self.assertEqual((Path(report['code_backup']) / 'worker.py').read_text(), "VERSION = 'old'\n")

    def test_running_task_blocks_all_source_and_service_mutations(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO tasks VALUES ('running')")
        result, _ = self.run_update()
        self.assertEqual(result, 75)
        self.assertEqual(self.commands, [])
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertTrue(self.worker_active)
        self.assertEqual((self.app / 'store.py').read_text(), "VERSION = 'old'\n")

    def test_unhealthy_web_rolls_back_worker_sources_before_resuming_worker(self):
        result, _ = self.run_update(health=[updater.UpdateError('unhealthy'), None])
        self.assertEqual(result, 1)
        for name in updater.REQUIRED + updater.WORKER_SOURCES:
            expected = "VERSION = 'old'\n" if Path(name).suffix == '.py' else 'old\n'
            self.assertEqual((self.app / name).read_text(), expected, name)
        self.assertTrue(self.worker_active)
        self.assertEqual(self.commands, [('stop', updater.WORKER), ('restart', updater.WEB),
                                         ('restart', updater.WEB), ('start', updater.WORKER)])


if __name__ == '__main__':
    unittest.main()
