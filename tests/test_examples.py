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
    'signal-demo': {'last': 'check-signal', 'contains': 'Check passed', 'fired': 3},
    'csv-pipeline': {'last': 'report', 'contains': 'Hottest: Phoenix', 'fired': 5, 'also': {'clean': '1 row(s) dropped'}},
    'branching-stats': {'last': 'report', 'contains': 'Report complete.', 'fired': 7, 'also': {'load': '200 samples'}},
}


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
                    run = GraphRun(doc, g.plan(doc, 'all'), kernel, on_result=lambda i, r: g.record_result(doc, i, r)).start()
                    self.assertEqual(run.wait(60), 'finished', run.snapshot().get('error'))
                    snap = run.snapshot()
                    self.assertTrue(all(n['status'] == 'ok' for n in snap['nodes'].values()), {k: v['status'] for k, v in snap['nodes'].items()})
                    self.assertIn(expect['contains'], snap['nodes'][expect['last']]['output'])
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
