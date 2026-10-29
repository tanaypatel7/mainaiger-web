"""Event triggers: sources on Start, arming as a reviewed step, timer/file/chat/manual firings, the queue.

Registry tests drive the watcher by hand (``tick()``) with a fake clock; HTTP tests run a real
LabServer with a local kernel so a fired run really binds the event and executes cells.
"""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_lab import events as ev, graph as g


class SourceTests(unittest.TestCase):
    def test_sources_are_checked_and_normalized(self):
        clean = ev.check_sources([{'kind': 'timer', 'every': 60}, {'kind': 'file', 'path': './data/in/'}, {'kind': 'chat'}, {'kind': 'manual'}])
        self.assertEqual(clean, [{'kind': 'timer', 'every': 60}, {'kind': 'file', 'path': 'data/in'}, {'kind': 'chat'}, {'kind': 'manual'}])
        self.assertEqual(ev.check_sources(None), []); self.assertEqual(ev.check_sources([]), [])
        for bad, message in (([{'kind': 'cron'}], 'kind must be'), ([{'kind': 'timer', 'every': 5}], 'between'), ([{'kind': 'timer', 'every': True}], 'between'), ([{'kind': 'timer', 'every': 60, 'x': 1}], 'only "every"'),
                             ([{'kind': 'file', 'path': '/etc'}], 'relative'), ([{'kind': 'file', 'path': '../x'}], 'leave the workspace'), ([{'kind': 'chat', 'every': 1}], 'no settings'),
                             ([{'kind': 'chat'}, {'kind': 'chat'}], 'repeats'), ([{'kind': 'manual'}] * 9, 'at most'), ('timer', 'list')):
            with self.subTest(bad=bad), self.assertRaisesRegex(ev.EventError, message):
                ev.check_sources(bad)
    def test_start_node_carries_sources_and_the_document_validates_them(self):
        doc = g.new_document(); start = doc['graph']['nodes'][0]; start['events'] = [{'kind': 'timer', 'every': 30}, {'kind': 'file', 'path': 'inbox'}]
        clean = g.validate(doc); self.assertEqual(g.start_events(clean), [{'kind': 'timer', 'every': 30}, {'kind': 'file', 'path': 'inbox'}])
        start['events'] = []; self.assertNotIn('events', g.validate(doc)['graph']['nodes'][0])
        start['events'] = [{'kind': 'timer'}]
        with self.assertRaisesRegex(g.GraphError, 'Start events'): g.validate(doc)
    def test_fingerprint_ignores_layout_and_outputs(self):
        doc = g.validate(g.new_document()); fp = g.fingerprint(doc)
        doc['layout']['nodes']['cell-1'] = {'x': 999, 'y': 999}; doc['notebook']['cells'][0]['outputs'] = [{'output_type': 'stream', 'name': 'stdout', 'text': 'hi'}]; doc['notebook']['cells'][0]['execution_count'] = 7
        doc['notebook']['cells'][0]['metadata']['mainaiger']['last_run'] = {'status': 'ok'}
        self.assertEqual(g.fingerprint(doc), fp)
        doc['notebook']['cells'][0]['source'] = 'print(2)'; self.assertNotEqual(g.fingerprint(doc), fp)
        doc = g.validate(g.new_document()); doc['graph']['nodes'][0]['events'] = [{'kind': 'manual'}]; self.assertNotEqual(g.fingerprint(doc), fp, 'sources are part of what a run depends on')
    def test_descriptions_and_bindings(self):
        self.assertEqual(ev.describe({'kind': 'timer', 'every': 60}), 'Every 60 s while armed (bound as now)'); self.assertIn('5 min', ev.describe({'kind': 'timer', 'every': 300})); self.assertIn('2.0 h', ev.describe({'kind': 'timer', 'every': 7200}))
        self.assertIn('/ws/data', ev.describe({'kind': 'file', 'path': 'data'}, '/ws')); self.assertIn('Models page', ev.describe({'kind': 'chat'})); self.assertIn('Fire now', ev.describe({'kind': 'manual'}))
        b = ev.bindings_for('chat', {'prompt': 'hello', 'model': 'm'}, 5.0); self.assertEqual(b, {'event': {'prompt': 'hello', 'model': 'm', 'kind': 'chat', 'at': 5.0}, 'prompt': 'hello'})
        self.assertEqual(ev.bindings_for('timer', {}, 1.0)['now'], 1.0); self.assertEqual(ev.bindings_for('file', {'path': 'a.csv'}, 1.0)['path'], 'a.csv'); self.assertNotIn('prompt', ev.bindings_for('manual', {'x': 1}, 1.0))


