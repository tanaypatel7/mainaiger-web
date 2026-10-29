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


class ModelCellTests(unittest.TestCase):
    """A model cell: prompt rendered from cell outputs and variable summaries, answer bound into the session."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'), kernel_factory=local_factory)
        self.asked = []
        def fake_ask(cfg, prompt):
            self.asked.append((cfg, prompt))
            if cfg.get('schema'): return {'ok': True, 'value': {'action': 'keep', 'confidence': 0.9}, 'raw': '{"action":"keep","confidence":0.9}', 'error': None, 'elapsed_ms': 12, 'attempts': 1, 'model': cfg['model']}
            if 'fail' in prompt: return {'ok': False, 'value': None, 'raw': None, 'error': 'Model x is not downloaded yet.', 'elapsed_ms': 1, 'attempts': 1, 'model': cfg['model']}
            return {'ok': True, 'value': 'The accuracy looks fine.', 'raw': 'The accuracy looks fine.', 'error': None, 'elapsed_ms': 12, 'attempts': 1, 'model': cfg['model']}
        self.server.ask_model = fake_ask
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    req = SessionsHttpTests.req; login = SessionsHttpTests.login; wait_run = SessionsHttpTests.wait_run
    def doc(self, prompt, schema=None, output='verdict'):
        doc = g.new_document(); doc['notebook']['cells'][0]['source'] = 'metrics = {"accuracy": 0.9}\nprint("accuracy 0.9")\n'; doc['notebook']['cells'][0]['metadata']['mainaiger']['title'] = 'Evaluate'
        cfg = {'model': 'llama3.2:3b', 'output': output}
        if schema: cfg['schema'] = schema
        doc['notebook']['cells'].append({'id': 'judge', 'cell_type': 'code', 'metadata': {'mainaiger': {'title': 'Judge', 'model': cfg}}, 'source': prompt, 'execution_count': None, 'outputs': []})
        doc['notebook']['cells'].append({'id': 'use', 'cell_type': 'code', 'metadata': {'mainaiger': {'title': 'Use'}}, 'source': 'print(type(%s).__name__, %s)\n' % (output, output), 'execution_count': None, 'outputs': []})
        doc['graph']['nodes'] = [{'id': 'start', 'kind': 'start'}, {'id': 'cell-1', 'kind': 'code', 'cell_index': 0}, {'id': 'judge', 'kind': 'code', 'cell_index': 1}, {'id': 'use', 'kind': 'code', 'cell_index': 2}, {'id': 'finish', 'kind': 'finish'}]
        doc['graph']['edges'] = [{'id': 'e1', 'kind': 'control', 'source': 'start', 'target': 'cell-1'}, {'id': 'e2', 'kind': 'control', 'source': 'cell-1', 'target': 'judge'}, {'id': 'e3', 'kind': 'control', 'source': 'judge', 'target': 'use'}, {'id': 'e4', 'kind': 'control', 'source': 'use', 'target': 'finish'}]
        return doc
    def test_plain_reply_is_bound_as_text_and_data_edges_connect(self):
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        status, view, _ = self.req('/api/graph', {'document': self.doc('Here is what Evaluate printed:\n{{output:Evaluate}}\nAnd the variable {{var:metrics}}. Is it fine?')}, auth)
        self.assertEqual(status, 200); self.assertEqual(view['analysis']['judge'], {'inputs': ['metrics'], 'outputs': ['verdict'], 'error': None, 'model': 'llama3.2:3b', 'declared': True})
        self.assertIn({'source': 'judge', 'target': 'use', 'name': 'verdict'}, [{k: e[k] for k in ('source', 'target', 'name')} for e in view['data_edges']])
        self.assertIn({'source': 'cell-1', 'target': 'judge', 'name': 'metrics'}, [{k: e[k] for k in ('source', 'target', 'name')} for e in view['data_edges']])
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; self.req('/api/sessions/start', {'confirm': True, 'revision': rev, 'label': 'main'}, auth)
        self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); run = self.wait_run(cookie)
        self.assertEqual([run['nodes'][i]['status'] for i in ('cell-1', 'judge', 'use')], ['ok', 'ok', 'ok'], run)
        self.assertEqual(run['nodes']['judge']['model'], 'llama3.2:3b'); self.assertIn('Bound to verdict', run['nodes']['judge']['note'])
        cfg, prompt = self.asked[0]
        self.assertIn('accuracy 0.9', prompt, 'the printed output was pasted into the prompt'); self.assertIn('metrics (dict) = 1 items', prompt, 'the variable summary was pasted'); self.assertNotIn('{{', prompt)
        self.assertEqual(run['nodes']['use']['output'].strip(), 'str The accuracy looks fine.')
        doc = self.req('/api/graph', headers=cookie)[1]['document']
        self.assertEqual(doc['notebook']['cells'][1]['outputs'][0]['text'], 'The accuracy looks fine.')
        exported = self.req('/api/graph/export', headers=cookie)[1]['notebook']; self.assertEqual(exported['cells'][1]['cell_type'], 'raw'); self.assertIn('model cell', exported['cells'][1]['source'])
    def test_schema_answer_is_bound_as_object_and_failures_are_truthful(self):
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        schema = {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['keep', 'revert']}, 'confidence': {'type': 'number'}}}
        self.req('/api/graph', {'document': self.doc('Keep or revert? {{output:Evaluate}}', schema=schema, output='decision')}, auth)
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; self.req('/api/sessions/start', {'confirm': True, 'revision': rev, 'label': 'main'}, auth)
        self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); run = self.wait_run(cookie)
        self.assertEqual(run['nodes']['use']['output'].strip(), "dict {'action': 'keep', 'confidence': 0.9}"); self.assertIn('schema-checked', run['nodes']['judge']['note'])
        # A bad placeholder is a PromptError before any model is asked; a failed model is an error that stops the run; both skip "use".
        for prompt, ename, evalue in (('{{output:Nowhere}}', 'PromptError', 'no code cell named'), ('{{var:ghost}}', 'PromptError', 'not defined in the session'), ('please fail', 'ModelError', 'not downloaded')):
            with self.subTest(prompt=prompt):
                before = len(self.asked); self.req('/api/graph', {'document': self.doc(prompt)}, auth)
                self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); run = self.wait_run(cookie)
                self.assertEqual((run['state'], run['nodes']['judge']['status'], run['nodes']['judge']['error']['ename'], run['nodes']['use']['status']), ('failed', 'error', ename, 'skipped'))
                self.assertIn(evalue, run['nodes']['judge']['error']['evalue'])
                if ename == 'PromptError': self.assertEqual(len(self.asked), before, 'no model call for a broken prompt')
        for bad in ({'model': 'Bad Name', 'output': 'x'}, {'model': 'llama3.2:3b', 'output': '1x'}, {'model': 'llama3.2:3b', 'output': 'x', 'schema': {'nope': 1}}, {'model': 'llama3.2:3b', 'output': 'x', 'attempts': 9}):
            with self.subTest(bad=bad):
                doc = self.doc('p'); doc['notebook']['cells'][1]['metadata']['mainaiger']['model'] = bad
                self.assertEqual(self.req('/api/graph', {'document': doc}, auth)[0], 400)


class TerminalTests(unittest.TestCase):
    def test_command_runs_inside_the_session_and_is_journaled(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = sessions_module.SessionRegistry(local_factory, tmp, journal_module.Journal(tmp)); reg.start({'workspace': None, 'cpus': '0.5', 'memory': '256m'}, label='main')
            try:
                row, result = reg.command('echo hello && exit 3')
                self.assertEqual(result['status'], 'ok'); self.assertEqual(result['output'], 'hello\n[exit 3]\n'); self.assertEqual(row['label'], 'main')
                self.assertEqual([v['name'] for v in reg.active_kernel().variables()], [], 'no helper names leak into the session')
                self.assertEqual(reg.journal.tail()[-1]['kind'], 'shell.command')
                for bad in ('', 'x' * 4001, 'a\x00b'):
                    with self.subTest(bad=bad), self.assertRaises(KernelError): reg.command(bad)
                reg.stop_all()
                with self.assertRaises(KernelError): reg.command('ls')
            finally:
                reg.stop_all()


class TerminalHttpTests(SessionsHttpTests):
    def test_resources_table_per_cell_run_on_journal_and_files(self):
        self.skipTest('inherited')
    def test_terminal_route(self):
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        self.assertEqual(self.req('/api/sessions/command', {'command': 'ls', 'confirm': True}, auth)[0], 409, 'no session yet')
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; s1 = self.req('/api/sessions/start', {'confirm': True, 'revision': rev, 'label': 'main'}, auth)[1]
        self.assertEqual(self.req('/api/sessions/command', {'command': 'ls'}, auth)[0], 400, 'confirm required')
        status, data, _ = self.req('/api/sessions/command', {'id': s1['id'], 'command': 'printf hi', 'confirm': True}, auth)
        self.assertEqual((status, data['result']['output']), (200, 'hi\n[exit 0]\n'))


class FormCellTests(unittest.TestCase):
    """Form cells: fields become variables when the cell runs."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'), kernel_factory=local_factory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    req = SessionsHttpTests.req; login = SessionsHttpTests.login; wait_run = SessionsHttpTests.wait_run
    def test_validation_declared_names_and_export(self):
        doc = g.new_document(); c = doc['notebook']['cells'][0]; c['source'] = 'total = n * price\nprint(total, mode, flag)\n'
        c['metadata']['mainaiger']['form'] = {'fields': [{'name': 'n', 'type': 'integer', 'default': 2}, {'name': 'mode', 'type': 'choice', 'options': ['a', 'b']}, {'name': 'flag', 'type': 'flag'}], 'values': {'n': 3, 'flag': True}}
        v = g.validate(doc)
        self.assertEqual(g.declared(v, 'cell-1'), {'inputs': ['price'], 'outputs': ['flag', 'mode', 'n', 'total'], 'error': None, 'form': True, 'declared': False})
        self.assertEqual(g.form_bindings(g.form_config(v['notebook']['cells'][0])), {'n': 3, 'mode': 'a', 'flag': True})
        exported = g.to_notebook(v)[0]['cells'][0]; self.assertTrue(exported['source'].startswith('# MainAIger form values\nn = ')); self.assertNotIn('form', exported['metadata']['mainaiger'])
        for bad in ({'fields': []}, {'fields': [{'name': '1x', 'type': 'text'}]}, {'fields': [{'name': 'a', 'type': 'blob'}]}, {'fields': [{'name': 'a', 'type': 'choice'}]}, {'fields': [{'name': 'a', 'type': 'integer', 'default': 'x'}]},
                    {'fields': [{'name': 'a', 'type': 'flag'}], 'values': {'a': 'yes'}}, {'fields': [{'name': 'a', 'type': 'choice', 'options': ['x']}], 'values': {'a': 'y'}}, {'fields': [{'name': 'a', 'type': 'text'}, {'name': 'a', 'type': 'text'}]}, {'fields': [{'name': 'f%d' % i, 'type': 'text'} for i in range(13)]}):
            with self.subTest(bad=bad):
                doc2 = g.new_document(); doc2['notebook']['cells'][0]['metadata']['mainaiger']['form'] = bad
                with self.assertRaises(g.GraphError): g.validate(doc2)
        doc3 = g.new_document(); doc3['notebook']['cells'][0]['metadata']['mainaiger']['form'] = {'fields': [{'name': 'a', 'type': 'text'}], 'values': {'a': 'x', 'ghost': 1}}
        self.assertEqual(g.validate(doc3)['notebook']['cells'][0]['metadata']['mainaiger']['form']['values'], {'a': 'x'}, 'values for unknown fields are dropped')
    def test_values_are_bound_before_the_code_runs(self):
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        doc = g.new_document(); c = doc['notebook']['cells'][0]; c['source'] = 'print("form:", n, mode, flag, repr(note))\n'
        c['metadata']['mainaiger']['form'] = {'fields': [{'name': 'n', 'type': 'number', 'default': 1.5}, {'name': 'mode', 'type': 'choice', 'options': ['fast', 'slow']}, {'name': 'flag', 'type': 'flag', 'default': True}, {'name': 'note', 'type': 'text'}], 'values': {'mode': 'slow', 'note': 'he said "hi"'}}
        doc['notebook']['cells'].append({'id': 'use', 'cell_type': 'code', 'metadata': {'mainaiger': {'title': 'Use'}}, 'source': 'print(n * 2, mode.upper())\n', 'execution_count': None, 'outputs': []})
        doc['graph']['nodes'].insert(2, {'id': 'use', 'kind': 'code', 'cell_index': 1}); doc['graph']['edges'] = [{'id': 'e1', 'kind': 'control', 'source': 'start', 'target': 'cell-1'}, {'id': 'e2', 'kind': 'control', 'source': 'cell-1', 'target': 'use'}, {'id': 'e3', 'kind': 'control', 'source': 'use', 'target': 'finish'}]
        status, view, _ = self.req('/api/graph', {'document': doc}, auth); self.assertEqual(status, 200)
        self.assertIn({'source': 'cell-1', 'target': 'use', 'name': 'n'}, [{k: e[k] for k in ('source', 'target', 'name')} for e in view['data_edges']], 'form fields feed data edges')
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; self.req('/api/sessions/start', {'confirm': True, 'revision': rev, 'label': 'main'}, auth)
        self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); run = self.wait_run(cookie)
        self.assertEqual(run['nodes']['cell-1']['output'], 'form: 1.5 slow True \'he said "hi"\'\n'); self.assertIn('Form values bound', run['nodes']['cell-1']['note'])
        self.assertEqual(run['nodes']['use']['output'], '3.0 SLOW\n')
