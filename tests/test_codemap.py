"""The code map: the Lab's own package as nodes and edges, read with ast."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from local_lab import codemap
from local_lab.server import LabServer, initialize_state

PACKAGE = Path(__file__).resolve().parent.parent / 'local_lab'


class ScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.map = codemap.scan(PACKAGE)
        cls.ids = {n['id']: n for n in cls.map['nodes']}
        cls.edges = {(e['source'], e['target'], e['kind']) for e in cls.map['edges']}
    def test_modules_classes_functions_and_methods(self):
        self.assertIn('mod:server.py', self.ids); self.assertIn('def:server.py:LabServer', self.ids); self.assertIn('def:server.py:LabServer.graph_view', self.ids); self.assertIn('def:graph.py:validate', self.ids)
        self.assertEqual(self.ids['def:server.py:LabServer.graph_view']['kind'], 'method'); self.assertEqual(self.ids['def:graph.py:validate']['kind'], 'function')
        self.assertIn(('mod:server.py', 'def:server.py:LabServer', 'contains'), self.edges); self.assertIn(('def:server.py:LabServer', 'def:server.py:LabServer.graph_view', 'contains'), self.edges)
        self.assertTrue(self.ids['mod:graph.py']['doc'].startswith('Graph documents')); self.assertGreater(self.ids['def:graph.py:validate']['end_line'], self.ids['def:graph.py:validate']['line'])
        self.assertEqual(self.map['stats']['syntax_errors'], 0); self.assertEqual(self.map['stats']['skipped'], [])
    def test_imports_and_calls_resolve_inside_the_package(self):
        self.assertIn(('mod:server.py', 'mod:graph.py', 'imports'), self.edges); self.assertIn(('mod:sessions.py', 'mod:remote.py', 'imports'), self.edges)
        self.assertIn(('def:server.py:LabServer.graph_view', 'def:graph.py:analysis', 'calls'), self.edges, 'alias.function through "from . import graph as graph_module"')
        self.assertIn(('def:server.py:LabServer.graph_view', 'def:server.py:LabServer.library_links', 'calls'), self.edges, 'self.method')
        self.assertIn(('def:sessions.py:SessionRegistry.command', 'def:sessions.py:SessionRegistry.shell_program', 'calls'), self.edges)
        self.assertIn(('def:graph_run.py:GraphRun._run', 'def:graph.py:session_of', 'calls'), self.edges)
        self.assertGreater(self.map['stats']['unresolved_calls'], 100, 'library and builtin calls are counted, not drawn')
    def test_routes_and_ui_edges(self):
        self.assertIn('route:GET /api/graph', self.ids); self.assertIn('route:POST /api/graph/run', self.ids); self.assertIn('route:GET /api/mainbooks/', self.ids, 'startswith routes end with a slash')
        self.assertIn(('route:POST /api/graph/run', 'def:server.py:LabHandler.do_POST', 'routes'), self.edges)
        self.assertIn(('ui:ui/graph.js', 'route:GET /api/graph', 'ui'), self.edges); self.assertIn(('ui:ui/graph.js', 'route:POST /api/graph/run', 'ui'), self.edges); self.assertIn(('ui:ui/devices.js', 'route:POST /api/devices/pair', 'ui'), self.edges)
        self.assertIn('/api/graph', self.ids['ui:ui/graph.js']['routes_called'])
    def test_small_folder_relative_imports_limits_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'pkg').mkdir(); (root / 'pkg' / '__init__.py').write_text('')
            (root / 'pkg' / 'a.py').write_text('from . import b\nfrom .b import helper\n\ndef run():\n    """Runs."""\n    b.step()\n    helper()\n    return undefined_thing()\n')
            (root / 'pkg' / 'b.py').write_text('def step():\n    pass\n\ndef helper():\n    step()\n')
            (root / 'pkg' / 'broken.py').write_text('def (\n'); (root / 'skip').symlink_to(root / 'pkg'); (root / '.git').mkdir(); (root / '.git' / 'x.py').write_text('def hidden(): pass\n')
            m = codemap.scan(root); edges = {(e['source'], e['target'], e['kind']) for e in m['edges']}; ids = {n['id'] for n in m['nodes']}
            self.assertIn(('mod:pkg/a.py', 'mod:pkg/b.py', 'imports'), edges); self.assertIn(('def:pkg/a.py:run', 'def:pkg/b.py:step', 'calls'), edges); self.assertIn(('def:pkg/a.py:run', 'def:pkg/b.py:helper', 'calls'), edges); self.assertIn(('def:pkg/b.py:helper', 'def:pkg/b.py:step', 'calls'), edges)
            self.assertNotIn('mod:.git/x.py', ids); self.assertEqual(m['stats']['syntax_errors'], 1); self.assertTrue(any('broken.py' in s for s in m['stats']['skipped'])); self.assertEqual(m['stats']['unresolved_calls'], 1)
            src = codemap.source(root, 'pkg/a.py', 4, 5); self.assertEqual(src['text'], 'def run():\n    """Runs."""')
            for bad in ('../x.py', 'skip/a.py', 'nope.py'):
                with self.subTest(bad=bad), self.assertRaises(codemap.MapError): codemap.source(root, bad, 1, 1)
            with self.assertRaises(codemap.MapError): codemap.scan(root / 'pkg' / 'a.py')


class CodemapHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); state, key = initialize_state(Path(self.tmp.name) / 'private'); self.key = key
        self.server = LabServer(('127.0.0.1', 0), state, key); threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def test_routes_need_owner_and_map_self(self):
        self.assertEqual(self.req('/api/codemap')[0], 401)
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin}); cookie = {'Cookie': head['Set-Cookie'].split(';')[0]}
        status, data, _ = self.req('/api/codemap', headers=cookie)
        self.assertEqual((status, data['root_name']), (200, 'self')); self.assertIn('self', data['roots']); self.assertGreater(data['stats']['nodes'], 300)
        self.assertEqual(self.req('/api/codemap?root=nope', headers=cookie)[0], 404)
        status, data, _ = self.req('/api/codemap/source?root=self&path=graph.py&line=1&end_line=2', headers=cookie); self.assertEqual(status, 200); self.assertIn('Graph documents', data['text'])
        self.assertEqual(self.req('/api/codemap/source?root=self&path=../tests/test_codemap.py&line=1&end_line=2', headers=cookie)[0], 400)
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10); c.request('GET', '/map', headers={'Host': f'127.0.0.1:{self.port}'}); r = c.getresponse(); self.assertEqual(r.status, 200); self.assertIn(b'Code map', r.read()); c.close()


if __name__ == '__main__':
    unittest.main()
