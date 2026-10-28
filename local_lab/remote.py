"""A client for another device's Lab, and a kernel adapter so its sessions fit the local table.

The other device runs this same code (``python3 -m local_lab --port 8795``). It listens on its
own loopback only; the Mac reaches it through an SSH port-forward (``devices.py`` starts one
or the owner does), so from here it looks like ``127.0.0.1:<port>``. The forward uses the
*same* port number on both ends because the remote Lab checks the ``Host`` header against
its own port. Sign-in is the remote Lab's normal owner-key login; the key lives in a private
file on this device that the owner copied over themselves. Nothing in this module changes
anything on the remote device by itself: every call maps to one of the remote Lab's own
reviewed routes.

``RemoteKernel`` gives a remote session the small interface ``sessions.py`` and
``graph_run.py`` use for a local ``KernelSession`` (``alive``, ``executing``, ``status()``,
``execute()``, ``interrupt()``, ``variables()``, ``stop()``), so a cell can "run on"
``nexus/main`` without the scheduler knowing where that is.
"""
import http.client
import json
import threading
import time
from http.cookies import SimpleCookie

from .kernel_session import KernelError, KernelBusy, CELL_TIMEOUT

HTTP_TIMEOUT = 15


class RemoteError(RuntimeError):
    pass


class RemoteLab:
    def __init__(self, port, key, host='127.0.0.1'):
        self.port, self.key, self.host = int(port), key, host
        self.cookie = None
        self.csrf = None
        self.lock = threading.Lock()

    # ---- transport -----------------------------------------------------------
    def _request(self, method, path, body=None, timeout=HTTP_TIMEOUT, retry=True):
        headers = {'Host': '%s:%d' % (self.host, self.port)}
        if self.cookie:
            headers['Cookie'] = self.cookie
        if body is not None:
            headers['Content-Type'] = 'application/json'
            headers['Origin'] = 'http://%s:%d' % (self.host, self.port)
            if self.csrf:
                headers['X-CSRF-Token'] = self.csrf
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = conn.getresponse()
            payload = response.read()
            head = {k.lower(): v for k, v in response.getheaders()}
            conn.close()
        except (OSError, http.client.HTTPException) as error:
            raise RemoteError('The device did not answer: %s' % error)
        try:
            data = json.loads(payload or b'{}')
        except ValueError:
            raise RemoteError('The device answered with something that is not JSON')
        if response.status == 401 and retry and path != '/api/login':
            self.login()
            return self._request(method, path, body, timeout, retry=False)
        return response.status, data, head

    def login(self):
        with self.lock:
            status, data, head = self._request('POST', '/api/login', {'key': self.key}, retry=False)
            if status != 200:
                raise RemoteError('The device refused the owner key (HTTP %s): %s' % (status, data.get('error', '')))
            cookie = SimpleCookie()
            cookie.load(head.get('set-cookie', ''))
            if 'mainaiger_lab_owner' not in cookie:
                raise RemoteError('The device did not return a session cookie')
            self.cookie = 'mainaiger_lab_owner=' + cookie['mainaiger_lab_owner'].value
            self.csrf = data.get('csrf')
            return True

    def get(self, path, timeout=HTTP_TIMEOUT):
        status, data, _ = self._request('GET', path, timeout=timeout)
        if status != 200:
            raise RemoteError(data.get('error') or 'HTTP %s from the device' % status)
        return data

    def post(self, path, body, timeout=HTTP_TIMEOUT):
        status, data, _ = self._request('POST', path, body, timeout=timeout)
        if status != 200:
            error = RemoteError(data.get('error') or 'HTTP %s from the device' % status)
            error.status = status
            raise error
        return data

    # ---- the remote Lab's own routes -----------------------------------------
    def state(self):
        return self.get('/api/state')

    def sessions(self):
        return self.get('/api/sessions')

    def start_session(self, label=None, cpus=None, memory=None):
        current = self.sessions()
        body = {'confirm': True, 'revision': current['revision']}
        if label:
            body['label'] = label
        if cpus:
            body['cpus'] = cpus
        if memory:
            body['memory'] = memory
        return self.post('/api/sessions/start', body)

    def session_action(self, action, ident):
        return self.post('/api/sessions/' + action, {'id': ident})

    def execute(self, ident, code, timeout=CELL_TIMEOUT):
        return self.post('/api/sessions/execute', {'id': ident, 'code': code, 'confirm': True}, timeout=timeout + 15)

    def variables(self, ident):
        return self.post('/api/sessions/variables', {'id': ident})

    def evaluate(self, ident, expression):
        return self.post('/api/sessions/evaluate', {'id': ident, 'expression': expression, 'confirm': True}, timeout=HTTP_TIMEOUT + 15)

    def files(self, root=None, path=''):
        if not root:
            return self.get('/api/files')
        return self.get('/api/files?root=%s&path=%s' % (_q(root), _q(path)))

    def read_file(self, root, path):
        return self.get('/api/files/read?root=%s&path=%s' % (_q(root), _q(path)))

    def journal(self, after=0, limit=100):
        return self.get('/api/journal?after=%d&limit=%d' % (int(after), int(limit)))

    def network(self):
        return self.get('/api/network')


