import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_lab.server import LabServer, initialize_state


class SetupJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        state, self.key = initialize_state(Path(self.temp.name) / 'state')
        self.server = LabServer(('127.0.0.1', 0), state, self.key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'127.0.0.1:{self.server.server_port}'
        self.origin = 'http://' + self.host

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        h = {'Host': self.host, **(headers or {})}
        if data is not None: h['Content-Type'] = 'application/json'
        connection.request('GET' if data is None else 'POST', path,
                           body=None if data is None else json.dumps(data), headers=h)
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), dict(response.getheaders())
        connection.close()
        return result

    def login(self):
        status, result, headers = self.request('/api/login', {'key': self.key}, {'Origin': self.origin})
        self.assertEqual(status, 200)
        return {'Origin': self.origin, 'Cookie': headers['Set-Cookie'].split(';')[0],
                'X-CSRF-Token': result['csrf']}

    def test_installer_authority_and_fixed_actions(self):
        download = '/api/setup/installer/download'
        open_dmg = '/api/setup/installer/open'
        self.assertEqual(self.request('/api/setup/job')[0], 401)
        self.assertEqual(self.request(download, {'confirm': True}, {'Origin': self.origin})[0], 403)
        auth = self.login()
        with patch('local_lab.server.lab_setup.download_installer') as download_action, \
             patch('local_lab.server.lab_setup.open_installer') as open_action:
            for path in (download, open_dmg):
                self.assertEqual(self.request(path, {'confirm': True}, {**auth, 'Origin': 'https://evil.example'})[0], 403)
                self.assertEqual(self.request(path, {'confirm': True}, {'Origin': self.origin, 'Cookie': auth['Cookie']})[0], 403)
                for payload in ({}, {'confirm': False}, {'confirm': 1}, {'confirm': True, 'command': 'echo unsafe'}):
                    self.assertEqual(self.request(path, payload, auth)[0], 400)
            download_action.assert_not_called()
            open_action.assert_not_called()
            self.assertEqual(self.request('/api/setup/installer/shell', {'confirm': True}, auth)[0], 404)

    def test_progress_completion_and_concurrent_action_rejection(self):
        started, release = threading.Event(), threading.Event()
        def download(state_dir, progress):
            progress({'phase': 'downloading', 'bytes': 25, 'total': 100})
            started.set()
            release.wait(5)
            return {'ok': True, 'output': 'Verified test installer'}
        auth = self.login()
        try:
            with patch('local_lab.server.lab_setup.download_installer', side_effect=download) as action, \
                 patch('local_lab.server.lab_setup.open_installer') as open_action:
                status, _, _ = self.request('/api/setup/installer/download', {'confirm': True}, auth)
                self.assertEqual(status, 202)
                self.assertTrue(started.wait(3))
                status, job, _ = self.request('/api/setup/job', headers={'Cookie': auth['Cookie']})
                self.assertEqual(status, 200)
                self.assertEqual((job['phase'], job['bytes'], job['total']), ('downloading', 25, 100))
                self.assertEqual(self.request('/api/setup/installer/open', {'confirm': True}, auth)[0], 409)
                self.assertEqual(self.request('/api/setup/pull', {'confirm': True}, auth)[0], 409)
                open_action.assert_not_called()
                release.set()
                for _ in range(100):
                    _, job, _ = self.request('/api/setup/job', headers={'Cookie': auth['Cookie']})
                    if not job['running']: break
                    time.sleep(0.01)
                self.assertEqual(job['phase'], 'done')
                self.assertEqual(job['result'], {'ok': True, 'output': 'Verified test installer'})
                action.assert_called_once()
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
