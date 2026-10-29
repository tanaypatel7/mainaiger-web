"""Chat import (ChatGPT and Claude export shapes) into chats/, and the review step that turns chats into memory."""
import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

from local_lab import memory as m, memory_import as mi, models
from local_lab.server import LabServer, initialize_state


def chatgpt_rows():
    return [
        {'title': 'Fraud model talk', 'create_time': 1700000000, 'update_time': 1700003600, 'conversation_id': 'c1', 'current_node': 'n3', 'mapping': {
            'n0': {'id': 'n0', 'message': None, 'parent': None, 'children': ['n1']},
            'n1': {'id': 'n1', 'message': {'author': {'role': 'system'}, 'content': {'content_type': 'text', 'parts': ['hidden']}, 'metadata': {'is_visually_hidden_from_conversation': True}}, 'parent': 'n0', 'children': ['n2']},
            'n2': {'id': 'n2', 'message': {'author': {'role': 'user'}, 'create_time': 1700000001, 'content': {'content_type': 'text', 'parts': ['How do I detect fraud with quantum kernels?']}, 'metadata': {'attachments': [{'id': 'file-1', 'name': 'data.csv'}]}}, 'parent': 'n1', 'children': ['n3', 'n4']},
            'n3': {'id': 'n3', 'message': {'author': {'role': 'assistant'}, 'create_time': 1700000005, 'content': {'content_type': 'text', 'parts': ['Use a feature map and an SVM.', {'content_type': 'image_asset_pointer', 'asset_pointer': 'file-service://file-abc'}]}}, 'parent': 'n2', 'children': []},
            'n4': {'id': 'n4', 'message': {'author': {'role': 'assistant'}, 'content': {'content_type': 'text', 'parts': ['An abandoned branch']}}, 'parent': 'n2', 'children': []}}},
        {'title': 'Code chat', 'create_time': 1700100000, 'conversation_id': 'c2', 'mapping': {
            'm1': {'id': 'm1', 'message': {'author': {'role': 'user'}, 'content': {'content_type': 'text', 'parts': ['print hello']}}, 'parent': None, 'children': ['m2']},
            'm2': {'id': 'm2', 'message': {'author': {'role': 'assistant'}, 'content': {'content_type': 'code', 'language': 'python', 'text': 'print("hello")'}}, 'parent': 'm1', 'children': ['m3']},
            'm3': {'id': 'm3', 'message': {'author': {'role': 'tool'}, 'content': {'content_type': 'execution_output', 'text': 'hello'}}, 'parent': 'm2', 'children': []}}},
        {'title': 'Empty', 'mapping': {}, 'conversation_id': 'c3'},
        'not a conversation']


def claude_rows():
    return [{'uuid': 'u1', 'name': 'Haptics plan', 'created_at': '2026-01-02T10:00:00Z', 'updated_at': '2026-01-02T11:00:00Z', 'chat_messages': [
        {'uuid': 'a', 'sender': 'human', 'text': 'Plan the haptics demo', 'created_at': '2026-01-02T10:00:00Z', 'attachments': [{'file_name': 'notes.txt', 'extracted_content': 'glove specs'}], 'files': [{'file_name': 'photo.png'}]},
        {'uuid': 'b', 'sender': 'assistant', 'text': '', 'content': [{'type': 'text', 'text': 'Start with the glove.'}], 'created_at': '2026-01-02T10:01:00Z'}]}]


class Vault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve(); self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start(); m.configure(self.state, str(self.root / 'Memory'))
    def tearDown(self):
        self.home.stop(); self.tmp.cleanup()
    def zip_of(self, rows, name='export.zip', extra=()):
        z = self.root / name
        with zipfile.ZipFile(z, 'w') as a:
            a.writestr('conversations.json', json.dumps(rows))
            for n, text in extra: a.writestr(n, text)
        return str(z)


