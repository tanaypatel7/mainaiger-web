"""Paired devices: a second Lab reached through a forward; its sessions in the local table; cells run there."""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_lab import devices as devices_module, graph as g, remote as remote_module
from local_lab.kernel_session import KernelSession
from local_lab.runner import KERNEL_DIR
from local_lab.server import LabServer, initialize_state


def local_factory(settings, state_dir):
    return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')


def fake_ssh(args, **kwargs):
    """Stands in for the ssh forward: the 'remote' Lab already listens on loopback, so a sleeping child is enough."""
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], **kwargs)


class TwoLabs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start()
        rstate, rkey = initialize_state(self.root / 'remote-private')
        self.remote = LabServer(('127.0.0.1', 0), rstate, rkey, executor=lambda c, s: self.fail('one-shot'), kernel_factory=local_factory)
        threading.Thread(target=self.remote.serve_forever, daemon=True).start()
        self.rport = self.remote.server_port
        self.key_file = self.root / 'nexus.key'; self.key_file.write_text(rkey + '\n'); os.chmod(self.key_file, 0o600)
        lstate, lkey = initialize_state(self.root / 'local-private'); self.key = lkey
        self.server = LabServer(('127.0.0.1', 0), lstate, lkey, executor=lambda c, s: self.fail('one-shot'), kernel_factory=local_factory)
        self.server.devices._popen = fake_ssh
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.remote.shutdown(); self.remote.server_close(); self.home.stop(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=90); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
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


class RemoteClientTests(TwoLabs):
    def test_login_sessions_execute_variables_files(self):
        lab = remote_module.RemoteLab(self.rport, self.key_file.read_text().strip())
        with self.assertRaises(remote_module.RemoteError): remote_module.RemoteLab(self.rport, 'x' * 40).login()
        lab.login(); self.assertEqual(lab.sessions()['sessions'], [])
        row = lab.start_session(label='main', cpus='1'); self.assertEqual((row['label'], row['cpus'], row['alive']), ('main', '1', True))
        self.assertEqual(lab.execute(row['id'], 'x = 41\nprint(x + 1)')['result']['output'], '42\n')
        self.assertEqual([v['name'] for v in lab.variables(row['id'])['variables']], ['x'])
        self.assertEqual(lab.files()['roots'], {})
        lab.cookie = 'mainaiger_lab_owner=stale'; self.assertEqual(lab.sessions()['sessions'][0]['label'], 'main', 'a stale cookie triggers one re-login')
        kernel = remote_module.RemoteKernel(lab, 'nexus', row)
        self.assertEqual(kernel.label, 'nexus/main'); self.assertEqual(kernel.execute('print(x)')['output'], '41\n'); self.assertEqual([v['name'] for v in kernel.variables()], ['x'])
        self.assertTrue(kernel.status()['alive']); kernel.stop(); self.assertFalse(kernel.alive)


