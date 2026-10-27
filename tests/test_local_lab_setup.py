import unittest
import tempfile
import threading
import http.client
import json
from local_lab.server import LabServer, initialize_state
from unittest.mock import patch
from pathlib import Path
from local_lab import setup

class SetupTest(unittest.TestCase):
    def test_remote_context_fails_closed(self):
        with patch.object(setup.shutil, 'which', return_value='/usr/local/bin/docker'), patch.object(setup.platform, 'system', return_value='Darwin'), patch.object(setup, '_run', return_value=(0, 'tcp://192.0.2.1:2375')) as run:
            docker, message = setup._local_docker()
            self.assertIsNone(docker)
            self.assertIn('could not be verified', message)
            self.assertEqual(run.call_count, 1)

    def test_desktop_checks_endpoint_and_engine(self):
        endpoint = 'unix://' + str(Path.home() / '.docker/run/docker.sock')
        with patch.object(setup.shutil, 'which', return_value='/usr/local/bin/docker'), patch.object(setup.platform, 'system', return_value='Darwin'), patch.object(setup, '_run', side_effect=[(0, endpoint), (0, 'Docker Desktop|linux')]):
            docker, _ = setup._local_docker()
            self.assertEqual(docker, '/usr/local/bin/docker')

    def test_pull_is_fixed_and_refuses_missing_engine(self):
        with patch.object(setup, '_local_docker', return_value=(None, 'not local')), patch.object(setup, '_run') as run:
            self.assertFalse(setup.pull()['ok'])
            run.assert_not_called()
        with patch.object(setup, '_local_docker', return_value=('/bin/docker', 'ok')), patch.object(setup, '_run', return_value=(0, 'done')) as run:
            self.assertTrue(setup.pull()['ok'])
            run.assert_called_once_with(['/bin/docker', '--context', 'desktop-linux', 'pull', '--quiet', setup.IMAGE], timeout=600)

    def test_environment_ignores_remote_overrides(self):
        with patch.dict(setup.os.environ, {'DOCKER_HOST': 'tcp://example', 'DOCKER_CONTEXT': 'remote', 'DOCKER_API_VERSION':'1'}):
            env = setup._env()
        self.assertNotIn('DOCKER_HOST', env)
        self.assertNotIn('DOCKER_CONTEXT', env)
        self.assertNotIn('DOCKER_API_VERSION', env)

class SetupHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state, self.key = initialize_state(Path(self.tmp.name) / 'private')
        self.server = LabServer(('127.0.0.1', 0), state, self.key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f'http://127.0.0.1:{self.port}'
        self.fixture = self
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        h = {'Host': f'127.0.0.1:{self.port}'}
        h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        connection.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        response = connection.getresponse(); payload = response.read(); head = dict(response.getheaders()); connection.close()
        return response.status, json.loads(payload), head
    def login(self):
        status, data, headers = self.req('/api/login', {'key':self.key}, {'Origin':self.origin})
        self.assertEqual(status, 200)
        return {'Origin':self.origin, 'Cookie':headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token':data['csrf']}
    def test_owner_csrf_and_explicit_approval(self):
        f = self.fixture
        with patch('local_lab.server.lab_setup.status', return_value={'docker_ready': True}), patch('local_lab.server.lab_setup.pull', return_value={'ok': True, 'output': 'done'}) as pull:
            self.assertEqual(f.req('/api/setup')[0], 401)
            self.assertEqual(f.req('/api/setup/pull', {'confirm':True}, {'Origin':f.origin})[0], 403)
            auth = f.login()
            self.assertEqual(f.req('/api/setup', headers={'Cookie':auth['Cookie']})[0], 200)
            self.assertEqual(f.req('/api/setup/pull', {'confirm':True}, {'Origin':f.origin, 'Cookie':auth['Cookie']})[0], 403)
            self.assertEqual(f.req('/api/setup/pull', {'confirm':True}, {**auth, 'Origin':'https://mainaiger.org'})[0], 403)
            self.assertEqual(f.req('/api/setup/pull', {}, auth)[0], 400)
            self.assertEqual(f.req('/api/setup/pull', {'confirm':False}, auth)[0], 400)
            self.assertEqual(f.req('/api/setup/pull', {'confirm':True, 'command':'evil'}, auth)[0], 400)
            pull.assert_not_called()
            status, result, _ = f.req('/api/setup/pull', {'confirm':True}, auth)
            self.assertEqual(status, 200)
            self.assertTrue(result['ok'])
            pull.assert_called_once_with()

if __name__ == '__main__': unittest.main()
