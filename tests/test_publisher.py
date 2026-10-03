"""Public snapshots are explicit, isolated, bounded, and filesystem-safe."""
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import publisher
from publisher import PublicationError, list_publications, list_sites, promote_existing, publish_requested
from store import Store


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.store = Store(self.data / 'data')
        self.user = self.store.create_user('publish-user', 'publication test password')
        self.peer = self.store.create_user('publish-peer', 'publication peer password')
        self.conversation = self.store.create_conversation(self.user['id'])
        self.workspace = self.store.workspace_path(self.user['id'])
        self.site = self.workspace / 'public' / 'example'
        self.site.mkdir(parents=True)
        (self.site / 'index.html').write_text('<html>Public example</html>')
        self.task = self.store.submit_task(self.user['id'], self.conversation['id'], 'Build this website', 'publish-task')
        self.task = self.store.claim_task('publisher-worker', lease_seconds=300)
        self.manifest_dir = self.workspace / '.ever-publish'
        self.manifest_dir.mkdir()
        self.write_manifest()
        data_patch = patch.object(publisher, 'DATA', self.data)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        config_patch = patch.object(publisher, 'config', return_value={'site_base_url': 'http://203.0.113.10'})
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def write_manifest(self, **changes):
        manifest = {'version': 1, 'task_id': self.task['id'], 'directory': 'public/example', 'title': 'Example'}
        manifest.update(changes)
        (self.manifest_dir / (self.task['id'] + '.json')).write_text(json.dumps(manifest))

    def publish(self, **options):
        return publish_requested(self.store, self.task, **options)

    def published_directories(self):
        root = self.data / 'published'
        return list(root.iterdir()) if root.exists() else []

    def next_task(self, *, conversation=None, user=None):
        previous = self.store.get_task(self.task['user_id'], self.task['id'])
        if previous['status'] == 'running':
            self.store.finish_task(previous['id'], previous['worker_id'])
        user = user or self.user
        conversation = conversation or self.conversation
        task = self.store.submit_task(user['id'], conversation['id'], 'Update website', uuid.uuid4().hex)
        self.task = self.store.claim_task('publisher-worker', lease_seconds=300)
        workspace = self.store.workspace_path(user['id'])
        self.manifest_dir = workspace / '.ever-publish'
        self.manifest_dir.mkdir(exist_ok=True)
        return task

    def legacy_snapshot(self, *, directory='public/example', body='Legacy website', with_conversation=False):
        snapshot = uuid.uuid4().hex
        root = self.data / 'published' / snapshot
        root.mkdir(parents=True, mode=0o755)
        (root / 'index.html').write_text(body)
        records = self.data / 'publication-records'
        records.mkdir(exist_ok=True, mode=0o700)
        record = {'id': snapshot, 'url': 'http://203.0.113.10/sites/' + snapshot + '/',
                  'title': 'Legacy', 'directory': directory, 'file_count': 1,
                  'total_bytes': len(body.encode()), 'user_id': self.task['user_id'],
                  'task_id': self.task['id'], 'public_base': 'http://203.0.113.10', 'created_at': time.time()}
        if with_conversation:
            record['conversation_id'] = self.task['conversation_id']
        (records / (snapshot + '.json')).write_text(json.dumps(record))
        (records / ('task-' + self.task['id'] + '.json')).write_text(json.dumps({
            'id': snapshot, 'user_id': self.task['user_id'], 'task_id': self.task['id']}))
        return record

    def test_publishes_snapshot_with_public_url_and_private_metadata(self):
        assets = self.site / 'assets'
        assets.mkdir()
        (assets / 'site.css').write_text('body{color:navy}')
        (assets / 'image.bin').write_bytes(bytes(range(256)))
        result = self.publish()
        self.assertRegex(result['id'], '^[0-9a-f]{32}$')
        self.assertRegex(result['site_id'], '^[a-z0-9]{6}$')
        self.assertEqual('http://203.0.113.10/' + result['site_id'] + '/', result['url'])
        self.assertNotIn(self.user['id'], result['url'])
        self.assertNotIn('user_id', result)
        self.assertEqual(3, result['file_count'])
        destination = self.data / 'published' / result['id']
        self.assertEqual((self.site / 'index.html').read_bytes(), (destination / 'index.html').read_bytes())
        self.assertEqual(bytes(range(256)), (destination / 'assets' / 'image.bin').read_bytes())
        self.assertEqual(0o755, stat.S_IMODE(destination.stat().st_mode))
        self.assertEqual(0o644, stat.S_IMODE((destination / 'index.html').stat().st_mode))
        records = self.data / 'publication-records'
        self.assertEqual(0o700, stat.S_IMODE(records.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE((records / (result['id'] + '.json')).stat().st_mode))
        self.assertFalse(any(path.name.endswith('.json') for path in destination.iterdir()))
        alias = self.data / 'published-short' / result['site_id']
        self.assertEqual('../published/' + result['id'], os.readlink(alias))
        self.assertEqual((destination / 'index.html').read_bytes(), (alias / 'index.html').read_bytes())
        self.assertEqual([], list_publications(self.data, self.peer['id']))
        own = list_publications(self.data, self.user['id'], [self.task['id']])
        self.assertEqual(result['url'], own[0]['url'])
        self.assertEqual(self.task['id'], own[0]['task_id'])
        self.assertEqual([], list_publications(self.data, self.user['id'], ['f' * 32]))

    def test_absent_manifest_and_other_task_manifest_do_not_publish(self):
        (self.manifest_dir / (self.task['id'] + '.json')).rename(self.manifest_dir / ('a' * 32 + '.json'))
        self.assertIsNone(self.publish())
        self.assertEqual([], self.published_directories())

    def test_unconfigured_origin_never_invents_a_public_address(self):
        with patch.object(publisher, 'config', return_value={}):
            with self.assertRaisesRegex(PublicationError, '尚未配置网站发布地址'):
                self.publish()
            self.assertEqual([], self.published_directories())
            (self.manifest_dir / (self.task['id'] + '.json')).unlink()
            self.assertIsNone(self.publish())

    def test_explicit_origin_overrides_unconfigured_deployment(self):
        with patch.object(publisher, 'config', return_value={}):
            result = self.publish(public_base='https://projects.example.com')
        self.assertEqual('https://projects.example.com/' + result['site_id'] + '/', result['url'])

    def test_current_conversation_site_is_published_and_bound_in_metadata(self):
        scoped = self.workspace / 'conversations' / self.conversation['id'] / 'public' / 'example'
        scoped.mkdir(parents=True)
        (scoped / 'index.html').write_text('<h1>Current conversation</h1>')
        (scoped / 'assets.txt').write_text('a conversation asset')
        self.write_manifest(directory='conversations/' + self.conversation['id'] + '/public/example')
        result = self.publish()
        destination = self.data / 'published' / result['id']
        self.assertEqual('<h1>Current conversation</h1>', (destination / 'index.html').read_text())
        self.assertEqual(2, result['file_count'])
        record_path = self.data / 'publication-records' / (result['id'] + '.json')
        record = json.loads(record_path.read_text())
        self.assertEqual(self.conversation['id'], record['conversation_id'])
        self.assertEqual(result['id'], list_publications(self.data, self.user['id'])[0]['id'])
        self.assertEqual(result, self.publish())

    def test_another_conversation_directory_cannot_be_published(self):
        other = self.store.create_conversation(self.user['id'])
        scoped = self.workspace / 'conversations' / other['id'] / 'public' / 'example'
        scoped.mkdir(parents=True)
        (scoped / 'index.html').write_text('another conversation website')
        self.write_manifest(directory='conversations/' + other['id'] + '/public/example')
        with self.assertRaisesRegex(PublicationError, '不属于当前任务的对话'):
            self.publish()
        self.assertEqual([], self.published_directories())

    def test_scoped_directory_requires_exact_public_path(self):
        for directory in ('conversations/public/example', 'conversations/not-an-id/public/example',
                          'conversations/' + self.conversation['id'] + '/files/example',
                          'conversations/' + self.conversation['id'] + '/public',
                          'conversations/' + self.conversation['id'] + '/public/../files'):
            with self.subTest(directory=directory):
                self.write_manifest(directory=directory)
                with self.assertRaises(PublicationError):
                    self.publish()
        self.assertEqual([], self.published_directories())

    def test_conversation_component_symlink_cannot_escape(self):
        outside = self.data / 'outside-conversation'
        site = outside / 'public' / 'example'
        site.mkdir(parents=True)
        (site / 'index.html').write_text('outside conversation')
        conversations = self.workspace / 'conversations'
        conversations.mkdir()
        (conversations / self.conversation['id']).symlink_to(outside, target_is_directory=True)
        self.write_manifest(directory='conversations/' + self.conversation['id'] + '/public/example')
        with self.assertRaises(PublicationError):
            self.publish()
        self.assertEqual([], self.published_directories())

    def test_conversation_record_mismatch_cannot_reuse_existing_publication(self):
        result = self.publish()
        path = self.data / 'publication-records' / (result['id'] + '.json')
        record = json.loads(path.read_text())
        record['conversation_id'] = 'f' * 32
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(PublicationError, '发布记录校验失败'):
            self.publish()

    def test_legacy_record_without_conversation_id_remains_compatible(self):
        record = self.legacy_snapshot()
        result = self.publish()
        self.assertEqual(record['url'], result['url'])
        self.assertNotIn('site_id', result)
        self.assertEqual(record['id'], list_publications(self.data, self.user['id'])[0]['id'])

    def test_manifest_must_name_exact_task_and_version(self):
        for changes in ({'task_id': 'a' * 32}, {'version': True}, {'version': 2}):
            with self.subTest(changes=changes):
                self.write_manifest(**changes)
                with self.assertRaises(PublicationError):
                    self.publish()
        self.assertEqual([], self.published_directories())

    def test_directory_traversal_hidden_and_absolute_paths_are_rejected(self):
        for directory in ('../secret', '/etc', 'public/../home', 'public/.secret', 'public//example',
                          'public', 'public/example/', 'public\\example', 'public/example\x00'):
            with self.subTest(directory=directory):
                self.write_manifest(directory=directory)
                with self.assertRaises(PublicationError):
                    self.publish()
        self.assertEqual([], self.published_directories())

    def test_invalid_and_large_manifest_are_rejected_without_private_path(self):
        path = self.manifest_dir / (self.task['id'] + '.json')
        for content in ('{bad json', '[]', 'x' * (publisher.MAX_MANIFEST_BYTES + 1)):
            path.write_text(content)
            with self.assertRaises(PublicationError) as failure:
                self.publish()
            self.assertNotIn(str(self.workspace), str(failure.exception))
            self.assertLess(len(str(failure.exception)), 160)

    def test_title_must_be_text_and_is_bounded(self):
        for title in (None, False, [], 42):
            with self.subTest(title=title):
                self.write_manifest(title=title)
                with self.assertRaises(PublicationError):
                    self.publish()
        self.write_manifest(title='Example ' + 'x' * 500)
        self.assertEqual(160, len(self.publish()['title']))

    def test_hidden_files_and_directories_are_not_published(self):
        (self.site / '.env').write_text('DO_NOT_PUBLISH=private')
        hidden = self.site / '.git'
        hidden.mkdir()
        (hidden / 'secret').write_text('hidden object')
        (hidden / 'link').symlink_to('/etc/passwd')
        result = self.publish()
        root = self.data / 'published' / result['id']
        self.assertEqual({'index.html'}, {entry.name for entry in root.iterdir()})
        self.assertEqual(1, result['file_count'])

    def test_symlinks_and_path_component_symlinks_fail_without_following(self):
        outside = self.data / 'private-file'
        outside.write_text('private outside contents')
        link = self.site / 'secret.txt'
        link.symlink_to(outside)
        with self.assertRaises(PublicationError):
            self.publish()
        self.assertEqual([], self.published_directories())
        link.unlink()
        outside_dir = self.data / 'private-directory'
        outside_dir.mkdir()
        (outside_dir / 'index.html').write_text('private outside site')
        self.site.rename(self.site.with_name('saved'))
        self.site.symlink_to(outside_dir, target_is_directory=True)
        with self.assertRaises(PublicationError):
            self.publish()
        self.assertEqual([], self.published_directories())

    def test_manifest_links_and_linked_workspace_cannot_escape(self):
        manifest = self.manifest_dir / (self.task['id'] + '.json')
        original = manifest.read_text()
        manifest.unlink()
        outside = self.data / 'outside-manifest'
        outside.write_text(original)
        manifest.symlink_to(outside)
        with self.assertRaises(PublicationError):
            self.publish()
        manifest.unlink()
        manifest.write_text(original)
        self.workspace.rename(self.workspace.with_name('saved-workspace'))
        self.workspace.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(PublicationError):
            self.publish()
        self.assertEqual([], self.published_directories())

    def test_hardlinks_are_rejected(self):
        outside = self.data / 'outside-hardlink'
        outside.write_text('a private file with two names')
        os.link(outside, self.site / 'linked.txt')
        with self.assertRaisesRegex(PublicationError, '硬链接'):
            self.publish()
        self.assertEqual([], self.published_directories())

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'Requires a POSIX FIFO')
    def test_special_files_fail_without_blocking(self):
        os.mkfifo(self.site / 'pipe')
        with self.assertRaisesRegex(PublicationError, '普通文件'):
            self.publish()
        self.assertEqual([], self.published_directories())

    def test_index_is_required(self):
        (self.site / 'index.html').rename(self.site / 'other.html')
        with self.assertRaises(PublicationError):
            self.publish()
        self.assertEqual([], self.published_directories())

    def test_single_file_total_and_count_limits_clean_staging(self):
        (self.site / 'one.txt').write_text('ten bytes!')
        for name, bound in (('MAX_FILE_BYTES', 8), ('MAX_TOTAL_BYTES', 30), ('MAX_FILES', 1), ('MAX_ENTRIES', 1)):
            with self.subTest(limit=name), patch.object(publisher, name, bound):
                with self.assertRaises(PublicationError):
                    self.publish()
            self.assertEqual([], self.published_directories())

    def test_inode_descriptor_is_pinned_across_directory_swap(self):
        assets = self.site / 'assets'
        assets.mkdir()
        (assets / 'own.txt').write_text('legitimate snapshot')
        outside = self.data / 'outside-race'
        outside.mkdir()
        (outside / 'secret.txt').write_text('never copy this')
        original_open = os.open
        swapped = False

        def swapping_open(path, flags, *args, **kwargs):
            nonlocal swapped
            fd = original_open(path, flags, *args, **kwargs)
            if path == 'assets' and not swapped and kwargs.get('dir_fd') is not None:
                swapped = True
                assets.rename(assets.with_name('saved-assets'))
                assets.symlink_to(outside, target_is_directory=True)
            return fd

        with patch.object(publisher.os, 'open', side_effect=swapping_open):
            result = self.publish()
        self.assertTrue(swapped)
        copied = self.data / 'published' / result['id'] / 'assets'
        self.assertEqual('legitimate snapshot', (copied / 'own.txt').read_text())
        self.assertFalse((copied / 'secret.txt').exists())

    def test_stale_worker_cancelled_and_expired_task_do_not_publish(self):
        stale = {**self.task, 'worker_id': 'obsolete-worker'}
        with self.assertRaisesRegex(PublicationError, '执行权'):
            publish_requested(self.store, stale)
        with self.store._transaction() as connection:
            connection.execute('UPDATE tasks SET lease_expires_at=0 WHERE id=?', (self.task['id'],))
        with self.assertRaisesRegex(PublicationError, '执行权'):
            self.publish()
        with self.store._transaction() as connection:
            connection.execute('UPDATE tasks SET lease_expires_at=9999999999,cancel_requested=1 WHERE id=?', (self.task['id'],))
        with self.assertRaisesRegex(PublicationError, '执行权'):
            self.publish()
        self.assertEqual([], self.published_directories())

    def test_completed_task_requires_explicit_operator_option(self):
        completed = self.store.finish_task(self.task['id'], self.task['worker_id'])
        with self.assertRaises(PublicationError):
            publish_requested(self.store, completed)
        result = publish_requested(self.store, completed, allow_completed=True)
        self.assertRegex(result['url'], r'^http://203\.0\.113\.10/[a-z0-9]{6}/$')

    def test_owner_and_conversation_must_match_store(self):
        for task in ({**self.task, 'user_id': self.peer['id']},
                     {**self.task, 'conversation_id': 'a' * 32},
                     {**self.task, 'id': '../file'}):
            with self.assertRaises(PublicationError):
                publish_requested(self.store, task)
        self.assertEqual([], self.published_directories())

    def test_task_cancellation_during_copy_is_fenced_before_commit(self):
        copy = publisher._copy_tree

        def cancel_after_copy(*args, **kwargs):
            copy(*args, **kwargs)
            self.store.cancel_task(self.user['id'], self.task['id'])

        with patch.object(publisher, '_copy_tree', side_effect=cancel_after_copy):
            with self.assertRaises(PublicationError):
                self.publish()
        self.assertEqual([], self.published_directories())
        self.assertEqual([], list_publications(self.data, self.user['id']))

    def test_idempotence_keeps_original_snapshot_after_source_changes(self):
        first = self.publish()
        (self.site / 'index.html').write_text('changed after publication')
        second = self.publish()
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.published_directories()))
        self.assertIn('Public example', (self.data / 'published' / first['id'] / 'index.html').read_text())

    def test_interrupted_post_rename_commit_recovers_same_snapshot(self):
        sync = publisher.os.fsync

        def fail_public_sync(fd):
            if os.fstat(fd).st_ino == (self.data / 'published').stat().st_ino:
                raise OSError('simulated process interruption after rename')
            return sync(fd)

        with patch.object(publisher.os, 'fsync', side_effect=fail_public_sync):
            with self.assertRaises(PublicationError):
                self.publish()
        first = json.loads((self.data / 'publication-records' / ('pending-task-' + self.task['id'] + '.json')).read_text())
        self.assertEqual([], list_publications(self.data, self.user['id']))
        self.assertFalse((self.data / 'publication-records' / ('task-' + self.task['id'] + '.json')).exists())
        recovered = self.publish()
        self.assertEqual(first['id'], recovered['id'])
        self.assertEqual(1, len(self.published_directories()))

    def test_interrupted_pre_rename_commit_stays_private_and_retries(self):
        with patch.object(publisher.os, 'rename', side_effect=OSError('simulated rename failure')):
            with self.assertRaises(PublicationError):
                self.publish()
        self.assertEqual([], self.published_directories())
        self.assertEqual([], list_publications(self.data, self.user['id']))
        recovered = self.publish()
        self.assertEqual(1, len(self.published_directories()))
        self.assertEqual(recovered['id'], list_publications(self.data, self.user['id'])[0]['id'])

    def test_private_record_corruption_cannot_expose_another_owner(self):
        result = self.publish()
        record = self.data / 'publication-records' / (result['id'] + '.json')
        body = json.loads(record.read_text())
        body['user_id'] = self.peer['id']
        record.write_text(json.dumps(body))
        self.assertEqual([], list_publications(self.data, self.user['id']))
        with self.assertRaises(PublicationError):
            self.publish()

    def test_two_tasks_keep_one_short_url_and_old_snapshot(self):
        first_task = dict(self.task)
        first = self.publish()
        self.next_task()
        (self.site / 'index.html').write_text('Updated website')
        self.write_manifest()
        second = self.publish()
        self.assertEqual(first['site_id'], second['site_id'])
        self.assertEqual(first['url'], second['url'])
        self.assertNotEqual(first['id'], second['id'])
        alias = self.data / 'published-short' / first['site_id']
        self.assertEqual('Updated website', (alias / 'index.html').read_text())
        self.assertEqual('<html>Public example</html>', (self.data / 'published' / first['id'] / 'index.html').read_text())
        retried = publish_requested(self.store, first_task, allow_completed=True)
        self.assertEqual(first, retried)
        self.assertEqual('../published/' + second['id'], os.readlink(alias))
        sites = list_sites(self.data, self.user['id'], self.conversation['id'])
        self.assertEqual([second], sites)
        self.assertEqual([], list_sites(self.data, self.peer['id'], self.conversation['id']))

    def test_legacy_and_conversation_project_paths_share_the_fixed_url(self):
        first = self.publish()
        self.next_task()
        directory = 'conversations/' + self.conversation['id'] + '/public/example'
        site = self.workspace / directory
        site.mkdir(parents=True)
        (site / 'index.html').write_text('Scoped update')
        self.write_manifest(directory=directory)
        second = self.publish()
        self.assertEqual(first['url'], second['url'])
        self.assertEqual('Scoped update', (self.data / 'published-short' / second['site_id'] / 'index.html').read_text())

    def test_different_projects_conversations_and_users_get_distinct_urls(self):
        first = self.publish()
        urls = [first['url']]
        self.next_task()
        sibling = self.workspace / 'public' / 'other'
        sibling.mkdir()
        (sibling / 'index.html').write_text('Another project')
        self.write_manifest(directory='public/other')
        urls.append(self.publish()['url'])
        another_conversation = self.store.create_conversation(self.user['id'])
        self.next_task(conversation=another_conversation)
        self.write_manifest()
        urls.append(self.publish()['url'])
        peer_conversation = self.store.create_conversation(self.peer['id'])
        self.next_task(user=self.peer, conversation=peer_conversation)
        peer_site = self.store.workspace_path(self.peer['id']) / 'public' / 'example'
        peer_site.mkdir(parents=True)
        (peer_site / 'index.html').write_text('Another user')
        self.write_manifest()
        urls.append(self.publish()['url'])
        self.assertEqual(4, len(set(urls)))
        self.assertEqual(2, len(list_sites(self.data, self.user['id'], self.conversation['id'])))
        self.assertEqual(1, len(list_sites(self.data, self.user['id'], another_conversation['id'])))

    def test_requested_id_reuses_owned_site_when_project_directory_changes(self):
        first = self.publish()
        self.next_task()
        renamed = self.workspace / 'public' / 'renamed'
        renamed.mkdir()
        (renamed / 'index.html').write_text('Moved project')
        self.write_manifest(directory='public/renamed', site_id=first['site_id'])
        second = self.publish()
        self.assertEqual(first['url'], second['url'])
        self.assertEqual('public/renamed', list_sites(self.data, self.user['id'], self.conversation['id'])[0]['directory'])

    def test_requested_id_rejects_other_user_or_conversation(self):
        first = self.publish()
        other = self.store.create_conversation(self.user['id'])
        self.next_task(conversation=other)
        self.write_manifest(site_id=first['site_id'])
        with self.assertRaisesRegex(PublicationError, '不属于当前用户和对话'):
            self.publish()
        self.store.finish_task(self.task['id'], self.task['worker_id'])
        peer_conversation = self.store.create_conversation(self.peer['id'])
        self.next_task(user=self.peer, conversation=peer_conversation)
        site = self.store.workspace_path(self.peer['id']) / 'public' / 'example'
        site.mkdir(parents=True)
        (site / 'index.html').write_text('Peer site')
        self.write_manifest(site_id=first['site_id'])
        with self.assertRaisesRegex(PublicationError, '不属于当前用户和对话'):
            self.publish()
        self.assertEqual('../published/' + first['id'], os.readlink(self.data / 'published-short' / first['site_id']))

    def test_slug_collision_retries_and_reserved_names_are_rejected(self):
        self.write_manifest(site_id='aaaaaa')
        self.publish()
        self.next_task()
        site = self.workspace / 'public' / 'new'
        site.mkdir()
        (site / 'index.html').write_text('New project')
        for slug in ('abc', 'toolongname1', 'UPPER6', '../foo', 'assets', 'settings', 'published'):
            self.write_manifest(directory='public/new', site_id=slug)
            with self.assertRaises(PublicationError):
                self.publish()
        self.write_manifest(directory='public/new')
        with patch.object(publisher, '_new_slug', side_effect=['aaaaaa', 'assets', 'bbbbbb']):
            second = self.publish()
        self.assertEqual('bbbbbb', second['site_id'])

    def test_recovery_after_alias_swap_keeps_slug_and_commits_task_map_last(self):
        write = publisher._write_json

        def fail_site_activation(directory_fd, name, value):
            if name.endswith('.json') and value.get('current_snapshot') and value.get('revision', 0) > 0:
                raise OSError('simulated interruption after live alias swap')
            return write(directory_fd, name, value)

        with patch.object(publisher, '_write_json', side_effect=fail_site_activation):
            with self.assertRaises(PublicationError):
                self.publish()
        records = self.data / 'publication-records'
        pending = json.loads((records / ('pending-task-' + self.task['id'] + '.json')).read_text())
        self.assertEqual('../published/' + pending['id'], os.readlink(self.data / 'published-short' / pending['site_id']))
        self.assertFalse((records / ('task-' + self.task['id'] + '.json')).exists())
        recovered = self.publish()
        self.assertEqual(pending['id'], recovered['id'])
        self.assertEqual(pending['site_id'], recovered['site_id'])
        self.assertEqual(1, len(self.published_directories()))

    def test_recovery_after_task_map_failure_does_not_roll_back_newer_release(self):
        first_task = dict(self.task)
        write = publisher._write_json

        def fail_task_map(directory_fd, name, value):
            if name == 'task-' + first_task['id'] + '.json':
                raise OSError('simulated interruption before final idempotence mapping')
            return write(directory_fd, name, value)

        with patch.object(publisher, '_write_json', side_effect=fail_task_map):
            with self.assertRaises(PublicationError):
                self.publish()
        pending = json.loads((self.data / 'publication-records' / ('pending-task-' + first_task['id'] + '.json')).read_text())
        self.next_task()
        (self.site / 'index.html').write_text('Later live version')
        self.write_manifest()
        latest = self.publish()
        recovered = publish_requested(self.store, first_task, allow_completed=True)
        self.assertEqual(pending['id'], recovered['id'])
        self.assertEqual(latest['url'], recovered['url'])
        self.assertEqual('../published/' + latest['id'], os.readlink(self.data / 'published-short' / latest['site_id']))
        self.assertEqual('Later live version', (self.data / 'published-short' / latest['site_id'] / 'index.html').read_text())

    def test_pre_snapshot_failure_keeps_durable_slug_reservation(self):
        with patch.object(publisher.os, 'rename', side_effect=OSError('snapshot rename failed')):
            with self.assertRaises(PublicationError):
                self.publish()
        pending = json.loads((self.data / 'publication-records' / ('pending-task-' + self.task['id'] + '.json')).read_text())
        recovered = self.publish()
        self.assertEqual(pending['site_id'], recovered['site_id'])
        self.assertNotEqual(pending['id'], recovered['id'])
        self.assertEqual(1, len(self.published_directories()))

    def test_promoting_legacy_versions_keeps_old_routes_and_never_rolls_back(self):
        first_task = dict(self.task)
        old = self.legacy_snapshot(body='Old website')
        self.store.finish_task(first_task['id'], first_task['worker_id'])
        with self.assertRaises(PublicationError):
            promote_existing(self.store, first_task, old['id'], slug='ruler')
        first = promote_existing(self.store, first_task, old['id'], slug='ruler', allow_completed=True)
        self.assertEqual('http://203.0.113.10/ruler/', first['url'])
        self.next_task()
        directory = 'conversations/' + self.conversation['id'] + '/public/example'
        latest = self.legacy_snapshot(directory=directory, body='Latest website', with_conversation=True)
        second_task = dict(self.task)
        self.store.finish_task(second_task['id'], second_task['worker_id'])
        second = promote_existing(self.store, second_task, latest['id'], slug='ruler', allow_completed=True)
        repeated = promote_existing(self.store, first_task, old['id'], slug='ruler', allow_completed=True)
        self.assertEqual(first, repeated)
        self.assertEqual(first['url'], second['url'])
        self.assertEqual('Latest website', (self.data / 'published-short' / 'ruler' / 'index.html').read_text())
        self.assertEqual('Old website', (self.data / 'published' / old['id'] / 'index.html').read_text())
        self.assertEqual('Latest website', (self.data / 'published' / latest['id'] / 'index.html').read_text())
        self.assertEqual(first['url'], publish_requested(self.store, first_task, allow_completed=True)['url'])
        resolved = list_publications(self.data, self.user['id'], [first_task['id'], second_task['id']])
        self.assertEqual(2, len(resolved))
        self.assertTrue(all(site['url'] == first['url'] for site in resolved))

    def test_filtered_history_lookup_uses_task_mappings_without_global_scan(self):
        release = self.publish()
        with patch.object(publisher.os, 'scandir', side_effect=AssertionError('must not scan global publication directory')):
            own = list_publications(self.data, self.user['id'], [self.task['id']])
        self.assertEqual(release['url'], own[0]['url'])
        self.assertEqual(1, len(self.published_directories()))

    def test_public_base_rejects_credentials_and_non_http_urls(self):
        for base in ('file:///etc', 'https://user:secret@example.com', 'http://example.com/other',
                     'https://example.com/?secret=x', 'http://example.com:invalid',
                     'http://exa\nmple.com', 'http://example.com\\secret'):
            with self.subTest(base=base), self.assertRaises(PublicationError):
                self.publish(public_base=base)
        self.assertEqual([], self.published_directories())


if __name__ == '__main__':
    unittest.main()
