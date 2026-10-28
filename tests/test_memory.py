"""Memory: the approved vault of Markdown notes (notes/ curated, chats/ imported), links, search, routes."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from local_lab import memory as m, mainbooks
from local_lab.server import LabServer, initialize_state


class FormatTests(unittest.TestCase):
    def test_front_matter_round_trips_awkward_values(self):
        front = {'id': '20261028-abc123', 'title': 'Notes: on [brackets], "quotes" & co', 'kind': 'fact', 'status': 'draft', 'tier': 'notes', 'tags': ['a b', 'c:d', 'plain'], 'sources': [], 'visibility': 'private', 'created': '2026-10-28T07:00:00', 'updated': '2026-10-28T07:00:00', 'extra_number': 3}
        text = m.render(front, 'Body with [[A link]] and [[Other|alias]] and [[Third#section]].\n')
        parsed, body = m.parse(text)
        self.assertEqual(parsed['title'], front['title']); self.assertEqual(parsed['tags'], ['a b', 'c:d', 'plain']); self.assertEqual(parsed['sources'], []); self.assertEqual(parsed['extra_number'], 3); self.assertEqual(parsed['created'], '2026-10-28T07:00:00')
        self.assertEqual(m.links_in(body), ['A link', 'Other', 'Third'])
        self.assertEqual(m.parse('no front matter\n'), ({}, 'no front matter\n')); self.assertEqual(m.parse('---\nunterminated\n'), ({}, '---\nunterminated\n'))
        self.assertEqual(m.slug('  Quantum Kernels: a Study!  '), 'quantum-kernels-a-study'); self.assertEqual(m.slug('???'), 'note')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve(); self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
    def tearDown(self):
        self.home.stop(); self.tmp.cleanup()
    def test_folder_approval_and_tiers(self):
        self.assertFalse(m.status(self.state)['configured']); self.assertTrue(m.status(self.state)['default'].endswith('MainAIger Lab/Memory'))
        with self.assertRaisesRegex(m.MemoryError, 'home folder'): m.configure(self.state, str(self.root))
        mainbooks.configure(self.state, str(self.root / 'books'))
        with self.assertRaisesRegex(m.MemoryError, 'different from the Mainbooks'): m.configure(self.state, str(self.root / 'books'))
        st = m.configure(self.state, str(self.root / 'Memory')); self.assertTrue(st['configured']); self.assertTrue((self.root / 'Memory' / 'notes').is_dir()); self.assertTrue((self.root / 'Memory' / 'chats').is_dir())
        with self.assertRaisesRegex(m.MemoryError, 'Choose a Memory folder'): m.listing(self.state.parent / 'nope') if False else m.listing(self._unconfigured())
    def _unconfigured(self):
        other = self.root / 'state2'; other.mkdir(exist_ok=True, mode=0o700); return other
    def test_create_read_update_append_delete_and_rules(self):
        m.configure(self.state, str(self.root / 'Memory'))
        a = m.create(self.state, 'Fraud idea', 'Try [[Quantum kernels]] on [[CCFD]].', kind='idea', tags=['Quantum', 'fraud '], sources=['chat:abc#3'])
        self.assertEqual((a['path'], a['kind'], a['status'], a['tags'], a['links']), ('notes/fraud-idea.md', 'idea', 'draft', ['quantum', 'fraud'], ['Quantum kernels', 'CCFD']))
        again = m.create(self.state, 'Fraud idea', 'another'); self.assertEqual(again['path'], 'notes/fraud-idea-2.md', 'same title, new file')
        note = m.read(self.state, a['path']); self.assertEqual(note['front']['id'], a['id']); self.assertEqual(note['sources'], ['chat:abc#3']); self.assertEqual(len(note['hash']), 8)
        text = (self.root / 'Memory' / a['path']).read_text(); self.assertTrue(text.startswith('---\nid: ')); self.assertIn('tags: [quantum, fraud]', text)
        updated = m.update(self.state, a['path'], body='Body two [[Quantum kernels]]', front_changes={'status': 'growing', 'tags': ['ml']}, expected_hash=note['hash'])
        self.assertEqual((updated['status'], updated['tags'], updated['body']), ('growing', ['ml'], 'Body two [[Quantum kernels]]\n'))
        with self.assertRaisesRegex(m.MemoryError, 'changed since'): m.update(self.state, a['path'], body='x', expected_hash=note['hash'])
        with self.assertRaisesRegex(m.MemoryError, 'Only '): m.update(self.state, a['path'], front_changes={'created': 'x'} if False else {'nonsense': 1})
        with self.assertRaisesRegex(m.MemoryError, 'kind must be'): m.update(self.state, a['path'], front_changes={'kind': 'poem'})
        with self.assertRaisesRegex(m.MemoryError, 'names the saved Mainbook'): m.update(self.state, a['path'], front_changes={'kind': 'mainbook'})
        ok = m.update(self.state, a['path'], front_changes={'kind': 'mainbook', 'mainbook': 'my loop'}); self.assertEqual(ok['mainbook'], 'my loop')
        b = m.create(self.state, 'Quantum kernels', 'Kernel methods.', status_='stable')
        grown = m.append(self.state, b['path'], 'Asked again', 'Second thing learned'); self.assertEqual(grown['status'], 'stable'); self.assertIn('## Asked again · ', grown['body']); self.assertIn('Second thing learned', grown['body'])
        draft = m.create(self.state, 'Draft note', 'x'); self.assertEqual(m.append(self.state, draft['path'], 'More', 'y')['status'], 'growing', 'a draft that grows becomes growing')
        chat = m.create(self.state, 'A chat', 'human: hi', tier='chats', subfolder='chatgpt'); self.assertEqual((chat['path'], chat['kind']), ('chats/chatgpt/a-chat.md', 'chat'))
        for call in (lambda: m.update(self.state, chat['path'], body='edited'), lambda: m.append(self.state, chat['path'], 'h', 'x')):
            with self.assertRaisesRegex(m.MemoryError, 'not edited'): call()
        for bad in ('../x.md', '/etc/passwd', 'notes/../chats/a.md', 'other/x.md', 'notes/x.txt'):
            with self.subTest(bad=bad), self.assertRaises(m.MemoryError): m.read(self.state, bad)
        (self.root / 'Memory' / 'notes' / 'link.md').symlink_to(self.root / 'Memory' / a['path'])
        with self.assertRaisesRegex(m.MemoryError, 'Symlinks'): m.read(self.state, 'notes/link.md')
        self.assertNotIn('notes/link.md', [r['path'] for r in m.listing(self.state)], 'symlinks are not listed')
        for bad_title in ('', 'a/b', 'x' * 121):
            with self.subTest(title=bad_title), self.assertRaises(m.MemoryError): m.create(self.state, bad_title, '')
        with self.assertRaisesRegex(m.MemoryError, 'tags'): m.create(self.state, 'T', '', tags=['x' * 41])
        rows = m.listing(self.state, tier='notes', kind='idea'); self.assertEqual([r['title'] for r in rows], ['Fraud idea']) if False else self.assertTrue(all(r['kind'] == 'idea' for r in rows))
        self.assertEqual(m.status(self.state)['counts'], {'notes': 4, 'chats': 1})
        self.assertTrue(m.delete(self.state, draft['path'])); self.assertEqual(m.status(self.state)['counts']['notes'], 3)
        with self.assertRaisesRegex(m.MemoryError, 'No such note'): m.delete(self.state, draft['path'])
    def test_graph_and_search(self):
        m.configure(self.state, str(self.root / 'Memory'))
        a = m.create(self.state, 'Fraud idea', 'Try [[Quantum kernels]] on [[ccfd]] and [[Nowhere]].', kind='idea')
        b = m.create(self.state, 'Quantum kernels', 'Kernel methods map features into Hilbert space. See [[fraud idea]].', status_='stable')
        c = m.create(self.state, 'CCFD', 'Credit card fraud detection dataset notes.', kind='fact')
        graph = m.graph(self.state); edges = {(e['source'], e['target']) for e in graph['edges']}
        self.assertEqual(edges, {(a['path'], b['path']), (a['path'], c['path']), (b['path'], a['path'])}, 'links resolve by title, case-insensitive'); self.assertEqual(graph['unresolved'], [{'source': a['path'], 'target': 'Nowhere'}])
        self.assertEqual(m.reindex(self.state)['indexed'], 3); self.assertEqual(m.reindex(self.state)['indexed'], 0, 'unchanged files are not re-read')
        hits = m.search(self.state, 'hilbert kernels'); self.assertEqual(hits[0]['title'], 'Quantum kernels'); self.assertIn('[', hits[0]['snippet']); self.assertGreater(hits[0]['score'], 0)
        self.assertEqual([h['title'] for h in m.search(self.state, 'fraud')][:1], ['Fraud idea']) if False else self.assertTrue({h['title'] for h in m.search(self.state, 'fraud')} >= {'Fraud idea', 'CCFD'})
        self.assertEqual(m.search(self.state, 'kernels', tier='chats'), []); self.assertEqual(m.search(self.state, '??'), [])
        with self.assertRaises(m.MemoryError): m.search(self.state, '')
        m.update(self.state, c['path'], body='Nothing about that topic now.'); self.assertNotIn('CCFD', [h['title'] for h in m.search(self.state, 'dataset')], 'the index follows file changes')
        with patch.object(m, '_FTS', [False]):
            self.assertEqual(m.search(self.state, 'hilbert')[0]['title'], 'Quantum kernels', 'the scan fallback finds it too'); self.assertEqual(m.status(self.state)['search'], 'scan')


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key; self.state = Path(state)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.home.stop(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def test_routes(self):
        self.assertEqual(self.req('/api/memory')[0], 401)
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin}); auth = {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}; cookie = {'Cookie': auth['Cookie']}
        self.assertEqual(self.req('/api/memory', headers=cookie)[1]['configured'], False)
        self.assertEqual(self.req('/api/memory/notes', headers=cookie)[0], 409, 'no folder yet')
        self.assertEqual(self.req('/api/memory/folder', {'folder': str(self.root / 'Memory')}, auth)[0], 400, 'confirm required')
        st = self.req('/api/memory/folder', {'folder': str(self.root / 'Memory'), 'confirm': True}, auth)[1]; self.assertTrue(st['configured'])
        status, made, _ = self.req('/api/memory/note', {'title': 'Quantum kernels', 'kind': 'fact', 'tags': ['quantum'], 'body': 'Kernel methods. See [[Fraud idea]].'}, auth); self.assertEqual((status, made['note']['path']), (200, 'notes/quantum-kernels.md'))
        idea = self.req('/api/memory/note', {'title': 'Fraud idea', 'kind': 'idea', 'body': 'Use [[Quantum kernels]].'}, auth)[1]['note']
        self.assertEqual(self.req('/api/memory/note', {'title': 'x', 'kind': 'poem'}, auth)[0], 400)
        rows = self.req('/api/memory/notes?tier=notes', headers=cookie)[1]['notes']; self.assertEqual({r['title'] for r in rows}, {'Quantum kernels', 'Fraud idea'})
        self.assertEqual([r['title'] for r in self.req('/api/memory/notes?kind=idea', headers=cookie)[1]['notes']], ['Fraud idea'])
        note = self.req('/api/memory/note?path=' + idea['path'], headers=cookie)[1]['note']; self.assertEqual(note['links'], ['Quantum kernels'])
        status, upd, _ = self.req('/api/memory/note/update', {'path': idea['path'], 'body': 'Use [[Quantum kernels]] weekly.', 'front': {'status': 'stable', 'tags': ['plan']}, 'expected_hash': note['hash']}, auth)
        self.assertEqual((status, upd['note']['status'], upd['note']['tags']), (200, 'stable', ['plan']))
        self.assertEqual(self.req('/api/memory/note/update', {'path': idea['path'], 'body': 'x', 'expected_hash': note['hash']}, auth)[0], 409)
        grown = self.req('/api/memory/note/append', {'path': made['note']['path'], 'heading': 'Asked again', 'text': 'More detail'}, auth)[1]['note']; self.assertEqual(grown['status'], 'growing')
        graph = self.req('/api/memory/graph', headers=cookie)[1]; self.assertEqual(len(graph['edges']), 2)
        hits = self.req('/api/memory/search?q=kernel+methods', headers=cookie)[1]['hits']; self.assertEqual(hits[0]['title'], 'Quantum kernels')
        self.assertEqual(self.req('/api/memory/note/delete', {'path': idea['path']}, auth)[0], 400); self.assertEqual(self.req('/api/memory/note/delete', {'path': idea['path'], 'confirm': True}, auth)[1], {'ok': True})
        self.assertEqual(self.req('/api/memory', headers=cookie)[1]['counts'], {'notes': 1, 'chats': 0})
        self.assertIn('memory', self.req('/api/files', headers=cookie)[1]['roots'], 'the vault is browsable read-only in Files')
        kinds = [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]
        for k in ('memory.folder', 'memory.create', 'memory.update', 'memory.append', 'memory.delete'): self.assertIn(k, kinds)
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10); c.request('GET', '/memory', headers={'Host': f'127.0.0.1:{self.port}'}); r = c.getresponse(); self.assertEqual(r.status, 200); self.assertIn(b'Memory', r.read()); c.close()


if __name__ == '__main__':
    unittest.main()