class ParserTests(Vault):
    def test_chatgpt_tree_walk_content_types_and_skips(self):
        z = self.zip_of(chatgpt_rows(), extra=[('user.json', '{}'), ('chat.html', '<html>')])
        info = mi.inspect_zip(z)
        self.assertEqual((info['source'], info['conversations'], info['messages'], info['titles'], info['other_files']), ('chatgpt', 2, 5, ['Fraud model talk', 'Code chat'], ['user.json', 'chat.html']))
        self.assertEqual(info['first'][:4], '2023')
        source, convs, errors = mi.parse_all(json.dumps(chatgpt_rows()))
        fraud = convs[0]; self.assertEqual([x['role'] for x in fraud['messages']], ['user', 'assistant'], 'the hidden system message and the abandoned branch are left out')
        self.assertEqual(fraud['messages'][0]['attachments'], ['data.csv']); self.assertIn('[attachment: file-abc]', fraud['messages'][1]['text']); self.assertEqual(fraud['id'], 'c1')
        code = convs[1]; self.assertIn('```python\nprint("hello")\n```', code['messages'][1]['text']); self.assertEqual(code['messages'][2]['role'], 'tool'); self.assertIn('Output:', code['messages'][2]['text'])
        self.assertEqual(errors, [])
    def test_claude_shape_and_json_file_directly(self):
        j = self.root / 'conversations.json'; j.write_text(json.dumps(claude_rows()))
        info = mi.inspect_zip(str(j)); self.assertEqual((info['source'], info['conversations'], info['messages']), ('claude', 1, 2))
        source, convs, _ = mi.parse_all(j.read_text())
        msg = convs[0]['messages']; self.assertEqual(msg[0]['attachments'], ['notes.txt', 'photo.png']); self.assertIn('[attachment notes.txt]\nglove specs', msg[0]['text']); self.assertEqual(msg[1]['text'], 'Start with the glove.'); self.assertEqual(convs[0]['created'], '2026-01-02T10:00:00')
    def test_bad_inputs(self):
        for bad, message in (('relative.zip', 'absolute'), (str(self.root / 'missing.zip'), 'No such file'), (str(self.root / 'x.txt'), 'export zip')):
            (self.root / 'x.txt').write_text('x')
            with self.subTest(bad=bad), self.assertRaisesRegex(mi.ImportError_, message): mi.inspect_zip(bad)
        (self.root / 'notzip.zip').write_text('hello')
        with self.assertRaisesRegex(mi.ImportError_, 'not a zip'): mi.inspect_zip(str(self.root / 'notzip.zip'))
        with self.assertRaisesRegex(mi.ImportError_, 'no conversations.json'): mi.inspect_zip(self.zip_of([], name='empty.zip').replace('conversations.json', 'x') if False else self._zip_without())
        with self.assertRaisesRegex(mi.ImportError_, 'neither'): mi.inspect_zip(self.zip_of([{'foo': 1}], name='odd.zip'))
        with self.assertRaisesRegex(mi.ImportError_, 'could not be parsed'): mi.inspect_zip(self._zip_text('{not json', 'broken.zip'))
    def _zip_without(self):
        z = self.root / 'no-conv.zip'
        with zipfile.ZipFile(z, 'w') as a: a.writestr('user.json', '{}')
        return str(z)
    def _zip_text(self, text, name):
        z = self.root / name
        with zipfile.ZipFile(z, 'w') as a: a.writestr('conversations.json', text)
        return str(z)


