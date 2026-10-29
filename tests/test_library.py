"""The cell library: one approved folder of reusable cells, linked into any Mainbook."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_lab import graph as g
from local_lab import library, mainbooks


def record(name='load-data', source='rows = [1, 2]\n', **extra):
    base = {'format': 'mainaiger-cell', 'schema_version': 1, 'name': name, 'title': 'Load', 'source': source, 'tags': ['ml'], 'description': 'd'}
    base.update(extra)
    return base


class FolderRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
    def tearDown(self):
        self.home.stop(); self.tmp.cleanup()
    def test_default_is_next_to_mainbooks(self):
        self.assertTrue(library.default_folder(self.state).endswith(os.path.join('MainAIger Lab', 'Library')))
        mainbooks.configure(self.state, str(self.root / 'Docs' / 'Books'))
        self.assertEqual(library.default_folder(self.state), str(self.root / 'Docs' / 'Library'))
    def test_unconfigured_until_approved(self):
        st = library.status(self.state)
        self.assertEqual((st['configured'], st['folder'], st['count']), (False, None, 0))
        self.assertIn('load-data', st['starters'])
        with self.assertRaises(library.LibraryError):
            library.save(self.state, record())
    def test_configure_and_rejections(self):
        mainbooks.configure(self.state, str(self.root / 'Docs' / 'Books'))
        for bad in ('relative', '/', str(self.root), str(self.state), str(self.root / 'Docs' / 'Books'), ''):
            with self.subTest(bad=bad), self.assertRaises(library.LibraryError):
                library.validate_folder(bad, self.state)
        chosen = self.root / 'Docs' / 'Library'
        st = library.configure(self.state, str(chosen))
        self.assertTrue(chosen.is_dir()); self.assertEqual((st['configured'], st['folder']), (True, str(chosen)))
        self.assertEqual(oct((self.state / 'library.json').stat().st_mode & 0o777), '0o600')


class RecordTests(unittest.TestCase):
    def test_validate_normalises(self):
        r = library.validate_record(record(tags=['ML', 'ml', 'Data'], inputs=['a', 'a'], source=['x = ', '1']))
        self.assertEqual((r['tags'], r['inputs'], r['source'], r['version'], r['cell_type']), (['data', 'ml'], ['a'], 'x = 1', 1, 'code'))
        self.assertEqual(r['source_hash'], g.source_hash('x = 1'))
    def test_validate_rejects(self):
        for bad in (record(name='a b'), record(name='../x'), record(format='other'), record(schema_version=2), record(version=0),
                    record(version=True), record(tags=['has space']), record(inputs=['not a name']), record(cell_type='raw'),
                    record(source='x' * 200001), record(title=''), 'nope'):
            with self.subTest(bad=bad), self.assertRaises(library.LibraryError):
                library.validate_record(bad)
    def test_record_from_cell_uses_declared_or_analysed_names(self):
        doc = g.new_document(); cell = doc['notebook']['cells'][0]
        cell['source'] = 'y = x + 1\n'; cell['metadata']['mainaiger']['title'] = 'Add one'
        r = library.record_from_cell(doc, 'cell-1', 'add-one', tags=['math'])
        self.assertEqual((r['title'], r['inputs'], r['outputs'], r['tags']), ('Add one', ['x'], ['y'], ['math']))
        cell['metadata']['mainaiger']['outputs'] = ['y', 'z']
        self.assertEqual(library.record_from_cell(doc, 'cell-1', 'add-one')['outputs'], ['y', 'z'])
        with self.assertRaises(library.LibraryError):
            library.record_from_cell(doc, 'start', 'nope')


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
        self.folder = self.root / 'Library'; library.configure(self.state, str(self.folder))
    def tearDown(self):
        self.home.stop(); self.tmp.cleanup()
    def test_save_versions_list_search_load(self):
        r1 = library.save(self.state, record())
        path = Path(r1['path'])
        self.assertEqual(path, self.folder / 'load-data.cell.json'); self.assertEqual(oct(path.stat().st_mode & 0o777), '0o644')
        self.assertEqual([p.name for p in self.folder.iterdir()], ['load-data.cell.json'])
        self.assertEqual(r1['record']['version'], 1)
        with self.assertRaises(library.LibraryError):
            library.save(self.state, record(source='rows = [3]\n'))
        r2 = library.save(self.state, record(source='rows = [3]\n', version=99), overwrite=True)
        self.assertEqual(r2['record']['version'], 2, 'the stored version wins over whatever the caller sent')
        self.assertEqual(library.load(self.state, 'load-data')['source'], 'rows = [3]\n')
        library.save(self.state, record(name='train', title='Train model', tags=['ml', 'train'], description='fits a model', inputs=['X'], outputs=['model']))
        rows = library.listing(self.state)
        self.assertEqual([r['name'] for r in rows], ['load-data', 'train'])
        self.assertEqual([r['name'] for r in library.listing(self.state, 'train')], ['train'])
        self.assertEqual([r['name'] for r in library.listing(self.state, 'MODEL fits')], ['train'])
        self.assertEqual([r['name'] for r in library.listing(self.state, 'ml')], ['load-data', 'train'])
        self.assertEqual(library.listing(self.state, 'nothing-here'), [])
    def test_names_symlinks_and_corrupt_files(self):
        for bad in ('../escape', 'a/b', '.hidden', 'x' * 70, '', 'has space'):
            with self.subTest(bad=bad), self.assertRaises(library.LibraryError):
                library.save(self.state, record(name=bad))
        outside = self.root / 'outside.cell.json'; outside.write_text('{}')
        (self.folder / 'link.cell.json').symlink_to(outside)
        with self.assertRaises(library.LibraryError):
            library.save(self.state, record(name='link'), overwrite=True)
        with self.assertRaises(library.LibraryError):
            library.load(self.state, 'link')
        self.assertEqual(outside.read_text(), '{}')
        (self.folder / 'bad.cell.json').write_text('not json')
        (self.folder / 'renamed.cell.json').write_text(json.dumps(record(name='other')))
        rows = library.listing(self.state)
        self.assertEqual([(r['name'], r.get('unreadable', False)) for r in rows], [('bad', True)])  # symlink skipped; mismatched name skipped
    def test_insert_links_cell_and_chains_after_node(self):
        library.save(self.state, record(source='rows = [1]\n', outputs=['rows']))
        rec = library.load(self.state, 'load-data')
        doc, node_id = library.insert(g.new_document(), rec, {'x': 10, 'y': 20}, after='cell-1')
        self.assertEqual(node_id, 'load-data')
        node = g.node_by_id(doc, node_id); cell = doc['notebook']['cells'][node['cell_index']]
        self.assertEqual((cell['source'], cell['metadata']['mainaiger']['title'], cell['metadata']['mainaiger']['outputs']), ('rows = [1]\n', 'Load', ['rows']))
        self.assertEqual(library.link_of(cell), {'name': 'load-data', 'version': 1, 'source_hash': g.source_hash('rows = [1]\n')})
        self.assertEqual(doc['layout']['nodes'][node_id], {'x': 10, 'y': 20})
        pairs = {(e['source'], e['target']) for e in doc['graph']['edges']}
        self.assertEqual(pairs, {('start', 'cell-1'), ('cell-1', 'load-data'), ('load-data', 'finish')}, 'inserted between cell-1 and Finish')
        doc, second = library.insert(doc, rec, {'x': 0, 'y': 0})
        self.assertEqual(second, 'load-data-2'); self.assertEqual(len(doc['graph']['edges']), 3, 'no chaining without after')
        with self.assertRaises(library.LibraryError):
            library.insert(doc, rec, {'x': 0, 'y': 0}, after='finish')
        with self.assertRaises(library.LibraryError):
            library.insert(doc, rec, {'x': 'a'})
    def test_link_state_and_pull(self):
        library.save(self.state, record(source='rows = [1]\n'))
        rec = library.load(self.state, 'load-data')
        doc, node_id = library.insert(g.new_document(), rec, {'x': 0, 'y': 0})
        by_name = {r['name']: r for r in library.listing(self.state)}
        cell = lambda: doc['notebook']['cells'][g.node_by_id(doc, node_id)['cell_index']]
        self.assertEqual(library.link_state(cell(), by_name), 'current')
        self.assertIsNone(library.link_state(doc['notebook']['cells'][0], by_name))
        cell()['source'] = 'rows = [1, 2]\n'
        self.assertEqual(library.link_state(cell(), by_name), 'modified')
        cell()['source'] = 'rows = [1]\n'
        library.save(self.state, record(source='rows = [5]\n'), overwrite=True)
        by_name = {r['name']: r for r in library.listing(self.state)}
        self.assertEqual(library.link_state(cell(), by_name), 'behind')
        doc = library.pull(doc, node_id, library.load(self.state, 'load-data'))
        self.assertEqual((cell()['source'], library.link_of(cell())['version']), ('rows = [5]\n', 2))
        self.assertEqual(library.link_state(cell(), by_name), 'current')
        with self.assertRaises(library.LibraryError):
            library.pull(doc, 'cell-1', library.load(self.state, 'load-data'))  # not linked
        self.assertEqual(library.link_state(cell(), {}), 'missing')
    def test_seed_copies_starters_once(self):
        first = library.seed(self.state)
        self.assertEqual(first['added'], ['evaluate', 'extract-features', 'load-data', 'report', 'split-train-test', 'train-classifier'])
        self.assertEqual(library.seed(self.state)['skipped'], first['added'])
        self.assertTrue(all(r['version'] == 1 for r in library.listing(self.state)))


class StarterFlowTests(unittest.TestCase):
    """The starter cells form a runnable ML flow with the standard library only."""
    def test_starters_run_in_order_with_stdlib_and_data_edges_connect_them(self):
        order = ['load-data', 'extract-features', 'split-train-test', 'train-classifier', 'evaluate', 'report']
        by_name = {r['name']: r for r in library.starters()}
        self.assertEqual(sorted(by_name), sorted(order))
        doc = g.new_document(); doc['notebook']['cells'] = []; doc['graph'] = {'nodes': [{'id': 'start', 'kind': 'start'}, {'id': 'finish', 'kind': 'finish'}], 'edges': [{'id': 'e0', 'kind': 'control', 'source': 'start', 'target': 'finish'}]}
        doc['layout'] = {'nodes': {}, 'viewport': {'x': 0, 'y': 0, 'zoom': 1}}
        previous = 'start'
        for name in order:
            doc, previous = library.insert(doc, by_name[name], {'x': 0, 'y': 0}, after=previous)
        self.assertEqual(g.order(doc)[1:-1], order)
        self.assertEqual(g.preflight(doc, order)['missing'], [])
        edges = {(e['source'], e['target'], e['name']) for e in g.data_edges(doc)}
        for pair in (('load-data', 'extract-features', 'rows'), ('split-train-test', 'train-classifier', 'X_train'), ('evaluate', 'report', 'metrics')):
            self.assertIn(pair, edges)
        namespace = {}
        import io, contextlib
        for name in order:
            with contextlib.redirect_stdout(io.StringIO()):
                exec(by_name[name]['source'], namespace)
        self.assertGreater(namespace['metrics']['accuracy'], 0.8)


if __name__ == '__main__':
    unittest.main()


class LibraryHttpTests(unittest.TestCase):
    def setUp(self):
        import threading
        from local_lab.server import LabServer, initialize_state
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
        self.server = LabServer(('127.0.0.1', 0), state, key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.home.stop(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        import http.client
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}
    def test_folder_seed_search_insert_save_pull(self):
        self.assertEqual(self.req('/api/library')[0], 401)
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        status, data, _ = self.req('/api/library', headers=cookie)
        self.assertEqual((status, data['configured'], data['cells']), (200, False, []))
        self.assertTrue(data['default'].endswith('Library')); self.assertIn('load-data', data['starters'])
        self.assertEqual(self.req('/api/library/seed', {'confirm': True}, auth)[0], 400)  # no folder yet
        self.assertEqual(self.req('/api/library/folder', {'folder': '/'}, auth)[0], 400)  # no confirm
        chosen = str(self.root / 'Documents' / 'Library')
        status, data, _ = self.req('/api/library/folder', {'folder': chosen, 'confirm': True}, auth)
        self.assertEqual((status, data['configured'], data['folder']), (200, True, chosen))
        self.assertEqual(self.req('/api/library/seed', {}, auth)[0], 400)  # confirm required
        status, data, _ = self.req('/api/library/seed', {'confirm': True}, auth)
        self.assertEqual((status, len(data['added'])), (200, 6))
        status, data, _ = self.req('/api/library?q=train+classifier', headers=cookie)
        self.assertEqual([c['name'] for c in data['cells']], ['train-classifier'])
        status, data, _ = self.req('/api/library/load-data', headers=cookie)
        self.assertEqual((status, data['cell']['version'], data['cell']['outputs']), (200, 1, ['rows']))
        self.assertEqual(self.req('/api/library/nope', headers=cookie)[0], 404)
        # Insert after cell-1: the new node sits between cell-1 and Finish and is linked.
        status, data, _ = self.req('/api/library/insert', {'name': 'load-data', 'position': {'x': 100, 'y': 200}, 'after': 'cell-1'}, auth)
        self.assertEqual((status, data['node']), (200, 'load-data'))
        view = data['view']; pairs = {(e['source'], e['target']) for e in view['document']['graph']['edges']}
        self.assertEqual(pairs, {('start', 'cell-1'), ('cell-1', 'load-data'), ('load-data', 'finish')})
        self.assertEqual(view['library']['load-data'], {'name': 'load-data', 'version': 1, 'state': 'current', 'latest': 1})
        self.assertEqual(self.req('/api/library/insert', {'name': 'load-data', 'position': {'x': 0, 'y': 0}, 'after': 'finish'}, auth)[0], 400)
        # Edit it locally and save a new version from the graph.
        doc = view['document']; node = next(n for n in doc['graph']['nodes'] if n['id'] == 'load-data'); doc['notebook']['cells'][node['cell_index']]['source'] = 'rows = [(0, 0)]\n'
        status, data, _ = self.req('/api/graph', {'document': doc}, auth)
        self.assertEqual(data['library']['load-data']['state'], 'modified')
        self.assertEqual(self.req('/api/library/save', {'node': 'load-data', 'name': 'load-data'}, auth)[0], 409)  # exists, no overwrite
        status, data, _ = self.req('/api/library/save', {'node': 'load-data', 'name': 'load-data', 'tags': ['ml'], 'description': 'tiny', 'overwrite': True}, auth)
        self.assertEqual((status, data['version'], data['view']['library']['load-data']['state']), (200, 2, 'current'))
        self.assertEqual(library.load(self.server.state_dir, 'load-data')['source'], 'rows = [(0, 0)]\n')
        self.assertEqual(self.req('/api/library/save', {'node': 'start', 'name': 'x'}, auth)[0], 400)
        self.assertEqual(self.req('/api/library/save', {'node': 'cell-1', 'name': 'bad name'}, auth)[0], 400)
        # A second graph that inserted v1 earlier would now be behind; simulate by pointing the link at v1 and pulling.
        status, data, _ = self.req('/api/graph', headers=cookie); doc = data['document']
        cell = doc['notebook']['cells'][next(n for n in doc['graph']['nodes'] if n['id'] == 'load-data')['cell_index']]
        cell['metadata']['mainaiger']['library']['version'] = 1; cell['source'] = 'rows = [1]\n'; cell['metadata']['mainaiger']['library']['source_hash'] = g.source_hash('rows = [1]\n')
        status, data, _ = self.req('/api/graph', {'document': doc}, auth)
        self.assertEqual(data['library']['load-data'], {'name': 'load-data', 'version': 1, 'state': 'behind', 'latest': 2})
        status, data, _ = self.req('/api/library/pull', {'node': 'load-data'}, auth)
        self.assertEqual((status, data['view']['library']['load-data']['state'], data['view']['library']['load-data']['version']), (200, 'current', 2))
        self.assertEqual(self.req('/api/library/pull', {'node': 'cell-1'}, auth)[0], 400)
