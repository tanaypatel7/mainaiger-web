"""Host-side handle for one persistent kernel process.

The session owns a child process that speaks the kernel's JSON-lines protocol
(see ``kernel/kernel.py``). On a Mac the child is ``docker run …`` with the
kernel mounted read-only inside the container; tests use the same class with a
plain ``python kernel.py`` child so the protocol is exercised for real without
Docker. Everything the child writes is untrusted and validated here.

Lifecycle of one cell: send → wait up to the cell timeout → on timeout send an
interrupt and wait a short grace period → if still silent, kill the whole
session and report that truthfully. Nothing here runs Python on the host.
"""
import json
import queue
import signal
import subprocess
import threading
import time

CELL_TIMEOUT = 60        # seconds a cell may run before an interrupt is sent
INTERRUPT_GRACE = 5      # seconds to wait for the kernel to acknowledge an interrupt
START_TIMEOUT = 90       # seconds for the container/kernel to report ready
PROTOCOLS = (1, 2)       # kernel protocol versions this client speaks (2 adds "evaluate")
CONDITION_TIMEOUT = 10   # seconds a trigger condition (one expression) may take
MAX_EXPRESSION = 2000    # bytes of a condition expression
MAX_LINE = 1 << 20       # bytes; a longer protocol line ends the session
MAX_CODE = 100000


class KernelError(RuntimeError):
    """The session is unusable; the caller should report it and start a new one."""


class KernelBusy(KernelError):
    """A cell is already running; the request was not queued."""


