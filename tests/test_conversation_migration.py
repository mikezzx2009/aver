import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from store import Store


spec = importlib.util.spec_from_file_location('conversation_migration',
    Path(__file__).resolve().parents[1] / 'deploy' / 'migrate-conversation-files.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class ConversationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / 'data')
        self.user = self.store.create_user('migration', 'safe-migration-password')
        self.conversation = self.store.create_conversation(self.user['id'], 'Existing project')
        self.root = self.store.workspace_path(self.user['id'])
        self.target = self.store.conversation_workspace_path(self.user['id'], self.conversation['id'])

    def test_existing_project_is_classified_without_removing_originals(self):
        (self.root / 'index.html').write_text('<h1>Existing site</h1>')
        (self.root / 'files' / 'attachment.txt').write_text('private attachment')
        internal = self.root / '.ever-publish'
        internal.mkdir()
        (internal / 'task.json').write_text('{}')
        result = migration.migrate(self.store)
        self.assertEqual(result[0]['copied'], 2)
        self.assertEqual((self.target / 'index.html').read_bytes(), (self.root / 'index.html').read_bytes())
        self.assertEqual((self.target / 'files' / 'attachment.txt').read_text(), 'private attachment')
        self.assertFalse((self.target / '.ever-publish').exists())
        self.assertNotEqual((self.target / 'index.html').stat().st_ino, (self.root / 'index.html').stat().st_ino)
        self.assertEqual(migration.migrate(self.store)[0]['copied'], 0)

    def test_existing_classified_file_is_never_overwritten(self):
        (self.root / 'result.txt').write_text('original')
        self.store.prepare_conversation_workspace(self.user['id'], self.conversation['id'])
        (self.target / 'result.txt').write_text('new user edits')
        with self.assertRaisesRegex(RuntimeError, 'different content'):
            migration.migrate(self.store)
        self.assertEqual((self.target / 'result.txt').read_text(), 'new user edits')
        self.assertEqual((self.root / 'result.txt').read_text(), 'original')

    def test_ambiguous_legacy_files_are_preserved_for_manual_classification(self):
        self.store.create_conversation(self.user['id'], 'Second project')
        (self.root / 'unknown.txt').write_text('unclassified')
        result = migration.migrate(self.store)
        self.assertEqual(result[0]['status'], 'manual classification required')
        self.assertFalse(self.target.exists())
        self.assertEqual((self.root / 'unknown.txt').read_text(), 'unclassified')

    def test_source_and_destination_links_cannot_copy_or_overwrite_outside_files(self):
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        marker = outside / 'secret.txt'
        marker.write_text('unchanged')
        (self.root / 'secret.txt').symlink_to(marker)
        with self.assertRaisesRegex(RuntimeError, 'link or special file'):
            migration.migrate(self.store)
        self.assertFalse((self.target / 'secret.txt').exists())
        (self.root / 'secret.txt').unlink()
        (self.root / 'secret.txt').write_text('different')
        (self.target / 'secret.txt').symlink_to(marker)
        with self.assertRaises(OSError):
            migration.migrate(self.store)
        self.assertEqual(marker.read_text(), 'unchanged')

    def test_interrupted_copy_does_not_leave_a_partial_final_file(self):
        (self.root / 'result.txt').write_text('complete original')
        with mock.patch.object(migration.os, 'read', side_effect=[b'partial', OSError('interrupted')]):
            with self.assertRaises(OSError):
                migration.migrate(self.store)
        self.assertFalse((self.target / 'result.txt').exists())
        self.assertFalse(any(p.name.startswith('.aver-migrate-') for p in self.target.iterdir()))
        self.assertEqual((self.root / 'result.txt').read_text(), 'complete original')
        self.assertEqual(migration.migrate(self.store)[0]['copied'], 1)

    def test_concurrent_final_creation_is_retained_at_commit(self):
        (self.root / 'result.txt').write_text('original')
        def concurrent_file(*args, **kwargs):
            (self.target / 'result.txt').write_text('concurrent user change')
            raise FileExistsError('created concurrently')
        with mock.patch.object(migration.os, 'link', side_effect=concurrent_file):
            with self.assertRaises(FileExistsError):
                migration.migrate(self.store)
        self.assertEqual((self.target / 'result.txt').read_text(), 'concurrent user change')
        self.assertFalse(any(p.name.startswith('.aver-migrate-') for p in self.target.iterdir()))


if __name__ == '__main__':
    unittest.main()