class ImportAndReviewTests(Vault):
    def test_import_is_idempotent_and_notes_are_read_only_with_provenance(self):
        z = self.zip_of(chatgpt_rows())
        out = mi.import_zip(self.state, z)
        self.assertEqual((out['source'], out['imported'], out['skipped'], [n['path'] for n in out['notes']]), ('chatgpt', 2, 0, ['chats/chatgpt/fraud-model-talk.md', 'chats/chatgpt/code-chat.md']))
        note = m.read(self.state, 'chats/chatgpt/fraud-model-talk.md')
        self.assertEqual((note['kind'], note['status'], note['front']['source'], note['front']['source_id'], note['front']['messages'], note['sources']), ('chat', 'stable', 'chatgpt', 'c1', 2, ['export: export.zip']))
        self.assertIn('### 1 · You · 2023', note['body']); self.assertIn('### 2 · Assistant', note['body']); self.assertIn('Attachments: data.csv', note['body'])
        self.assertEqual(mi.import_zip(self.state, z)['skipped'], 2, 'the same export again adds nothing')
        self.assertEqual(m.status(self.state)['counts'], {'notes': 0, 'chats': 2})
        with self.assertRaisesRegex(m.MemoryError, 'not edited'): m.update(self.state, note['path'], body='x')
        j = self.root / 'conversations.json'; j.write_text(json.dumps(claude_rows())); self.assertEqual(mi.import_zip(self.state, str(j))['imported'], 1)
        self.assertEqual(mi.import_zip(self.state, z.replace('export.zip', 'export.zip'), limit=1)['skipped'], 1)
        long_rows = [{'uuid': 'big', 'name': 'Big', 'chat_messages': [{'sender': 'human', 'text': 'x' * 50000} for _ in range(60)]}]
        j.write_text(json.dumps(long_rows)); out = mi.import_zip(self.state, str(j)); self.assertEqual(out['imported'], 1)
        self.assertIn('[truncated:', m.read(self.state, out['notes'][0]['path'])['body'])
    def test_review_proposes_keeps_with_sources_and_tracks_progress(self):
        mi.import_zip(self.state, self.zip_of(chatgpt_rows()))
        st = mi.review_status(self.state); self.assertEqual((st['chats'], st['reviewed'], st['pending'], st['next']), (2, 0, 2, 'chats/chatgpt/fraud-model-talk.md'))
        seen = []
        def ask(model, question, context, schema, attempts=2):
            seen.append((model, context, schema)); return {'ok': True, 'decision': {'items': [
                {'kind': 'fact', 'title': 'Quantum kernel fraud approach', 'text': 'Feature map, then an SVM.', 'tags': ['Quantum', 'fraud'], 'messages': [2, 1]},
                {'kind': 'poem', 'title': 'Odd kind becomes fact', 'text': 'x'}, {'kind': 'idea', 'title': '', 'text': 'no title'}]}, 'elapsed_ms': 40, 'attempts': 1, 'model': model}
        out = mi.propose(self.state, st['next'], ask=ask, installed=lambda: [{'name': 'q:1b'}])
        self.assertTrue(out['ok']); self.assertEqual(out['model'], 'q:1b'); self.assertFalse(out['truncated']); self.assertEqual(seen[0][2], mi.REVIEW_SCHEMA); self.assertIn('quantum kernels', seen[0][1].lower())
        self.assertEqual(out['items'], [{'kind': 'fact', 'title': 'Quantum kernel fraud approach', 'text': 'Feature map, then an SVM.', 'tags': ['quantum', 'fraud'], 'messages': [1, 2]}, {'kind': 'fact', 'title': 'Odd kind becomes fact', 'text': 'x', 'tags': [], 'messages': []}])
        none = mi.propose(self.state, st['next'], installed=lambda: []); self.assertFalse(none['ok']); self.assertIn('No local model', none['error'])
        with self.assertRaisesRegex(mi.ImportError_, 'chat notes'): mi.propose(self.state, m.create(self.state, 'A note', 'x')['path'], installed=lambda: [])
        kept = mi.keep(self.state, st['next'], [dict(out['items'][0], title='Quantum kernels for fraud (edited)')], model='q:1b')
        self.assertEqual([w['title'] for w in kept['written']], ['Quantum kernels for fraud (edited)']); self.assertEqual((kept['review']['reviewed'], kept['review']['pending'], kept['review']['kept_total']), (1, 1, 1))
        note = m.read(self.state, kept['written'][0]['path'])
        self.assertEqual((note['kind'], note['status'], note['tags']), ('fact', 'draft', ['quantum', 'fraud'])); self.assertEqual(note['sources'], ['chat: chats/chatgpt/fraud-model-talk.md', 'chat: chats/chatgpt/fraud-model-talk.md#1', 'chat: chats/chatgpt/fraud-model-talk.md#2'])
        self.assertIn('From [[Fraud model talk]].', note['body']); self.assertEqual(note['front']['reviewed_by'], 'q:1b')
        self.assertEqual(m.graph(self.state)['edges'][0]['target'], 'chats/chatgpt/fraud-model-talk.md', 'the kept note links back to the chat')
        self.assertEqual(mi.skip(self.state, 'chats/chatgpt/code-chat.md')['pending'], 0); self.assertIsNone(mi.review_status(self.state)['next'])
        self.assertFalse('reviewed' in m.read(self.state, st['next'])['front'], 'the chat file itself is untouched; the ledger is private state')
        self.assertTrue((self.state / 'memory-review.json').is_file()); self.assertEqual(oct((self.state / 'memory-review.json').stat().st_mode & 0o777), '0o600')


