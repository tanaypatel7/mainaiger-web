"""The Devices page observes this device and the tailnet; it never changes the network."""
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from local_lab import network
from local_lab.server import LabServer, initialize_state

STATUS = {'BackendState': 'Running', 'MagicDNSSuffix': 'tail1234.ts.net', 'CurrentTailnet': {'Name': 'tanay.github'},
          'Self': {'HostName': 'macbook', 'DNSName': 'macbook.tail1234.ts.net.', 'OS': 'macOS', 'Online': True, 'TailscaleIPs': ['100.64.0.1', 'fd7a::1']},
          'Peer': {'k1': {'HostName': 'nexus', 'DNSName': 'nexus.tail1234.ts.net.', 'OS': 'linux', 'Online': True, 'TailscaleIPs': ['100.64.0.2'], 'Tags': ['tag:server']},
                   'k2': {'HostName': 'ipad', 'OS': 'iOS', 'Online': False, 'TailscaleIPs': ['100.64.0.3'], 'LastSeen': '2026-10-27T01:00:00Z'},
                   'k3': {'HostName': 'asu-lab', 'OS': 'linux', 'Online': True, 'TailscaleIPs': ['100.64.0.4'], 'ExitNode': True}}}


def fake_run(output, code=0):
    calls = []
    def run(args, **kwargs):
        calls.append(args); return subprocess.CompletedProcess(args, code, stdout=output.encode() if isinstance(output, str) else output)
    run.calls = calls
    return run


class TailnetTests(unittest.TestCase):
    def test_absent_cli_means_not_available_not_guessed(self):
        t = network.tailnet(find=lambda n: None, run=fake_run('{}'))
        self.assertEqual((t['available'], t['logged_in'], t['peers']), (False, False, []))
    def test_status_is_parsed_sorted_and_only_status_is_run(self):
        run = fake_run(json.dumps(STATUS))
        t = network.tailnet(find=lambda n: '/usr/bin/tailscale', run=run)
        self.assertEqual(run.calls, [['/usr/bin/tailscale', 'status', '--json']], 'read-only: only status is ever run')
        self.assertEqual((t['logged_in'], t['tailnet'], t['self']['name'], t['self']['ips']), (True, 'tanay.github', 'macbook', ['100.64.0.1', 'fd7a::1']))
        self.assertEqual([p['name'] for p in t['peers']], ['asu-lab', 'nexus', 'ipad'], 'online first, then by name')
        self.assertEqual(t['peers'][1]['tags'], ['tag:server']); self.assertTrue(t['peers'][0]['exit_node']); self.assertEqual(t['peers'][2]['last_seen'], '2026-10-27T01:00:00Z')
    def test_needs_login_and_garbage_output(self):
        t = network.tailnet(find=lambda n: 'tailscale', run=fake_run(json.dumps({'BackendState': 'NeedsLogin', 'Peer': {}})))
        self.assertEqual((t['available'], t['logged_in'], t['backend']), (True, False, 'NeedsLogin')); self.assertIn('not logged in', t['note'])
        t = network.tailnet(find=lambda n: 'tailscale', run=fake_run('Logged out.', code=1))
        self.assertFalse(t['logged_in']); self.assertIn('without JSON', t['note'])
        def boom(args, **kw): raise OSError('permission denied')
        self.assertIn('could not be run', network.tailnet(find=lambda n: 'tailscale', run=boom)['note'])
    def test_this_device_reports_facts(self):
        d = network.this_device(find=lambda n: None)
        self.assertTrue(d['hostname']); self.assertIn(d['docker_present'], (True, False)); self.assertEqual(d['role'], 'this device (the Lab runs here)')


class NetworkHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); state, key = initialize_state(Path(self.tmp.name) / 'private'); self.key = key
        self.server = LabServer(('127.0.0.1', 0), state, key); threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, payload, head
    def test_page_and_route_need_owner(self):
        self.assertEqual(self.req('/api/network')[0], 401)
        status, body, _ = self.req('/devices'); self.assertEqual(status, 200); self.assertIn(b'Your tailnet', body); self.assertIn(b'READ-ONLY', body)
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin}); cookie = {'Cookie': head['Set-Cookie'].split(';')[0]}
        status, body, _ = self.req('/api/network', headers=cookie); data = json.loads(body)
        self.assertEqual(status, 200); self.assertIn('device', data); self.assertIn('tailnet', data); self.assertEqual(data['paired'], []); self.assertEqual(data['sessions'], [])
        self.assertIn(data['tailnet']['available'], (True, False))


if __name__ == '__main__':
    unittest.main()
