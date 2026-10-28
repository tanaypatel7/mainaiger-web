"""Derived session images: declared packages -> Dockerfile -> one reviewed docker build."""
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_lab import environment as env
from local_lab import graph as g
from local_lab import runner
from local_lab.kernel_session import KernelSession
from local_lab.runner import KERNEL_DIR
from local_lab.server import LabServer, initialize_state


class PackageSpecTests(unittest.TestCase):
    def test_accepts_exact_pins_and_sorts(self):
        self.assertEqual(env.parse_packages(['scikit-learn==1.7.2', 'numpy==2.3.3', 'pandas[excel]==2.3.2']), ['numpy==2.3.3', 'pandas[excel]==2.3.2', 'scikit-learn==1.7.2'])
        self.assertEqual(env.parse_packages(None), []); self.assertEqual(env.parse_packages([]), [])
    def test_rejects_unpinned_and_malformed(self):
        for bad in (['numpy'], ['numpy>=1'], ['numpy==1.0 '], ['numpy==x'], ['Numpy==1', 'numpy==2'], ['bad[==1'], ['a==1;rm'], 'x', ['x' * 200 + '==1'], [None], ['numpy==1'] * 51):
            with self.subTest(bad=bad), self.assertRaises(env.EnvironmentSpecError):
                env.parse_packages(bad)
    def test_tag_dockerfile_and_command_depend_only_on_the_list(self):
        a = env.image_tag(['numpy==2.3.3', 'scipy==1.16.2']); b = env.image_tag(['scipy==1.16.2', 'numpy==2.3.3'])
        self.assertEqual(a, b); self.assertTrue(a.startswith('mainaiger-lab-env:')); self.assertNotEqual(a, env.image_tag(['numpy==2.3.4']))
        self.assertEqual(env.image_tag([]), runner.IMAGE)
        text = env.dockerfile(['numpy==2.3.3'])
        self.assertEqual(text.splitlines()[0], 'FROM ' + runner.IMAGE); self.assertIn('pip install --no-cache-dir', text); self.assertIn('numpy==2.3.3', text)
        self.assertEqual(env.dockerfile([]), 'FROM %s\n' % runner.IMAGE)
        cmd = env.build_command(['numpy==2.3.3'])
        self.assertEqual(cmd[-1], '-', 'Dockerfile over stdin: no build context from the Mac'); self.assertIn('--tag', cmd); self.assertNotIn('--network=none', cmd)
        d = env.describe(['numpy==2.3.3']); self.assertTrue(d['derived']); self.assertIn('network', d)
        self.assertFalse(env.describe([])['derived'])
    def test_document_declares_environment(self):
        doc = g.new_document(); doc['notebook']['metadata']['mainaiger'] = {'environment': {'packages': ['scipy==1.16.2', 'numpy==2.3.3']}}
        out = g.validate(doc)
        self.assertEqual(out['notebook']['metadata']['mainaiger']['environment'], {'packages': ['numpy==2.3.3', 'scipy==1.16.2']})
        self.assertEqual(env.packages_of(out), ['numpy==2.3.3', 'scipy==1.16.2'])
        doc['notebook']['metadata']['mainaiger'] = {'environment': {'packages': []}}
        self.assertNotIn('environment', g.validate(doc)['notebook']['metadata']['mainaiger'])
        for bad in ({'packages': ['numpy']}, {'packages': 'numpy==1'}, {'extra': 1}, 'x'):
            doc['notebook']['metadata']['mainaiger'] = {'environment': bad}
            with self.subTest(bad=bad), self.assertRaises(g.GraphError):
                g.validate(doc)
    def test_session_command_uses_the_derived_image_when_given(self):
        settings = {'workspace': None, 'cpus': '0.5', 'memory': '256m'}
        base = runner.session_command(settings, 'n'); derived = runner.session_command(dict(settings, image='mainaiger-lab-env:abc'), 'n')
        self.assertIn(runner.IMAGE, base); self.assertIn('mainaiger-lab-env:abc', derived); self.assertNotIn(runner.IMAGE, derived)
        self.assertEqual(derived[:derived.index('mainaiger-lab-env:abc')], base[:base.index(runner.IMAGE)], 'every limit is identical; only the image differs')
        self.assertIn('--network=none', derived)


REAL_POPEN = subprocess.Popen


def fake_build(script):
    """A Popen replacement that runs a small Python program in place of docker build."""
    def popen(args, **kwargs):
        return REAL_POPEN([sys.executable, '-c', script], **kwargs)
    return popen


