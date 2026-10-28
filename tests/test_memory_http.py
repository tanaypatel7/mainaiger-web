"""Owner boundary and reviewed memory workflow through the actual HTTP server.

Synthetic exports and a fake model only; no Docker, external provider, or user
archive is required. Code in memory must remain data through every route.
"""
import base64
import copy
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from local_lab import graph, memory_api
from local_lab.server import LabServer, initialize_state

REAL_LOCAL_MODEL = memory_api.local_model


def chatgpt_export():
    return [{'id': 'chat-1', 'title': 'Research project', 'mapping': {
        'root': {'parent': None, 'message': None},
        'm1': {'parent': 'root', 'message': {'author': {'role': 'user'}, 'content': {'parts': ['Selected research evidence']}}},
        'm2': {'parent': 'm1', 'message': {'author': {'role': 'assistant'}, 'content': {'parts': ['UNSELECTED PRIVATE TEXT']}}},
    }}]


class MemoryHttpTests(unittest.TestCase):
    def setUp(self):
        self.local_model_patch = patch.object(memory_api, 'local_model', return_value=True)
        self.local_model_patch.start()
        self.addCleanup(self.local_model_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        state, self.key = initialize_state(self.root / 'private')
        self.server = LabServer(('127.0.0.1', 0), state, self.key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = 'http://127.0.0.1:%d' % self.port
        status, data, headers = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        self.assertEqual(status, 200)
        self.auth = {'Origin': self.origin, 'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def req(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': '127.0.0.1:%d' % self.port}
        h.update(headers or {})
        if body is not None:
            h['Content-Type'] = 'application/json'
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None, h)
        response = connection.getresponse()
        raw = response.read()
        result = json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw
        status, heads = response.status, dict(response.getheaders())
        connection.close()
        return status, result, heads

    def call(self, path, body=None):
        status, result, _ = self.req(path, body, self.auth)
        self.assertEqual(status, 200, result)
        return result

    def import_chat(self, rows=None, archive=False):
        raw = json.dumps(rows if rows is not None else chatgpt_export()).encode()
        filename = 'conversations.json'
        if archive:
            packed = io.BytesIO()
            with zipfile.ZipFile(packed, 'w', zipfile.ZIP_DEFLATED) as z:
                z.writestr('conversations.json', raw)
            raw, filename = packed.getvalue(), 'export.zip'
        result = self.call('/api/memory/import', {'filename': filename, 'data': base64.b64encode(raw).decode()})
        return result

    def sources(self):
        conversations = self.call('/api/memory')['conversations']
        return self.call('/api/memory/conversation?id=' + conversations[0]['id'])['messages']

    def create(self, **overrides):
        payload = {'title': 'Research plan', 'body': 'Use reviewed research evidence.', 'kind': 'note', 'tags': [], 'source_message_ids': []}
        payload.update(overrides)
        return self.call('/api/memory/save', payload)

    def confirm(self, entry):
        return self.call('/api/memory/review', {'id': entry['id'], 'action': 'confirm', 'expected_revision': entry['revision']})

    def test_owner_auth_host_and_csrf_cover_all_memory_routes(self):
        for path in ('/api/memory', '/api/memory/export', '/api/memory/models', '/api/memory/search?q=research', '/api/memory/entry?id=missing'):
            with self.subTest(path=path):
                self.assertEqual(self.req(path)[0], 401)
        self.assertEqual(self.req('/api/memory', headers=dict(self.auth, Host='evil.example'))[0], 403)
        for headers in ({'Origin': self.origin}, dict(self.auth, **{'X-CSRF-Token': 'bad'}), dict(self.auth, Origin='https://evil.example')):
            self.assertEqual(self.req('/api/memory/ideas', {'title': 'No'}, headers)[0], 403)
        # Refuse even large-upload routes before decoding the request.
        with patch.object(memory_api.chat_import, 'parse_archive') as parse:
            self.assertEqual(self.req('/api/memory/import', {'filename': 'x.zip', 'data': 'bad'}, {'Origin': self.origin})[0], 403)
            parse.assert_not_called()
        self.assertIn('csrf', self.call('/api/memory'))
        self.assertEqual(self.call('/api/memory')['counts']['ideas'], 0)

    def test_chatgpt_zip_and_claude_json_import_are_separate_from_memory(self):
        with patch.object(memory_api.models, 'chat') as model:
            first = self.import_chat(archive=True)
            self.assertEqual((first['conversations'], first['messages']), (1, 2))
            self.assertTrue(self.import_chat(archive=True)['duplicate'])
            claude = [{'uuid': 'claude-1', 'name': 'Claude project', 'chat_messages': [
                {'uuid': 'cm1', 'sender': 'human', 'text': 'Claude source text'},
                {'uuid': 'cm2', 'sender': 'assistant', 'content': [{'type': 'text', 'text': 'Claude reply'}]},
            ]}]
            self.assertEqual(self.import_chat(claude)['messages'], 2)
            overview = self.call('/api/memory')
            self.assertEqual(overview['counts']['messages'], 4)
            self.assertEqual(overview['entries'], [])
            self.assertEqual(self.call('/api/memory/search?q=research')['entries'], [])
            model.assert_not_called()

    def test_select_review_edit_and_search_preserve_originals(self):
        self.import_chat()
        messages = self.sources()
        idea = self.call('/api/memory/ideas', {'title': 'Research'})
        draft = self.create(source_message_ids=[messages[0]['id']], idea_id=idea['id'])
        self.assertEqual(draft['status'], 'draft')
        self.assertEqual(self.call('/api/memory/search?q=research')['entries'], [])
        confirmed = self.confirm(draft)
        results = self.call('/api/memory/search?q=research&idea_id=' + idea['id'])
        self.assertEqual([r['id'] for r in results['entries']], [confirmed['id']])
        self.assertEqual(results['route']['model_calls'], 0)
        self.assertEqual(results['route']['api_cost_usd'], 0)
        detail = self.call('/api/memory/entry?id=' + draft['id'])
        self.assertEqual(detail['sources'][0]['text'], messages[0]['text'])
        self.assertEqual(len(detail['history']), 2)
        changed = self.create(id=confirmed['id'], expected_revision=confirmed['revision'], body='Revised research.', source_message_ids=[messages[0]['id']], idea_id=idea['id'])
        self.assertEqual(changed['status'], 'draft')
        self.assertEqual(self.call('/api/memory/search?q=research')['entries'], [])
        self.assertEqual(self.sources(), messages)
        self.assertEqual(len(changed['history']), 3)
        stale = {'id': confirmed['id'], 'title': 'Overwrite', 'body': 'lost update', 'expected_revision': confirmed['revision']}
        self.assertEqual(self.req('/api/memory/save', stale, self.auth)[0], 400)
        self.assertEqual(self.req('/api/memory/review', {'id': confirmed['id'], 'action': 'confirm', 'expected_revision': confirmed['revision']}, self.auth)[0], 400)
        self.assertEqual(self.call('/api/memory/entry?id=' + draft['id'])['body'], 'Revised research.')

    def test_rejected_and_archived_content_is_never_retrieved(self):
        draft = self.create()
        rejected = self.call('/api/memory/review', {'id': draft['id'], 'action': 'reject', 'expected_revision': draft['revision']})
        self.assertEqual(rejected['status'], 'rejected')
        confirmed = self.confirm(self.create())
        archived = self.call('/api/memory/review', {'id': confirmed['id'], 'action': 'archive', 'expected_revision': confirmed['revision']})
        self.assertEqual(archived['status'], 'archived')
        self.assertEqual(self.call('/api/memory/search?q=research')['entries'], [])

    def test_local_ai_proposes_only_from_selected_context_and_never_confirms(self):
        self.import_chat()
        messages = self.sources()
        result = {'ok': True, 'model': 'test:1b', 'reply': json.dumps({'title': 'Research memory', 'body': 'Selected research evidence', 'kind': 'claim'}),
                  'tokens': {'prompt': 11, 'reply': 7}, 'elapsed_ms': 5}
        payload = {'model': 'test:1b', 'instruction': 'Keep a concise claim', 'message_ids': [messages[0]['id']], 'confirm': True}
        with patch.object(memory_api.models, 'chat', return_value=result) as chat:
            draft = self.call('/api/memory/propose', payload)
            self.assertEqual(draft['status'], 'draft')
            self.assertEqual(draft['source_message_ids'], [messages[0]['id']])
            self.assertEqual(draft['generation']['tokens'], {'prompt': 11, 'reply': 7, 'calls': 1})
            sent = json.dumps(chat.call_args.args[1])
            self.assertIn('Selected research evidence', sent)
            self.assertNotIn('UNSELECTED PRIVATE TEXT', sent)
            self.assertEqual(chat.call_count, 1)
            self.assertEqual(chat.call_args.kwargs['format'], memory_api.decide.check_schema(memory_api.PROPOSAL_SCHEMA))
            self.assertEqual(self.call('/api/memory/search?q=research')['entries'], [])
        journal = (self.server.state_dir / 'journal.jsonl')
        if journal.exists():
            self.assertNotIn('UNSELECTED PRIVATE TEXT', journal.read_text())

    def test_proposal_refuses_unknown_sources_stale_revisions_and_missing_confirmation(self):
        confirmed = self.confirm(self.create())
        base = {'model': 'test:1b', 'instruction': 'Shorten this', 'message_ids': [], 'confirm': True}
        with patch.object(memory_api.models, 'chat') as chat:
            for payload in (dict(base, message_ids=['unknown']), dict(base, confirm=False),
                            dict(base, entry_id=confirmed['id'], expected_revision=confirmed['revision'] - 1)):
                self.assertEqual(self.req('/api/memory/propose', payload, self.auth)[0], 400)
            chat.assert_not_called()
        self.assertEqual(self.call('/api/memory/entry?id=' + confirmed['id'])['status'], 'confirmed')

    def test_invalid_model_reply_cannot_change_store_or_execute(self):
        payload = {'model': 'test:1b', 'instruction': 'Draft a note', 'message_ids': [], 'confirm': True}
        fake = {'ok': True, 'reply': '{"status":"confirmed","execute":"rm -rf /"}', 'tokens': {}, 'elapsed_ms': 1}
        with patch.object(memory_api.models, 'chat', return_value=fake) as chat, patch.object(self.server, 'start_graph_run') as run:
            self.assertEqual(self.req('/api/memory/propose', payload, self.auth)[0], 400)
            self.assertEqual(self.call('/api/memory')['entries'], [])
            self.assertEqual(chat.call_count, 1)
            run.assert_not_called()

    def test_ai_edit_cannot_overwrite_a_concurrent_owner_edit(self):
        entry = self.confirm(self.create())
        proposal = {'model': 'test:1b', 'instruction': 'Shorten this', 'message_ids': [], 'confirm': True,
                    'entry_id': entry['id'], 'expected_revision': entry['revision']}
        def concurrent_edit(*args, **kwargs):
            self.server.memory.save_entry({'id': entry['id'], 'expected_revision': entry['revision'],
                                           'title': entry['title'], 'body': 'Owner changed this while model ran.', 'kind': 'note'})
            return {'ok': True, 'reply': json.dumps({'title': 'Model suggestion', 'body': 'Stale model result', 'kind': 'note'}),
                    'tokens': {}, 'elapsed_ms': 1}
        with patch.object(memory_api.models, 'chat', side_effect=concurrent_edit):
            self.assertEqual(self.req('/api/memory/propose', proposal, self.auth)[0], 400)
        current = self.call('/api/memory/entry?id=' + entry['id'])
        self.assertEqual(current['body'], 'Owner changed this while model ran.')
        self.assertEqual(current['revision'], entry['revision'] + 1)

    def test_save_cannot_bypass_review_by_claiming_confirmed_status(self):
        payload = {'title': 'Research claim', 'body': 'Not reviewed.', 'status': 'confirmed', 'confirmed_at': 1,
                   'kind': 'claim', 'source_message_ids': []}
        status, result, _ = self.req('/api/memory/save', payload, self.auth)
        self.assertIn(status, (200, 400))  # reject unknown authority fields, or ignore them
        if status == 200:
            self.assertEqual(result['status'], 'draft')
            self.assertIsNone(result['confirmed_at'])
        self.assertEqual(self.call('/api/memory/search?q=research')['entries'], [])

    def test_mainbook_snapshot_open_is_explicit_versioned_and_inert(self):
        original = graph.new_document()
        original['notebook']['cells'][0]['source'] = 'raise RuntimeError("must not execute")'
        original['graph']['nodes'][0]['events'] = [{'kind': 'manual'}]
        self.server.set_graph(original)
        snapshot = self.call('/api/memory/mainbook', {'title': 'Research method'})
        self.assertEqual(snapshot['status'], 'draft')
        self.assertTrue(snapshot['has_mainbook'])
        opening = {'id': snapshot['id'], 'expected_revision': snapshot['revision'], 'confirm': True, 'graph_revision': self.server.graph_revision}
        self.assertEqual(self.req('/api/memory/open-mainbook', opening, self.auth)[0], 400)
        confirmed = self.confirm(snapshot)
        opening['expected_revision'] = confirmed['revision']
        self.server.set_graph(graph.new_document())
        self.assertEqual(self.req('/api/memory/open-mainbook', opening, self.auth)[0], 400)
        opening['graph_revision'] = self.server.graph_revision
        self.assertEqual(self.req('/api/memory/open-mainbook', dict(opening, confirm=False), self.auth)[0], 400)
        with patch.object(self.server, 'start_graph_run') as run:
            loaded = self.call('/api/memory/open-mainbook', opening)
            self.assertFalse(loaded['executed'])
            self.assertIn('must not execute', loaded['view']['document']['notebook']['cells'][0]['source'])
            self.assertFalse(self.server.events.status()['armed'])
            self.assertIsNone(self.server.kernel)
            run.assert_not_called()

    def test_memory_graph_and_export_retain_evidence_without_executable_cells(self):
        self.import_chat()
        messages = self.sources()
        entry = self.confirm(self.create(kind='code', body='__import__("os").system("unsafe")', source_message_ids=[messages[0]['id']]))
        doc = self.call('/api/memory/graph?id=' + entry['id'])
        self.assertEqual(graph.validate(doc), doc)
        self.assertTrue(all(c['cell_type'] == 'markdown' for c in doc['notebook']['cells']))
        self.assertIn(messages[0]['text'], json.dumps(doc))
        exported = self.call('/api/memory/export')
        self.assertEqual(exported['schema'], 'mainaiger.memory.v1')
        self.assertEqual(exported['entries'][0]['id'], entry['id'])
        self.assertEqual(len(exported['entries'][0]['history']), 2)
        self.assertEqual(exported['conversations'][0]['messages'][0]['text'], messages[0]['text'])

    def test_memory_graph_chunks_unicode_by_bytes_without_losing_content(self):
        body = '漢🙂' * 20000 + ' — final sentence'
        entry = self.create(body=body)
        doc = self.call('/api/memory/graph?id=' + entry['id'])
        self.assertEqual(graph.validate(doc), doc)
        cells = doc['notebook']['cells']
        self.assertTrue(all(c['cell_type'] == 'markdown' for c in cells))
        self.assertTrue(all(len(graph.source_text(c).encode('utf-8')) <= graph.MAX_SOURCE for c in cells))
        self.assertIn(body, ''.join(graph.source_text(c) for c in cells))
        self.assertLessEqual(len(doc['graph']['nodes']), graph.MAX_NODES)

    def test_graph_download_preserves_saved_mainbook_snapshot(self):
        original = graph.new_document()
        original['notebook']['cells'][0]['source'] = 'answer = 42'
        self.server.set_graph(original)
        snapshot = self.call('/api/memory/mainbook', {'title': 'Reusable method'})
        with patch.object(self.server, 'start_graph_run') as run:
            downloaded = self.call('/api/memory/graph?id=' + snapshot['id'])
            self.assertEqual(downloaded, snapshot['mainbook'])
            self.assertEqual(downloaded['notebook']['cells'][0]['cell_type'], 'code')
            run.assert_not_called()
        self.assertEqual(self.call('/api/memory/entry?id=' + snapshot['id'])['status'], 'draft')

    def test_private_context_never_reaches_remote_or_unknown_models(self):
        self.import_chat()
        messages = self.sources()
        payload = {'model': 'test:1b', 'instruction': 'Keep a concise claim', 'message_ids': [messages[0]['id']], 'confirm': True}
        good = {'details': {'format': 'gguf'}, 'model_info': {'general.architecture': 'llama'}}
        responses = [(None, None), (500, {'error': 'unavailable'}), (200, {}), (200, {'details': []}),
                     (200, {'details': {'format': 'gguf'}, 'model_info': {}}),
                     (200, {'details': {'format': 'unknown'}, 'model_info': {'general.architecture': 'llama'}})]
        responses += [(200, dict(good, **{key: 'https://remote.example'})) for key in ('remote_host', 'remote_url', 'remote_model')]
        with patch.object(memory_api, 'local_model', REAL_LOCAL_MODEL):
            for response in responses:
                with self.subTest(response=response), patch.object(memory_api.models, '_request', return_value=response) as request, patch.object(memory_api.models, 'chat') as chat:
                    self.assertEqual(self.req('/api/memory/propose', payload, self.auth)[0], 400)
                    chat.assert_not_called()
                    self.assertEqual(request.call_args.args[:3], ('POST', '/api/show', {'model': 'test:1b'}))
                    self.assertNotIn(messages[0]['text'], str(request.call_args))
            with patch.object(memory_api.models, '_request') as request, patch.object(memory_api.models, 'chat') as chat:
                self.assertEqual(self.req('/api/memory/propose', dict(payload, model='test:cloud'), self.auth)[0], 400)
                request.assert_not_called()
                chat.assert_not_called()
        self.assertEqual(self.call('/api/memory')['entries'], [])

    def test_local_weights_preflight_then_structured_completion(self):
        self.import_chat()
        messages = self.sources()
        calls = []
        def request(method, path, body=None, **kwargs):
            calls.append((method, path, copy.deepcopy(body)))
            if path == '/api/show':
                return 200, {'details': {'format': 'gguf'}, 'model_info': {'general.architecture': 'llama'}}
            if path == '/api/tags':
                return 200, {'models': [{'name': 'test:1b', 'size': 123}]}
            if path == '/api/chat':
                return 200, {'model': 'test:1b', 'message': {'content': json.dumps({'title': 'Evidence', 'body': 'Selected research evidence', 'kind': 'claim'})},
                             'prompt_eval_count': 9, 'eval_count': 5}
            self.fail('Unexpected model route: ' + path)
        payload = {'model': 'test:1b', 'instruction': 'Draft the claim', 'message_ids': [messages[0]['id']], 'confirm': True}
        with patch.object(memory_api, 'local_model', REAL_LOCAL_MODEL), patch.object(memory_api.models, '_request', side_effect=request):
            result = self.call('/api/memory/propose', payload)
        self.assertEqual([c[1] for c in calls], ['/api/show', '/api/tags', '/api/chat'])
        self.assertNotIn(messages[0]['text'], json.dumps(calls[:2]))
        self.assertIn(messages[0]['text'], json.dumps(calls[2]))
        self.assertNotIn('UNSELECTED PRIVATE TEXT', json.dumps(calls))
        self.assertEqual(calls[-1][2]['format'], memory_api.decide.check_schema(memory_api.PROPOSAL_SCHEMA))
        self.assertEqual(result['status'], 'draft')
        self.assertEqual(result['generation']['tokens'], {'prompt': 9, 'reply': 5, 'calls': 1})

    def test_model_picker_filters_cloud_unknown_and_accepts_local_weight_formats(self):
        names = ['local-gguf:1b', 'local-safe:1b', 'unknown:1b', 'remote:1b', 'model:cloud']
        def request(method, path, body=None, **kwargs):
            self.assertEqual((method, path), ('POST', '/api/show'))
            name = body['model']
            if name == 'unknown:1b': return 200, {}
            metadata = {'details': {'format': 'safetensors' if name == 'local-safe:1b' else 'gguf'},
                        'model_info': {'general.architecture': 'llama'}}
            if name == 'remote:1b': metadata['remote_model'] = 'remote-name'
            return 200, metadata
        with patch.object(memory_api, 'local_model', REAL_LOCAL_MODEL), patch.object(memory_api.models, 'installed_models', return_value=[{'name': n} for n in names]), patch.object(memory_api.models, '_request', side_effect=request) as req:
            result = self.call('/api/memory/models')
        self.assertEqual([m['name'] for m in result['models']], ['local-gguf:1b', 'local-safe:1b'])
        self.assertEqual(req.call_count, 4)  # cloud-tagged name never needs metadata

    def browser_capture(self, files=None):
        parsed = memory_api.chat_import.parse_archive(json.dumps(chatgpt_export()).encode(), 'conversations.json')
        parsed['provider'] = 'chatgpt-browser'
        for conversation in parsed['conversations']:
            conversation['provider'] = 'chatgpt-browser'
            conversation['source_id'] = 'https://chatgpt.com/c/' + conversation['source_id']
        return {'available': True, 'running': True, 'busy': False, 'provider': 'chatgpt', 'chats': [],
                'job': {'id': 'capture-one', 'action': 'capture', 'state': 'complete', 'completed': 1, 'total': 1},
                'result': {'parsed': parsed, 'captured': 1, 'skipped': [], 'files': files or [],
                           'warnings': ['Rendered text only.'], 'message': 'Captured source history.'}}

    def test_browser_routes_require_owner_and_action_confirmation(self):
        manager = MagicMock()
        with patch.object(self.server, 'browser_import', manager):
            for route in ('/api/memory/browser', '/api/memory/browser/bundle'):
                self.assertEqual(self.req(route)[0], 401)
            for action in ('open', 'export', 'scan', 'capture', 'close'):
                route = '/api/memory/browser/' + action
                self.assertEqual(self.req(route, {'confirm': True}, {'Origin': self.origin})[0], 403)
                self.assertEqual(self.req(route, {'confirm': False}, self.auth)[0], 400)
            self.assertEqual(manager.mock_calls, [])
        self.assertEqual(self.req('/api/memory/browser/bundle', headers=self.auth)[0], 400)

    def test_browser_actions_dispatch_only_requested_operation(self):
        manager = MagicMock()
        for name in ('open', 'request_export', 'scan', 'capture', 'close_browser'):
            getattr(manager, name).return_value = {'busy': True}
        urls = ['https://chatgpt.com/c/one']
        with patch.object(self.server, 'browser_import', manager):
            self.call('/api/memory/browser/open', {'confirm': True, 'provider': 'chatgpt'})
            self.call('/api/memory/browser/export', {'confirm': True})
            self.call('/api/memory/browser/scan', {'confirm': True})
            self.call('/api/memory/browser/capture', {'confirm': True, 'urls': urls, 'capture_files': True})
            self.call('/api/memory/browser/close', {'confirm': True})
            self.assertEqual(self.req('/api/memory/browser/unknown', {'confirm': True}, self.auth)[0], 400)
        manager.open.assert_called_once_with('chatgpt')
        manager.request_export.assert_called_once_with()
        manager.scan.assert_called_once_with()
        manager.capture.assert_called_once_with(urls, True)
        manager.close_browser.assert_called_once_with()
        manager.status.assert_not_called()
        self.assertEqual(self.call('/api/memory')['counts']['messages'], 0)

    def test_completed_capture_imports_once_and_status_hides_private_text(self):
        manager = MagicMock()
        captured = self.browser_capture()
        manager.status.side_effect = lambda: copy.deepcopy(captured)
        with patch.object(self.server, 'browser_import', manager), patch.object(self.server.memory, 'import_chats', wraps=self.server.memory.import_chats) as ingest, patch.object(memory_api.models, 'chat') as model:
            first = self.call('/api/memory/browser')
            second = self.call('/api/memory/browser')
            self.assertEqual(ingest.call_count, 1)
            self.assertEqual(first, second)
            self.assertTrue(first['result']['bundle_available'])
            self.assertEqual(first['result']['imported']['messages'], 2)
            self.assertNotIn('parsed', first['result'])
            self.assertNotIn('files', first['result'])
            self.assertNotIn('UNSELECTED PRIVATE TEXT', json.dumps(first))
            self.assertNotIn('Selected research evidence', json.dumps(first))
            model.assert_not_called()
        overview = self.call('/api/memory')
        self.assertEqual(overview['counts']['messages'], 2)
        self.assertEqual(overview['entries'], [])
        self.assertEqual(self.call('/api/memory/search?q=research')['entries'], [])
        record = self.server.state_dir / 'browser-capture.json'
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertIn('UNSELECTED PRIVATE TEXT', record.read_text())

    def test_failed_or_running_browser_job_does_not_import(self):
        manager = MagicMock()
        with patch.object(self.server, 'browser_import', manager), patch.object(self.server.memory, 'import_chats', wraps=self.server.memory.import_chats) as ingest:
            for state in ('running', 'failed'):
                data = self.browser_capture()
                data['job']['state'] = state
                data['result'] = None
                data['busy'] = state == 'running'
                manager.status.return_value = data
                self.call('/api/memory/browser')
            ingest.assert_not_called()
        self.assertFalse((self.server.state_dir / 'browser-capture.json').exists())

    def test_capture_bundle_contains_sources_report_and_only_private_files(self):
        folder = self.server.state_dir / 'browser-import-files'
        folder.mkdir(mode=0o700)
        attachment = folder / 'locally-generated-id.dat'
        attachment.write_bytes(b'inert attachment bytes')
        attachment.chmod(0o600)
        files = [{'path': str(attachment), 'name': 'User attachment', 'downloaded': True}]
        captured = self.browser_capture(files)
        manager = MagicMock()
        manager.status.side_effect = lambda: copy.deepcopy(captured)
        with patch.object(self.server, 'browser_import', manager):
            self.call('/api/memory/browser')
        status, blob, headers = self.req('/api/memory/browser/bundle', headers=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/zip')
        reconstructed = memory_api.chat_import.parse_archive(blob, 'captured.zip')
        self.assertEqual(reconstructed['provider'], 'chatgpt-browser')
        self.assertEqual(self.server.memory.import_chats(reconstructed)['messages'], 0)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertEqual(set(archive.namelist()), {'conversations.json', 'capture-report.json', 'README.txt', 'files/locally-generated-id.dat'})
            self.assertEqual(archive.read('files/locally-generated-id.dat'), b'inert attachment bytes')
            self.assertIn('Selected research evidence', archive.read('conversations.json').decode())
            report = json.loads(archive.read('capture-report.json'))
            self.assertNotIn(str(self.server.state_dir), json.dumps(report))
            self.assertNotIn('parsed', report)
            self.assertEqual(report['files'][0]['bundle_path'], 'files/locally-generated-id.dat')
        self.assertEqual(self.call('/api/memory')['entries'], [])

    def test_capture_bundle_refuses_outside_files_and_symlinks(self):
        outside = self.root / 'private-secret'
        outside.write_text('do not export')
        folder = self.server.state_dir / 'browser-import-files'
        folder.mkdir(mode=0o700)
        link = folder / 'link'
        link.symlink_to(outside)
        manager = MagicMock()
        with patch.object(self.server, 'browser_import', manager):
            for index, path in enumerate((outside, link)):
                captured = self.browser_capture([{'path': str(path), 'name': 'secret'}])
                captured['job']['id'] = 'capture-' + str(index)
                manager.status.side_effect = lambda captured=captured: copy.deepcopy(captured)
                self.call('/api/memory/browser')
                status, error, _ = self.req('/api/memory/browser/bundle', headers=self.auth)
                self.assertEqual(status, 400)
                self.assertNotIn('do not export', json.dumps(error))

    def test_bad_archive_encoding_and_unknown_routes_are_rejected(self):
        for payload in ({'filename': 'x.zip', 'data': '**not base64**'}, {'filename': 'x.json', 'data': base64.b64encode(b'not json').decode()}):
            self.assertEqual(self.req('/api/memory/import', payload, self.auth)[0], 400)
        self.assertEqual(self.req('/api/memory/not-real', {}, self.auth)[0], 400)
        self.assertEqual(self.req('/api/memory/not-real', headers=self.auth)[0], 400)
        self.assertEqual(self.call('/api/memory')['counts']['messages'], 0)


if __name__ == '__main__':
    unittest.main()
