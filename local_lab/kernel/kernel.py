"""MainAIger session kernel: one persistent Python process that runs cells on request.

This file is copied read-only into the sandbox container and started as
``python -I /opt/mainaiger/kernel.py``. The host service talks to it with one
JSON object per line on the process's original stdin/stdout. Variables created
by one cell stay in memory for the next cell, like a Jupyter kernel.

A kernel is an execution process, not a security boundary. Cell code runs with
the kernel's own privileges; the container around it is what limits files,
network, CPU and memory. The host treats every byte this process writes as
untrusted and validates it.

Standard library only; it must run inside the plain ``python:*-slim`` image.
"""
import ast
import builtins
import io
import json
import os
import signal
import sys
import tempfile
import time
import traceback
import types

PROTOCOL = 1
MAX_OUTPUT = 65536          # bytes of captured output returned per cell
MAX_CODE = 200000           # bytes of source accepted per cell
MAX_VARIABLES = 200
SUMMARY_LENGTH = 80
PLAIN_TYPES = (int, float, complex, bool, str, bytes, type(None))
CONTAINERS = (list, tuple, set, frozenset, dict)
ARRAY_MODULES = {'numpy', 'pandas', 'torch', 'jax', 'scipy'}


class _CappedWriter(io.TextIOBase):
    """Text stream that writes to a file descriptor until a byte budget is spent."""

    def __init__(self, fd, budget):
        self.fd, self.budget, self.written, self.overflow = fd, budget, 0, False

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError('write() argument must be str')
        data = text.encode('utf-8', 'replace')
        remaining = self.budget[0] - self.written
        if remaining <= 0:
            self.overflow = True
            return len(text)
        chunk = data[:remaining]
        if len(chunk) < len(data):
            self.overflow = True
        while chunk:
            count = os.write(self.fd, chunk)
            chunk = chunk[count:]
        self.written += len(data[:remaining])
        return len(text)

    def flush(self):
        pass


