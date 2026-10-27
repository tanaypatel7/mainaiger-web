"""Graph document, analysis, scheduling and serial execution tests.

Execution tests spawn the real kernel as a local process (no Docker), so the
trigger flow — run, fire edges, skip after failure, cancel — is exercised for real.
"""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from local_lab import graph as g
from local_lab.graph_run import GraphRun
from local_lab.kernel_session import KernelSession
from local_lab.runner import KERNEL_DIR
from local_lab.server import LabServer, initialize_state


def branching_document():
    """start -> load -> normalize -> {train, inspect} -> finish, plus a disconnected note."""
    doc = g.new_document()
    cells = doc['notebook']['cells']
    cells[0].update(id='load', source='signal = [1, 2, 3]\nprint("loaded")', metadata={'mainaiger': {'title': 'Load'}})
    cells.append({'id': 'normalize', 'cell_type': 'code', 'metadata': {}, 'source': 'scale = max(signal)\nfeatures = [s / scale for s in signal]', 'execution_count': None, 'outputs': []})
    cells.append({'id': 'train', 'cell_type': 'code', 'metadata': {}, 'source': 'model = {"peak": max(features)}\nmodel', 'execution_count': None, 'outputs': []})
    cells.append({'id': 'inspect', 'cell_type': 'code', 'metadata': {}, 'source': 'print(features[:2])', 'execution_count': None, 'outputs': []})
    cells.append({'id': 'orphan', 'cell_type': 'code', 'metadata': {}, 'source': 'print("not connected")', 'execution_count': None, 'outputs': []})
    graph = doc['graph']
    graph['nodes'] = [{'id': 'start', 'kind': 'start'}, {'id': 'load', 'kind': 'code', 'cell_index': 0}, {'id': 'normalize', 'kind': 'code', 'cell_index': 1},
                      {'id': 'train', 'kind': 'code', 'cell_index': 2}, {'id': 'inspect', 'kind': 'code', 'cell_index': 3},
                      {'id': 'orphan', 'kind': 'code', 'cell_index': 4}, {'id': 'finish', 'kind': 'finish'}]
    graph['edges'] = [{'id': 'e1', 'kind': 'control', 'source': 'start', 'target': 'load'}, {'id': 'e2', 'kind': 'control', 'source': 'load', 'target': 'normalize'},
                      {'id': 'e3', 'kind': 'control', 'source': 'normalize', 'target': 'train'}, {'id': 'e4', 'kind': 'control', 'source': 'normalize', 'target': 'inspect'},
                      {'id': 'e5', 'kind': 'control', 'source': 'train', 'target': 'finish'}, {'id': 'e6', 'kind': 'control', 'source': 'inspect', 'target': 'finish'}]
    doc['layout'] = {'nodes': {}, 'viewport': {'x': 0, 'y': 0, 'zoom': 1}}
    return g.validate(doc)


class AnalysisTests(unittest.TestCase):
    def test_inputs_are_names_read_but_not_defined(self):
        result = g.analyze('import numpy as np\nx = np.linspace(0, 1, 8)\ny = f(x) + offset\nfor i in range(2): pass')
        self.assertEqual(result['inputs'], ['f', 'offset'])
        self.assertEqual(result['outputs'], ['i', 'np', 'x', 'y'])
    def test_function_params_and_comprehension_variables_are_not_inputs(self):
        result = g.analyze('def norm(v):\n    return v / scale\nfeatures = [norm(s) for s in signal]')
        self.assertEqual(result['inputs'], ['scale', 'signal'])
        self.assertEqual(result['outputs'], ['features', 'norm'])
    def test_syntax_error_is_reported_not_raised(self):
        self.assertIn('SyntaxError', g.analyze('def (')['error'])
    def test_declarations_override_analysis(self):
        doc = g.new_document()
        doc['notebook']['cells'][0]['metadata']['mainaiger'].update(inputs=['config'], outputs=['result'])
        info = g.declared(g.validate(doc), 'cell-1')
        self.assertEqual((info['inputs'], info['outputs'], info['declared']), (['config'], ['result'], True))


