"""The trigger engine: conditional arrows, loop arrows with budgets, idle cells, step cap.

Every test runs the real kernel as a local process (no Docker); the HTTP tests run a real
LabServer with a local kernel factory, so the journal lines and the review check are exercised.
"""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_lab import graph as g, graph_run as graph_run_module
from local_lab.graph_run import GraphRun
from local_lab.kernel_session import KernelSession
from local_lab.runner import KERNEL_DIR
from local_lab.server import LabServer, initialize_state


def document(cells, edges):
    """cells: [(id, source)] chained only by the edges given; edges: [(id, source, target, extra)]."""
    doc = g.new_document()
    doc['notebook']['cells'] = [{'id': ident, 'cell_type': 'code', 'metadata': {'mainaiger': {'title': ident.capitalize()}}, 'source': src, 'execution_count': None, 'outputs': []} for ident, src in cells]
    doc['graph']['nodes'] = [{'id': 'start', 'kind': 'start'}] + [{'id': ident, 'kind': 'code', 'cell_index': i} for i, (ident, _) in enumerate(cells)] + [{'id': 'finish', 'kind': 'finish'}]
    doc['graph']['edges'] = [dict({'id': ident, 'kind': 'control', 'source': s, 'target': t}, **(extra or {})) for ident, s, t, extra in edges]
    doc['layout'] = {'nodes': {}, 'viewport': {'x': 0, 'y': 0, 'zoom': 1}}
    return g.validate(doc)


def branches():
    """pick sets x; x==1 takes 'left', x==2 takes 'right'; both lead to 'merge'."""
    return document([('pick', 'x = 1\nprint("picked", x)'), ('left', 'print("left ran")'), ('right', 'print("right ran")'), ('merge', 'print("merged")')],
                    [('s', 'start', 'pick', None), ('l', 'pick', 'left', {'when': 'x == 1'}), ('r', 'pick', 'right', {'when': 'x == 2'}),
                     ('lm', 'left', 'merge', None), ('rm', 'right', 'merge', None), ('f', 'merge', 'finish', None)])