class Kernel:
    def __init__(self, proto_in, proto_out, capture_fd):
        self.proto_in, self.proto_out, self.capture_fd = proto_in, proto_out, capture_fd
        self.namespace = {'__name__': '__main__', '__builtins__': builtins}
        self.count = 0
        self.executing = False
        self.budget = [MAX_OUTPUT]
        self.out = _CappedWriter(1, self.budget)
        self.err = _CappedWriter(2, self.budget)

    # ---- protocol -------------------------------------------------------
    def send(self, message):
        line = json.dumps(message, ensure_ascii=True, separators=(',', ':')) + '\n'
        self.proto_out.write(line.encode('ascii'))
        self.proto_out.flush()

    def serve(self):
        self.send({'type': 'ready', 'protocol': PROTOCOL, 'python': sys.version.split()[0],
                   'pid': os.getpid(), 'cwd': os.getcwd()})
        while True:
            try:
                line = self.proto_in.readline()
            except KeyboardInterrupt:
                continue  # an interrupt that arrived while idle is ignored
            if not line:
                return
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError('request must be an object')
            except (ValueError, UnicodeDecodeError) as error:
                self.send({'type': 'error', 'id': None, 'message': 'Malformed request: %s' % error})
                continue
            ident = request.get('id')
            op = request.get('op')
            try:
                if op == 'execute':
                    reply = self.execute(request.get('code'))
                elif op == 'variables':
                    reply = {'variables': self.variables()}
                elif op == 'ping':
                    reply = {'pong': True, 'count': self.count}
                elif op == 'shutdown':
                    self.send({'type': 'reply', 'id': ident, 'ok': True, 'shutdown': True})
                    return
                else:
                    raise ValueError('unknown op')
                reply.update(type='reply', id=ident, ok=True)
            except KeyboardInterrupt:
                reply = {'type': 'reply', 'id': ident, 'ok': False, 'message': 'interrupted while idle'}
            except Exception as error:  # a bug in the kernel itself, not user code
                reply = {'type': 'reply', 'id': ident, 'ok': False, 'message': 'kernel error: %s' % error}
            self.send(reply)

    # ---- output capture --------------------------------------------------
    def _collect_output(self):
        sys.stdout.flush(); sys.stderr.flush()
        os.lseek(self.capture_fd, 0, os.SEEK_SET)
        chunks, size = [], 0
        while size <= MAX_OUTPUT:
            chunk = os.read(self.capture_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk); size += len(chunk)
        os.ftruncate(self.capture_fd, 0)
        os.lseek(self.capture_fd, 0, os.SEEK_SET)
        data = b''.join(chunks)
        truncated = len(data) > MAX_OUTPUT or self.out.overflow or self.err.overflow
        return data[:MAX_OUTPUT].decode('utf-8', 'replace'), truncated

    # ---- execution -------------------------------------------------------
    def execute(self, code):
        if not isinstance(code, str):
            raise ValueError('code must be a string')
        if len(code.encode('utf-8')) > MAX_CODE:
            raise ValueError('code exceeds %d bytes' % MAX_CODE)
        self.count += 1
        filename = '<cell %d>' % self.count
        self.out.written = self.err.written = 0
        self.out.overflow = self.err.overflow = False
        status, error = 'ok', None
        started = time.monotonic()
        self.executing = True
        try:
            tree = ast.parse(code, filename, 'exec')
            last = tree.body[-1] if tree.body and isinstance(tree.body[-1], ast.Expr) else None
            body = tree.body[:-1] if last is not None else tree.body
            if body:
                exec(compile(ast.Module(body=body, type_ignores=[]), filename, 'exec'), self.namespace)
            if last is not None:
                value = eval(compile(ast.Expression(body=last.value), filename, 'eval'), self.namespace)
                if value is not None:
                    self.namespace['_'] = value
                    print(repr(value))
        except KeyboardInterrupt:
            status = 'interrupted'
            error = self._format_error(*sys.exc_info(), filename=filename)
        except SystemExit as exit_request:
            status = 'error'
            error = {'ename': 'SystemExit', 'evalue': str(exit_request.code), 'traceback': [
                'SystemExit is ignored inside a session; stop the session to end it.']}
        except BaseException:  # noqa: B902 - user code can raise anything
            status = 'error'
            error = self._format_error(*sys.exc_info(), filename=filename)
        finally:
            self.executing = False
            sys.stdout, sys.stderr = self.out, self.err  # cells may reassign these
        output, truncated = self._collect_output()
        return {'status': status, 'count': self.count, 'output': output, 'truncated': truncated,
                'error': error, 'elapsed_ms': int((time.monotonic() - started) * 1000)}

    @staticmethod
    def _format_error(kind, value, tb, filename):
        # Drop the kernel's own exec/eval frame so the traceback starts in the cell.
        while tb is not None and tb.tb_frame.f_code.co_filename != filename:
            tb = tb.tb_next
        lines = traceback.format_exception(kind, value, tb, limit=30)
        text = ''.join(lines)
        if len(text) > 16000:
            text = text[:16000] + '\n[traceback truncated]\n'
        return {'ename': kind.__name__, 'evalue': str(value)[:2000], 'traceback': text.splitlines()}

    # ---- variables -------------------------------------------------------
    def variables(self):
        items = []
        for name, value in list(self.namespace.items()):
            if name.startswith('__') or name == '_':
                continue
            if len(items) >= MAX_VARIABLES:
                items.append({'name': '…', 'type': '', 'summary': 'more variables not listed'})
                break
            items.append({'name': name, 'type': type(value).__name__, 'summary': self.summarize(value)})
        return items

    @staticmethod
    def summarize(value):
        """Describe a value without calling user-defined code."""
        kind = type(value)
        if isinstance(value, PLAIN_TYPES):
            text = repr(value)
            return text if len(text) <= SUMMARY_LENGTH else text[:SUMMARY_LENGTH - 1] + '…'
        if isinstance(value, CONTAINERS):
            return '%d items' % len(value)
        if isinstance(value, types.ModuleType):
            return 'module'
        if isinstance(value, type):
            return 'class'
        if isinstance(value, (types.FunctionType, types.BuiltinFunctionType, types.MethodType)):
            return 'function'
        root = (kind.__module__ or '').split('.')[0]
        if root in ARRAY_MODULES:
            parts = []
            try:
                shape = getattr(value, 'shape', None)
                if isinstance(shape, tuple):
                    parts.append('shape %s' % (shape,))
                dtype = getattr(value, 'dtype', None)
                if dtype is not None:
                    parts.append('dtype %s' % dtype)
            except Exception:
                pass
            return ', '.join(parts) or 'object'
        return 'object'  # repr() of arbitrary objects is not called


def main():
    # Keep private copies of the protocol pipes, then point fds 0/1/2 elsewhere so
    # cell code (even os.write(1, …)) cannot corrupt the protocol and input() does
    # not block waiting for the host.
    proto_in = os.fdopen(os.dup(0), 'rb')
    proto_out = os.fdopen(os.dup(1), 'wb')
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0); os.close(devnull)
    capture_fd, path = tempfile.mkstemp(prefix='mainaiger-cell-')
    os.unlink(path)
    os.dup2(capture_fd, 1); os.dup2(capture_fd, 2)
    kernel = Kernel(proto_in, proto_out, capture_fd)
    sys.stdin = io.StringIO('')
    sys.stdout, sys.stderr = kernel.out, kernel.err

    def on_interrupt(signum, frame):
        if kernel.executing:
            raise KeyboardInterrupt
    signal.signal(signal.SIGINT, on_interrupt)
    signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(0))  # not catchable by cell code
    kernel.serve()


if __name__ == '__main__':
    main()
