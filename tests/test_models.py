"""Ollama integration tests against a fake local Ollama HTTP server.

The fake speaks the parts of Ollama's API the Lab uses (/api/version, /api/tags,
streaming /api/pull, /api/chat). Installer download/verification is covered with
mocks; the real download, signature and notarization checks need a Mac.
"""
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from local_lab import models
from local_lab.server import LabServer, initialize_state


class FakeOllama(BaseHTTPRequestHandler):
    installed = [{'name': 'llama3.2:3b', 'size': 2_000_000_000, 'modified_at': '2026-10-27T10:00:00Z'}]
    pull_events = [{'status': 'pulling manifest'}, {'status': 'pulling 1234', 'total': 1000, 'completed': 200},
                   {'status': 'pulling 1234', 'total': 1000, 'completed': 1000}, {'status': 'verifying sha256 digest'}, {'status': 'success'}]
    chats = []
    def log_message(self, *args): pass
    def _json(self, code, data):
        payload = json.dumps(data).encode(); self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
    def do_GET(self):
        if self.path == '/api/version': return self._json(200, {'version': '0.40.12'})
        if self.path == '/api/tags': return self._json(200, {'models': self.installed})
        self._json(404, {'error': 'not found'})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        if self.path == '/api/pull':
            if body.get('model') == 'nope:1b':
                return self._json(200, {'error': 'pull model manifest: file does not exist'}) if False else self._stream([{'error': 'pull model manifest: file does not exist'}])
            return self._stream(self.pull_events)
        if self.path == '/api/chat':
            FakeOllama.chats.append(body)
            return self._json(200, {'model': body['model'], 'message': {'role': 'assistant', 'content': 'Echo: ' + body['messages'][-1]['content']}, 'prompt_eval_count': 5, 'eval_count': 7})
        self._json(404, {'error': 'not found'})
    def _stream(self, events):
        self.send_response(200); self.send_header('Content-Type', 'application/x-ndjson'); self.end_headers()
        for event in events:
            self.wfile.write(json.dumps(event).encode() + b'\n'); self.wfile.flush(); time.sleep(0.01)


class OllamaApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama); cls.fake.daemon_threads = True
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.patch = patch.object(models, 'OLLAMA_PORT', cls.fake.server_port); cls.patch.start()
    @classmethod
    def tearDownClass(cls):
        cls.patch.stop(); cls.fake.shutdown(); cls.fake.server_close()

    def test_running_and_installed_models(self):
        self.assertEqual(models.running(), '0.40.12')
        self.assertEqual([m['name'] for m in models.installed_models()], ['llama3.2:3b'])
    def test_pull_streams_progress_and_succeeds(self):
        events = []
        result = models.pull('qwen2.5:3b', events.append)
        self.assertTrue(result['ok'], result)
        self.assertEqual(events[1], {'phase': 'downloading', 'bytes': 200, 'total': 1000, 'detail': 'pulling 1234'})
        self.assertEqual(events[-1]['detail'], 'success')
    def test_pull_reports_ollama_errors_and_rejects_bad_names(self):
        self.assertIn('file does not exist', models.pull('nope:1b')['output'])
        self.assertFalse(models.pull('../etc; rm -rf')['ok'])
        self.assertFalse(models.pull('UPPER:1b')['ok'])
    def test_chat_round_trip_only_with_installed_models(self):
        result = models.chat('llama3.2:3b', [{'role': 'user', 'content': 'hello'}])
        self.assertEqual((result['ok'], result['reply'], result['tokens']['reply']), (True, 'Echo: hello', 7))
        self.assertEqual(FakeOllama.chats[-1]['stream'], False)
        self.assertFalse(models.chat('gemma3:4b', [{'role': 'user', 'content': 'hi'}])['ok'])  # not downloaded
        self.assertFalse(models.chat('llama3.2:3b', [{'role': 'hacker', 'content': 'hi'}])['ok'])
        self.assertFalse(models.chat('llama3.2:3b', [])['ok'])
    def test_status_combines_device_catalog_and_models(self):
        data = models.status()
        self.assertTrue(data['running']); self.assertEqual(data['version'], '0.40.12')
        names = [c['name'] for c in data['catalog']]
        self.assertIn('llama3.2:3b', names)
        self.assertTrue(next(c for c in data['catalog'] if c['name'] == 'llama3.2:3b')['installed'])
        self.assertEqual(data['installer']['sha256'], models.OLLAMA_SHA256)