class DeviceManagerTests(TwoLabs):
    def test_pair_validation_connect_and_tunnel(self):
        dm = self.server.devices
        for kwargs in (dict(name='Bad Name'), dict(host='a b'), dict(port=self.port), dict(port=80), dict(tunnel='magic'), dict(key_file='/nope')):
            spec = dict(name='nexus', host='nexus', port=self.rport, key_file=str(self.key_file), tunnel='lab'); spec.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(devices_module.DeviceError): dm.pair(**spec)
        loose = self.root / 'loose.key'; loose.write_text('k' * 40); os.chmod(loose, 0o644)
        with self.assertRaises(devices_module.DeviceError): dm.pair('nexus', 'nexus', self.rport, str(loose))
        info = dm.pair('nexus', 'nexus', self.rport, str(self.key_file), tunnel='lab')
        self.assertEqual((info['connected'], info['command'][0], info['command'][-1]), (False, 'ssh', 'nexus')); self.assertIn('-L', info['command']); self.assertIn('127.0.0.1:%d:127.0.0.1:%d' % (self.rport, self.rport), info['command'])
        self.assertEqual(oct((self.server.state_dir / 'devices.json').stat().st_mode & 0o777), '0o600')
        with self.assertRaises(devices_module.DeviceError): dm.pair('nexus', 'other', self.rport + 1, str(self.key_file))
        # The fake ssh refuses to "forward" when the port is already open locally (as a real forward would).
        with self.assertRaises(devices_module.DeviceError) as cm: dm.connect('nexus')
        self.assertIn('already in use', str(cm.exception))
        # Manual tunnel: the owner runs the forward; here the remote already listens, so connect signs in directly.
        dm.forget('nexus'); dm.pair('nexus', 'nexus', self.rport, str(self.key_file), tunnel='manual')
        info = dm.connect('nexus'); self.assertTrue(info['connected']); self.assertIsNone(info['command']); self.assertIn('docker_available', info['remote'])
        self.assertEqual(dm.connected(), ['nexus']); self.assertTrue(dm.connect('nexus')['connected'], 'connecting twice is harmless')
        dm.disconnect('nexus'); self.assertEqual(dm.connected(), []); self.assertFalse(dm.describe('nexus')['connected'])
        # A wrong key is reported plainly.
        wrong = self.root / 'wrong.key'; wrong.write_text('w' * 40); os.chmod(wrong, 0o600)
        dm.pair('other', 'nexus', self.rport, str(wrong), tunnel='manual')
        with self.assertRaises(devices_module.DeviceError) as cm: dm.connect('other')
        self.assertIn('refused the sign-in', str(cm.exception)); self.assertIn('other', dm.errors)
    def test_tunnel_process_lifecycle(self):
        t = devices_module.Tunnel('nexus', self.rport, popen=fake_ssh)
        with self.assertRaises(devices_module.DeviceError): t.start()  # port already open here
        free = devices_module.Tunnel('nexus', 1, popen=lambda a, **k: subprocess.Popen([sys.executable, '-c', 'import sys; print("Permission denied (publickey)."); sys.exit(255)'], **k))
        with self.assertRaises(devices_module.DeviceError) as cm: free.start()
        self.assertIn('Permission denied', str(cm.exception))
        with patch.object(devices_module, 'TUNNEL_READY_SECONDS', 1):
            slow = devices_module.Tunnel('nexus', 1, popen=fake_ssh)
            with self.assertRaises(devices_module.DeviceError) as cm: slow.start()
            self.assertIn('did not come up', str(cm.exception)); self.assertFalse(slow.alive)


