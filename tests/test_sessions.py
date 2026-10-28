"""Several sessions on one device, per-cell "run on", the journal and the read-only Files API."""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_lab import files, graph as g, journal as journal_module, sessions as sessions_module
from local_lab.kernel_session import KernelSession, KernelError
from local_lab.runner import KERNEL_DIR
from local_lab.server import LabServer, initialize_state


def local_factory(settings, state_dir):
    return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local-' + settings.get('cpus', '?'))


class JournalTests(unittest.TestCase):
    def test_append_tail_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = journal_module.Journal(tmp)
            a = j.write('session.start', 'first', {'id': 1}); b = j.write('run.start', 'second')
            self.assertEqual((a['seq'], b['seq']), (1, 2))
            self.assertEqual([e['text'] for e in j.tail()], ['first', 'second'])
            self.assertEqual([e['seq'] for e in j.tail(after=1)], [2])
            self.assertEqual(oct(Path(tmp, 'journal.jsonl').stat().st_mode & 0o777), '0o600')
            again = journal_module.Journal(tmp)
            self.assertEqual(again.seq, 2, 'seq continues after a restart')
            j.write('x', 'y', {'big': 'z' * 10000})
            self.assertEqual(j.tail()[-1]['data'], {'truncated': True})
            with patch.object(journal_module, 'MAX_BYTES', 2000):
                for i in range(100): j.write('fill', 'line %d' % i)
            kept = j.tail(limit=1000)
            self.assertLess(len(kept), 100); self.assertEqual(kept[-1]['text'], 'line 99'); self.assertGreater(kept[0]['seq'], 1)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.reg = sessions_module.SessionRegistry(local_factory, self.tmp.name, journal_module.Journal(self.tmp.name))
        self.settings = {'workspace': None, 'cpus': '0.5', 'memory': '256m'}
    def tearDown(self):
        self.reg.stop_all(); self.tmp.cleanup()
    def test_two_sessions_separate_namespaces_and_active(self):
        a = self.reg.start(self.settings, label='cpu'); b = self.reg.start(dict(self.settings, cpus='2'), label='big', image='mainaiger-lab-env:abc')
        self.assertTrue(a['active']); self.assertFalse(b['active']); self.assertEqual((b['cpus'], b['image'], b['label']), ('2', 'mainaiger-lab-env:abc', 'big'))
        self.reg.kernel_for('cpu').execute('x = 1'); self.reg.kernel_for('big').execute('y = 2')
        self.assertEqual([v['name'] for v in self.reg.kernel_for('cpu').variables()], ['x'])
        self.assertEqual([v['name'] for v in self.reg.kernel_for('big').variables()], ['y'], 'sessions do not share variables')
        self.assertIs(self.reg.kernel_for(None), self.reg.kernel_for('cpu'))
        self.reg.select(b['id']); self.assertIs(self.reg.kernel_for(None), self.reg.kernel_for('big'))
        with self.assertRaises(KernelError): self.reg.kernel_for('nope')
        self.assertEqual(self.reg.start(self.settings, label='cpu')['label'], 'cpu 2', 'labels stay unique')
    def test_cap_stop_forget_and_dead_sessions(self):
        rows = [self.reg.start(self.settings) for _ in range(sessions_module.MAX_SESSIONS)]
        with self.assertRaises(KernelError): self.reg.start(self.settings)
        self.reg.stop(rows[0]['id'])
        self.assertFalse(self.reg.get(rows[0]['id'])['kernel'].alive)
        with self.assertRaises(KernelError): self.reg.kernel_for(rows[0]['label'])
        self.assertEqual(len(self.reg.list()), sessions_module.MAX_SESSIONS, 'stopped sessions stay listed')
        self.reg.start(self.settings)  # the dead one does not count against the cap
        with self.assertRaises(KernelError): self.reg.forget(rows[1]['id'])  # alive
        self.reg.forget(rows[0]['id']); self.assertIsNone(self.reg.get(rows[0]['id']))
        for bad in ('a/b', 'x' * 40, ''):
            with self.subTest(bad=bad), self.assertRaises(KernelError): self.reg.start(self.settings, label=bad)
    def test_journal_records_lifecycle(self):
        row = self.reg.start(self.settings, label='j'); self.reg.interrupt(row['id']); self.reg.stop(row['id'])
        kinds = [e['kind'] for e in self.reg.journal.tail()]
        self.assertEqual(kinds, ['session.start', 'session.interrupt', 'session.stop'])


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve(); self.ws = self.root / 'ws'; self.ws.mkdir()
        (self.ws / 'data').mkdir(); (self.ws / 'data' / 'a.csv').write_text('x,y\n1,2\n'); (self.ws / 'notes.md').write_text('hello'); (self.ws / 'blob.bin').write_bytes(b'\x00\x01\x02')
        (self.ws / '.hidden').write_text('h'); (self.root / 'outside.txt').write_text('secret'); (self.ws / 'link').symlink_to(self.root / 'outside.txt'); (self.ws / 'dirlink').symlink_to(self.root)
    def tearDown(self):
        self.tmp.cleanup()
    def test_listing_skips_symlinks_and_sorts_dirs_first(self):
        out = files.listing(self.ws)
        self.assertEqual([e['name'] for e in out['entries']], ['data', '.hidden', 'blob.bin', 'notes.md']); self.assertEqual(out['skipped'], 2)
        self.assertEqual([(e['kind'], e['text']) for e in out['entries']][0], ('dir', False)); self.assertTrue(out['entries'][3]['text'])
        self.assertEqual([e['path'] for e in files.listing(self.ws, 'data')['entries']], ['data/a.csv'])
    def test_read_text_binary_and_bounds(self):
        self.assertEqual(files.read(self.ws, 'notes.md')['text'], 'hello'); self.assertTrue(files.read(self.ws, 'blob.bin')['binary'])
        (self.ws / 'big.txt').write_text('a' * (files.MAX_PREVIEW + 10)); r = files.read(self.ws, 'big.txt'); self.assertTrue(r['truncated']); self.assertEqual(len(r['text']), files.MAX_PREVIEW)
    def test_escapes_are_refused(self):
        for bad in ('../outside.txt', 'link', 'dirlink', 'dirlink/outside.txt', '/etc/passwd', 'data/../../outside.txt', 'nope', 'a\x00b'):
            with self.subTest(bad=bad), self.assertRaises(files.FilesError):
                files.read(self.ws, bad)
        with self.assertRaises(files.FilesError): files.listing(self.ws, 'dirlink')
        self.assertEqual((self.root / 'outside.txt').read_text(), 'secret')
    def test_roots_follow_settings_and_approved_folders(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(files.roots({'workspace': None}, state), {})
            self.assertEqual(files.roots({'workspace': str(self.ws)}, state), {'workspace': str(self.ws)})


class SessionsHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
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
        for _ in range(400):
            run = self.req('/api/graph/run', headers=cookie)[1]['run']
            if run['state'] not in ('pending', 'running'): return run
            time.sleep(0.05)
        self.fail('run did not finish')
    def test_resources_table_per_cell_run_on_journal_and_files(self):
        self.assertEqual(self.req('/api/sessions')[0], 401); self.assertEqual(self.req('/api/journal')[0], 401); self.assertEqual(self.req('/api/files')[0], 401)
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        status, data, _ = self.req('/api/sessions', headers=cookie)
        self.assertEqual((status, data['sessions'], data['active'], data['max']), (200, [], None, 4)); self.assertIn('0.5', data['options']['cpus'])
        rev = data['revision']
        self.assertEqual(self.req('/api/sessions/start', {'confirm': True, 'revision': rev, 'cpus': '7'}, auth)[0], 400)
        self.assertEqual(self.req('/api/sessions/start', {'revision': rev}, auth)[0], 400)
        s1 = self.req('/api/sessions/start', {'confirm': True, 'revision': rev, 'label': 'main'}, auth)[1]
        s2 = self.req('/api/sessions/start', {'confirm': True, 'revision': rev, 'label': 'worker', 'cpus': '2', 'memory': '1g'}, auth)[1]
        self.assertTrue(s1['active']); self.assertEqual((s2['cpus'], s2['memory'], s2['active']), ('2', '1g', False))
        self.assertEqual(self.req('/api/kernel/start', {'confirm': True, 'revision': rev}, auth)[0], 409, 'legacy route stays one-at-a-time')
        self.assertEqual(self.req('/api/settings', {'workspace': '', 'cpus': '1', 'memory': '512m'}, auth)[0], 409, 'limits are frozen while any session runs')
        # A graph where the second cell names the worker session: the variable does not cross, and preflight says so.
        doc = g.new_document(); doc['notebook']['cells'][0]['source'] = 'x = 41\nprint(x)\n'
        doc['notebook']['cells'].append({'id': 'c2', 'cell_type': 'code', 'metadata': {'mainaiger': {'title': 'Use x', 'session': 'worker'}}, 'source': 'print(x + 1)\n', 'execution_count': None, 'outputs': []})
        doc['graph']['nodes'].insert(2, {'id': 'c2', 'kind': 'code', 'cell_index': 1}); doc['graph']['edges'] = [{'id': 'e1', 'kind': 'control', 'source': 'start', 'target': 'cell-1'}, {'id': 'e2', 'kind': 'control', 'source': 'cell-1', 'target': 'c2'}, {'id': 'e3', 'kind': 'control', 'source': 'c2', 'target': 'finish'}]
        self.assertEqual(self.req('/api/graph', {'document': doc}, auth)[0], 200)
        check = self.req('/api/graph/preflight', {'mode': 'all', 'nodes': [], 'include_prerequisites': False}, auth)[1]
        self.assertEqual(check['sessions'], {'c2': 'worker'}); self.assertEqual(check['missing_sessions'], [])
        self.assertEqual(check['cross_session'], [{'name': 'x', 'producer': 'cell-1', 'consumer': 'c2', 'producer_session': None, 'consumer_session': 'worker'}])
        run = self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth)[1]['run']
        run = self.wait_run(cookie)
        self.assertEqual(run['nodes']['cell-1']['status'], 'ok'); self.assertEqual(run['nodes']['c2']['status'], 'error', 'x is not in the worker session: truthful NameError')
        self.assertEqual(run['nodes']['c2']['session'], 'worker'); self.assertIn('NameError', run['nodes']['c2']['error']['ename'])
        self.assertEqual([v['name'] for v in self.server.kernels.by_label('worker')['kernel'].variables()], [])
        # A cell naming a stopped session cannot even start a run.
        self.req('/api/sessions/stop', {'id': s2['id']}, auth)
        check = self.req('/api/graph/preflight', {'mode': 'all', 'nodes': [], 'include_prerequisites': False}, auth)[1]
        self.assertEqual(check['missing_sessions'], ['worker'])
        status, data, _ = self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth)
        self.assertEqual(status, 409); self.assertIn('worker', data['error'])
        # Select, interrupt, forget.
        self.assertEqual(self.req('/api/sessions/select', {'id': s2['id']}, auth)[1]['session']['active'], True)
        self.assertEqual(self.req('/api/sessions/interrupt', {'id': s2['id']}, auth)[0], 409)  # stopped
        self.assertEqual(self.req('/api/sessions/forget', {'id': s1['id']}, auth)[0], 409)    # alive
        self.assertEqual(self.req('/api/sessions/forget', {'id': s2['id']}, auth)[1], {'ok': True})
        self.assertEqual([s['label'] for s in self.req('/api/sessions', headers=cookie)[1]['sessions']], ['main'])
        # The journal tells the story in order.
        kinds = [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]
        self.assertEqual(kinds[:2], ['session.start', 'session.start']); self.assertIn('run.start', kinds); self.assertIn('cell.ok', kinds); self.assertIn('cell.error', kinds); self.assertIn('run.failed', kinds); self.assertIn('session.stop', kinds)
        after = self.req('/api/journal?after=%d' % (len(kinds) - 1), headers=cookie)[1]['entries']; self.assertEqual(len(after), 1)
        # Files: no roots until a workspace or an approved folder exists; then read-only browsing.
        self.assertEqual(self.req('/api/files', headers=cookie)[1]['roots'], {})
        books = self.root / 'Documents' / 'Books'; self.req('/api/mainbooks/folder', {'folder': str(books), 'confirm': True}, auth)
        (books / 'one.ipymb').write_text(json.dumps(g.new_document()))
        data = self.req('/api/files?root=mainbooks', headers=cookie)[1]
        self.assertEqual([e['name'] for e in data['entries']], ['one.ipymb']); self.assertIn('mainbooks', data['roots'])
        self.assertEqual(self.req('/api/files?root=nope', headers=cookie)[0], 404)
        self.assertEqual(self.req('/api/files/read?root=mainbooks&path=../private/owner.key', headers=cookie)[0], 400)
        self.assertIn('"format": "mainaiger"', self.req('/api/files/read?root=mainbooks&path=one.ipymb', headers=cookie)[1]['text'])


if __name__ == '__main__':
    unittest.main()