class NotRunningTests(unittest.TestCase):
    def test_everything_degrades_when_ollama_is_absent(self):
        with patch.object(models, 'OLLAMA_PORT', 1):  # nothing listens there
            self.assertIsNone(models.running()); self.assertEqual(models.installed_models(), [])
            self.assertIn('not running', models.pull('llama3.2:3b')['output'])
            self.assertFalse(models.chat('llama3.2:3b', [{'role': 'user', 'content': 'x'}])['ok'])
            data = models.status(); self.assertFalse(data['running']); self.assertEqual(data['models'], [])


class CatalogTests(unittest.TestCase):
    def test_fit_against_ram_and_disk(self):
        rows = {r['name']: r for r in models.catalog(16 * models.GiB, 30 * models.GiB, ['llama3.1:8b'])}
        self.assertEqual(rows['llama3.2:3b']['fit'], 'fits')          # 8 GB floor, 16 GB RAM
        self.assertEqual(rows['llama3.1:8b']['fit'], 'tight')         # 16 GB floor, exactly 16
        self.assertEqual(rows['gemma3:12b']['fit'], 'too_large')      # 24 GB floor
        self.assertTrue(rows['llama3.1:8b']['installed']); self.assertTrue(rows['llama3.2:3b']['disk_ok']); self.assertFalse(rows['llama3.3:70b']['disk_ok'])
        self.assertEqual(models.catalog(None)[0]['fit'], 'unknown')
    def test_device_reports_ram_or_unknown(self):
        dev = models.device()
        self.assertIn(dev['system'], ('Linux', 'Darwin', 'Windows'))
        self.assertTrue(dev['ram_bytes'] is None or dev['ram_bytes'] > 0)


class InstallerTests(unittest.TestCase):
    def test_download_verifies_pinned_hash_then_signature_and_notarization(self):
        calls = []
        def fake_fetch(url, expected, stage, final_name, progress, hosts, label, timeout=1800):
            calls.append((url, expected, final_name, tuple(hosts), label)); final = Path(stage) / final_name; final.write_bytes(b'dmg'); return final
        with tempfile.TemporaryDirectory() as temp, patch.object(models, '_ollama_app', return_value=None), patch.object(models.platform, 'system', return_value='Darwin'), \
             patch.object(models.lab_setup, 'fetch_verified', side_effect=fake_fetch), patch.object(models, '_verify_ollama', return_value=(True, 'verified')) as verify:
            root = Path(temp); root.chmod(0o700)
            result = models.download_installer(root)
            self.assertTrue(result['ok'], result)
            self.assertEqual(calls[0][:3], (models.OLLAMA_URL, models.OLLAMA_SHA256, 'Ollama.dmg'))
            self.assertIn('release-assets.githubusercontent.com', calls[0][3])
            verify.assert_called_once()
            with patch.object(models, '_verify_ollama', return_value=(False, 'not signed by Ollama')):
                result = models.download_installer(root)
            self.assertFalse(result['ok']); self.assertFalse((root / 'installers' / 'Ollama.dmg').exists())  # rejected file is removed
    def test_signature_check_requires_ollama_developer_id_and_gatekeeper(self):
        outputs = {'hdiutil': (0, ''), 'codesign-verify': (0, ''), 'codesign-dv': (0, 'Authority=Developer ID Application: Someone Else (YYYY)\nTeamIdentifier=YYYY'), 'spctl': (0, 'accepted')}
        seen = []
        def run(args, timeout=10):
            seen.append(args)
            if args[0] == '/usr/bin/hdiutil': return outputs['hdiutil'] if args[1] == 'attach' else (0, '')
            if args[0] == '/usr/bin/codesign': return outputs['codesign-dv'] if args[1] == '-dv' else outputs['codesign-verify']
            if args[0] == '/usr/sbin/spctl': return outputs['spctl']
            return (1, 'unexpected')
        with tempfile.TemporaryDirectory() as temp, patch.object(models.lab_setup, '_run', side_effect=run):
            stage = Path(temp)
            def mounted(path): return True
            with patch.object(Path, 'is_dir', mounted), patch.object(Path, 'is_symlink', lambda p: False):
                self.assertTrue(models._verify_ollama(stage / 'Ollama.dmg', stage)[0])
                verify = next(a for a in seen if a[0] == '/usr/bin/codesign' and '--verify' in a)
                self.assertIn('-R', verify); self.assertIn('3MU9H2V9Y9', verify[verify.index('-R') + 1]); self.assertIn('com.electron.ollama', verify[verify.index('-R') + 1])
                outputs['codesign-verify'] = (1, 'code failed to satisfy specified code requirement(s)')
                ok, message = models._verify_ollama(stage / 'Ollama.dmg', stage); self.assertFalse(ok); self.assertIn('Team ID 3MU9H2V9Y9', message); self.assertIn('Someone Else', message)
                outputs['codesign-verify'] = (0, ''); outputs['spctl'] = (3, 'rejected')
                ok, message = models._verify_ollama(stage / 'Ollama.dmg', stage); self.assertFalse(ok); self.assertIn('Gatekeeper', message)


class ModelsHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); state, key = initialize_state(Path(self.tmp.name) / 'private'); self.key = key
        self.server = LabServer(('127.0.0.1', 0), state, key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}
    def test_routes_need_owner_and_approval_and_run_as_jobs(self):
        self.assertEqual(self.req('/api/models')[0], 401)
        auth = self.login()
        with patch.object(models, 'OLLAMA_PORT', 1):
            status, data, _ = self.req('/api/models', headers={'Cookie': auth['Cookie']})
            self.assertEqual((status, data['running'], data['installer']['version']), (200, False, models.OLLAMA_VERSION))
        self.assertEqual(self.req('/api/models/installer/download', {'confirm': True}, {'Origin': self.origin})[0], 403)
        self.assertEqual(self.req('/api/models/installer/download', {}, auth)[0], 400)
        self.assertEqual(self.req('/api/models/pull', {'model': 'llama3.2:3b'}, auth)[0], 400)
        self.assertEqual(self.req('/api/models/pull', {'model': 'bad name!', 'confirm': True}, auth)[0], 400)
        events = []
        with patch('local_lab.server.lab_models.pull', side_effect=lambda model, progress: (progress({'phase': 'downloading', 'bytes': 5, 'total': 10, 'detail': 'pulling x'}), events.append(model), {'ok': True, 'output': 'ready'})[-1]):
            status, data, _ = self.req('/api/models/pull', {'model': 'llama3.2:3b', 'confirm': True}, auth)
            self.assertEqual((status, data['model']), (202, 'llama3.2:3b'))
            for _ in range(50):
                status, job, _ = self.req('/api/setup/job', headers={'Cookie': auth['Cookie']})
                if not job['running']: break
                time.sleep(0.05)
        self.assertEqual((job['action'], job['result']['ok'], events), ('ollama-pull:llama3.2:3b', True, ['llama3.2:3b']))
        with patch('local_lab.server.lab_models.chat', return_value={'ok': True, 'reply': 'hi there', 'model': 'llama3.2:3b', 'elapsed_ms': 1, 'tokens': {}}) as chat:
            status, data, _ = self.req('/api/models/chat', {'model': 'llama3.2:3b', 'messages': [{'role': 'user', 'content': 'hi'}]}, auth)
            self.assertEqual((status, data['reply']), (200, 'hi there')); chat.assert_called_once()
        self.assertEqual(self.req('/api/models/chat', {'model': 'llama3.2:3b'}, auth)[0], 400)
        with patch('local_lab.server.lab_models.open_app', return_value={'ok': True, 'output': 'starting'}) as opened:
            self.assertEqual(self.req('/api/models/app/open', {'confirm': True}, auth)[0], 200); opened.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
