import unittest
import tempfile
import threading
import http.client
import json
import io
import hashlib
from local_lab.server import LabServer, initialize_state
from unittest.mock import patch
from pathlib import Path
from local_lab import setup

class SetupTest(unittest.TestCase):
    def test_docker_executable_checks_user_cli_outside_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); cli=root/'.docker/bin/docker';cli.parent.mkdir(parents=True)
            cli.write_text('');cli.chmod(0o700)
            with patch.object(setup.Path, 'home', return_value=root), patch.object(setup.shutil, 'which', return_value=None):
                self.assertEqual(setup._find_docker(), str(cli))

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

    def test_unapproved_redirect_is_rejected(self):
        handler=setup._DockerRedirect()
        with self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, '', {}, 'https://evil.example/Docker.dmg')

    def test_download_checks_hash_and_signature_and_cleans_partial(self):
        payload=b'test installer'; digest=hashlib.sha256(payload).hexdigest()
        class Response(io.BytesIO):
            headers={'Content-Length': str(len(payload))}
        class Opener:
            def open(self, url, timeout): return Response(payload)
        with tempfile.TemporaryDirectory() as temp, patch.object(setup, '_installed', return_value=False), \
             patch.object(setup, '_installer_spec', return_value=('https://desktop.docker.com/test', digest)), \
             patch.object(setup.urllib.request, 'build_opener', return_value=Opener()), \
             patch.object(setup, '_verify_signature', return_value=(False,'bad signature')):
            root=Path(temp); root.chmod(0o700)
            result=setup.download_installer(root)
            self.assertFalse(result['ok'])
            self.assertEqual(list((root/'installers').iterdir()), [])
            with patch.object(setup, '_verify_signature', return_value=(True,'verified')):
                events=[]
                self.assertTrue(setup.download_installer(root,events.append)['ok'])
                self.assertEqual((root/'installers'/'Docker.dmg').read_bytes(),payload)
                self.assertEqual(events[-1]['phase'],'verifying')

    def test_modified_installer_is_never_opened(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(setup, '_installed', return_value=False), \
             patch.object(setup, '_installer_spec', return_value=('https://desktop.docker.com/test', '0'*64)), \
             patch.object(setup, '_run') as run:
            root=Path(temp); root.chmod(0o700)
            stage=setup._staging(root);(stage/'Docker.dmg').write_bytes(b'modified')
            self.assertFalse(setup.open_installer(root)['ok'])
            run.assert_not_called()

    def test_open_rechecks_signature_before_opening(self):
        payload=b'verified installer'; digest=hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temp, patch.object(setup, '_installed', return_value=False), \
             patch.object(setup, '_installer_spec', return_value=('https://desktop.docker.com/test', digest)), \
             patch.object(setup, '_verify_signature', return_value=(True,'verified')) as verify, \
             patch.object(setup, '_run', return_value=(0,'')) as run:
            root=Path(temp); root.chmod(0o700)
            stage=setup._staging(root); dmg=stage/'Docker.dmg';dmg.write_bytes(payload)
            self.assertTrue(setup.open_installer(root)['ok'])
            verify.assert_called_once_with(dmg, stage)
            run.assert_called_once_with(['/usr/bin/open', str(dmg)], timeout=30)

    def test_existing_app_never_downloaded_or_opened(self):
        with patch.object(setup, '_installed', return_value=True), patch.object(setup.urllib.request, 'build_opener') as opener, patch.object(setup, '_run') as run:
            self.assertFalse(setup.download_installer('/unused')['ok'])
            self.assertFalse(setup.open_installer('/unused')['ok'])
            opener.assert_not_called();run.assert_not_called()

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


class DownloadTrustTests(unittest.TestCase):
    """The packaged app's Python has no certificate file on another Mac; Apple's roots must be used."""
    def _one_real_root_pem(self):
        import ssl as _ssl
        for cert in _ssl.create_default_context().get_ca_certs(binary_form=True):
            return _ssl.DER_cert_to_PEM_cert(cert)
        self.skipTest('no CA certificates available on this machine')
    def test_empty_default_trust_on_mac_loads_system_roots(self):
        import ssl as _ssl
        pem = self._one_real_root_pem()
        empty = lambda: _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)  # what the bundled Python gets on a user's Mac
        with patch.object(setup.ssl, 'create_default_context', empty), patch.object(setup.platform, 'system', return_value='Darwin'), \
             patch.object(setup, '_system_roots', return_value=pem) as roots:
            context = setup._ssl_context()
        roots.assert_called_once_with()
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
        self.assertEqual(context.verify_mode, _ssl.CERT_REQUIRED)  # verification is never relaxed
    def test_empty_default_trust_elsewhere_is_an_error_not_a_bypass(self):
        import ssl as _ssl
        empty = lambda: _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(setup.ssl, 'create_default_context', empty), patch.object(setup.platform, 'system', return_value='Linux'):
            with self.assertRaises(ValueError): setup._ssl_context()
    def test_populated_default_trust_is_used_without_touching_the_keychain(self):
        with patch.object(setup, '_system_roots') as roots:
            context = setup._ssl_context()
        roots.assert_not_called()
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
    def test_system_roots_rejects_non_pem_output(self):
        class Result: returncode=0; stdout=b'not a certificate'
        with patch.object(setup.subprocess, 'run', return_value=Result()):
            with self.assertRaises(ValueError): setup._system_roots()
        class Failed: returncode=1; stdout=b''
        with patch.object(setup.subprocess, 'run', return_value=Failed()):
            with self.assertRaises(ValueError): setup._system_roots()
    def test_download_uses_verifying_context(self):
        payload=b'x'; digest=hashlib.sha256(payload).hexdigest()
        class Response(io.BytesIO):
            headers={'Content-Length':'1'}
        captured={}
        def build_opener(*handlers):
            captured['handlers']=handlers
            class Opener:
                def open(self, url, timeout): return Response(payload)
            return Opener()
        with tempfile.TemporaryDirectory() as temp, patch.object(setup,'_installed',return_value=False), \
             patch.object(setup,'_installer_spec',return_value=('https://desktop.docker.com/test',digest)), \
             patch.object(setup.urllib.request,'build_opener',side_effect=build_opener), patch.object(setup,'_verify_signature',return_value=(True,'ok')):
            root=Path(temp); root.chmod(0o700)
            self.assertTrue(setup.download_installer(root)['ok'])
        https=[h for h in captured['handlers'] if isinstance(h, setup.urllib.request.HTTPSHandler)]
        self.assertEqual(len(https),1)
        self.assertEqual(https[0]._context.verify_mode, setup.ssl.CERT_REQUIRED)
