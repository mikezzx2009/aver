"""Workspace access uses the same layout and UID as production accounts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from store import AGENT_UID, Store, prepare_agent_directory
import worker


class WorkspaceAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.user = self.store.create_user('access-user', 'access test password')
        self.workspace = self.store.workspace_path(self.user['id'])

    def test_new_user_initial_directories_are_owned_through_open_descriptors(self):
        owned = []
        with patch('store.os.geteuid', return_value=0), patch('store.os.fchown') as change:
            change.side_effect = lambda fd, uid, gid: owned.append((os.fstat(fd).st_ino, uid, gid))
            user = self.store.create_user('second-user', 'another test password')
        workspace = self.store.workspace_path(user['id'])
        expected = {path.stat().st_ino for path in (workspace, workspace / 'home', workspace / 'files')}
        self.assertEqual(expected, {record[0] for record in owned})
        self.assertTrue(all(uid == gid == AGENT_UID for _, uid, gid in owned))

    def test_local_preview_does_not_require_root(self):
        with patch('store.os.geteuid', return_value=1000), patch('store.os.fchown') as change:
            self.store.prepare_workspace(self.user['id'])
        change.assert_not_called()
        for path in (self.workspace, self.workspace / 'home', self.workspace / 'files'):
            marker = path / 'local-preview.txt'
            marker.write_text('local writable directory')
            self.assertEqual('local writable directory', marker.read_text())

    def test_existing_child_symlink_is_preserved_and_target_is_never_owned(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.workspace / 'files').rmdir()
        (self.workspace / 'files').symlink_to(outside, target_is_directory=True)
        owned = []
        with patch('store.os.geteuid', return_value=0), patch('store.os.fchown') as change:
            change.side_effect = lambda fd, _uid, _gid: owned.append(os.fstat(fd).st_ino)
            self.store.prepare_workspace(self.user['id'])
        self.assertTrue((self.workspace / 'files').is_symlink())
        self.assertEqual({self.workspace.stat().st_ino, (self.workspace / 'home').stat().st_ino}, set(owned))
        self.assertNotIn(outside.stat().st_ino, owned)
        alias = self.root / 'alias'
        alias.symlink_to(outside, target_is_directory=True)
        with patch('store.os.fchown') as change, self.assertRaises(OSError):
            prepare_agent_directory(alias)
        change.assert_not_called()

    def test_worker_repairs_real_store_layout_before_starting_container(self):
        owned = []
        existing = subprocess.CompletedProcess([], 0, json.dumps([{'HostConfig': {'Runtime': 'runsc-systrap'}}]), '')
        with patch.object(worker, 'DATA', self.root), patch.object(worker, 'STORE', self.store), \
                patch.object(worker, 'docker', return_value=existing) as docker, \
                patch('store.os.geteuid', return_value=0), patch('store.os.fchown') as change:
            change.side_effect = lambda fd, _uid, _gid: owned.append(os.fstat(fd).st_ino)
            name = worker.ensure_sandbox(self.user)
        home = self.root / 'sandboxes' / self.user['id'] / 'home'
        expected = {path.stat().st_ino for path in (home, self.workspace, self.workspace / 'home', self.workspace / 'files')}
        self.assertEqual(expected, set(owned))
        docker.assert_any_call('start', name)

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0,
                         'Requires Linux root to test the real sandbox UID')
    def test_uid10001_can_write_new_and_migrated_store_directories(self):
        paths = (self.workspace, self.workspace / 'home', self.workspace / 'files')

        def check_access(marker):
            descriptors = [os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) for path in paths]
            code = '''
import os, sys
os.setgroups([])
os.setgid(10001)
os.setuid(10001)
for directory in map(int, sys.argv[2:]):
    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    os.write(fd, b'written as the real tenant UID')
    os.close(fd)
'''
            try:
                return subprocess.run([sys.executable, '-I', '-c', code, marker, *map(str, descriptors)],
                                      pass_fds=descriptors, capture_output=True, text=True)
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)

        fresh = check_access('fresh-agent-write')
        self.assertEqual(0, fresh.returncode, fresh.stderr)
        for path in paths:
            self.assertEqual(AGENT_UID, path.stat().st_uid)
        # Reproduce the original account layout: only the mount root was owned
        # by UID10001; files/home remained root-owned with mode0700.
        for path in paths[1:]:
            os.chown(path, 0, 0)
        denied = check_access('legacy-agent-write')
        self.assertNotEqual(0, denied.returncode)
        self.assertIn('PermissionError', denied.stderr)
        self.store.prepare_workspace(self.user['id'])
        repaired = check_access('repaired-agent-write')
        self.assertEqual(0, repaired.returncode, repaired.stderr)
        for path in paths:
            self.assertEqual(b'written as the real tenant UID', (path / 'repaired-agent-write').read_bytes())


if __name__ == '__main__':
    unittest.main()
