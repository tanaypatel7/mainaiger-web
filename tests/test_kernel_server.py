"""HTTP tests for the persistent-session endpoints.

The server is given a kernel factory that starts the real kernel as a local
process, so these cover authentication, approval, state transitions and the
JSON contract the browser relies on. Docker is not involved.
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from local_lab.kernel_session import KernelSession
from local_lab.runner import KERNEL_DIR
from local_lab.server import LabServer, initialize_state


class KernelHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state, key = initialize_state(Path(self.tmp.name) / 'private')
        self.key = key
        self.started = []
        def factory(settings):
            self.started.append(settings)
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local-test')
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot run used'), kernel_factory=factory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
        kernel = self.server.kernel
        if kernel is not None:
            self.assertFalse(kernel.alive)  # server_close stops the session

    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close()
        return r.status, json.loads(payload), head

    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        self.assertEqual(status, 200)
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}

    def test_session_requires_login_csrf_and_explicit_confirmation(self):
        self.assertEqual(self.req('/api/kernel')[0], 401)
        self.assertEqual(self.req('/api/kernel/start', {'confirm': True, 'revision': 0}, {'Origin': self.origin})[0], 403)
        auth = self.login()
        self.assertEqual(self.req('/api/kernel/start', {'confirm': False, 'revision': 0}, auth)[0], 400)
        self.assertEqual(self.req('/api/kernel/start', {'confirm': True, 'revision': 5}, auth)[0], 409)
        self.assertEqual(self.req('/api/kernel/execute', {'code': 'print(1)', 'confirm': True}, auth)[0], 409)  # no session yet
        self.assertEqual(self.started, [])

    def test_full_session_lifecycle_over_http(self):
        auth = self.login()
        status, data, _ = self.req('/api/state', headers={'Cookie': auth['Cookie']})
        self.assertFalse(data['session']['alive'])
        self.assertEqual(data['session_command'][-1], '/opt/mainaiger/kernel.py')
        status, started, _ = self.req('/api/kernel/start', {'confirm': True, 'revision': 0}, auth)
        self.assertEqual(status, 200); self.assertTrue(started['alive']); self.assertEqual(self.started[0]['cpus'], '0.5')
        self.assertEqual(self.req('/api/kernel/start', {'confirm': True, 'revision': 0}, auth)[0], 409)  # one at a time
        status, settings, _ = self.req('/api/settings', {'workspace': '', 'cpus': '1', 'memory': '512m'}, auth)
        self.assertEqual(status, 409); self.assertIn('Stop the running session', settings['error'])
        status, first, _ = self.req('/api/kernel/execute', {'code': 'total = 2 + 3\nprint("set")', 'confirm': True}, auth)
        self.assertEqual((status, first['status'], first['output'], first['count']), (200, 'ok', 'set\n', 1))
        self.assertIn('elapsed_ms', first); self.assertTrue(first['session']['alive'])
        status, second, _ = self.req('/api/kernel/execute', {'code': 'total * 2', 'confirm': True}, auth)
        self.assertEqual((second['status'], second['output'], second['count']), ('ok', '10\n', 2))
        status, failed, _ = self.req('/api/kernel/execute', {'code': 'missing_name', 'confirm': True}, auth)
        self.assertEqual((status, failed['status'], failed['error']['ename']), (200, 'error', 'NameError'))
        status, variables, _ = self.req('/api/kernel/variables', headers={'Cookie': auth['Cookie']})
        self.assertEqual(status, 200)
        self.assertEqual([v for v in variables['variables'] if v['name'] == 'total'], [{'name': 'total', 'type': 'int', 'summary': '5'}])
        self.assertEqual(self.req('/api/kernel/execute', {'code': 'print(1)'}, auth)[0], 400)  # confirm missing
        status, stopped, _ = self.req('/api/kernel/stop', {}, auth)
        self.assertEqual((status, stopped['alive']), (200, False))
        self.assertEqual(self.req('/api/kernel/execute', {'code': 'print(1)', 'confirm': True}, auth)[0], 409)
        self.assertEqual(self.req('/api/kernel/variables', headers={'Cookie': auth['Cookie']})[0], 409)
        self.assertEqual(self.req('/api/settings', {'workspace': '', 'cpus': '1', 'memory': '512m'}, auth)[0], 200)
        status, again, _ = self.req('/api/kernel/start', {'confirm': True, 'revision': 1}, auth)
        self.assertEqual((status, again['count']), (200, 0))  # a new session starts empty
        self.assertEqual(self.started[1]['cpus'], '1')
        status, fresh, _ = self.req('/api/kernel/execute', {'code': 'total', 'confirm': True}, auth)
        self.assertEqual(fresh['error']['ename'], 'NameError')

    def test_interrupt_endpoint_stops_a_running_cell(self):
        auth = self.login()
        self.assertEqual(self.req('/api/kernel/interrupt', {}, auth)[0], 409)
        self.req('/api/kernel/start', {'confirm': True, 'revision': 0}, auth)
        outcome = {}
        def run():
            outcome['response'] = self.req('/api/kernel/execute', {'code': 'import time\nwhile True: time.sleep(0.01)', 'confirm': True}, auth)
        worker = threading.Thread(target=run); worker.start()
        import time
        deadline = time.monotonic() + 5
        while not self.server.kernel.executing and time.monotonic() < deadline: time.sleep(0.01)
        time.sleep(0.2)
        self.assertEqual(self.req('/api/kernel/execute', {'code': 'print(1)', 'confirm': True}, auth)[0], 409)  # busy
        status, data, _ = self.req('/api/kernel/interrupt', {}, auth)
        self.assertEqual((status, data['ok']), (200, True))
        worker.join(15)
        self.assertEqual(outcome['response'][1]['status'], 'interrupted')
        status, after, _ = self.req('/api/kernel/execute', {'code': 'print("still here")', 'confirm': True}, auth)
        self.assertEqual(after['output'], 'still here\n')


if __name__ == '__main__':
    unittest.main()