class DevicesHttpTests(TwoLabs):
    def test_pair_connect_run_cells_on_device_files_journal(self):
        self.assertEqual(self.req('/api/devices')[0], 401)
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        data = self.req('/api/devices', headers=cookie)[1]; self.assertEqual(data['devices'], []); self.assertEqual(data['local_port'], self.port); self.assertTrue(any('python3 -m local_lab' in line for line in data['install']))
        self.assertEqual(self.req('/api/devices/pair', {'name': 'nexus', 'host': 'nexus', 'port': self.rport, 'key_file': str(self.key_file), 'tunnel': 'manual'}, auth)[0], 400, 'confirm required')
        status, info, _ = self.req('/api/devices/pair', {'name': 'nexus', 'host': 'nexus', 'port': self.rport, 'key_file': str(self.key_file), 'tunnel': 'manual', 'confirm': True}, auth)
        self.assertEqual((status, info['name'], info['connected']), (200, 'nexus', False))
        self.assertEqual(self.req('/api/devices/session/start', {'name': 'nexus', 'confirm': True}, auth)[0], 409, 'not connected yet')
        status, info, _ = self.req('/api/devices/connect', {'name': 'nexus', 'confirm': True}, auth); self.assertEqual((status, info['connected']), (200, True))
        # A session started on the device appears in the local table as nexus/<label>, with device = nexus.
        status, row, _ = self.req('/api/devices/session/start', {'name': 'nexus', 'label': 'main', 'cpus': '2', 'confirm': True}, auth)
        self.assertEqual((status, row['label'], row['device'], row['cpus'], row['alive']), (200, 'nexus/main', 'nexus', '2', True))
        rows = self.req('/api/sessions', headers=cookie)[1]['sessions']; self.assertEqual([(r['label'], r['device']) for r in rows], [('nexus/main', 'nexus')])
        self.assertEqual(len(self.remote.kernels.list()), 1, 'the session really runs on the other Lab')
        # A cell naming nexus/main runs there; the variable lives there, not here.
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; self.req('/api/sessions/start', {'confirm': True, 'revision': rev, 'label': 'local'}, auth)
        doc = g.new_document(); doc['notebook']['cells'][0]['source'] = 'import socket, os\nremote_mark = os.getpid()\nprint("ran on", socket.gethostname())\n'; doc['notebook']['cells'][0]['metadata']['mainaiger']['session'] = 'nexus/main'
        self.assertEqual(self.req('/api/graph', {'document': doc}, auth)[0], 200)
        check = self.req('/api/graph/preflight', {'mode': 'all', 'nodes': [], 'include_prerequisites': False}, auth)[1]; self.assertEqual(check['missing_sessions'], []); self.assertEqual(check['sessions'], {'cell-1': 'nexus/main'})
        self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); run = self.wait_run(cookie)
        self.assertEqual((run['state'], run['nodes']['cell-1']['status'], run['nodes']['cell-1']['session']), ('finished', 'ok', 'nexus/main')); self.assertIn('ran on', run['nodes']['cell-1']['output'])
        self.assertEqual(sorted(v['name'] for v in self.remote.kernels.active_kernel().variables()), ['os', 'remote_mark', 'socket']); self.assertEqual([v['name'] for v in self.server.kernels.by_label('local')['kernel'].variables()], [])
        # Terminal on the device, variables, interrupt/stop through the table.
        rid = rows[0]['id']
        self.assertEqual(self.req('/api/sessions/command', {'id': rid, 'command': 'printf remote', 'confirm': True}, auth)[1]['result']['output'], 'remote\n[exit 0]\n')
        self.assertEqual(sorted(v['name'] for v in self.req('/api/sessions/variables', {'id': rid}, auth)[1]['variables']), ['os', 'remote_mark', 'socket'])
        # Files and journal of the device.
        self.assertEqual(self.req('/api/devices/files?name=nexus', headers=cookie)[1]['roots'], {})
        self.assertEqual(self.req('/api/devices/files?name=ghost', headers=cookie)[0], 409)
        kinds = [e['kind'] for e in self.req('/api/devices/journal?name=nexus', headers=cookie)[1]['entries']]; self.assertIn('session.start', kinds); self.assertIn('session.execute', kinds, 'the device records code that arrived through its API')
        self.assertIn('shell.command', [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']], 'this Mac records the terminal line')
        # Stop it from here; then disconnect marks the row ended; forget cleans up.
        self.assertEqual(self.req('/api/sessions/stop', {'id': rid}, auth)[1]['session']['alive'], False); self.assertFalse(self.remote.kernels.list()[0]['alive'])
        self.req('/api/devices/disconnect', {'name': 'nexus'}, auth)
        rows = self.req('/api/sessions', headers=cookie)[1]['sessions']; self.assertEqual([r['alive'] for r in rows if r['device'] == 'nexus'], [False])
        self.assertEqual(self.req('/api/sessions/forget', {'id': rid}, auth)[1], {'ok': True})
        self.assertEqual(self.req('/api/devices/forget', {'name': 'nexus'}, auth)[1], {'ok': True}); self.assertEqual(self.req('/api/devices', headers=cookie)[1]['devices'], [])
        kinds = [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]; self.assertIn('device.pair', kinds); self.assertIn('device.connect', kinds); self.assertIn('device.forget', kinds)


if __name__ == '__main__':
    unittest.main()
