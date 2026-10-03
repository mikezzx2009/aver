"""Conversation directory ownership, safe preparation and real sandbox access."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from store import AGENT_UID, NotFound, Store


class ConversationWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.user = self.store.create_user('workspace-user', 'workspace private password')
        self.uid = self.user['id']
        self.conversation = self.store.create_conversation(self.uid)['id']

    def test_preparation_owns_opened_directories_and_sets_private_modes(self):
        owned = []
        with patch('store.os.geteuid', return_value=0), patch('store.os.fchown') as change:
            change.side_effect = lambda fd, uid, gid: owned.append((os.fstat(fd).st_ino, uid, gid))
            scope = self.store.prepare_conversation_workspace(self.uid, self.conversation)
        expected = {scope.stat().st_ino, scope.parent.stat().st_ino}
        self.assertEqual(expected, {record[0] for record in owned})
        self.assertTrue(all(uid == gid == AGENT_UID for _, uid, gid in owned))
        self.assertEqual(0o700, scope.stat().st_mode & 0o777)
        self.assertEqual(0o700, scope.parent.stat().st_mode & 0o777)
        (scope / 'agent-file.txt').write_text('persistent conversation file')
        self.store.prepare_conversation_workspace(self.uid, self.conversation)
        self.assertEqual('persistent conversation file', (scope / 'agent-file.txt').read_text())

    def test_other_user_or_invalid_conversation_cannot_create_directory(self):
        other = self.store.create_user('other-user', 'another private password')
        for uid, cid in ((other['id'], self.conversation), (self.uid, '../escape'),
                         (self.uid, ''), (self.uid, '0' * 32)):
            with self.assertRaises(NotFound):
                self.store.prepare_conversation_workspace(uid, cid)
        self.assertFalse((self.store.workspace_path(other['id']) / 'conversations').exists())
        self.assertFalse((self.store.workspace_path(self.uid) / 'conversations').exists())

    def test_preparation_rejects_both_scope_ancestors_without_touching_target(self):
        scope = self.store.prepare_conversation_workspace(self.uid, self.conversation)
        outside = self.root / 'outside'
        outside.mkdir(mode=0o755)
        original_mode = outside.stat().st_mode & 0o777
        for entry in (scope, scope.parent):
            if entry == scope:
                entry.rmdir()
            else:
                scope.unlink()
                entry.rmdir()
            entry.symlink_to(outside, target_is_directory=True)
            owned = []
            with patch('store.os.fchown') as change, self.assertRaises(OSError):
                change.side_effect = lambda fd, _uid, _gid: owned.append(os.fstat(fd).st_ino)
                self.store.prepare_conversation_workspace(self.uid, self.conversation)
            self.assertNotIn(outside.stat().st_ino, owned)
            self.assertEqual(original_mode, outside.stat().st_mode & 0o777)
            self.assertFalse((outside / self.conversation).exists())

    def test_owned_conversation_rejects_replaced_user_workspace(self):
        workspace = self.store.workspace_path(self.uid)
        held = workspace.with_name(workspace.name + '-held')
        workspace.rename(held)
        outside = self.root / 'outside'
        outside.mkdir()
        workspace.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.prepare_conversation_workspace(self.uid, self.conversation)
        self.assertFalse((outside / 'conversations').exists())

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0,
                         'Requires Linux root for the real container UID')
    def test_real_uid10001_can_write_fresh_and_repaired_conversation_directories(self):
        scope = self.store.prepare_conversation_workspace(self.uid, self.conversation)
        paths = (scope.parent, scope)

        def write_as_agent(marker):
            descriptors = [os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) for path in paths]
            code = '''
import os, sys
os.setgroups([])
os.setgid(10001)
os.setuid(10001)
for directory in map(int, sys.argv[2:]):
    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    os.write(fd, b'conversation agent data')
    os.close(fd)
'''
            try:
                return subprocess.run([sys.executable, '-I', '-c', code, marker, *map(str, descriptors)],
                                      pass_fds=descriptors, capture_output=True, text=True)
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)

        fresh = write_as_agent('fresh.txt')
        self.assertEqual(0, fresh.returncode, fresh.stderr)
        for path in paths:
            os.chown(path, 0, 0)
        denied = write_as_agent('denied.txt')
        self.assertNotEqual(0, denied.returncode)
        self.store.prepare_conversation_workspace(self.uid, self.conversation)
        repaired = write_as_agent('repaired.txt')
        self.assertEqual(0, repaired.returncode, repaired.stderr)
        self.assertTrue(all(path.stat().st_uid == AGENT_UID for path in paths))


if __name__ == '__main__':
    unittest.main()