class FakeJournal:
    def __init__(self): self.lines = []
    def write(self, kind, text, data=None): self.lines.append((kind, text, data))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]; self.runs = []; self.busy = [False]; self.journal = FakeJournal()
        self.tmp = tempfile.TemporaryDirectory(); self.ws = Path(self.tmp.name); (self.ws / 'data').mkdir(); (self.ws / 'data' / 'a.txt').write_text('1')
        def start_run(bindings, reason):
            if self.busy[0]: raise RuntimeError('A graph run is already in progress')
            self.runs.append((bindings, reason)); return {'id': 'run-%d' % len(self.runs)}
        self.reg = ev.EventRegistry(start_run, self.journal, running=lambda: self.busy[0], workspace=lambda: str(self.ws), clock=lambda: self.now[0])
        self.reg._ensure_thread = lambda: None  # tests drive tick() by hand
    def tearDown(self):
        self.reg.close(); self.tmp.cleanup()
    def kinds(self):
        return [k for k, _, _ in self.journal.lines]

    def test_nothing_fires_until_armed_and_arming_is_reviewed_content(self):
        self.assertEqual(self.reg.fire('manual'), 'not armed'); self.assertEqual(self.reg.status()['dropped'], 1); self.assertEqual(self.runs, [])
        with self.assertRaisesRegex(ev.EventError, 'no event sources'): self.reg.arm([], 'fp')
        st = self.reg.arm([{'kind': 'manual'}, {'kind': 'timer', 'every': 10}], 'fp1', 'Demo')
        self.assertTrue(st['armed']); self.assertEqual([s['description'] for s in st['sources']], ['When you press Fire now (payload bound as event)', 'Every 10 s while armed (bound as now)'])
        self.assertEqual(self.kinds()[-1], 'events.arm'); self.assertIn('Armed Demo', self.journal.lines[-1][1])
        self.assertEqual(self.reg.fire('manual', {'x': 1}, origin='owner'), 'queued'); self.reg.tick()
        self.assertEqual(len(self.runs), 1); self.assertEqual(self.runs[0][0]['event'], {'x': 1, 'kind': 'manual', 'at': 1000.0}); self.assertEqual(self.runs[0][1], 'fired by manual')
        self.assertEqual(self.reg.status()['recent'][0]['outcome'], 'run run-1'); self.assertEqual(self.kinds()[-1], 'event.fired')
        self.assertEqual(self.reg.fire('chat', {'prompt': 'x'}), 'no chat source', 'a kind that is not a source of this Mainbook is dropped')
        self.assertFalse(self.reg.graph_changed('fp1')); self.assertTrue(self.reg.graph_changed('fp2')); self.assertFalse(self.reg.status()['armed']); self.assertIn('changed', self.journal.lines[-1][1])

    def test_timer_fires_on_schedule_and_ticks_are_coalesced_while_busy(self):
        self.reg.arm([{'kind': 'timer', 'every': 10}], 'fp')
        self.reg.tick(); self.assertEqual(self.runs, [], 'not due yet')
        self.now[0] = 1010.0; self.reg.tick(); self.assertEqual(len(self.runs), 1); self.assertEqual(self.runs[0][0]['now'], 1010.0); self.assertEqual(self.reg.status()['next_timer'], 1020.0)
        self.busy[0] = True
        self.now[0] = 1020.0; self.reg.tick(); self.now[0] = 1030.0; self.reg.tick(); self.now[0] = 1040.0; self.reg.tick()
        self.assertEqual(self.reg.status()['queue'], 1, 'timer ticks during a run collapse into one queued event'); self.assertEqual(len(self.runs), 1)
        self.busy[0] = False; self.reg.tick(); self.assertEqual(len(self.runs), 2)

    def test_file_changes_fire_with_the_changed_path(self):
        self.reg.arm([{'kind': 'file', 'path': 'data'}], 'fp')
        self.now[0] += 3; self.reg.tick(); self.assertEqual(self.runs, [], 'nothing changed')
        (self.ws / 'data' / 'b.csv').write_text('new'); self.now[0] += 3; self.reg.tick()
        self.assertEqual(len(self.runs), 1); self.assertEqual(self.runs[0][0]['path'], 'data/b.csv'); self.assertEqual(self.runs[0][0]['event']['changes'], ['data/b.csv']); self.assertEqual(self.runs[0][1], 'fired by file (data/b.csv)')
        self.now[0] += 3; self.reg.tick(); self.assertEqual(len(self.runs), 1, 'a change fires once')
        (self.ws / 'data' / 'a.txt').write_text('22'); self.now[0] += 3; self.reg.tick(); self.assertEqual(self.runs[-1][0]['path'], 'data/a.txt')
        (self.ws / 'data' / 'a.txt').unlink(); self.now[0] += 3; self.reg.tick(); self.assertEqual(self.runs[-1][0]['path'], 'data/a.txt'); self.assertEqual(len(self.runs), 3)
        with self.assertRaisesRegex(ev.EventError, 'workspace'):
            ev.EventRegistry(lambda b, r: None, None, workspace=lambda: None).arm([{'kind': 'file', 'path': 'x'}], 'fp')

    def test_queue_cap_drops_and_a_failed_start_is_reported_not_raised(self):
        self.reg.arm([{'kind': 'manual'}], 'fp'); self.busy[0] = True
        for _ in range(ev.QUEUE_MAX): self.assertEqual(self.reg.fire('manual'), 'queued')
        self.assertEqual(self.reg.fire('manual'), 'queue full'); self.assertEqual(self.reg.status()['dropped'], 1)
        self.busy[0] = False
        self.reg.start_run = lambda b, r: (_ for _ in ()).throw(RuntimeError('Start a session before running the graph'))
        self.reg.tick(); self.assertEqual(self.reg.status()['queue'], ev.QUEUE_MAX - 1); self.assertEqual(self.kinds()[-1], 'event.dropped'); self.assertIn('Start a session', self.journal.lines[-1][1])
        self.reg.disarm(); self.assertEqual(self.reg.status()['queue'], 0); self.assertEqual(self.kinds()[-1], 'events.disarm')


