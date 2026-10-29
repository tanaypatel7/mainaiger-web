import io
import unittest
from unittest.mock import Mock, patch

from local_lab import runner, setup


class MacRunnerTests(unittest.TestCase):
    def test_verified_desktop_used_when_docker_not_on_path(self):
        process = Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO(b'42\n')
        process.returncode = 0
        popen = Mock(return_value=process)
        cleanup = Mock()
        verified_cli = '/Users/test/.docker/bin/docker'
        with patch.object(runner.sys, 'platform', 'darwin'), \
             patch.object(setup, '_local_docker', return_value=(verified_cli, 'verified')) as verify, \
             patch.dict(setup.os.environ, {'DOCKER_HOST': 'tcp://remote', 'DOCKER_CONTEXT': 'remote'}):
            result = runner.execute('print(42)', {'workspace': None, 'cpus': '1', 'memory': '512m'},
                                    popen=popen, run=cleanup, find=lambda _: None)

        self.assertEqual(result['output'], '42\n')
        verify.assert_called_once_with()
        args, kwargs = popen.call_args
        self.assertEqual(args[0][:4], [verified_cli, '--context', 'desktop-linux', 'run'])
        self.assertFalse(kwargs['shell'])
        self.assertNotIn('DOCKER_HOST', kwargs['env'])
        self.assertNotIn('DOCKER_CONTEXT', kwargs['env'])
        self.assertEqual(cleanup.call_args.args[0][:4], [verified_cli, '--context', 'desktop-linux', 'rm'])

    def test_unverified_engine_refused_before_starting_process(self):
        popen = Mock()
        with patch.object(runner.sys, 'platform', 'darwin'), \
             patch.object(setup, '_local_docker', return_value=(None, 'Local Docker engine not verified.')):
            result = runner.execute('print(42)', {'workspace': None, 'cpus': '1', 'memory': '512m'},
                                    popen=popen, find=lambda _: '/usr/bin/docker')
        self.assertIn('not verified', result['error'])
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
