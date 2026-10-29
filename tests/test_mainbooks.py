"""Saving and reopening Mainbooks in one owner-approved folder."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from local_lab import graph as g
from local_lab import mainbooks
from local_lab.server import LabServer, initialize_state


class FolderRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
    def tearDown(self):
        self.home.stop(); self.tmp.cleanup()
    def test_default_folder_is_under_documents(self):
        self.assertTrue(mainbooks.default_folder().endswith(os.path.join('Documents', 'MainAIger Lab', 'Mainbooks')))
    def test_unconfigured_until_approved(self):
        status = mainbooks.status(self.state)
        self.assertEqual((status['configured'], status['folder'], status['count']), (False, None, 0))
        with self.assertRaises(mainbooks.MainbookError):
            mainbooks.save(self.state, 'first', g.new_document())
    def test_configure_creates_folder_and_remembers_it(self):
        chosen = self.root / 'Documents' / 'MainAIger Lab' / 'Mainbooks'
        status = mainbooks.configure(self.state, str(chosen))
        self.assertTrue(chosen.is_dir()); self.assertEqual((status['configured'], status['exists'], status['folder']), (True, True, str(chosen)))
        self.assertEqual(mainbooks.folder(self.state), str(chosen))
        self.assertEqual(oct((self.state / 'mainbooks.json').stat().st_mode & 0o777), '0o600')
    def test_rejected_folders(self):
        for bad in ('relative/path', '/', str(self.root), str(self.state), str(self.state / 'inside'), str(self.root.parent), '', 'x\ny'):
            with self.subTest(bad=bad), self.assertRaises(mainbooks.MainbookError):
                mainbooks.validate_folder(bad, self.state)
        file = self.root / 'a-file'; file.write_text('x')
        with self.assertRaises(mainbooks.MainbookError):
            mainbooks.validate_folder(str(file), self.state)


class SaveLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
        self.folder = self.root / 'books'; mainbooks.configure(self.state, str(self.folder))
    def tearDown(self):
        self.home.stop(); self.tmp.cleanup()
    def test_save_is_atomic_named_and_reopens_identically(self):
        doc = g.new_document(); doc['notebook']['cells'][0]['source'] = 'answer = 42'
        result = mainbooks.save(self.state, 'My first graph', doc)
        path = Path(result['path'])
        self.assertEqual(path, self.folder / 'My first graph.ipymb'); self.assertTrue(path.is_file())
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o644')
        self.assertEqual([p.name for p in self.folder.iterdir()], ['My first graph.ipymb'])  # no temp files left
        again = mainbooks.load(self.state, 'My first graph')
        self.assertEqual(again['notebook']['cells'][0]['source'], 'answer = 42')
        self.assertEqual(again['notebook']['metadata']['mainaiger']['name'], 'My first graph')
        rows = mainbooks.listing(self.state)
        self.assertEqual((rows[0]['name'], rows[0]['cells']), ('My first graph', 1))
    def test_overwrite_needs_explicit_flag(self):
        mainbooks.save(self.state, 'book', g.new_document())
        with self.assertRaises(mainbooks.MainbookError):
            mainbooks.save(self.state, 'book', g.new_document())
        self.assertTrue(mainbooks.save(self.state, 'book', g.new_document(), overwrite=True)['bytes'] > 0)
    def test_names_and_symlinks_are_refused(self):
        for bad in ('../escape', 'a/b', '.hidden', ' lead', 'x' * 90, '', 'name.ipymb\n'):
            with self.subTest(bad=bad), self.assertRaises(mainbooks.MainbookError):
                mainbooks.save(self.state, bad, g.new_document())
        outside = self.root / 'outside.ipymb'; outside.write_text('{}')
        (self.folder / 'link.ipymb').symlink_to(outside)
        with self.assertRaises(mainbooks.MainbookError):
            mainbooks.save(self.state, 'link', g.new_document(), overwrite=True)
        with self.assertRaises(mainbooks.MainbookError):
            mainbooks.load(self.state, 'link')
        self.assertEqual(outside.read_text(), '{}')  # never written through the link
        self.assertEqual([r['name'] for r in mainbooks.listing(self.state)], [])  # symlinks are not listed
    def test_corrupt_file_is_reported_not_crashing(self):
        (self.folder / 'bad.ipymb').write_text('not json')
        self.assertEqual(mainbooks.listing(self.state)[0]['title'], '(unreadable)')
        with self.assertRaises(mainbooks.MainbookError):
            mainbooks.load(self.state, 'bad')


class MainbookHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
        self.server = LabServer(('127.0.0.1', 0), state, key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.home.stop(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}
    def test_choose_folder_save_list_reopen(self):
        self.assertEqual(self.req('/api/mainbooks')[0], 401)
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        status, data, _ = self.req('/api/mainbooks', headers=cookie)
        self.assertEqual((status, data['configured'], data['mainbooks']), (200, False, []))
        self.assertTrue(data['default'].endswith('Mainbooks'))
        self.assertEqual(self.req('/api/mainbooks/save', {'name': 'x'}, auth)[0], 400)  # no folder yet
        self.assertEqual(self.req('/api/mainbooks/folder', {'folder': '/'}, auth)[0], 400)  # no confirm
        self.assertEqual(self.req('/api/mainbooks/folder', {'folder': '/', 'confirm': True}, auth)[0], 400)
        chosen = str(self.root / 'Documents' / 'Books')
        status, data, _ = self.req('/api/mainbooks/folder', {'folder': chosen, 'confirm': True}, auth)
        self.assertEqual((status, data['configured'], data['folder']), (200, True, chosen))
        doc = g.new_document(); doc['notebook']['cells'][0]['source'] = 'saved = True'
        self.req('/api/graph', {'document': doc}, auth)
        status, saved, _ = self.req('/api/mainbooks/save', {'name': 'demo'}, auth)
        self.assertEqual((status, saved['name']), (200, 'demo')); self.assertTrue(Path(saved['path']).is_file())
        self.assertEqual(self.req('/api/mainbooks/save', {'name': 'demo'}, auth)[0], 409)
        self.assertEqual(self.req('/api/mainbooks/save', {'name': 'demo', 'overwrite': True}, auth)[0], 200)
        status, data, _ = self.req('/api/mainbooks', headers=cookie)
        self.assertEqual([m['name'] for m in data['mainbooks']], ['demo'])
        status, data, _ = self.req('/api/mainbooks/demo', headers=cookie)
        self.assertEqual((status, data['document']['notebook']['cells'][0]['source']), (200, 'saved = True'))
        self.assertEqual(self.req('/api/mainbooks/nope', headers=cookie)[0], 404)
        self.assertEqual(self.req('/api/mainbooks/..%2Fetc', headers=cookie)[0], 404)


if __name__ == '__main__':
    unittest.main()