def _q(value):
    from urllib.parse import quote
    return quote(str(value), safe='')


class RemoteKernel:
    """A remote session seen through the local session table."""

    def __init__(self, lab, device, row):
        self.lab, self.device = lab, device
        self.remote_id = row['id']
        self.label = '%s/%s' % (device, row['label'])
        self.last = dict(row)
        self.lock = threading.Lock()
        self.executing = False
        self.count = row.get('count', 0)
        self.ended = row.get('ended')
        self.alive = bool(row.get('alive'))
        self.started_at = row.get('started_at')
        self.info = {'python': row.get('python')}

    def start(self):
        return None  # already running on the device

    def refresh(self, row=None):
        if row is None:
            try:
                rows = self.lab.sessions()['sessions']
            except RemoteError as error:
                self.alive = False
                self.ended = self.ended or 'The device could not be reached: %s' % error
                return self
            row = next((r for r in rows if r['id'] == self.remote_id), None)
            if row is None:
                self.alive = False
                self.ended = self.ended or 'The session is gone on the device'
                return self
        self.last = dict(row)
        self.alive, self.count, self.ended = bool(row.get('alive')), row.get('count', self.count), row.get('ended')
        self.executing = bool(row.get('executing'))
        return self

    def status(self):
        return {'alive': self.alive, 'executing': self.executing, 'label': self.label, 'count': self.count, 'python': self.last.get('python'),
                'started_at': self.last.get('started_at'), 'ended': self.ended, 'remote_id': self.remote_id, 'device': self.device}

    def execute(self, code, timeout=CELL_TIMEOUT):
        if not self.lock.acquire(blocking=False):
            raise KernelBusy('A cell is already running on %s; interrupt it or wait' % self.label)
        try:
            if not self.alive:
                raise KernelError(self.ended or 'Session %s is not running' % self.label)
            self.executing = True
            try:
                data = self.lab.execute(self.remote_id, code, timeout=timeout)
            except RemoteError as error:
                if getattr(error, 'status', None) == 409 and 'already running' in str(error):
                    raise KernelBusy(str(error))
                self.refresh()
                raise KernelError('%s: %s' % (self.label, error))
            finally:
                self.executing = False
            result = data.get('result') or {}
            if 'session' in data and isinstance(data['session'], dict):
                self.refresh(data['session'])
            self.count = result.get('count', self.count)
            return result
        finally:
            self.lock.release()

    def interrupt(self):
        try:
            return bool(self.lab.session_action('interrupt', self.remote_id).get('ok'))
        except RemoteError:
            return False

    def variables(self):
        try:
            return self.lab.variables(self.remote_id).get('variables', [])
        except RemoteError as error:
            raise KernelError('%s: %s' % (self.label, error))

    def evaluate(self, expression, timeout=None):
        """A trigger condition, answered by the session on the device (POST /api/sessions/evaluate there)."""
        if not self.lock.acquire(blocking=False):
            raise KernelBusy('A cell is already running on %s; interrupt it or wait' % self.label)
        try:
            if not self.alive:
                raise KernelError(self.ended or 'Session %s is not running' % self.label)
            try:
                data = self.lab.evaluate(self.remote_id, expression)
            except RemoteError as error:
                if getattr(error, 'status', None) == 404:
                    raise KernelError('%s: the Lab on that device is older and cannot answer trigger conditions; update it' % self.label)
                raise KernelError('%s: %s' % (self.label, error))
            result = data.get('result') if isinstance(data.get('result'), dict) else {}
            return {'status': result.get('status', 'error'), 'truth': result.get('truth'), 'value': result.get('value'),
                    'error': result.get('error'), 'elapsed_ms': result.get('elapsed_ms', 0)}
        finally:
            self.lock.release()

    def stop(self):
        try:
            row = self.lab.session_action('stop', self.remote_id).get('session')
            self.refresh(row)
        except RemoteError as error:
            self.alive = False
            self.ended = 'Stop could not be confirmed: %s' % error
        return self.status()
