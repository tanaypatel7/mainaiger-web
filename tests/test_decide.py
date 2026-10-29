"""Thin-slice decisions: schema, parsing, retry budget, and the route against a fake Ollama."""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from local_lab import decide, models
from local_lab.server import LabServer, initialize_state

SCHEMA = {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['keep', 'revert', 'ask']}, 'confidence': {'type': 'number'},
                                            'reasons': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3}, 'note': {'type': 'string', 'maxLength': 40}},
          'required': ['action', 'confidence']}


def scripted(replies, fail=False):
    """A chat() stand-in that returns the scripted replies in order and records what it was asked."""
    calls = []
    def chat(model, messages, timeout=300, format=None):
        calls.append({'messages': list(messages), 'format': format})
        if fail:
            return {'ok': False, 'output': 'Model x is not downloaded yet.'}
        return {'ok': True, 'model': model, 'reply': replies[min(len(calls) - 1, len(replies) - 1)], 'elapsed_ms': 10}
    chat.calls = calls
    return chat


class SchemaTests(unittest.TestCase):
    def test_check_and_validate(self):
        s = decide.check_schema(SCHEMA)
        self.assertEqual(s['required'], ['action', 'confidence'])
        self.assertIsNone(decide.validate({'action': 'keep', 'confidence': 0.9}, s))
        self.assertIsNone(decide.validate({'action': 'ask', 'confidence': 1, 'reasons': ['a'], 'note': 'x'}, s))
        for bad, reason in (({'action': 'maybe', 'confidence': 1}, 'one of'), ({'confidence': 1}, 'missing field "action"'), ({'action': 'keep', 'confidence': 'high'}, 'type number'),
                            ({'action': 'keep', 'confidence': True}, 'number'), ({'action': 'keep', 'confidence': 1, 'extra': 1}, 'unexpected'),
                            ({'action': 'keep', 'confidence': 1, 'reasons': ['a', 'b', 'c', 'd']}, 'more than 3'), ({'action': 'keep', 'confidence': 1, 'reasons': [1]}, 'only string'),
                            ({'action': 'keep', 'confidence': 1, 'note': 'x' * 41}, 'longer than 40'), ([], 'object')):
            with self.subTest(bad=bad): self.assertIn(reason, decide.validate(bad, s))
        for bad in ({}, {'type': 'array'}, {'properties': {'a b': {}}}, {'properties': {'a': {'type': 'blob'}}}, {'properties': {'a': {'enum': []}}}, {'properties': {'a': {}}, 'required': ['zzz']},
                    {'properties': {('p%d' % i): {} for i in range(21)}}):
            with self.subTest(bad=bad), self.assertRaises(decide.DecisionError): decide.check_schema(bad)
    def test_min_length_rejects_a_fence_tag_where_code_was_asked(self):
        # Seen on a Mac with llama3.2:1b: "source": "python" for a whole training cell.
        s = decide.check_schema({'type': 'object', 'properties': {'source': {'type': 'string', 'minLength': 40}}})
        self.assertIn('too short', decide.validate({'source': 'python'}, s)); self.assertIn('too short', decide.validate({'source': ' ' * 50}, s))
        self.assertIsNone(decide.validate({'source': 'x = 1\n' * 10}, s))
    def test_extract_json(self):
        self.assertEqual(decide.extract_json('{"a": 1}'), {'a': 1})
        self.assertEqual(decide.extract_json('```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(decide.extract_json('Sure! Here it is: {"a": {"b": 2}} hope this helps'), {'a': {'b': 2}})
        for bad in ('no json here', '{"a": ', '', None):
            with self.subTest(bad=bad), self.assertRaises(decide.DecisionError): decide.extract_json(bad)


class AskTests(unittest.TestCase):
    def test_first_try_passes_schema_to_the_model(self):
        chat = scripted(['{"action": "keep", "confidence": 0.8}'])
        r = decide.ask('llama3.2:3b', 'Keep or revert?', 'diff: +1 -0', SCHEMA, chat=chat)
        self.assertEqual((r['ok'], r['decision'], r['attempts']), (True, {'action': 'keep', 'confidence': 0.8}, 1))
        self.assertEqual(chat.calls[0]['format']['properties']['action']['enum'], ['keep', 'revert', 'ask'])
        self.assertIn('diff: +1 -0', chat.calls[0]['messages'][1]['content']); self.assertIn('nothing else', chat.calls[0]['messages'][0]['content'])
    def test_retry_once_with_the_reason_then_give_up(self):
        chat = scripted(['I think keep it', '{"action": "keep", "confidence": 0.5}'])
        r = decide.ask('m', 'q', '', SCHEMA, chat=chat)
        self.assertEqual((r['ok'], r['attempts']), (True, 2)); self.assertIn('not acceptable', chat.calls[1]['messages'][-1]['content']); self.assertIn('no JSON object', chat.calls[1]['messages'][-1]['content'])
        chat = scripted(['{"action": "maybe", "confidence": 1}'])
        r = decide.ask('m', 'q', '', SCHEMA, chat=chat)
        self.assertEqual((r['ok'], r['attempts']), (False, 2)); self.assertIn('one of', r['error']); self.assertIsNone(r['decision'])
        self.assertEqual(decide.ask('m', 'q', '', SCHEMA, chat=scripted(['x'], fail=True))['error'], 'Model x is not downloaded yet.')
    def test_bounds(self):
        self.assertIn('question', decide.ask('m', '', '', SCHEMA, chat=scripted(['{}']))['error'])
        self.assertIn('context', decide.ask('m', 'q', 'x' * (decide.MAX_CONTEXT + 1), SCHEMA, chat=scripted(['{}']))['error'])
        self.assertIn('schema', decide.ask('m', 'q', '', {'nope': 1}, chat=scripted(['{}']))['error'])
        chat = scripted(['bad'] * 5); decide.ask('m', 'q', '', SCHEMA, chat=chat, attempts=99); self.assertEqual(len(chat.calls), 3, 'attempts are capped at 3')


class FakeOllama(BaseHTTPRequestHandler):
    replies = ['{"action": "revert", "confidence": 0.3, "reasons": ["tests fail"]}']
    seen = []
    def log_message(self, *a): pass
    def _json(self, code, data):
        body = json.dumps(data).encode(); self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path == '/api/version': return self._json(200, {'version': '0.12.0'})
        if self.path == '/api/tags': return self._json(200, {'models': [{'name': 'llama3.2:3b', 'size': 1, 'modified_at': 'x'}]})
        self._json(404, {})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        if self.path == '/api/chat':
            FakeOllama.seen.append(body); reply = FakeOllama.replies[min(len(FakeOllama.seen) - 1, len(FakeOllama.replies) - 1)]
            return self._json(200, {'model': body['model'], 'message': {'role': 'assistant', 'content': reply}, 'eval_count': 3})
        self._json(404, {})


class DecideHttpTests(unittest.TestCase):
    def setUp(self):
        self.ollama = HTTPServer(('127.0.0.1', 0), FakeOllama); threading.Thread(target=self.ollama.serve_forever, daemon=True).start()
        self.port_patch = patch.object(models, 'OLLAMA_PORT', self.ollama.server_port); self.port_patch.start()
        self.tmp = tempfile.TemporaryDirectory(); state, key = initialize_state(Path(self.tmp.name) / 'private'); self.key = key
        self.server = LabServer(('127.0.0.1', 0), state, key); threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.ollama.shutdown(); self.ollama.server_close()
        self.port_patch.stop()
        self.tmp.cleanup(); FakeOllama.seen = []
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}
    def test_route_requires_owner_and_returns_a_checked_decision(self):
        self.assertEqual(self.req('/api/models/decide', {'model': 'llama3.2:3b', 'question': 'q', 'schema': SCHEMA})[0], 403)
        auth = self.login()
        self.assertEqual(self.req('/api/models/decide', {'model': 'llama3.2:3b'}, auth)[0], 400)
        status, data, _ = self.req('/api/models/decide', {'model': 'llama3.2:3b', 'question': 'Keep or revert?', 'context': 'tests: 1 failed', 'schema': SCHEMA}, auth)
        self.assertEqual((status, data['ok'], data['decision']['action'], data['attempts']), (200, True, 'revert', 1))
        self.assertEqual(FakeOllama.seen[0]['format']['type'], 'object', 'Ollama was asked for structured output')
        kinds = [e['kind'] for e in self.server.journal.tail()]; self.assertIn('model.decide', kinds)


if __name__ == '__main__':
    unittest.main()
