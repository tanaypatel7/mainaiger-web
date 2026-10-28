"""Tests for the persistent kernel and its host-side session.

These spawn the real kernel program as a plain local process (``python -I
kernel.py``) instead of a Docker container, so the protocol, output capture,
error reporting, interrupt and timeout behavior are exercised for real. What
they do NOT establish: Docker container isolation on a Mac. That needs the
manual acceptance steps in docs/LOCAL_LAB_SETUP.md.
"""
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from local_lab import kernel_session
from local_lab.kernel_session import KernelSession, KernelError
from local_lab.runner import KERNEL_DIR, session_command, session_control, validate_settings

KERNEL = KERNEL_DIR / 'kernel.py'


def local_session():
    return KernelSession([sys.executable, '-I', str(KERNEL)], label='local-test')


class KernelProcessTests(unittest.TestCase):
    def setUp(self):
        self.session = local_session()
        status = self.session.start(timeout=20)
        self.assertTrue(status['alive'])
        self.assertTrue(status['python'].startswith('3.'))

    def tearDown(self):
        self.session.stop()
        self.assertFalse(self.session.alive)
        self.assertIsNotNone(self.session.proc.returncode)

    def test_variables_persist_and_last_expression_is_shown(self):
        first = self.session.execute('x = [1, 2, 3]\nprint("made x")')
        self.assertEqual((first['status'], first['output'], first['count']), ('ok', 'made x\n', 1))
        second = self.session.execute('sum(x)')
        self.assertEqual((second['status'], second['output'], second['count']), ('ok', '6\n', 2))
        third = self.session.execute('y = sum(x)')  # assignment: nothing echoed
        self.assertEqual(third['output'], '')
        names = {v['name']: v for v in self.session.variables()}
        self.assertEqual(names['x']['summary'], '3 items')
        self.assertEqual((names['y']['type'], names['y']['summary']), ('int', '6'))

    def test_evaluate_answers_a_condition_without_running_a_cell(self):
        self.session.execute('verdict = {"action": "revert"}\nn = 2')
        yes = self.session.evaluate('verdict["action"] == "revert" and n < 3')
        self.assertEqual((yes['status'], yes['truth'], yes['value']), ('ok', True, 'True'))
        no = self.session.evaluate('n > 10')
        self.assertEqual((no['status'], no['truth'], no['value']), ('ok', False, 'False'))
        shown = self.session.evaluate('print("noise"); n * 21' if False else 'n * 21')
        self.assertEqual((shown['truth'], shown['value']), (True, '42'))
        bad = self.session.evaluate('missing_name > 1')
        self.assertEqual((bad['status'], bad['truth'], bad['error']['ename']), ('error', None, 'NameError'))
        self.assertIn('missing_name', bad['error']['evalue'])
        statement = self.session.evaluate('n = 5')
        self.assertEqual((statement['status'], statement['error']['ename']), ('error', 'SyntaxError'))
        printed = self.session.evaluate('print("side effect") or True')
        self.assertEqual((printed['status'], printed['truth']), ('ok', True), 'what a condition prints is discarded, never shown as cell output')
        after = self.session.execute('n')
        self.assertEqual((after['count'], after['output']), (2, '2\n'), 'conditions do not advance the count or change _ and n')
        self.assertEqual(self.session.execute('_')['output'], '2\n')
        with self.assertRaises(ValueError): self.session.evaluate('   ')
        with self.assertRaises(ValueError): self.session.evaluate('1 + ' * 2000 + '1')
        slow = self.session.evaluate('__import__("time").sleep(30) or True', timeout=1)
        self.assertEqual(slow['status'], 'timeout'); self.assertTrue(self.session.alive)
        self.assertEqual(self.session.evaluate('n == 2')['truth'], True)

    def test_all_output_channels_are_captured_in_order(self):
        result = self.session.execute('import os, sys\nprint("a")\nos.write(1, b"b\\n")\nsys.stderr.write("c\\n")\nos.write(2, b"d\\n")\nNone')
        self.assertEqual(result['output'], 'a\nb\nc\nd\n')

    def test_errors_report_name_value_and_cell_traceback(self):
        result = self.session.execute('def f():\n    return 1 / 0\nf()')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error']['ename'], 'ZeroDivisionError')
        self.assertEqual(result['error']['evalue'], 'division by zero')
        self.assertIn('  File "<cell 1>", line 3, in <module>', result['error']['traceback'])
        self.assertIn('  File "<cell 1>", line 2, in f', result['error']['traceback'])
        self.assertNotIn('kernel.py', '\n'.join(result['error']['traceback']))
        self.assertEqual(self.session.execute('1 + 1')['output'], '2\n')  # session survives the error

    def test_syntax_error_and_input_do_not_hang_the_session(self):
        self.assertEqual(self.session.execute('def (')['error']['ename'], 'SyntaxError')
        self.assertEqual(self.session.execute('input("name? ")')['error']['ename'], 'EOFError')
        self.assertEqual(self.session.execute('import sys; sys.exit(3)')['error']['ename'], 'SystemExit')
        self.assertTrue(self.session.alive)

    def test_output_is_capped_and_marked_truncated(self):
        result = self.session.execute('print("x" * 200000)')
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['output']), 65536)
        self.assertEqual(self.session.execute('print("after")')['output'], 'after\n')

    def test_interrupt_stops_cell_and_keeps_session(self):
        outcome = {}
        def run():
            outcome['result'] = self.session.execute('import time\nwhile True: time.sleep(0.01)', timeout=30)
        worker = threading.Thread(target=run); worker.start()
        deadline = time.monotonic() + 5
        while not self.session.executing and time.monotonic() < deadline: time.sleep(0.01)
        time.sleep(0.2)
        self.assertTrue(self.session.interrupt())
        worker.join(10)
        self.assertEqual(outcome['result']['status'], 'interrupted')
        self.assertTrue(self.session.alive)
        self.assertEqual(self.session.execute('print("continues")')['output'], 'continues\n')

    def test_timeout_interrupts_then_session_continues(self):
        started = time.monotonic()
        result = self.session.execute('while True: pass', timeout=0.5)
        self.assertEqual(result['status'], 'timeout')
        self.assertLess(time.monotonic() - started, 6)
        self.assertTrue(self.session.alive)
        self.assertEqual(self.session.execute('7 * 6')['output'], '42\n')

    def test_cell_that_ignores_interrupt_ends_the_session(self):
        grace = kernel_session.INTERRUPT_GRACE
        kernel_session.INTERRUPT_GRACE = 0.5
        try:
            code = 'while True:\n    try:\n        while True: pass\n    except KeyboardInterrupt:\n        pass'
            result = self.session.execute(code, timeout=0.5)
        finally:
            kernel_session.INTERRUPT_GRACE = grace
        self.assertEqual(result['status'], 'killed')
        self.assertFalse(self.session.alive)
        self.assertIn('terminated', self.session.ended)
        with self.assertRaises(KernelError):
            self.session.execute('print(1)')

    def test_cell_cannot_corrupt_protocol_through_stdout(self):
        # A cell printing something that looks like a protocol reply is just output.
        result = self.session.execute('print(\'{"type":"reply","id":999,"ok":true}\')')
        self.assertEqual(result['status'], 'ok')
        self.assertIn('"type":"reply"', result['output'])
        self.assertEqual(self.session.execute('2')['output'], '2\n')

    def test_code_limits(self):
        for bad in ('', '   ', 'x' * 100001):
            with self.assertRaises(ValueError):
                self.session.execute(bad)


