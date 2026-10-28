"""Nested Mainbooks: a cell that runs a saved Mainbook, whole, in the same session.

Engine tests run the real kernel as a local process with an in-memory loader; HTTP tests save
Mainbooks into a configured folder and run through a real LabServer.
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

from local_lab import graph as g, graph_run as graph_run_module, mainbooks
from local_lab.graph_run import GraphRun
from local_lab.kernel_session import KernelSession
from local_lab.runner import KERNEL_DIR
from local_lab.server import LabServer, initialize_state
from tests.test_triggers import document


def chain(cells, name=None, extra_edges=()):
    """start -> cells in order -> finish, as a validated document, optionally named."""
    ids = [c[0] for c in cells]
    edges = [('s', 'start', ids[0], None)] + [('e%d' % i, a, b, None) for i, (a, b) in enumerate(zip(ids, ids[1:]))] + [('f', ids[-1], 'finish', None)] + list(extra_edges)
    doc = document(cells, edges)
    if name:
        doc['notebook']['metadata']['mainaiger'] = {'name': name}
    return g.validate(doc)


def book_cell(ident, name, **own):
    return (ident, '')  # the source is ignored; metadata is set by with_book()


def with_book(doc, ident, name):
    node = g.node_by_id(doc, ident)
    doc['notebook']['cells'][node['cell_index']]['metadata']['mainaiger']['mainbook'] = {'name': name}
    return g.validate(doc)


class ModelTests(unittest.TestCase):
    def test_validation_and_analysis_through_the_inner_mainbook(self):
        inner = chain([('a', 'total = base * 2'), ('b', 'print(total)')], name='double')
        outer = with_book(chain([('setup', 'base = 21'), ('run', ''), ('after', 'print(total + 1)')]), 'run', 'double')
        self.assertEqual(g.mainbook_cells(outer), [('run', 'double')])
        plain = g.analysis(outer)['run']; self.assertEqual((plain['inputs'], plain['outputs'], plain['mainbook'], plain['resolved']), ([], [], 'double', False))
        seen = g.analysis(outer, lambda name: inner if name == 'double' else None)['run']
        self.assertEqual((seen['inputs'], seen['outputs'], seen['resolved'], seen['cells']), (['base'], ['total'], True, 2), 'the cell reads what the Mainbook needs and defines what it produces')
        edges = {(e['source'], e['target'], e['name']) for e in g.data_edges(outer, lambda name: inner)}
        self.assertIn(('setup', 'run', 'base'), edges); self.assertIn(('run', 'after', 'total'), edges)
        self.assertEqual(g.preflight(outer, g.plan(outer, 'all'), (), lambda name: inner)['missing'], [])
        missing = g.analysis(outer, lambda name: None)['run']; self.assertIn('not found', missing['error'])
        raising = g.analysis(outer, lambda name: (_ for _ in ()).throw(ValueError('broken file')))['run']; self.assertIn('broken file', raising['error'])
        for bad, message in (({'name': '../x'}, 'mainbook must be'), ({'name': 'x', 'extra': 1}, 'mainbook must be'), ('double', 'mainbook must be')):
            with self.subTest(bad=bad), self.assertRaisesRegex(g.GraphError, message):
                d = chain([('setup', 'base = 21'), ('run', '')]); g.node_by_id(d, 'run'); d['notebook']['cells'][1]['metadata']['mainaiger']['mainbook'] = bad; g.validate(d)
        with self.assertRaisesRegex(g.GraphError, 'not several'):
            d = chain([('run', '')]); d['notebook']['cells'][0]['metadata']['mainaiger'].update(mainbook={'name': 'double'}, form={'fields': [{'name': 'n', 'type': 'integer'}]}); g.validate(d)
        d = chain([('run', '')]); d['notebook']['cells'][0]['metadata']['mainaiger']['mainbook'] = {}; self.assertNotIn('mainbook', g.validate(d)['notebook']['cells'][0]['metadata']['mainaiger'])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.kernel = KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')]); self.kernel.start(timeout=20)
        self.books = {}
        self.inner_results = []
    def tearDown(self):
        self.kernel.stop()
    def load(self, name):
        if name not in self.books:
            raise mainbooks.MainbookError('No Mainbook named "%s"' % name)
        return self.books[name]
    def run_graph(self, doc, mode='all', selected=(), **kw):
        run = GraphRun(doc, g.plan(doc, mode, selected), self.kernel, mode=mode, load_mainbook=self.load, on_inner=lambda parent, name, i, r: self.inner_results.append((parent, name, i, r['status'])), **kw).start()
        run.wait(60); return run.snapshot()

    def test_inner_mainbook_runs_in_the_same_session_and_shares_variables(self):
        self.books['double'] = chain([('a', 'total = base * 2'), ('b', 'print("inner says", total)')], name='double')
        outer = with_book(chain([('setup', 'base = 21'), ('run', ''), ('after', 'print("outer sees", total + 1)')]), 'run', 'double')
        snap = self.run_graph(outer)
        self.assertEqual(snap['state'], 'finished'); self.assertEqual({k: v['status'] for k, v in snap['nodes'].items()}, {'setup': 'ok', 'run': 'ok', 'after': 'ok'})
        run = snap['nodes']['run']
        self.assertEqual(run['output'].splitlines()[0], 'Mainbook "double": finished · 2/2 ok · 2 steps'); self.assertIn('B: ok', run['output']); self.assertIn('inner says 42', run['output'])
        self.assertEqual((run['sub']['name'], run['sub']['state'], run['sub']['nodes']['b']['output']), ('double', 'finished', 'inner says 42\n')); self.assertEqual(run['mainbook'], 'double'); self.assertIn('Ran Mainbook', run['note'])
        self.assertEqual(snap['nodes']['after']['output'], 'outer sees 43\n'); self.assertEqual(snap['steps'], 3, 'the outer run counts its own cells')
        self.assertEqual(self.inner_results, [('run', 'double', 'a', 'ok'), ('run', 'double', 'b', 'ok')])

    def test_inner_failure_stops_the_outer_run_with_the_reason(self):
        self.books['bad'] = chain([('a', 'x = 1'), ('b', 'raise ValueError("inner boom")')], name='bad')
        outer = with_book(chain([('run', ''), ('after', 'print("never")')]), 'run', 'bad')
        snap = self.run_graph(outer)
        self.assertEqual(snap['state'], 'failed'); self.assertEqual(snap['nodes']['run']['status'], 'error'); self.assertEqual(snap['nodes']['after']['status'], 'skipped')
        self.assertIn('B stopped the run (error): inner boom', snap['nodes']['run']['error']['evalue']); self.assertEqual(snap['nodes']['run']['sub']['nodes']['b']['status'], 'error'); self.assertIn('Run stopped the run (error): B stopped the run (error): inner boom', snap['error'])

    def test_missing_self_inclusion_and_depth_are_refused_plainly(self):
        outer = with_book(chain([('run', '')]), 'run', 'ghost')
        snap = self.run_graph(outer); self.assertEqual(snap['state'], 'failed'); self.assertIn('could not be loaded: No Mainbook named "ghost"', snap['nodes']['run']['error']['evalue'])
        self.books['d1'] = with_book(chain([('x', 'print(1)'), ('run', '')], name='d1'), 'run', 'd2')
        self.books['d2'] = with_book(chain([('run', '')], name='d2'), 'run', 'd3')
        self.books['d3'] = chain([('y', 'print(3)')], name='d3')
        outer = with_book(chain([('run', '')]), 'run', 'd1')
        snap = self.run_graph(outer); self.assertEqual(snap['state'], 'failed'); self.assertRegex(snap['nodes']['run']['output'], 'd1.*failed')
        self.assertIn('nest at most 3 deep', snap['error'], 'the deepest reason travels up through every summary')
        self.books['loop-a'] = with_book(chain([('run', '')], name='loop-a'), 'run', 'loop-b')
        self.books['loop-b'] = with_book(chain([('run', '')], name='loop-b'), 'run', 'loop-a')
        outer = with_book(chain([('run', '')]), 'run', 'loop-a')
        snap = self.run_graph(outer); self.assertIn('cannot include itself', snap['error']); self.assertIn('loop-a → loop-b → loop-a', snap['error'])

    def test_step_budget_is_shared_and_cancel_reaches_the_inner_run(self):
        self.books['many'] = chain([('a', 'n = 0'), ('b', 'n += 1'), ('c', 'print(n)')], name='many', extra_edges=[('back', 'c', 'b', {'loop': True, 'max_fires': 50})])
        outer = with_book(chain([('run', ''), ('after', 'print("after")')]), 'run', 'many')
        with patch.object(graph_run_module, 'MAX_STEPS', 6):
            snap = self.run_graph(outer)
        self.assertEqual(snap['state'], 'failed'); self.assertIn('6 cell executions', snap['nodes']['run']['error']['evalue']); self.assertEqual(snap['nodes']['run']['sub']['steps'], 5, 'one outer step plus five inner ones')
        self.books['slow'] = chain([('a', 'import time\ntime.sleep(20)\nprint("slept")'), ('b', 'print("b")')], name='slow')
        outer = with_book(chain([('run', ''), ('after', 'print("after")')]), 'run', 'slow')
        run = GraphRun(outer, g.plan(outer, 'all'), self.kernel, load_mainbook=self.load).start()
        for _ in range(600):  # wait until the inner cell is really executing (slow CI machines open a gap before the kernel starts it)
            if run.inner is not None and run.inner.current == 'a' and self.kernel.executing: break
            time.sleep(0.05)
        time.sleep(0.3)
        run.cancel(); self.assertEqual(run.wait(40), 'cancelled')
        snap = run.snapshot(); self.assertEqual(snap['nodes']['run']['status'], 'interrupted'); self.assertEqual(snap['nodes']['run']['sub']['nodes']['a']['status'], 'interrupted'); self.assertEqual(snap['nodes']['after']['status'], 'cancelled')


class MemoryCellTests(unittest.TestCase):
    """Memory cells: fixed host operations reading named session variables, binding one result."""
    def setUp(self):
        self.kernel = KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')]); self.kernel.start(timeout=20); self.ops = []
    def tearDown(self):
        self.kernel.stop()
    def memory_op(self, op, params):
        self.ops.append((op, params))
        if op == 'search': return [{'path': 'notes/a.md', 'title': 'A note about ' + params['question'], 'status': 'stable', 'snippet': 'x', 'kind': 'fact', 'tags': [], 'score': 3}]
        if op == 'read': return {'path': params['path'], 'title': 'A', 'body': 'body text'}
        if op == 'write': return 'notes/' + params['title'].lower().replace(' ', '-') + '.md'
        if op == 'ask': raise ValueError('No local model is running')
    def test_validation_analysis_run_and_failures(self):
        doc = chain([('q', 'question = "haptics"'), ('s', ''), ('use', 'use = [h["path"] for h in hits]\nprint(len(hits), use)'), ('r', ''), ('w', 'title = "Kept " + question\nbody = note["body"]'), ('out', '')])
        for ident, mem in (('s', {'op': 'search'}), ('r', {'op': 'read', 'output': 'note'}), ('out', {'op': 'write', 'output': 'where'})):
            doc['notebook']['cells'][g.node_by_id(doc, ident)['cell_index']]['metadata']['mainaiger']['memory'] = mem
        g.node_by_id(doc, 'r'); doc['notebook']['cells'][g.node_by_id(doc, 'use')['cell_index']]['source'] += '\npath = use[0]'
        doc = g.validate(doc)
        self.assertEqual(g.memory_cells(doc), [('s', 'search'), ('r', 'read'), ('out', 'write')]); self.assertEqual(g.analysis(doc)['s']['outputs'], ['hits']); self.assertEqual(g.analysis(doc)['out']['inputs'], ['title', 'body'])
        self.assertEqual(g.preflight(doc, g.plan(doc, 'all'))['missing'], [], 'data flows question → hits → use/path → note → title/body')
        run = GraphRun(doc, g.plan(doc, 'all'), self.kernel, memory_op=self.memory_op).start(); self.assertEqual(run.wait(60), 'finished', run.snapshot().get('error'))
        snap = run.snapshot(); self.assertEqual([o for o, _ in self.ops], ['search', 'read', 'write']); self.assertEqual(self.ops[0][1], {'question': 'haptics'}); self.assertEqual(self.ops[2][1]['title'], 'Kept haptics'); self.assertEqual(self.ops[2][1]['body'], 'body text')
        self.assertEqual(snap['nodes']['use']['output'], "1 ['notes/a.md']\n"); self.assertEqual(snap['nodes']['out']['output'], 'notes/kept-haptics.md'); self.assertIn('Memory write → where', snap['nodes']['out']['note'])
        self.assertEqual(self.kernel.execute('where')['output'], "'notes/kept-haptics.md'\n")
        # A missing input stops the run plainly; a refused op reports the reason; no op configured says so.
        doc2 = g.validate(chain([('s', '')])); doc2['notebook']['cells'][0]['metadata']['mainaiger']['memory'] = {'op': 'search'}; doc2 = g.validate(doc2)
        self.kernel.execute('del question')  # the session keeps variables between runs
        run = GraphRun(doc2, g.plan(doc2, 'all'), self.kernel, memory_op=self.memory_op).start(); run.wait(30); snap = run.snapshot()
        self.assertEqual(snap['state'], 'failed'); self.assertIn('needs the variable question', snap['nodes']['s']['error']['evalue'])
        doc3 = g.validate(chain([('q', 'question = "x"'), ('a', '')])); doc3['notebook']['cells'][1]['metadata']['mainaiger']['memory'] = {'op': 'ask'}; doc3 = g.validate(doc3)
        run = GraphRun(doc3, g.plan(doc3, 'all'), self.kernel, memory_op=self.memory_op).start(); run.wait(30); self.assertIn('No local model', run.snapshot()['nodes']['a']['error']['evalue'])
        run = GraphRun(doc3, g.plan(doc3, 'all'), self.kernel).start(); run.wait(30); self.assertIn('no Memory operations', run.snapshot()['nodes']['a']['error']['evalue'])
        for bad, message in (({'op': 'delete'}, 'memory must be'), ({'op': 'search', 'output': '1x'}, 'Python name'), ({'op': 'search', 'extra': 1}, 'memory must be')):
            with self.subTest(bad=bad), self.assertRaisesRegex(g.GraphError, message):
                d = chain([('s', '')]); d['notebook']['cells'][0]['metadata']['mainaiger']['memory'] = bad; g.validate(d)
        with self.assertRaisesRegex(g.GraphError, 'not several'):
            d = chain([('s', '')]); d['notebook']['cells'][0]['metadata']['mainaiger'].update(memory={'op': 'search'}, model={'model': 'x:1b', 'output': 'y'}); g.validate(d)


class HttpTests(unittest.TestCase):
    def setUp(self):
        from tests.test_sessions import local_factory
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key; self.state = Path(state)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
        mainbooks.configure(self.state, str(self.root / 'books'))
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'), kernel_factory=local_factory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.kernels.stop_all(); self.server.shutdown(); self.server.server_close(); self.thread.join(); self.home.stop(); self.tmp.cleanup()
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
    def test_review_names_the_mainbook_refuses_missing_and_runs_it(self):
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        mainbooks.save(self.state, 'double', chain([('a', 'total = base * 2'), ('b', 'print("inner", total)')], name='double'))
        outer = with_book(chain([('setup', 'base = 5'), ('run', ''), ('after', 'print("outer", total)')]), 'run', 'ghost')
        self.assertEqual(self.req('/api/graph', {'document': outer}, auth)[0], 200)
        view = self.req('/api/graph', headers=cookie)[1]; self.assertIn('not found', view['analysis']['run']['error'])
        check = self.req('/api/graph/preflight', {'mode': 'all', 'nodes': [], 'include_prerequisites': False}, auth)[1]
        self.assertEqual(check['mainbooks'], [{'node': 'run', 'name': 'ghost', 'found': False, 'cells': 0, 'self': False}]); self.assertEqual(check['missing_mainbooks'], ['ghost'])
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; self.req('/api/sessions/start', {'confirm': True, 'revision': rev}, auth)
        status, data, _ = self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); self.assertEqual(status, 400); self.assertIn('ghost', data['error'])
        outer = with_book(outer, 'run', 'double'); self.req('/api/graph', {'document': outer}, auth)
        view = self.req('/api/graph', headers=cookie)[1]; self.assertEqual((view['analysis']['run']['inputs'], view['analysis']['run']['outputs'], view['analysis']['run']['cells']), (['base'], ['total'], 2))
        self.assertIn({'source': 'run', 'target': 'after', 'name': 'total'}, [{k: e[k] for k in ('source', 'target', 'name')} for e in view['data_edges']])
        check = self.req('/api/graph/preflight', {'mode': 'all', 'nodes': [], 'include_prerequisites': False}, auth)[1]; self.assertEqual(check['missing'], []); self.assertEqual(check['mainbooks'][0]['found'], True)
        self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); run = self.wait_run(cookie)
        self.assertEqual((run['state'], run['nodes']['after']['output']), ('finished', 'outer 10\n')); self.assertIn('inner 10', run['nodes']['run']['output'])
        texts = [e['text'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]; self.assertTrue(any(t.startswith('double / a: ok') for t in texts), texts); self.assertTrue(any(t.startswith('double / b: ok') for t in texts))
        doc = self.req('/api/graph', headers=cookie)[1]['document']; self.assertIn('Mainbook "double": finished', doc['notebook']['cells'][1]['outputs'][0]['text'])
    def test_memory_cells_search_and_write_real_notes_through_the_server(self):
        from local_lab import memory
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        doc = with_book(chain([('q', 'question = "haptic glove"'), ('s', ''), ('k', 'title = "Kept: " + hits[0]["title"]\nbody = "From " + hits[0]["path"]\ntags = ["kept"]'), ('w', '')]), 'q', 'double') if False else chain([('q', 'question = "haptic glove"'), ('s', ''), ('k', 'title = "Kept: " + hits[0]["title"]\nbody = "From " + hits[0]["path"]\ntags = ["kept"]'), ('w', '')])
        doc['notebook']['cells'][1]['metadata']['mainaiger']['memory'] = {'op': 'search'}; doc['notebook']['cells'][3]['metadata']['mainaiger']['memory'] = {'op': 'write'}; doc = g.validate(doc)
        self.assertEqual(self.req('/api/graph', {'document': doc}, auth)[0], 200)
        check = self.req('/api/graph/preflight', {'mode': 'all', 'nodes': [], 'include_prerequisites': False}, auth)[1]
        self.assertEqual((check['memory'], check['memory_ready']), ([{'node': 's', 'op': 'search'}, {'node': 'w', 'op': 'write'}], False), 'no Memory folder yet')
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; self.req('/api/sessions/start', {'confirm': True, 'revision': rev}, auth)
        status, data, _ = self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); self.assertEqual(status, 400); self.assertIn('Memory folder', data['error'])
        memory.configure(self.state, str(self.root / 'Memory')); memory.create(self.state, 'Haptic glove setup', 'Calibrate each finger.', status_='stable')
        self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); run = self.wait_run(cookie)
        self.assertEqual(run['state'], 'finished', run.get('error')); self.assertIn('Haptic glove setup', run['nodes']['s']['output']); self.assertEqual(run['nodes']['w']['output'], 'notes/kept-haptic-glove-setup.md')
        note = memory.read(self.state, 'notes/kept-haptic-glove-setup.md'); self.assertEqual((note['status'], note['tags'], note['sources']), ('draft', ['kept'], ['mainbook: the open Mainbook'])); self.assertEqual(note['body'].strip(), 'From notes/haptic-glove-setup.md')
        kinds = [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]; self.assertIn('memory.cell.search', kinds); self.assertIn('memory.cell.write', kinds)


if __name__ == '__main__':
    unittest.main()
