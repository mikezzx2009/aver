"""Non-completed daemon outcomes retain a useful error and saved work."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from store import Store
import worker
import publisher


class CapturedInput(io.BytesIO):
    def close(self):
        self.content = self.getvalue()
        super().close()


class CompletedClient:
    def __init__(self, result):
        self.return_code = 0 if result.get('success') else 1
        self.stdin = CapturedInput()
        self.stdout = io.BytesIO((json.dumps({'type': 'result', 'data': result}) + '\n').encode())

    def wait(self, timeout=None):
        return self.return_code

    def poll(self):
        return self.return_code


class WorkerResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.store = Store(self.data / 'data')
        self.user = self.store.create_user('result-user', 'result test password')
        self.conversation = self.store.create_conversation(self.user['id'])
        self.marker = self.store.workspace_path(self.user['id']) / 'files' / 'partial.txt'
        self.marker.write_text('saved partial work')

    def finish_with(self, result, *, publication=None):
        task = self.store.submit_task(self.user['id'], self.conversation['id'], 'Build a website', 'result-test')
        claimed = self.store.claim_task(worker.WORKER_ID)
        if publication is not None:
            workspace = self.store.workspace_path(self.user['id'])
            if publication == 'conversation':
                publication = 'conversations/' + self.conversation['id'] + '/public/demo'
            site = workspace / (publication if publication.startswith('conversations/') else 'public/demo')
            site.mkdir(parents=True)
            (site / 'index.html').write_text('<h1>public demo</h1>')
            manifests = workspace / '.ever-publish'
            manifests.mkdir()
            (manifests / (task['id'] + '.json')).write_text(json.dumps({
                'version': 1, 'task_id': task['id'], 'directory': publication, 'title': 'Demo'}))
        result = {'task_id': task['id'], 'session_id': self.conversation['id'],
                  'journal_terminal': True, 'success': False, 'response': '', **result}
        tokens = Mock()
        tokens.issue.return_value = 'fixture-proxy-token'
        settings = {'api_key': 'fixture-key', 'proxy_url': 'http://127.0.0.1/v1',
                    'model': 'deepseek-v4-pro', 'task_timeout_seconds': 0,
                    'site_base_url': 'http://203.0.113.10'}
        self.client = CompletedClient(result)
        with patch.object(worker, 'DATA', self.data), patch.object(worker, 'STORE', self.store), \
                patch.object(publisher, 'DATA', self.data), \
                patch.object(worker, 'TOKENS', tokens), patch.object(worker, 'config', return_value=settings), \
                patch.object(worker, 'ensure_sandbox', return_value='fixture-container'), \
                patch.object(worker, 'ensure_daemon'), patch.object(worker, 'stop_execution') as stop, \
                patch.object(worker.subprocess, 'Popen', return_value=self.client), \
                patch.object(self.store, 'prepare_conversation_workspace', wraps=self.store.prepare_conversation_workspace) as prepare:
            worker.run_task(claimed)
        prepare.assert_called_once_with(self.user['id'], self.conversation['id'])
        self.request = json.loads(self.client.stdin.content)
        stop.assert_not_called()
        self.assertEqual('saved partial work', self.marker.read_text())
        return self.store.get_task(self.user['id'], task['id'])

    def test_token_limit_is_saved_in_task_and_system_message(self):
        task = self.finish_with({'finish_reason': 'max-tokens'})
        self.assertEqual('failed', task['status'])
        self.assertIn('本轮模型输出达到长度上限', task['error'])
        self.assertIn('新消息让 Agent 继续', task['error'])
        messages = self.store.list_messages(self.user['id'], self.conversation['id'])
        self.assertEqual('system', messages[-1]['role'])
        self.assertEqual(task['error'], messages[-1]['content'])

    def test_token_limit_retains_partial_response_with_clear_error(self):
        task = self.finish_with({'finish_reason': 'max-tokens', 'response': 'Created the initial page'})
        self.assertEqual('failed', task['status'])
        self.assertEqual('Created the initial page', task['result'])
        self.assertIn('长度上限', task['error'])
        messages = self.store.list_messages(self.user['id'], self.conversation['id'])
        self.assertEqual('assistant', messages[-1]['role'])
        self.assertEqual(task['result'], messages[-1]['content'])

    def test_explicit_failure_message_is_retained_and_bounded(self):
        task = self.finish_with({'finish_reason': 'failed', 'message': 'upstream unavailable ' + 'x' * 5000})
        self.assertTrue(task['error'].startswith('upstream unavailable'))
        self.assertEqual(4000, len(task['error']))

    def test_other_terminal_reason_is_exposed_with_a_bound(self):
        task = self.finish_with({'finish_reason': 'transport-closed-' + 'x' * 1000})
        self.assertIn('transport-closed-', task['error'])
        self.assertLess(len(task['error']), 240)

    def test_completed_website_is_published_and_actual_url_persisted(self):
        task = self.finish_with({'finish_reason': 'completed', 'success': True, 'response': 'Site created'},
                                publication='public/demo')
        self.assertEqual('succeeded', task['status'])
        sites = publisher.list_publications(self.data, self.user['id'], [task['id']])
        self.assertEqual(1, len(sites))
        self.assertIn(sites[0]['url'], task['result'])
        self.assertEqual('<h1>public demo</h1>', (self.data / 'published' / sites[0]['id'] / 'index.html').read_text())
        self.assertFalse((self.data / 'published' / sites[0]['id'] / 'files').exists())
        messages = self.store.list_messages(self.user['id'], self.conversation['id'])
        self.assertEqual(task['result'], messages[-1]['content'])
        self.assertIn(task['id'], worker.website_instructions(task))

    def test_each_task_prepares_conversation_directory_and_routes_existing_daemon(self):
        task = self.finish_with({'finish_reason': 'completed', 'success': True, 'response': 'Done'})
        directory = '/workspace/conversations/' + self.conversation['id']
        prompt = self.request['prompt']
        self.assertTrue(prompt.startswith('Build a website'))
        self.assertIn('[Aver 对话文件规则]', prompt)
        self.assertIn('当前对话的工作目录是 ' + directory, prompt)
        self.assertIn('明确绝对路径', prompt)
        self.assertIn('/home/agent', prompt)
        self.assertIn('[Aver 网站交付规则]', prompt)
        self.assertIn(directory + '/public/<项目名>/', prompt)
        self.assertIn('"directory": "conversations/' + self.conversation['id'] + '/public/my-site"', prompt)
        self.assertIn('/workspace/.ever-publish/' + task['id'] + '.json', prompt)
        self.assertNotIn('[Ever ', prompt)
        self.assertNotIn('cwd', self.request)
        self.assertTrue((self.store.workspace_path(self.user['id']) / 'conversations' / self.conversation['id']).is_dir())

    def test_scoped_website_publishes_only_current_conversation_files(self):
        task = self.finish_with({'finish_reason': 'completed', 'success': True, 'response': 'Site created'},
                                publication='conversation')
        self.assertEqual('succeeded', task['status'])
        sites = publisher.list_publications(self.data, self.user['id'], [task['id']])
        self.assertEqual(1, len(sites))
        self.assertEqual('conversations/' + self.conversation['id'] + '/public/demo', sites[0]['directory'])
        self.assertIn(sites[0]['url'], task['result'])

    def test_existing_site_id_directory_and_fixed_url_are_given_to_next_task(self):
        seed = self.store.submit_task(self.user['id'], self.conversation['id'], 'Create website', 'seed-site')
        claimed = self.store.claim_task(worker.WORKER_ID, lease_seconds=300)
        directory = 'conversations/' + self.conversation['id'] + '/public/demo'
        workspace = self.store.workspace_path(self.user['id'])
        site = workspace / directory
        site.mkdir(parents=True)
        (site / 'index.html').write_text('<h1>original website</h1>')
        manifests = workspace / '.ever-publish'
        manifests.mkdir()
        (manifests / (seed['id'] + '.json')).write_text(json.dumps({
            'version': 1, 'task_id': seed['id'], 'directory': directory, 'title': 'Demo'}))
        with patch.object(publisher, 'DATA', self.data):
            release = publisher.publish_requested(self.store, claimed, public_base='http://203.0.113.10')
        self.store.finish_task(seed['id'], worker.WORKER_ID)
        self.finish_with({'finish_reason': 'completed', 'success': True, 'response': 'Done'})
        prompt = self.request['prompt']
        self.assertIn('"site_id": "' + release['site_id'] + '"', prompt)
        self.assertIn(release['url'], prompt)
        self.assertIn(directory, prompt)
        self.assertIn('沿用固定的短地址', prompt)
        self.assertIn('如果必须更换目录', prompt)
        self.assertIn('发布后的页面位于 /<固定网站ID>/ 下', prompt)
        self.assertNotIn('/sites/<站点ID>/', prompt)

    def test_publication_failure_keeps_created_files_and_explains_failure(self):
        task = self.finish_with({'finish_reason': 'completed', 'success': True, 'response': 'Site created'},
                                publication='files')
        self.assertEqual('failed', task['status'])
        self.assertIn('公网发布未完成', task['error'])
        self.assertIn('Site created', task['result'])
        self.assertTrue((self.store.workspace_path(self.user['id']) / 'public' / 'demo' / 'index.html').exists())


if __name__ == '__main__':
    unittest.main()