class KernelSessionFailureTests(unittest.TestCase):
    def test_process_that_never_reports_ready_is_terminated(self):
        session = KernelSession([sys.executable, '-c', 'import time; time.sleep(30)'])
        with self.assertRaises(KernelError):
            session.start(timeout=0.5)
        self.assertFalse(session.alive)
        self.assertIsNotNone(session.proc.returncode)

    def test_process_that_exits_immediately_reports_ended(self):
        session = KernelSession([sys.executable, '-c', 'print("not json")'])
        with self.assertRaises(KernelError):
            session.start(timeout=5)
        self.assertIn('exited', session.ended)

    def test_launcher_stderr_is_reported_when_the_kernel_never_starts(self):
        # The docker client explains refused mounts or a stopped engine on stderr.
        session = KernelSession([sys.executable, '-c', 'import sys; sys.stderr.write("docker: Error response from daemon: mounts denied\\n"); sys.exit(125)'])
        with self.assertRaises(KernelError) as caught:
            session.start(timeout=5)
        self.assertIn('mounts denied', session.ended)
        self.assertIn('mounts denied', str(caught.exception))


class DockerSessionCommandTests(unittest.TestCase):
    def test_session_container_keeps_limits_and_mounts_kernel_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'; state.mkdir(mode=0o700)
            settings = validate_settings({'workspace': '', 'cpus': '1', 'memory': '512m'}, state)
        args = session_command(settings, 'mainaiger-lab-session-abc')
        for flag in ['--network=none', '--read-only', '--cap-drop=ALL', '--pull=never', '--user=65534:65534',
                     '--memory=512m', '--cpus=1', '--pids-limit=64', '--security-opt=no-new-privileges']:
            self.assertIn(flag, args)
        self.assertIn('type=bind,src=' + str(KERNEL_DIR) + ',dst=/opt/mainaiger,readonly', args)
        self.assertEqual(args[-4:], ['python:3.13.18-slim', 'python', '-I', '/opt/mainaiger/kernel.py'])
        self.assertNotIn('/var/run/docker.sock', ' '.join(args))
        self.assertTrue(KERNEL.is_file())
        control = session_control('mainaiger-lab-session-abc')
        self.assertEqual(control['interrupt'][-3:], ['kill', '--signal=INT', 'mainaiger-lab-session-abc'])
        self.assertEqual(control['kill'][-3:], ['rm', '-f', 'mainaiger-lab-session-abc'])

    def test_kernel_uses_only_the_standard_library(self):
        source = KERNEL.read_text(encoding='utf-8')
        imports = {line.split()[1].split('.')[0] for line in source.splitlines() if line.startswith(('import ', 'from '))}
        self.assertTrue(imports <= set(sys.stdlib_module_names), imports - set(sys.stdlib_module_names))


