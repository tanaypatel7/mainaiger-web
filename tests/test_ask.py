"""Costs (measured rates, estimates, watts) and the memory-first ask pipeline (plan, reuse, answer)."""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

from local_lab import ask, costs, memory as m, models
from local_lab.server import LabServer, initialize_state
from tests.test_decide import FakeOllama


class CostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.state = Path(self.tmp.name) / 'state'; self.state.mkdir(mode=0o700)
    def tearDown(self):
        self.tmp.cleanup()
    def test_defaults_then_measured_rates_and_watts(self):
        e = costs.estimate(self.state, 'llama3.2:3b', 4000, 200)
        self.assertEqual((e['basis'], e['prompt_tokens'], e['reply_tokens'], e['tokens'], e['watt_hours']), ('default', 1001, 200, 1201, None)); self.assertAlmostEqual(e['seconds'], 1001 / 250 + 200 / 20, places=1)
        self.assertIsNone(costs.record(self.state, {'ok': False})); self.assertIsNone(costs.record(self.state, {'ok': True, 'model': 'm', 'elapsed_ms': 10, 'tokens': {}}), 'no token count, nothing learned')
        for _ in range(3): costs.record(self.state, {'ok': True, 'model': 'llama3.2:3b', 'elapsed_ms': 2000, 'tokens': {'prompt': 100, 'reply': 80}})
        gen, prompt_tps, basis = costs.rates(self.state, 'llama3.2:3b'); self.assertEqual(basis, 'measured'); self.assertGreater(gen, 30); self.assertLess(gen, 45); self.assertEqual(prompt_tps, gen * 10)
        e = costs.estimate(self.state, 'llama3.2:3b', 4000, 200); self.assertEqual(e['basis'], 'measured'); self.assertLess(e['seconds'], 10)
        self.assertEqual(costs.set_watts(self.state, 30), 30.0); self.assertEqual(costs.estimate(self.state, 'llama3.2:3b', 4000, 200)['watts'], 30.0); self.assertGreater(costs.estimate(self.state, 'llama3.2:3b', 4000, 200)['watt_hours'], 0)
        for bad in (0, 5000, 'x', True):
            with self.subTest(bad=bad), self.assertRaises(ValueError): costs.set_watts(self.state, bad)
        self.assertIsNone(costs.set_watts(self.state, None))
        s = costs.summary(self.state); self.assertEqual((s['watts'], s['models']['llama3.2:3b']['calls'], s['models']['llama3.2:3b']['basis']), (None, 3, 'measured'))
        self.assertEqual(oct((self.state / 'model-stats.json').stat().st_mode & 0o777), '0o600')
        self.assertEqual(costs.estimate(self.state, None, 100)['basis'], 'default')


class AskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve(); self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start(); m.configure(self.state, str(self.root / 'Memory'))
    def tearDown(self):
        self.home.stop(); self.tmp.cleanup()
    def test_plan_prefers_a_stable_close_match_and_estimates_regeneration(self):
        stable = m.create(self.state, 'Quantum kernels for fraud', 'Feature map, then an SVM on the kernel matrix.', status_='stable', tags=['quantum'])
        draft = m.create(self.state, 'Fraud dataset notes', 'Credit card transactions, 0.17% positives.')
        plan = ask.plan(self.state, 'what do I know about quantum kernels for fraud?', installed=lambda: [{'name': 'llama3.2:3b'}])
        self.assertEqual((plan['hits'][0]['path'], plan['recommendation'], plan['model']), (stable['path'], 'reuse', 'llama3.2:3b')); self.assertIn('stable note matches', plan['why'])
        self.assertEqual(plan['reuse']['path'], stable['path']); self.assertEqual(plan['regenerate']['basis'], 'default'); self.assertGreater(plan['regenerate']['tokens'], 100); self.assertEqual(plan['use'][0], stable['path']); self.assertFalse(plan['auto'])
        plan = ask.plan(self.state, 'how many positives are in the fraud dataset', installed=lambda: [{'name': 'llama3.2:3b'}])
        self.assertEqual((plan['hits'][0]['path'], plan['recommendation']), (draft['path'], 'regenerate')); self.assertIn('not stable', plan['why'])
        plan = ask.plan(self.state, 'tell me about haptics gloves', installed=lambda: [{'name': 'llama3.2:3b'}]); self.assertEqual((plan['hits'], plan['recommendation']), ([], 'regenerate'))
        plan = ask.plan(self.state, 'tell me about haptics gloves', installed=lambda: []); self.assertEqual((plan['recommendation'], plan['model'], plan['regenerate']), ('none', None, None))
        plan = ask.plan(self.state, 'the fraud dataset positives', installed=lambda: []); self.assertEqual(plan['recommendation'], 'reuse'); self.assertIn('no local model', plan['why'])
        m.create(self.state, 'Answer style', 'Prefer the smallest model. Cite the note used.', kind='rule', tags=['ask', 'auto-reuse'])
        plan = ask.plan(self.state, 'quantum kernels for fraud', installed=lambda: [{'name': 'llama3.2:3b'}]); self.assertEqual([r['title'] for r in plan['rules']], ['Answer style']); self.assertTrue(plan['auto'])
        with self.assertRaises(ask.AskError): ask.plan(self.state, '   ')
        with self.assertRaises(ask.AskError): ask.plan(self.state, 'x', model='Bad Name')
    def test_reuse_and_answer_with_a_scripted_model(self):
        note = m.create(self.state, 'Quantum kernels for fraud', 'Feature map, then an SVM.', status_='stable')
        chat_note = m.create(self.state, 'A chat', 'raw', tier='chats', subfolder='claude')
        r = ask.reuse(self.state, 'quantum kernels?', note['path']); self.assertEqual((r['note']['title'], r['cost']['tokens']), ('Quantum kernels for fraud', 0))
        with self.assertRaisesRegex(ask.AskError, 'not raw chats'): ask.reuse(self.state, 'q', chat_note['path'])
        m.create(self.state, 'Style', 'Never explain what an SVM is.', kind='rule', tags=['ask'])
        seen = []
        def chat(model, messages, timeout=300, format=None):
            seen.append(messages); return {'ok': True, 'model': model, 'reply': 'Feature map, then SVM. Nothing on tuning in memory.', 'elapsed_ms': 1500, 'tokens': {'prompt': 300, 'reply': 20}}
        out = ask.answer(self.state, 'How do I use quantum kernels for fraud?', 'llama3.2:3b', use=[note['path'], chat_note['path'], 'notes/nope.md'], chat=chat)
        self.assertTrue(out['ok']); self.assertEqual(out['used'], [note['path']]); self.assertEqual(out['tokens']['reply'], 20); self.assertIsNone(out['watt_hours'])
        self.assertIn('fewest words', seen[0][0]['content']); self.assertIn('House rules from the owner:\n- Never explain what an SVM is.', seen[0][0]['content'])
        self.assertIn('### Quantum kernels for fraud (stable)\nFeature map, then an SVM.', seen[0][1]['content']); self.assertTrue(seen[0][1]['content'].endswith('Question: How do I use quantum kernels for fraud?'))
        saved = m.read(self.state, out['note']['path'])
        self.assertEqual((saved['kind'], saved['status'], saved['title'], saved['tags']), ('answer', 'draft', 'Q: How do I use quantum kernels for fraud', ['ask'])); self.assertEqual(saved['sources'], ['ask: llama3.2:3b', 'note: ' + note['path']])
        self.assertTrue(saved['body'].startswith('Feature map, then SVM.')); self.assertIn('## Notes used\n\n- [[Quantum kernels for fraud]]', saved['body']); self.assertEqual(saved['front']['model'], 'llama3.2:3b')
        self.assertEqual(costs.rates(self.state, 'llama3.2:3b')[2], 'default', 'one short call is not enough to call the rate measured'); self.assertEqual(costs.summary(self.state)['models']['llama3.2:3b']['calls'], 1)
        failed = ask.answer(self.state, 'q', 'llama3.2:3b', chat=lambda *a, **k: {'ok': False, 'output': 'Ollama did not answer'}); self.assertFalse(failed['ok']); self.assertIn('did not answer', failed['error']); self.assertIsNone(failed['note'])
        with self.assertRaises(ask.AskError): ask.answer(self.state, 'q', 'Bad Name', chat=chat)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key; self.state = Path(state)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start(); m.configure(self.state, str(self.root / 'Memory'))
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
        FakeOllama.replies = ['Feature map, then SVM.']; FakeOllama.seen = []
        self.ollama = HTTPServer(('127.0.0.1', 0), FakeOllama); threading.Thread(target=self.ollama.serve_forever, daemon=True).start()
        self.port_patch = patch.object(models, 'OLLAMA_PORT', self.ollama.server_port); self.port_patch.start()
    def tearDown(self):
        self.port_patch.stop(); self.ollama.shutdown(); self.ollama.server_close(); FakeOllama.seen = []
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.home.stop(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def test_plan_reuse_answer_costs_and_journal(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin}); auth = {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}; cookie = {'Cookie': auth['Cookie']}
        self.req('/api/memory/note', {'title': 'Quantum kernels for fraud', 'body': 'Feature map, then an SVM.', 'status': 'stable'}, auth)
        self.assertEqual(self.req('/api/memory/costs/watts', {'watts': 30}, auth)[1]['watts'], 30.0); self.assertEqual(self.req('/api/memory/costs/watts', {'watts': 9999}, auth)[0], 400)
        plan = self.req('/api/memory/ask/plan', {'question': 'quantum kernels for fraud?'}, auth)[1]
        self.assertEqual((plan['recommendation'], plan['model'], plan['regenerate']['watts']), ('reuse', 'llama3.2:3b', 30.0)); self.assertGreater(plan['regenerate']['watt_hours'], 0)
        reused = self.req('/api/memory/ask/reuse', {'question': 'quantum kernels for fraud?', 'path': plan['hits'][0]['path']}, auth)[1]; self.assertEqual(reused['note']['title'], 'Quantum kernels for fraud'); self.assertEqual(FakeOllama.seen, [], 'no model ran')
        self.assertEqual(self.req('/api/memory/ask', {'question': 'q', 'model': 'llama3.2:3b', 'use': []}, auth)[0], 400, 'confirm required')
        status, out, _ = self.req('/api/memory/ask', {'question': 'How do I use quantum kernels for fraud?', 'model': 'llama3.2:3b', 'use': plan['use'], 'confirm': True}, auth)
        self.assertEqual((status, out['ok'], out['reply'], out['used']), (200, True, 'Feature map, then SVM.', [plan['hits'][0]['path']])); self.assertIsNotNone(out['watt_hours']); self.assertEqual(out['note']['kind'], 'answer')
        self.assertIn('Question: How do I use quantum kernels for fraud?', FakeOllama.seen[-1]['messages'][-1]['content']); self.assertIn('fewest words', FakeOllama.seen[-1]['messages'][0]['content'])
        c = self.req('/api/memory/costs', headers=cookie)[1]; self.assertEqual(c['models']['llama3.2:3b']['calls'], 2, 'the plan did not call the model; the answer did, and the fake answers the installed-models probe too'); self.assertEqual(c['watts'], 30.0)
        kinds = [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]
        for k in ('memory.ask.plan', 'memory.ask.reuse', 'memory.ask', 'memory.costs.watts'): self.assertIn(k, kinds)
        # Every model call through the Lab is measured, the Models page chat included.
        self.req('/api/models/chat', {'model': 'llama3.2:3b', 'messages': [{'role': 'user', 'content': 'hi'}]}, auth); self.assertEqual(self.req('/api/memory/costs', headers=cookie)[1]['models']['llama3.2:3b']['calls'], 3)


if __name__ == '__main__':
    unittest.main()
