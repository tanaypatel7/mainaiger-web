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

    # ---- process lifetime --------------------------------------------------
    def start(self, timeout=START_TIMEOUT):
        if self.proc is not None:
            raise KernelError('Session already started')
        kwargs = {'env': self.env} if self.env is not None else {}
        self.proc = self._popen(self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, shell=False, **kwargs)
        self.alive = True
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        message = self._next_message(timeout)
        if message is None or message.get('type') != 'ready' or message.get('protocol') != 1:
            self._terminate('The kernel did not report ready. ' + (self.ended or 'Check Docker Desktop and the image.'))
            raise KernelError(self.ended)
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
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
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
        self.ended = self.ended or 'The kernel process exited.'
        self.replies.put(None)

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