class HttpTests(unittest.TestCase):
    def setUp(self):
        from local_lab.server import LabServer, initialize_state
        from tests.test_sessions import local_factory
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve(); (self.root / 'ws').mkdir()
        state, key = initialize_state(self.root / 'private'); self.key = key
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'), kernel_factory=local_factory)
        self.server.settings['workspace'] = str(self.root / 'ws')
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
    def wait_run(self, cookie, n=None):
        for _ in range(600):
            run = self.req('/api/graph/run', headers=cookie)[1]['run']
            if run and run['state'] not in ('pending', 'running') and (n is None or run['id'] != n): return run
            time.sleep(0.05)
        self.fail('run did not finish')
    def test_arm_fire_bind_and_disarm_over_http(self):
        self.assertEqual(self.req('/api/events')[0], 401)
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        doc = g.new_document(); doc['graph']['nodes'][0]['events'] = [{'kind': 'manual'}, {'kind': 'chat'}, {'kind': 'file', 'path': 'inbox'}]
        doc['notebook']['cells'][0]['source'] = 'print("kind:", event["kind"], "prompt:", globals().get("prompt"), "path:", globals().get("path"))'
        doc['notebook']['metadata']['mainaiger'] = {'title': 'Reactor'}
        self.assertEqual(self.req('/api/graph', {'document': doc}, auth)[0], 200)
        status, st, _ = self.req('/api/events', headers=cookie)
        self.assertEqual((status, st['armed'], st['document']['title'], [s['kind'] for s in st['document']['sources']]), (200, False, 'Reactor', ['manual', 'chat', 'file']))
        self.assertIn('inbox', st['document']['sources'][2]['description']); self.assertEqual(st['limits']['queue'], ev.QUEUE_MAX)
        self.assertEqual(self.req('/api/events/fire', {'kind': 'manual', 'confirm': True}, auth)[0], 409, 'not armed: dropped')
        self.assertEqual(self.req('/api/events/arm', {'confirm': True, 'revision': st['document']['revision'] + 5}, auth)[0], 409, 'stale review')
        status, armed, _ = self.req('/api/events/arm', {'confirm': True, 'revision': st['document']['revision']}, auth)
        self.assertEqual((status, armed['armed'], armed['title']), (200, True, 'Reactor'))
        # A session is needed: a firing without one is dropped and said so.
        self.assertEqual(self.req('/api/events/fire', {'kind': 'manual', 'payload': {'who': 'me'}, 'confirm': True}, auth)[1]['outcome'], 'queued')
        for _ in range(40):
            if self.server.events.status()['dropped'] == 2: break
            time.sleep(0.1)
        self.assertEqual(self.server.events.status()['dropped'], 2, 'the earlier "not armed" firing and this one without a session'); self.assertIn('Start a session', self.server.events.status()['recent'][0]['outcome'])
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; self.req('/api/sessions/start', {'confirm': True, 'revision': rev}, auth)
        self.assertEqual(self.req('/api/events/fire', {'kind': 'manual', 'payload': {'who': 'me'}, 'confirm': True}, auth)[1]['outcome'], 'queued')
        run = self.wait_run(cookie); self.assertEqual((run['state'], run['reason'], run['event']['who'], run['event']['kind']), ('finished', 'fired by manual', 'me', 'manual'))
        self.assertEqual(run['nodes']['cell-1']['output'], 'kind: manual prompt: None path: None\n')
        # A chat prompt fires too (the chat itself fails here: no Ollama), with the prompt bound.
        with patch('local_lab.server.lab_models.chat', return_value={'ok': False, 'error': 'no ollama'}):
            status, data, _ = self.req('/api/models/chat', {'model': 'llama3.2:3b', 'messages': [{'role': 'user', 'content': 'summarise inbox'}]}, auth)
        self.assertEqual(data['event'], 'queued')
        run2 = self.wait_run(cookie, run['id']); self.assertEqual(run2['nodes']['cell-1']['output'], 'kind: chat prompt: summarise inbox path: None\n'); self.assertEqual(run2['reason'], 'fired by chat')
        # A file appearing under the workspace fires with its path.
        (self.root / 'ws' / 'inbox').mkdir(); (self.root / 'ws' / 'inbox' / 'new.csv').write_text('a,b')
        run3 = self.wait_run(cookie, run2['id']); self.assertEqual(run3['nodes']['cell-1']['output'], 'kind: file prompt: summarise inbox path: inbox/new.csv\n', 'the session keeps variables between runs: `prompt` is the old one; event["kind"] says what fired now')
        self.assertEqual(run3['event']['changes'], ['inbox/new.csv'])
        self.assertEqual(self.server.events.status()['fired'], 3)
        kinds = [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]; self.assertEqual(kinds.count('event.fired'), 3); self.assertIn('events.arm', kinds); self.assertIn('event.dropped', kinds)
        texts = [e['text'] for e in self.req('/api/journal', headers=cookie)[1]['entries'] if e['kind'] == 'run.start']; self.assertIn('Run all fired by chat: 1 cell', texts)
        # Editing a cell disarms; moving a card does not.
        view = self.req('/api/graph', headers=cookie)[1]['document']; view['layout']['nodes']['cell-1'] = {'x': 5, 'y': 5}
        self.req('/api/graph', {'document': view}, auth); self.assertTrue(self.req('/api/events', headers=cookie)[1]['armed'])
        view['notebook']['cells'][0]['source'] = 'print("changed")'; self.req('/api/graph', {'document': view}, auth)
        st = self.req('/api/events', headers=cookie)[1]; self.assertFalse(st['armed'])
        self.assertEqual(self.req('/api/journal', headers=cookie)[1]['entries'][-1]['kind'], 'events.disarm', 'the journal API lists oldest first')
        self.assertEqual(self.req('/api/events/disarm', {}, auth)[1]['armed'], False)


if __name__ == '__main__':
    unittest.main()