class BuildJobTests(unittest.TestCase):
    def setUp(self):
        self.docker = patch.object(env, 'resolve_docker', return_value=('docker', None, None)); self.docker.start()
    def tearDown(self):
        self.docker.stop()
    def wait(self, build):
        for _ in range(200):
            if build.state in ('done', 'failed'): return build.snapshot()
            time.sleep(0.05)
        self.fail('build did not finish')
    def test_success_streams_log_and_reads_dockerfile_from_stdin(self):
        script = "import sys; d = sys.stdin.read(); print('got', len(d.splitlines()), 'lines'); print('FROM ok' if d.startswith('FROM ') else 'bad'); sys.exit(0)"
        build = env.Build(['numpy==2.3.3'], popen=fake_build(script)); build.start()
        snap = self.wait(build)
        self.assertEqual((snap['state'], snap['exit_code'], snap['error']), ('done', 0, None))
        self.assertIn('got 3 lines', snap['log']); self.assertIn('FROM ok', snap['log'])
        self.assertEqual(snap['image'], env.image_tag(['numpy==2.3.3'])); self.assertEqual(snap['command'][0], 'docker')
    def test_failure_reports_exit_code(self):
        build = env.Build(['numpy==2.3.3'], popen=fake_build("import sys; print('ERROR: No matching distribution'); sys.exit(1)")); build.start()
        snap = self.wait(build)
        self.assertEqual(snap['state'], 'failed'); self.assertIn('code 1', snap['error']); self.assertIn('No matching distribution', snap['log'])
    def test_log_is_bounded(self):
        build = env.Build(['numpy==2.3.3'], popen=fake_build("import sys; sys.stdout.write('x' * 200000); sys.exit(0)")); build.start()
        snap = self.wait(build)
        self.assertEqual(snap['state'], 'done'); self.assertTrue(snap['log_truncated']); self.assertLessEqual(len(build.log), env.MAX_LOG)
    def test_timeout_kills_the_build(self):
        with patch.object(env, 'BUILD_TIMEOUT', 1):
            build = env.Build(['numpy==2.3.3'], popen=fake_build("import time; time.sleep(30)")); build.start()
            snap = self.wait(build)
        self.assertEqual(snap['state'], 'failed'); self.assertIn('exceeded', snap['error'])
    def test_no_packages_means_no_build(self):
        with self.assertRaises(env.EnvironmentSpecError):
            env.Build([]).start()
    def test_image_ready_never_pulls(self):
        calls = []
        def run(args, **kw):
            calls.append(args); return subprocess.CompletedProcess(args, 0)
        self.assertTrue(env.image_ready('mainaiger-lab-env:abc', run=run))
        self.assertEqual(calls[0][1:], ['image', 'inspect', '--format', '{{.Id}}', 'mainaiger-lab-env:abc'])
        self.assertFalse(env.image_ready('x', run=lambda *a, **k: subprocess.CompletedProcess(a, 1)))
        self.assertFalse(env.image_ready('x', run=lambda *a, **k: (_ for _ in ()).throw(OSError('no docker'))))


class EnvironmentHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        state, key = initialize_state(self.root / 'private'); self.key = key
        self.started = []
        def factory(settings, state_dir):
            self.started.append(settings)
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local-test')
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'), kernel_factory=factory)
        self.ready = set(); self.server.image_ready = lambda tag: tag in self.ready
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'
        self.docker = patch.object(env, 'resolve_docker', return_value=('docker', None, None)); self.docker.start()
    def tearDown(self):
        self.docker.stop()
        if self.server.kernel is not None and self.server.kernel.alive: self.server.kernel.stop()
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    def req(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}
    def test_declare_review_build_then_session_uses_the_image(self):
        self.assertEqual(self.req('/api/environment')[0], 401)
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        status, data, _ = self.req('/api/environment', headers=cookie)
        self.assertEqual((status, data['derived'], data['ready'], data['image'], data['build']), (200, False, True, runner.IMAGE, None))
        self.assertEqual(self.req('/api/environment/build', {'confirm': True, 'image': runner.IMAGE}, auth)[0], 400)  # nothing to build
        # Declare packages in the Mainbook; the image is not ready; starting a session is refused with a clear reason.
        doc = g.new_document(); doc['notebook']['metadata']['mainaiger'] = {'environment': {'packages': ['numpy==2.3.3']}}
        self.assertEqual(self.req('/api/graph', {'document': doc}, auth)[0], 200)
        status, data, _ = self.req('/api/environment', headers=cookie)
        tag = env.image_tag(['numpy==2.3.3'])
        self.assertEqual((data['derived'], data['ready'], data['image'], data['packages']), (True, False, tag, ['numpy==2.3.3']))
        self.assertIn('FROM ' + runner.IMAGE, data['dockerfile']); self.assertEqual(data['command'][-1], '-')
        st = self.req('/api/state', headers=cookie)[1]
        self.assertEqual(st['environment'], {'image': tag, 'derived': True, 'ready': False, 'packages': ['numpy==2.3.3']}); self.assertIn(tag, st['session_command'])
        status, data, _ = self.req('/api/kernel/start', {'confirm': True, 'revision': st['revision']}, auth)
        self.assertEqual(status, 409); self.assertIn('not built yet', data['error']); self.assertEqual(self.started, [])
        # Build: confirm + the reviewed image tag; a stale tag is refused; the job runs in the background.
        self.assertEqual(self.req('/api/environment/build', {'confirm': True}, auth)[0], 400)
        self.assertEqual(self.req('/api/environment/build', {'confirm': True, 'image': 'mainaiger-lab-env:stale'}, auth)[0], 409)
        with patch.object(subprocess, 'Popen', fake_build("import sys, time; sys.stdin.read(); print('Step 1/2'); time.sleep(0.3); print('Successfully tagged'); sys.exit(0)")):
            status, data, _ = self.req('/api/environment/build', {'confirm': True, 'image': tag}, auth)
            self.assertEqual((status, data['build']['state']), (200, 'running'))
            self.assertEqual(self.req('/api/environment/build', {'confirm': True, 'image': tag}, auth)[0], 409)  # already running
            for _ in range(100):
                data = self.req('/api/environment', headers=cookie)[1]
                if data['build']['state'] != 'running': break
                time.sleep(0.05)
        self.assertEqual((data['build']['state'], data['ready']), ('done', True)); self.assertIn('Successfully tagged', data['build']['log'])
        # Now the session starts with the derived image in its settings.
        st = self.req('/api/state', headers=cookie)[1]
        status, data, _ = self.req('/api/kernel/start', {'confirm': True, 'revision': st['revision']}, auth)
        self.assertEqual(status, 200); self.assertEqual(self.started[0]['image'], tag)
        self.assertEqual(self.started[0]['cpus'], '0.5')


if __name__ == '__main__':
    unittest.main()