class KernelSession:
    def __init__(self, args, *, env=None, interrupt_args=None, kill_args=None, label='local',
                 popen=subprocess.Popen, run=subprocess.run):
        self.args, self.env, self.label = list(args), env, label
        self.interrupt_args, self.kill_args = interrupt_args, kill_args
        self._popen, self._run = popen, run
        self.proc = None
        self.replies = queue.Queue()
        self.lock = threading.Lock()
        self.next_id = 0
        self.alive = False
        self.executing = False
        self.info = {}
        self.started_at = None
        self.count = 0
        self.ended = None          # human-readable reason the session ended, if it did
        self.reader = None
        self.stderr_reader = None
        self.stderr_tail = b''

    # ---- process lifetime --------------------------------------------------
    def start(self, timeout=START_TIMEOUT):
        if self.proc is not None:
            raise KernelError('Session already started')
        kwargs = {'env': self.env} if self.env is not None else {}
        self.proc = self._popen(self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=False, **kwargs)
        self.alive = True
        self.stderr_tail = b''
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        message = self._next_message(timeout)
        if message is None or message.get('type') != 'ready' or message.get('protocol') not in PROTOCOLS:
            self._terminate('The kernel did not report ready. ' + (self.ended or 'Check Docker Desktop and the image.'))
            raise KernelError(self.ended)
        self.protocol = message.get('protocol')
        self.info = {'python': str(message.get('python', ''))[:40], 'cwd': str(message.get('cwd', ''))[:200]}
        self.started_at = time.time()
        return self.status()

    def stop(self):
        """Ask the kernel to exit, then make sure the process is gone."""
        if self.proc is None:
            return
        if self.alive:
            self.ended = self.ended or 'Stopped by the owner.'
            try:
                self._send({'id': self._ident(), 'op': 'shutdown'})
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self._terminate(self.ended or 'Stopped by the owner.')

    def _terminate(self, reason):
        self.alive = False
        self.executing = False
        self.ended = self.ended or reason
        if self.kill_args:
            try:
                self._run(self.kill_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                          check=False, **({'env': self.env} if self.env is not None else {}))
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass
        if self.reader is not None and self.reader is not threading.current_thread():
            self.reader.join(timeout=5)

    # ---- requests ---------------------------------------------------------
    def execute(self, code, timeout=CELL_TIMEOUT):
        if not isinstance(code, str) or not code.strip():
            raise ValueError('Enter Python code to run')
        if len(code.encode('utf-8')) > MAX_CODE:
            raise ValueError('Cell exceeds %d bytes' % MAX_CODE)
        if not self.lock.acquire(blocking=False):
            raise KernelBusy('A cell is already running; interrupt it or wait')
        try:
            if not self.alive:
                raise KernelError(self.ended or 'No running session')
            ident = self._ident()
            self.executing = True
            try:
                self._send({'id': ident, 'op': 'execute', 'code': code})
                reply = self._await(ident, timeout)
                if reply is None and self.alive:
                    self.interrupt()
                    reply = self._await(ident, INTERRUPT_GRACE)
                    if reply is not None and reply.get('status') == 'interrupted':
                        reply['status'] = 'timeout'
                        reply['note'] = 'Stopped after %d seconds; the session is still running.' % timeout
                if reply is None:
                    if self.alive:
                        self._terminate('The cell did not respond to an interrupt; the session was terminated.')
                    return {'status': 'killed', 'count': self.count, 'output': '', 'truncated': False, 'error': None,
                            'elapsed_ms': int(timeout * 1000), 'note': self.ended}
            finally:
                self.executing = False
            result = self._clean_result(reply)
            self.count = result['count']
            return result
        finally:
            self.lock.release()

    def interrupt(self):
        """Deliver SIGINT to the kernel; a running cell receives KeyboardInterrupt."""
        if not self.alive or self.proc is None:
            return False
        if self.interrupt_args:
            try:
                self._run(self.interrupt_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                          check=False, **({'env': self.env} if self.env is not None else {}))
            except (OSError, subprocess.TimeoutExpired):
                return False
        else:
            try:
                self.proc.send_signal(signal.SIGINT)
            except OSError:
                return False
        return True

    def evaluate(self, expression, timeout=CONDITION_TIMEOUT):
        """Ask the session one expression (a trigger condition) without running a cell.
        Returns {status: ok|error|interrupted|timeout, truth, value, error, elapsed_ms}."""
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError('Enter a Python expression')
        if len(expression.encode('utf-8')) > MAX_EXPRESSION:
            raise ValueError('The condition exceeds %d bytes' % MAX_EXPRESSION)
        if not self.lock.acquire(blocking=False):
            raise KernelBusy('A cell is already running; interrupt it or wait')
        try:
            if not self.alive:
                raise KernelError(self.ended or 'No running session')
            if getattr(self, 'protocol', 2) < 2:
                raise KernelError('This session\'s kernel is older and cannot answer trigger conditions; start a new session')
            ident = self._ident()
            self.executing = True
            try:
                self._send({'id': ident, 'op': 'evaluate', 'expression': expression})
                reply = self._await(ident, timeout)
                if reply is None and self.alive:
                    self.interrupt()
                    reply = self._await(ident, INTERRUPT_GRACE)
                    if reply is not None and reply.get('status') == 'interrupted':
                        reply['status'] = 'timeout'
                if reply is None:
                    if self.alive:
                        self._terminate('The condition did not respond to an interrupt; the session was terminated.')
                    raise KernelError(self.ended or 'The session stopped while evaluating a condition')
            finally:
                self.executing = False
            if not reply.get('ok', True):
                raise KernelError(str(reply.get('message', 'The kernel rejected the condition'))[:500])
            error = reply.get('error') if isinstance(reply.get('error'), dict) else None
            if error:
                error = {'ename': str(error.get('ename', ''))[:100], 'evalue': str(error.get('evalue', ''))[:2000],
                         'traceback': [str(l)[:500] for l in error.get('traceback', [])][:60] if isinstance(error.get('traceback'), list) else []}
            status = reply.get('status') if reply.get('status') in ('ok', 'error', 'interrupted', 'timeout') else 'error'
            return {'status': status, 'truth': bool(reply.get('truth')) if status == 'ok' else None,
                    'value': str(reply.get('value', ''))[:200] if status == 'ok' else None, 'error': error,
                    'elapsed_ms': int(reply.get('elapsed_ms') or 0)}
        finally:
            self.lock.release()

    def variables(self):
        with self.lock:
            if not self.alive:
                raise KernelError(self.ended or 'No running session')
            ident = self._ident()
            self._send({'id': ident, 'op': 'variables'})
            reply = self._await(ident, 10)
            if reply is None:
                self._terminate('The kernel stopped answering; the session was terminated.')
                raise KernelError(self.ended)
        items = reply.get('variables')
        clean = []
        if isinstance(items, list):
            for item in items[:201]:
                if isinstance(item, dict):
                    clean.append({key: str(item.get(key, ''))[:200] for key in ('name', 'type', 'summary')})
        return clean

    def status(self):
        return {'alive': self.alive, 'executing': self.executing, 'label': self.label, 'count': self.count,
                'python': self.info.get('python'), 'started_at': self.started_at, 'ended': self.ended}

    # ---- protocol plumbing ------------------------------------------------
    def _ident(self):
        self.next_id += 1
        return self.next_id

    def _send(self, message):
        line = json.dumps(message, ensure_ascii=True) + '\n'
        try:
            self.proc.stdin.write(line.encode('ascii'))
            self.proc.stdin.flush()
        except (OSError, ValueError) as error:
            self._terminate('The kernel connection closed: %s' % error)
            raise KernelError(self.ended)

    def _read(self):
        stream = self.proc.stdout
        try:
            while True:
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE or not line.endswith(b'\n'):
                    self.ended = 'The kernel sent an oversized message; the session was terminated.'
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # unparseable line from inside the sandbox is dropped
                if isinstance(message, dict):
                    self.replies.put(message)
        except (OSError, ValueError):
            pass
        self.alive = False
        if self.stderr_reader is not None:
            self.stderr_reader.join(timeout=2)
        self.ended = self.ended or self._exit_reason()
        self.replies.put(None)

    def _read_stderr(self):
        """Keep the tail of what the kernel launcher (e.g. the docker client) prints.

        It is the only place Docker explains a refused mount, a missing image or a
        stopped engine, so it is shown to the owner when the session ends early.
        """
        stream = self.proc.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read1(4096) if hasattr(stream, 'read1') else stream.read(4096)
                if not chunk:
                    break
                self.stderr_tail = (self.stderr_tail + chunk)[-4096:]
        except (OSError, ValueError):
            pass

    def _exit_reason(self):
        text = self.stderr_tail.decode('utf-8', 'replace')
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return 'The kernel process exited.'
        detail = ' | '.join(lines[-3:])
        detail = ''.join(c if c.isprintable() else ' ' for c in detail)[:400]
        return 'The kernel process exited: ' + detail

    def _next_message(self, timeout):
        try:
            return self.replies.get(timeout=timeout)
        except queue.Empty:
            return None

    def _await(self, ident, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            message = self._next_message(remaining)
            if message is None:
                if not self.alive:
                    self.replies.put(None)  # keep the sentinel for later waiters
                    return None
                return None
            if message.get('type') == 'reply' and message.get('id') == ident:
                return message
            # Replies for other ids (e.g. a late answer after a timeout) are dropped.

    @staticmethod
    def _clean_result(reply):
        if not reply.get('ok', False):
            return {'status': 'error', 'count': int(reply.get('count', 0) or 0), 'output': '', 'truncated': False,
                    'elapsed_ms': 0, 'error': {'ename': 'KernelError', 'evalue': str(reply.get('message', ''))[:500], 'traceback': []}}
        status = reply.get('status')
        if status not in ('ok', 'error', 'interrupted', 'timeout'):
            status = 'error'
        error = reply.get('error')
        if isinstance(error, dict):
            tb = error.get('traceback')
            error = {'ename': str(error.get('ename', ''))[:100], 'evalue': str(error.get('evalue', ''))[:2000],
                     'traceback': [str(x)[:1000] for x in tb[:400]] if isinstance(tb, list) else []}
        else:
            error = None
        result = {'status': status, 'count': int(reply.get('count', 0) or 0), 'output': str(reply.get('output', ''))[:70000],
                  'truncated': bool(reply.get('truncated', False)), 'error': error,
                  'elapsed_ms': int(reply.get('elapsed_ms', 0) or 0)}
        if 'note' in reply:
            result['note'] = str(reply['note'])[:300]
        return result