class DocumentTests(unittest.TestCase):
    def test_new_document_is_valid_and_v1_upgrades(self):
        doc = g.validate(g.new_document())
        self.assertEqual(doc['schema_version'], 2)
        legacy = g.new_document(); legacy['schema_version'] = 1
        self.assertEqual(g.validate(legacy)['schema_version'], 2)
    def test_structural_errors(self):
        base = g.new_document()
        broken = [
            ('two starts', lambda d: d['graph']['nodes'].append({'id': 's2', 'kind': 'start'})),
            ('edge into start', lambda d: d['graph']['edges'].append({'id': 'x', 'kind': 'control', 'source': 'cell-1', 'target': 'start'})),
            ('edge from finish', lambda d: d['graph']['edges'].append({'id': 'x', 'kind': 'control', 'source': 'finish', 'target': 'cell-1'})),
            ('unknown node', lambda d: d['graph']['edges'].append({'id': 'x', 'kind': 'control', 'source': 'cell-1', 'target': 'ghost'})),
            ('bad cell index', lambda d: d['graph']['nodes'][1].update(cell_index=7)),
            ('bad declared name', lambda d: d['notebook']['cells'][0]['metadata']['mainaiger'].update(inputs=['not a name'])),
            ('duplicate edge', lambda d: d['graph']['edges'].append({'id': 'x', 'kind': 'control', 'source': 'start', 'target': 'cell-1'})),
        ]
        for label, mutate in broken:
            doc = json.loads(json.dumps(base)); mutate(doc)
            with self.subTest(label=label), self.assertRaises(g.GraphError):
                g.validate(doc)
    def test_cycles_are_rejected(self):
        doc = branching_document()
        doc['graph']['edges'].append({'id': 'back', 'kind': 'control', 'source': 'train', 'target': 'load'})
        with self.assertRaises(g.GraphError):
            g.validate(doc)
    def test_bad_layout_entries_are_dropped_not_fatal(self):
        doc = g.new_document(); doc['layout']['nodes']['ghost'] = {'x': 1, 'y': 2}; doc['layout']['nodes']['start'] = {'x': 'no'}
        layout = g.validate(doc)['layout']['nodes']
        self.assertNotIn('ghost', layout); self.assertNotIn('start', layout); self.assertIn('cell-1', layout)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.doc = branching_document()
    def test_order_plans_and_disconnected(self):
        self.assertEqual(g.order(self.doc), ['start', 'load', 'normalize', 'train', 'inspect', 'orphan', 'finish'])
        self.assertEqual(g.plan(self.doc, 'all'), ['load', 'normalize', 'train', 'inspect'])  # orphan excluded
        self.assertEqual(g.plan(self.doc, 'from', ['normalize']), ['normalize', 'train', 'inspect'])
        self.assertEqual(g.plan(self.doc, 'one', ['train']), ['train'])
        self.assertEqual(g.disconnected(self.doc), ['orphan'])
        with self.assertRaises(g.GraphError):
            g.plan(self.doc, 'from', [])
    def test_data_edges_follow_nearest_upstream_producer(self):
        edges = {(e['source'], e['target'], e['name']) for e in g.data_edges(self.doc)}
        self.assertEqual(edges, {('load', 'normalize', 'signal'), ('normalize', 'train', 'features'), ('normalize', 'inspect', 'features')})
    def test_preflight_reports_missing_inputs_and_prerequisites(self):
        check = g.preflight(self.doc, g.plan(self.doc, 'one', ['train']))
        self.assertEqual([(m['node'], m['name'], m['producers']) for m in check['missing']], [('train', 'features', ['normalize'])])
        self.assertEqual(check['prerequisites'], ['load', 'normalize'])  # normalize needs load for `signal`
        self.assertEqual(check['unresolvable'], [])
        satisfied = g.preflight(self.doc, ['train'], present_names=['features'])
        self.assertEqual(satisfied['missing'], []); self.assertEqual(satisfied['from_session'], [{'node': 'train', 'name': 'features'}])
    def test_unresolvable_input_has_no_producer(self):
        self.doc['notebook']['cells'][2]['source'] = 'model = features + mystery'
        check = g.preflight(self.doc, ['load', 'normalize', 'train'])
        self.assertEqual([m['name'] for m in check['unresolvable']], ['mystery'])


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.kernel = KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')]); self.kernel.start(timeout=20)
        self.doc = branching_document()
    def tearDown(self):
        self.kernel.stop()
    def test_run_all_fires_edges_in_trigger_order_and_records_outputs(self):
        recorded = []
        run = GraphRun(self.doc, g.plan(self.doc, 'all'), self.kernel, on_result=lambda i, r: (recorded.append(i), g.record_result(self.doc, i, r))).start()
        self.assertEqual(run.wait(30), 'finished')
        snap = run.snapshot()
        self.assertEqual(recorded, ['load', 'normalize', 'train', 'inspect'])
        self.assertEqual({k: v['status'] for k, v in snap['nodes'].items()}, {'load': 'ok', 'normalize': 'ok', 'train': 'ok', 'inspect': 'ok'})
        self.assertEqual(snap['fired'], ['e2', 'e3', 'e4', 'e5', 'e6'])  # e1 (start->load) is the entry, never "fired" by a cell
        self.assertEqual(snap['nodes']['train']['output'], "{'peak': 1.0}\n")
        cells = {c['id']: c for c in self.doc['notebook']['cells']}
        self.assertEqual(cells['load']['outputs'][0]['text'], 'loaded\n'); self.assertEqual(cells['load']['execution_count'], 1)
        self.assertEqual(cells['inspect']['metadata']['mainaiger']['last_run']['status'], 'ok')
    def test_failure_stops_the_run_and_skips_downstream(self):
        self.doc['notebook']['cells'][1]['source'] = 'scale = max(signal)\nfeatures = undefined_name'
        run = GraphRun(self.doc, g.plan(self.doc, 'all'), self.kernel).start()
        self.assertEqual(run.wait(30), 'failed')
        statuses = {k: v['status'] for k, v in run.snapshot()['nodes'].items()}
        self.assertEqual(statuses, {'load': 'ok', 'normalize': 'error', 'train': 'skipped', 'inspect': 'skipped'})
        self.assertEqual(run.snapshot()['fired'], ['e2'])
        self.assertIn('normalize stopped the run', run.snapshot()['error'])
    def test_cancel_interrupts_current_cell_and_skips_rest(self):
        self.doc['notebook']['cells'][1]['source'] = 'import time\nwhile True: time.sleep(0.01)'
        run = GraphRun(self.doc, g.plan(self.doc, 'all'), self.kernel).start()
        deadline = time.monotonic() + 10
        while run.current != 'normalize' and time.monotonic() < deadline: time.sleep(0.02)
        time.sleep(0.2); run.cancel()
        self.assertEqual(run.wait(15), 'cancelled')
        statuses = {k: v['status'] for k, v in run.snapshot()['nodes'].items()}
        self.assertEqual(statuses, {'load': 'ok', 'normalize': 'interrupted', 'train': 'skipped', 'inspect': 'skipped'})
        self.assertTrue(self.kernel.alive)
    def test_run_one_without_inputs_fails_honestly(self):
        run = GraphRun(self.doc, ['train'], self.kernel).start()
        self.assertEqual(run.wait(30), 'failed')
        self.assertEqual(run.snapshot()['nodes']['train']['error']['ename'], 'NameError')


class GraphHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); state, key = initialize_state(Path(self.tmp.name) / 'private'); self.key = key
        factory = lambda settings, state_dir: KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local-test')
        self.server = LabServer(('127.0.0.1', 0), state, key, kernel_factory=factory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close()
        return r.status, json.loads(payload), head
    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}
    def test_graph_page_and_document_round_trip(self):
        self.assertEqual(self.req('/api/graph')[0], 401)
        auth = self.login()
        status, view, _ = self.req('/api/graph', headers={'Cookie': auth['Cookie']})
        self.assertEqual((status, view['revision'], view['order']), (200, 0, ['start', 'cell-1', 'finish']))
        status, saved, _ = self.req('/api/graph', {'document': branching_document()}, auth)
        self.assertEqual((status, saved['revision'], saved['disconnected']), (200, 1, ['orphan']))
        self.assertEqual({e['name'] for e in saved['data_edges']}, {'signal', 'features'})
        self.assertEqual(self.req('/api/graph', {'document': {'format': 'wrong'}}, auth)[0], 400)
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10); c.request('GET', '/graph', headers={'Host': f'127.0.0.1:{self.port}'})
        r = c.getresponse(); page = r.read().decode(); c.close()
        self.assertEqual(r.status, 200); self.assertIn('Graph canvas', page); self.assertIn('/graph.js', page)
    def test_preflight_run_cancel_over_http(self):
        auth = self.login()
        self.req('/api/graph', {'document': branching_document()}, auth)
        status, check, _ = self.req('/api/graph/preflight', {'mode': 'one', 'nodes': ['train']}, auth)
        self.assertEqual((status, [m['name'] for m in check['missing']], check['prerequisites'], check['session_alive']), (200, ['features'], ['load', 'normalize'], False))
        self.assertEqual(self.req('/api/graph/run', {'mode': 'one', 'nodes': ['train'], 'confirm': True}, auth)[0], 409)  # no session
        self.req('/api/kernel/start', {'confirm': True, 'revision': 0}, auth)
        status, started, _ = self.req('/api/graph/run', {'mode': 'one', 'nodes': ['train'], 'confirm': True, 'include_prerequisites': True}, auth)
        self.assertEqual((status, started['run']['plan']), (202, ['load', 'normalize', 'train']))
        for _ in range(100):
            status, data, _ = self.req('/api/graph/run', headers={'Cookie': auth['Cookie']})
            if data['run']['state'] not in ('pending', 'running'): break
            time.sleep(0.1)
        self.assertEqual(data['run']['state'], 'finished')
        self.assertEqual(data['run']['nodes']['train']['output'], "{'peak': 1.0}\n")
        status, view, _ = self.req('/api/graph', headers={'Cookie': auth['Cookie']})
        cells = {c['id']: c for c in view['document']['notebook']['cells']}
        self.assertEqual(cells['train']['execution_count'], 3)  # third cell executed in this session
        # editing is refused only while a run is active
        long_doc = view['document']; long_doc['notebook']['cells'][1]['source'] = 'import time\nwhile True: time.sleep(0.01)'
        self.assertEqual(self.req('/api/graph', {'document': long_doc}, auth)[0], 200)
        status, started, _ = self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True}, auth)
        self.assertEqual(status, 202)
        for _ in range(100):
            if self.server.graph_run.current == 'normalize': break
            time.sleep(0.05)
        self.assertEqual(self.req('/api/graph', {'document': long_doc}, auth)[0], 409)
        self.assertEqual(self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True}, auth)[0], 409)
        status, cancelled, _ = self.req('/api/graph/cancel', {}, auth)
        self.assertEqual(status, 200)
        for _ in range(100):
            status, data, _ = self.req('/api/graph/run', headers={'Cookie': auth['Cookie']})
            if data['run']['state'] not in ('pending', 'running'): break
            time.sleep(0.1)
        self.assertEqual(data['run']['state'], 'cancelled')
        self.assertEqual(data['run']['nodes']['train']['status'], 'skipped')


if __name__ == '__main__':
    unittest.main()