if __name__ == '__main__':
    unittest.main()


class KernelStagingTests(unittest.TestCase):
    """stage_kernel() places a verified copy of the kernel where Docker Desktop can mount it."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.state = Path(self.tmp.name) / 'state'; self.state.mkdir(mode=0o700)
    def tearDown(self):
        self.tmp.cleanup()
    def test_copy_matches_bundled_kernel_and_is_readable_by_container_user(self):
        from local_lab.runner import stage_kernel
        import stat as st
        staged_dir = stage_kernel(self.state)
        staged = staged_dir / 'kernel.py'
        self.assertEqual(staged.read_bytes(), KERNEL.read_bytes())
        self.assertEqual(st.S_IMODE(staged_dir.stat().st_mode), 0o755)
        self.assertEqual(st.S_IMODE(staged.stat().st_mode), 0o644)
        self.assertEqual(st.S_IMODE(self.state.stat().st_mode), 0o700)  # the private state dir is untouched
        args = session_command({'workspace': None, 'cpus': '1', 'memory': '512m'}, 'n', staged_dir)
        self.assertIn('type=bind,src=' + str(staged_dir) + ',dst=/opt/mainaiger,readonly', args)
    def test_tampered_copy_is_replaced_before_a_session_starts(self):
        from local_lab.runner import stage_kernel
        staged = stage_kernel(self.state) / 'kernel.py'
        staged.write_text('print("not the kernel")')
        stage_kernel(self.state)
        self.assertEqual(staged.read_bytes(), KERNEL.read_bytes())
    def test_symlinked_staging_refused(self):
        from local_lab.runner import stage_kernel
        elsewhere = Path(self.tmp.name) / 'elsewhere'; elsewhere.mkdir()
        (self.state / 'kernel').symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            stage_kernel(self.state)


class ServiceShutdownTests(unittest.TestCase):
    def test_sigterm_from_launcher_runs_clean_shutdown(self):
        import os, socket
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen([sys.executable, '-m', 'local_lab', '--port', str(port), '--state-dir', tmp + '/state'],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(Path(__file__).resolve().parent.parent))
            try:
                line = proc.stdout.readline()
                self.assertIn('Lab panel: http://127.0.0.1:%d' % port, line)
                proc.stdout.readline()  # owner key line
                proc.send_signal(signal.SIGTERM)
                rest = proc.stdout.read()
                self.assertEqual(proc.wait(timeout=10), 0)
                self.assertIn('Lab stopped.', rest)
            finally:
                if proc.poll() is None:
                    proc.kill()
