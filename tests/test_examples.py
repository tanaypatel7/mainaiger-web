"""Example Mainbooks must load, validate, and run to completion with expected outputs.

Each example runs with the real kernel as a local process, so a broken example
fails here rather than on a user's Mac. Also covers notebook import/export.
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from local_lab import graph as g
from local_lab.graph_run import GraphRun
from local_lab.kernel_session import KernelSession
from local_lab.runner import KERNEL_DIR
from local_lab.server import LabServer, initialize_state, EXAMPLES

EXPECTED = {
    'signal-demo': {'last': 'check-signal', 'contains': 'Check passed', 'fired': 4},
    'csv-pipeline': {'last': 'report', 'contains': 'Hottest: Phoenix', 'fired': 6, 'also': {'clean': '1 row(s) dropped'}},
    'branching-stats': {'last': 'report', 'contains': 'Report complete.', 'fired': 8, 'also': {'load': '200 samples'}},
    'self-improve': {'last': 'report', 'contains': 'Self-improvement loop: 2 attempts', 'fired': 14, 'steps': 12, 'also': {'apply': 'candidate accuracy', 'report': 'last verdict: keep'}, 'model': True,
                     'loop': ('e-judge-propose', 1, 'not_fired')},
    'ask-memory': {'last': 'report', 'contains': 'model: fake:1b', 'fired': 8, 'steps': 7, 'memory': True, 'also': {'choose': 'using 1:', 'report': 'saved as: notes/q-what-do-i-know.md'}},
    'review-chats': {'last': 'report', 'contains': 'kept 0 draft note(s) from chats/b.md; 0 chat(s) still pending', 'fired': 10, 'steps': 8, 'memory': True,
                     'also': {'choose': 'chat: B | 0 proposed, keeping 0 | 0 chat(s) still pending'}, 'loop': ('e-report-review', 1, 'not_fired')},
    'self-improve-repo': {'last': 'report', 'contains': 'Repository self-improvement loop: 2 attempts', 'fired': 17, 'steps': 15, 'model': 'repo',
                          'also': {'copy': 'copied', 'baseline': 'baseline: OK', 'tests': 'tests after the change: OK', 'heldout': 'held-out tests (tests.test_graph): OK', 'report': 'git apply change.patch'},
                          'loop': ('e-judge-propose', 1, 'not_fired')},
}

BETTER_TRAIN = '''import math
weights = [0.0] * len(X_train[0]); bias = 0.0; lr = 0.1
for epoch in range(300):
    grad_w = [0.0] * len(weights); grad_b = 0.0
    for xi, yi in zip(X_train, y_train):
        z = sum(w * v for w, v in zip(weights, xi)) + bias
        p = 1 / (1 + math.exp(-max(-30, min(30, z))))
        err = p - yi
        for j, v in enumerate(xi):
            grad_w[j] += err * v
        grad_b += err
    n = len(X_train)
    weights = [w - lr * g / n for w, g in zip(weights, grad_w)]
    bias -= lr * grad_b / n
def predict(x):
    return 1 if sum(w * v for w, v in zip(weights, x)) + bias > 0 else 0
model = {"weights": weights, "bias": bias, "predict": predict}
print("candidate trained")
'''


WORSE_TRAIN = '''weights = [0.0] * len(X_train[0]); bias = 0.0
def predict(x):
    return 1 if sum(w * v for w, v in zip(weights, x)) + bias > 0 else 0
model = {"weights": weights, "bias": bias, "predict": predict}
print("candidate trained")
'''


def make_fake_model(kind=True):
    """Stands in for Ollama in CI: a bad proposal first (the loop sends the run back), then a good one;
    the judge reads the numbers (or the test result) in its prompt. Records every prompt so the test can check what the model saw."""
    seen = {'proposals': 0, 'prompts': []}
    def fake_model(cfg, prompt):
        seen['prompts'].append(prompt)
        ok = lambda value: {'ok': True, 'value': value, 'raw': '', 'error': None, 'elapsed_ms': 1, 'attempts': 1, 'model': cfg['model']}
        if kind == 'repo':
            if cfg['output'] == 'proposal':
                seen['proposals'] += 1
                if seen['proposals'] == 1:
                    return ok({'find': 'def extract_json(text):', 'replace': 'def extract_json_renamed(text):', 'why': 'Rename the function (breaks its callers: a bad idea, to show a revert).'})
                return ok({'find': 'def extract_json(text):', 'replace': '# Pulls the first JSON object out of a model reply, fenced or not.\ndef extract_json(text):', 'why': 'A one-line comment above extract_json, as the task asked.'})
            good = 'tests after the change: OK' in prompt
            return ok({'action': 'keep' if good else 'revert', 'confidence': 0.9, 'reasons': ['tests pass' if good else 'tests fail or the change did not apply']})
        if cfg['output'] == 'proposal':
            seen['proposals'] += 1
            source, why = (WORSE_TRAIN, 'Skip training: zero weights (a bad idea, to show a revert).') if seen['proposals'] == 1 else (BETTER_TRAIN, 'Train properly: ten times the learning rate and 300 epochs.')
            return ok({'source': source, 'why': why})
        import re
        delta = re.search(r'delta: ([+-][0-9.]+)', prompt)
        action = 'keep' if delta and float(delta.group(1)) >= 0.02 else ('revert' if delta and float(delta.group(1)) < 0 else 'ask')
        return ok({'action': action, 'confidence': 0.8, 'reasons': ['delta ' + (delta.group(1) if delta else 'unknown')]})
    fake_model.seen = seen
    return fake_model


def load_example(name):
    return json.loads((EXAMPLES / (name + '.ipymb')).read_text(encoding='utf-8'))


class ExampleDocumentTests(unittest.TestCase):
    def test_every_example_validates_and_is_connected(self):
        for file in sorted(EXAMPLES.glob('*.ipymb')):
            with self.subTest(example=file.name):
                doc = g.validate(json.loads(file.read_text(encoding='utf-8')))
                self.assertEqual(doc['schema_version'], 2)
                self.assertEqual(g.disconnected(doc), [], 'every code cell must be reachable from Start')
                self.assertTrue(all(c.get('metadata', {}).get('mainaiger', {}).get('title') for c in doc['notebook']['cells']), 'cells need titles')
                self.assertTrue(doc['notebook']['metadata'].get('mainaiger', {}).get('example'))
    def test_expected_examples_exist(self):
        self.assertEqual(sorted(f.stem for f in EXAMPLES.glob('*.ipymb')), sorted(EXPECTED))


class ExampleRunTests(unittest.TestCase):
    def setUp(self):
        self.kernel = KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')]); self.kernel.start(timeout=20)
    def tearDown(self):
        self.kernel.stop()
    def test_examples_run_to_completion_with_expected_output(self):
        for name, expect in EXPECTED.items():
            with self.subTest(example=name):
                kernel = KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')]); kernel.start(timeout=20)
                try:
                    doc = g.validate(load_example(name))
                    fake = make_fake_model(expect['model']) if expect.get('model') else None
                    reviews = []
                    def memory_op(op, params):
                        if op == 'review':
                            reviews.append(params)
                            if len(reviews) == 1: return {'chat': {'path': 'chats/a.md', 'title': 'A'}, 'items': [{'kind': 'fact', 'title': 'Keep me', 'text': 'A fact long enough to keep.', 'tags': [], 'messages': [1]}, {'kind': 'idea', 'title': 'Short', 'text': 'tiny', 'tags': [], 'messages': []}], 'model': 'fake:1b', 'ok': True, 'error': None, 'pending': 1}
                            return {'chat': {'path': 'chats/b.md', 'title': 'B'}, 'items': [], 'model': 'fake:1b', 'ok': True, 'error': None, 'pending': 0}
                        if op == 'keep': return {'chat': params['path'], 'written': [{'path': 'notes/kept.md', 'title': i['title'], 'kind': i['kind']} for i in params['items']], 'review': {'pending': 1 if params['path'] == 'chats/a.md' else 0}}
                        if op == 'search': return [{'path': 'notes/project.md', 'title': 'Project notes', 'status': 'stable', 'kind': 'fact', 'tags': [], 'snippet': 'x', 'score': 4}]
                        if op == 'ask': return {'reply': 'A local-first graph lab.', 'model': 'fake:1b', 'used': params.get('use', []), 'note_path': None, 'elapsed_ms': 12, 'tokens': {'reply': 6}}
                        if op == 'write': return 'notes/q-what-do-i-know.md'
                        raise ValueError(op)
                    run = GraphRun(doc, g.plan(doc, 'all'), kernel, on_result=lambda i, r: g.record_result(doc, i, r), ask_model=fake, memory_op=memory_op if expect.get('memory') else None).start()
                    self.assertEqual(run.wait(300), 'finished', run.snapshot().get('error'))  # the repository example runs a test module three times; macOS CI runners are slow
                    snap = run.snapshot()
                    self.assertTrue(all(n['status'] == 'ok' for n in snap['nodes'].values()), {k: v['status'] for k, v in snap['nodes'].items()})
                    self.assertIn(expect['contains'], snap['nodes'][expect['last']]['output'])
                    if expect.get('model') == 'repo':
                        judge_prompts = [p for p in fake.seen['prompts'] if 'Decide:' in p]
                        self.assertEqual(len(judge_prompts), 2); self.assertIn('tests after the change: FAILED', judge_prompts[0]); self.assertIn('-def extract_json(text):', judge_prompts[0]); self.assertIn('tests after the change: OK', judge_prompts[1])
                        self.assertIn('held-out tests (tests.test_graph): OK', judge_prompts[1], 'the judge sees the held-out run'); self.assertFalse(any('held-out tests (' in p for p in fake.seen['prompts'] if 'Decide:' not in p), 'the proposer never sees the held-out results (only the module name on the Task line)')
                        self.assertIn('2. tests OK, held-out OK', snap['nodes']['report']['output'])
                        self.assertIn('Rename the function', fake.seen['prompts'][2], 'the second proposal prompt carries the first attempt'); self.assertRegex(fake.seen['prompts'][0], r'\n +\d+  def extract_json\(text\):', 'the model saw the numbered file')
                        self.assertIn('1. tests FAILED', snap['nodes']['report']['output']); self.assertIn('2. tests OK', snap['nodes']['report']['output']); self.assertIn('+# Pulls the first JSON object', snap['nodes']['report']['output'])
                        self.assertNotIn('Pulls the first JSON object', (Path(__file__).resolve().parent.parent / 'local_lab' / 'decide.py').read_text(), 'the checkout is never written')
                    elif expect.get('model'):
                        judge_prompts = [p for p in fake.seen['prompts'] if 'Decide:' in p]
                        self.assertEqual(len(judge_prompts), 2); self.assertIn('delta: -0.3', judge_prompts[0]); self.assertIn('delta: +0.0', judge_prompts[1], 'the judge saw each pass\'s Apply output through {{output:Apply to copy}}')
                        self.assertIn('Skip training', fake.seen['prompts'][2], 'the second proposal prompt carries the previous attempt')
                        self.assertNotIn('Skip training', fake.seen['prompts'][0], 'the first proposal prompt has no previous attempt')
                        self.assertIn('1. accuracy 0.540 (-0.320)', snap['nodes']['report']['output']); self.assertIn('2. accuracy 0.900 (+0.040)', snap['nodes']['report']['output'])
                    if expect.get('loop'):
                        edge, fires, final = expect['loop']; self.assertEqual((snap['edges'][edge]['fires'], snap['edges'][edge]['state']), (fires, final)); self.assertEqual(snap['steps'], expect['steps'])
                    self.assertEqual(len(snap['fired']), expect['fired'])
                    for ident, text in expect.get('also', {}).items():
                        self.assertIn(text, snap['nodes'][ident]['output'])
                    self.assertEqual(g.preflight(doc, g.plan(doc, 'all'))['missing'], [], 'examples must not depend on hidden state')
                finally:
                    kernel.stop()
    def test_branching_example_reports_missing_input_when_run_alone(self):
        doc = g.validate(load_example('branching-stats'))
        check = g.preflight(doc, g.plan(doc, 'one', ['report']))
        self.assertEqual(sorted(m['name'] for m in check['missing']), ['mean', 'quantiles', 'samples', 'std'])
        self.assertEqual(check['prerequisites'], ['load', 'moments', 'quantiles'])  # histogram is not needed by report


class NotebookImportTests(unittest.TestCase):
    def test_import_keeps_ids_outputs_counts_and_builds_chain(self):
        nb = json.loads((EXAMPLES / 'signal-demo.ipynb').read_text(encoding='utf-8'))
        nb['cells'][1]['outputs'] = [{'output_type': 'stream', 'name': 'stdout', 'text': '[1.0, 2.0]\n'}]; nb['cells'][1]['execution_count'] = 7
        doc, report = g.from_notebook(nb)
        self.assertEqual([n['id'] for n in doc['graph']['nodes']], ['start', 'plan', 'load-signal', 'center-signal', 'check-signal', 'finish'])
        self.assertEqual([(e['source'], e['target']) for e in doc['graph']['edges']], [('start', 'load-signal'), ('load-signal', 'center-signal'), ('center-signal', 'check-signal'), ('check-signal', 'finish')])
        cell = doc['notebook']['cells'][1]
        self.assertEqual((cell['execution_count'], cell['outputs'][0]['text']), (7, '[1.0, 2.0]\n'))
        self.assertEqual(report['code_cells'], 3); self.assertEqual(report['notes'], 1); self.assertEqual(report['warnings'], [])
        self.assertEqual(doc['notebook']['cells'][1]['metadata']['mainaiger']['title'], 'Plan → Code → Check (1)')  # heading inherited
    def test_import_reports_hidden_state_magics_and_opaque_constructs(self):
        nb = {'nbformat': 4, 'nbformat_minor': 5, 'metadata': {}, 'cells': [
            {'cell_type': 'code', 'source': '%matplotlib inline\nimport numpy as np', 'metadata': {}, 'outputs': [], 'execution_count': None},
            {'cell_type': 'code', 'source': 'from os import *\ny = model.predict(x)', 'metadata': {}, 'outputs': [], 'execution_count': None},
            {'cell_type': 'raw', 'source': 'raw text', 'metadata': {}},
            'garbage', {'cell_type': 'code', 'id': 'cell-1', 'source': 'print(1)', 'metadata': {}, 'outputs': [], 'execution_count': None},
            {'cell_type': 'code', 'id': 'cell-1', 'source': 'print(2)', 'metadata': {}, 'outputs': [], 'execution_count': None}]}
        doc, report = g.from_notebook(nb)
        kinds = sorted(w['kind'] for w in report['warnings'])
        self.assertEqual(report['dropped'], 1); self.assertEqual(report['notes'], 1)
        self.assertIn('magic', kinds); self.assertIn('opaque', kinds); self.assertIn('input', kinds)
        self.assertEqual(sorted(report['unresolved_inputs']), ['model', 'x'])
        ids = [n['id'] for n in doc['graph']['nodes'] if n['kind'] == 'code']
        self.assertEqual(len(ids), len(set(ids)))  # duplicate id de-duplicated
        self.assertIn('cell-1', ids)
    def test_import_rejects_non_notebooks(self):
        for bad in ({'nbformat': 3, 'cells': []}, {'format': 'mainaiger'}, [], 'x'):
            with self.subTest(bad=bad), self.assertRaises(g.GraphError):
                g.from_notebook(bad)
    def test_export_round_trip_and_lossy_flag(self):
        chain = g.validate(load_example('csv-pipeline'))
        nb, info = g.to_notebook(chain)
        self.assertFalse(info['lossy']); self.assertEqual(len(nb['cells']), 6)
        back, _ = g.from_notebook(nb)
        self.assertEqual([c['id'] for c in back['notebook']['cells']], [c['id'] for c in chain['notebook']['cells']])
        branching = g.validate(load_example('branching-stats'))
        nb2, info2 = g.to_notebook(branching)
        self.assertTrue(info2['lossy'])
        order = [c['id'] for c in nb2['cells'] if c['cell_type'] == 'code']
        self.assertLess(order.index('load'), order.index('report')); self.assertLess(order.index('moments'), order.index('report'))


class ImportHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); state, key = initialize_state(Path(self.tmp.name) / 'private'); self.key = key
        self.server = LabServer(('127.0.0.1', 0), state, key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}
    def test_examples_list_fetch_import_and_export(self):
        self.assertEqual(self.req('/api/examples')[0], 401)
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        status, data, _ = self.req('/api/examples', headers=cookie)
        self.assertEqual(status, 200); self.assertEqual(sorted(e['name'] for e in data['examples']), sorted(EXPECTED))
        self.assertEqual(self.req('/examples/../etc', headers=cookie)[0], 404); self.assertEqual(self.req('/examples/nope', headers=cookie)[0], 404)
        status, data, _ = self.req('/examples/csv-pipeline', headers=cookie)
        self.assertEqual((status, data['document']['schema_version']), (200, 2))
        nb = json.loads((EXAMPLES / 'signal-demo.ipynb').read_text(encoding='utf-8'))
        status, data, _ = self.req('/api/graph/import', {'notebook': nb}, auth)
        self.assertEqual((status, data['report']['code_cells']), (200, 3))
        self.assertEqual(self.req('/api/graph/import', {'notebook': {'nbformat': 2}}, auth)[0], 400)
        status, saved, _ = self.req('/api/graph', {'document': data['document']}, auth)
        self.assertEqual(status, 200)
        status, exported, _ = self.req('/api/graph/export', headers=cookie)
        self.assertEqual((status, exported['lossy'], len(exported['notebook']['cells'])), (200, False, 4))


if __name__ == '__main__':
    unittest.main()