from tests.test_decide import FakeOllama  # its handler reads the class attributes of this very class


class HttpTests(Vault):
    def setUp(self):
        super().setUp()
        state, key = initialize_state(self.root / 'private'); self.key = key; self.lab_state = Path(state)
        m.configure(self.lab_state, str(self.root / 'Memory2'))
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
        FakeOllama.replies = ['{"items": [{"kind": "procedure", "title": "Quantum kernel recipe", "text": "Feature map then SVM.", "tags": ["quantum"], "messages": [2]}]}']; FakeOllama.seen = []
        self.ollama = HTTPServer(('127.0.0.1', 0), FakeOllama); threading.Thread(target=self.ollama.serve_forever, daemon=True).start()
        self.port_patch = patch.object(models, 'OLLAMA_PORT', self.ollama.server_port); self.port_patch.start()
    def tearDown(self):
        self.port_patch.stop(); self.ollama.shutdown(); self.ollama.server_close(); FakeOllama.seen = []
        self.server.shutdown(); self.server.server_close(); self.thread.join(); super().tearDown()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def test_inspect_import_review_routes(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin}); auth = {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}; cookie = {'Cookie': auth['Cookie']}
        z = self.zip_of(chatgpt_rows())
        self.assertEqual(self.req('/api/memory/import/inspect', {'path': '/nope.zip'}, auth)[0], 400)
        info = self.req('/api/memory/import/inspect', {'path': z}, auth)[1]; self.assertEqual((info['source'], info['conversations']), ('chatgpt', 2))
        self.assertEqual(self.req('/api/memory/import', {'path': z}, auth)[0], 400, 'confirm required')
        out = self.req('/api/memory/import', {'path': z, 'confirm': True}, auth)[1]; self.assertEqual((out['imported'], out['skipped']), (2, 0))
        self.assertEqual(self.req('/api/memory/review', headers=cookie)[1]['pending'], 2)
        status, prop, _ = self.req('/api/memory/review/propose', {}, auth); self.assertEqual((status, prop['ok'], prop['model'], prop['items'][0]['title']), (200, True, 'llama3.2:3b', 'Quantum kernel recipe'))
        self.assertIn('Fraud model talk', FakeOllama.seen[-1]['messages'][-1]['content']); self.assertEqual(FakeOllama.seen[-1]['format']['properties']['items']['items']['type'], 'object')
        kept = self.req('/api/memory/review/keep', {'path': prop['chat']['path'], 'items': prop['items'], 'model': prop['model'], 'confirm': True}, auth)[1]
        self.assertEqual(kept['written'][0]['kind'], 'procedure'); self.assertEqual(kept['review']['pending'], 1)
        self.assertEqual(self.req('/api/memory/review/skip', {'path': 'chats/chatgpt/code-chat.md'}, auth)[1]['pending'], 0)
        self.assertEqual(self.req('/api/memory/review/propose', {}, auth)[0], 409, 'nothing left to review')
        kinds = [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]
        for k in ('memory.import', 'memory.review.propose', 'memory.review.keep', 'memory.review.skip'): self.assertIn(k, kinds)
        self.assertEqual([n['kind'] for n in self.req('/api/memory/notes?tier=notes', headers=cookie)[1]['notes']], ['procedure'])


if __name__ == '__main__':
    unittest.main()