def counter(when='n < 5', max_fires=3, after='print("after", n)'):
    """init sets n=0 -> bump adds one -> after; a loop arrow after -> bump runs the body (bump, after) again."""
    return document([('init', 'n = 0\nlog = []'), ('bump', 'n += 1\nlog.append(n)\nprint("n =", n)'), ('after', after)],
                    [('s', 'start', 'init', None), ('ib', 'init', 'bump', None), ('ba', 'bump', 'after', None), ('af', 'after', 'finish', None),
                     ('back', 'after', 'bump', {'loop': True, 'max_fires': max_fires, 'when': when})])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.kernel = KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')]); self.kernel.start(timeout=20)
    def tearDown(self):
        self.kernel.stop()
    def run_graph(self, doc, mode='all', selected=(), **kw):
        plan = g.plan(doc, mode, selected)
        decisions = []
        run = GraphRun(doc, plan, self.kernel, mode=mode, on_result=lambda i, r: g.record_result(doc, i, r), on_edge=lambda e, info: decisions.append((e['id'], info['state'], info.get('fires'), info.get('value'))), **kw).start()
        run.wait(60); snap = run.snapshot(); snap['decisions'] = decisions
        return snap
    def states(self, snap):
        return {k: v['status'] for k, v in snap['nodes'].items()}
    def edges(self, snap):
        return {k: v['state'] for k, v in snap['edges'].items()}

    def test_condition_picks_a_branch_and_the_other_side_stays_idle(self):
        snap = self.run_graph(branches())
        self.assertEqual(snap['state'], 'finished')
        self.assertEqual(self.states(snap), {'pick': 'ok', 'left': 'ok', 'right': 'idle', 'merge': 'ok'})
        self.assertEqual(self.edges(snap), {'s': 'fired', 'l': 'fired', 'r': 'not_fired', 'lm': 'fired', 'rm': 'idle', 'f': 'fired'})
        self.assertEqual(snap['fired'], ['s', 'l', 'lm', 'f']); self.assertEqual(snap['steps'], 3)
        self.assertEqual(snap['edges']['l']['value'], 'True'); self.assertEqual(snap['edges']['r']['value'], 'False')
        self.assertEqual(snap['decisions'], [('l', 'fired', 1, 'True'), ('r', 'not_fired', 0, 'False')], 'only arrows with a condition or a loop are reported')
        self.assertEqual(snap['nodes']['right']['note'], 'No arrow into this cell fired.'); self.assertEqual(snap['nodes']['merge']['output'], 'merged\n')
        self.assertEqual(self.kernel.execute('x')['count'], 4, 'three cells ran; deciding two conditions did not advance the count')

    def test_no_branch_taken_leaves_everything_behind_idle(self):
        doc = branches(); doc['notebook']['cells'][0]['source'] = 'x = 3'
        snap = self.run_graph(doc)
        self.assertEqual((snap['state'], self.states(snap)), ('finished', {'pick': 'ok', 'left': 'idle', 'right': 'idle', 'merge': 'idle'}))
        self.assertEqual(self.edges(snap)['f'], 'idle')

    def test_loop_runs_its_body_until_the_condition_is_false(self):
        snap = self.run_graph(counter(when='n < 3', max_fires=10))
        self.assertEqual(snap['state'], 'finished')
        self.assertEqual(self.states(snap), {'init': 'ok', 'bump': 'ok', 'after': 'ok'})
        self.assertEqual(snap['steps'], 7, 'init once, then bump+after three times (two rewinds)')
        self.assertEqual(snap['fired'].count('back'), 2); self.assertEqual(snap['edges']['back'], {'state': 'not_fired', 'fires': 2, 'source': 'after', 'target': 'bump', 'loop': True, 'when': 'n < 3', 'max_fires': 10, 'value': 'False'})
        self.assertEqual(snap['nodes']['bump']['output'], 'n = 3\n'); self.assertEqual(snap['nodes']['bump']['passes'], 2); self.assertEqual(snap['nodes']['after']['passes'], 2)
        self.assertNotIn('passes', snap['nodes']['init'])
        self.assertEqual(self.kernel.execute('log')['output'], '[1, 2, 3]\n')
        self.assertEqual([d for d in snap['decisions'] if d[0] == 'back'], [('back', 'fired', 1, 'True'), ('back', 'fired', 2, 'True'), ('back', 'not_fired', 2, 'False')])

    def test_loop_budget_stops_the_loop_and_the_run_carries_on(self):
        snap = self.run_graph(counter(when='n < 100', max_fires=3))
        self.assertEqual((snap['state'], snap['steps']), ('finished', 9), 'init once, body four times: the first pass plus three rewinds')
        self.assertEqual(snap['edges']['back']['state'], 'exhausted'); self.assertEqual(snap['edges']['back']['fires'], 3)
        self.assertEqual(snap['nodes']['after']['output'], 'after 4\n'); self.assertEqual(snap['edges']['af']['state'], 'fired')
        self.assertEqual(snap['decisions'][-1], ('back', 'exhausted', 3, 'True'))

    def test_loop_without_condition_uses_its_budget_exactly(self):
        doc = counter(max_fires=2); doc['graph']['edges'][-1].pop('when'); doc = g.validate(doc)
        snap = self.run_graph(doc)
        self.assertEqual((snap['state'], snap['steps'], snap['edges']['back']['state']), ('finished', 7, 'exhausted'))

    def test_condition_error_stops_the_run_and_names_the_arrow(self):
        doc = branches(); doc['graph']['edges'][1]['when'] = 'missing_name == 1'; doc = g.validate(doc)
        snap = self.run_graph(doc)
        self.assertEqual(snap['state'], 'failed'); self.assertIn('Pick → Left', snap['error']); self.assertIn('NameError', snap['error'])
        self.assertEqual(self.states(snap), {'pick': 'ok', 'left': 'skipped', 'right': 'skipped', 'merge': 'skipped'})
        self.assertEqual(snap['edges']['l']['state'], 'error'); self.assertIn('missing_name', snap['edges']['l']['error'])

    def test_condition_that_is_not_true_or_false_uses_truthiness(self):
        doc = branches(); doc['graph']['edges'][1]['when'] = '[x]'; doc['graph']['edges'][2]['when'] = '""'; doc = g.validate(doc)
        snap = self.run_graph(doc)
        self.assertEqual(self.edges(snap)['l'], 'fired'); self.assertEqual(self.edges(snap)['r'], 'not_fired'); self.assertEqual(snap['edges']['l']['value'], '1 items')

    def test_cancel_inside_a_loop_marks_the_rest_cancelled(self):
        doc = counter(when='n < 100', max_fires=50, after='import time\nif n == 2: time.sleep(20)\nprint("after", n)')
        plan = g.plan(doc, 'all'); run = GraphRun(doc, plan, self.kernel).start()
        for _ in range(400):
            if run.snapshot()['steps'] >= 5 and run.current == 'after': break
            time.sleep(0.05)
        run.cancel(); self.assertEqual(run.wait(30), 'cancelled')
        snap = run.snapshot(); self.assertEqual(snap['nodes']['after']['status'], 'interrupted'); self.assertEqual(snap['nodes']['bump']['status'], 'ok')

    def test_step_cap_is_the_last_line_of_defence(self):
        with patch.object(graph_run_module, 'MAX_STEPS', 4):
            snap = self.run_graph(counter(when='True', max_fires=100))
        self.assertEqual(snap['state'], 'failed'); self.assertIn('4 cell executions', snap['error']); self.assertEqual(snap['steps'], 4)
        self.assertIn('skipped', self.states(snap).values())

    def test_run_from_inside_a_loop_blocks_the_loop_arrow(self):
        doc = counter(when='True', max_fires=3)
        self.kernel.execute('n = 0\nlog = []')
        snap = self.run_graph(doc, mode='from', selected=['after'])
        self.assertEqual((snap['state'], snap['steps']), ('finished', 1)); self.assertEqual(snap['edges']['back']['state'], 'blocked', 'the loop leads back to a cell outside this run')
        self.assertNotIn('s', snap['edges'], 'arrows from Start are not decided unless the whole graph runs')

    def test_run_from_the_loop_target_runs_the_loop(self):
        doc = counter(when='n < 3', max_fires=5)
        self.kernel.execute('n = 0\nlog = []')
        snap = self.run_graph(doc, mode='from', selected=['bump'])
        self.assertEqual((snap['state'], snap['steps']), ('finished', 6)); self.assertEqual(self.kernel.execute('log')['output'], '[1, 2, 3]\n')

    def test_model_cell_inside_a_loop_sees_the_fresh_output_each_pass(self):
        # A loop around a model cell: the judge reads what the body printed in *this* pass.
        seen = []
        def fake(cfg, prompt):
            seen.append(prompt); return {'ok': True, 'value': {'again': 'n = 3' not in prompt}, 'model': cfg['model']}
        doc = document([('init', 'n = 0'), ('bump', 'n += 1\nprint("n =", n)'), ('judge', 'Body said:\n{{output:Bump}}'), ('done', 'print("done at", n)')],
                       [('s', 'start', 'init', None), ('ib', 'init', 'bump', None), ('bj', 'bump', 'judge', None), ('jd', 'judge', 'done', None), ('df', 'done', 'finish', None),
                        ('back', 'judge', 'bump', {'loop': True, 'max_fires': 10, 'when': 'verdict["again"]'})])
        doc['notebook']['cells'][2]['metadata']['mainaiger']['model'] = {'model': 'fake:1b', 'output': 'verdict'}; doc = g.validate(doc)
        snap = self.run_graph(doc, ask_model=fake)
        self.assertEqual(snap['state'], 'finished'); self.assertEqual([p.splitlines()[-1] for p in seen], ['n = 1', 'n = 2', 'n = 3'])
        self.assertEqual(snap['nodes']['done']['output'], 'done at 3\n'); self.assertEqual(snap['edges']['back']['fires'], 2)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
        from tests.test_sessions import local_factory
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'), kernel_factory=local_factory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.home.stop(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}
    def wait_run(self, cookie):
        for _ in range(600):
            run = self.req('/api/graph/run', headers=cookie)[1]['run']
            if run['state'] not in ('pending', 'running'): return run
            time.sleep(0.05)
        self.fail('run did not finish')
    def test_review_names_loops_and_conditions_and_the_journal_records_decisions(self):
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        doc = counter(when='n < 3', max_fires=5)
        self.assertEqual(self.req('/api/graph', {'document': doc}, auth)[0], 200)
        bad = json.loads(json.dumps(doc)); bad['graph']['edges'][-1]['when'] = 'n <'
        status, data, _ = self.req('/api/graph', {'document': bad}, auth); self.assertEqual(status, 400); self.assertIn('not a Python expression', data['error'])
        check = self.req('/api/graph/preflight', {'mode': 'all', 'nodes': [], 'include_prerequisites': False}, auth)[1]
        self.assertEqual(check['loops'], [{'id': 'back', 'source': 'after', 'target': 'bump', 'source_title': 'After', 'target_title': 'Bump', 'max_fires': 5, 'when': 'n < 3', 'inside': True}]); self.assertEqual(check['conditions'], [])
        check = self.req('/api/graph/preflight', {'mode': 'from', 'nodes': ['after'], 'include_prerequisites': False}, auth)[1]; self.assertEqual(check['loops'][0]['inside'], False)
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; self.req('/api/sessions/start', {'confirm': True, 'revision': rev}, auth)
        self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); run = self.wait_run(cookie)
        self.assertEqual((run['state'], run['steps'], run['edges']['back']['fires'], run['edges']['back']['state']), ('finished', 7, 2, 'not_fired'))
        entries = self.req('/api/journal', headers=cookie)[1]['entries']; arrows = [e for e in entries if e['kind'].startswith('arrow.')]
        self.assertEqual([a['kind'] for a in arrows], ['arrow.fired', 'arrow.fired', 'arrow.not_fired']); self.assertIn('Loop After → Bump fired: running the body again (1/5) · when n < 3 → True', [a['text'] for a in arrows])
        finished = next(e for e in entries if e['kind'] == 'run.finished'); self.assertIn('7 steps', finished['text'])
        view = self.req('/api/graph', headers=cookie)[1]; self.assertEqual(view['document']['notebook']['cells'][1]['outputs'][0]['text'], 'n = 3\n', 'the last pass is what the notebook keeps')


if __name__ == '__main__':
    unittest.main()
