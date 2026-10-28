"""Several persistent sessions on this device, one of them "active".

Before this module the Lab had exactly one kernel. Now it keeps a small table of sessions
(the RESOURCES panel), each with its own label, CPU and memory limits and image, and one
of them is *active*: the session the ``/api/kernel/*`` routes and plain runs use. A cell
can instead name the session it runs on (``metadata.mainaiger.session = "<label>"``), which
is how "choose which code uses what resources" starts: each session is its own container,
its own Python process and its own variables. Nothing crosses between sessions unless a
cell writes a file in the shared workspace and another reads it; the preflight says so.

The registry does not know how a kernel is made: ``factory(settings, state_dir)`` returns a
``KernelSession`` (Docker in the app, a plain Python child in tests), exactly as before.
"""
import re
import secrets
import threading
import time

from .kernel_session import KernelError

MAX_SESSIONS = 4
LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _-]{0,30}$')
DEVICE = 'this-device'


class SessionRegistry:
    def __init__(self, factory, state_dir, journal=None):
        self.factory, self.state_dir, self.journal = factory, state_dir, journal
        self.lock = threading.Lock()
        self.rows = {}       # id -> row
        self.active_id = None

    # ---- helpers -----------------------------------------------------------
    def _log(self, kind, text, data=None):
        if self.journal is not None:
            self.journal.write(kind, text, data)

    def _fresh_label(self, label):
        taken = {r['label'] for r in self.rows.values()}
        base = label or 'session'
        if base not in taken:
            return base
        n = 2
        while '%s %d' % (base, n) in taken:
            n += 1
        return '%s %d' % (base, n)

    def row_status(self, row):
        status = row['kernel'].status()
        status['container'] = status.pop('label', None)  # the kernel's own label is the container/process name
        return {**status, 'id': row['id'], 'label': row['label'], 'device': DEVICE, 'image': row['image'],
                'cpus': row['settings']['cpus'], 'memory': row['settings']['memory'], 'workspace': row['settings']['workspace'],
                'created_at': row['created_at'], 'active': row['id'] == self.active_id}

    # ---- queries -------------------------------------------------------------
    def list(self):
        with self.lock:
            return [self.row_status(r) for r in self.rows.values()]

    def get(self, ident):
        with self.lock:
            return self.rows.get(ident)

    def by_label(self, label):
        with self.lock:
            for row in self.rows.values():
                if row['label'] == label:
                    return row
        return None

    def active(self):
        """The active row, or None."""
        with self.lock:
            return self.rows.get(self.active_id)

    def active_kernel(self):
        row = self.active()
        return row['kernel'] if row else None

    def kernel_for(self, label=None):
        """The kernel a cell should use: the named session, else the active one. Raises when
        the named session does not exist or is not alive."""
        if label:
            row = self.by_label(label)
            if row is None:
                raise KernelError('No session named "%s"; start one in Resources or change the cell\'s "run on"' % label)
            if not row['kernel'].alive:
                raise KernelError('Session "%s" is not running' % label)
            return row['kernel']
        kernel = self.active_kernel()
        if kernel is None or not kernel.alive:
            raise KernelError('Start a session before running the graph')
        return kernel

    def any_alive(self):
        with self.lock:
            return any(r['kernel'].alive for r in self.rows.values())

    # ---- lifecycle -----------------------------------------------------------
    def start(self, settings, label=None, image=None):
        if label is not None and (not isinstance(label, str) or not LABEL.match(label)):
            raise KernelError('Session labels use letters, digits, spaces, dashes or underscores (up to 31 characters)')
        with self.lock:
            # Rows whose kernel died stay listed (the owner can read why); they do not count against the cap.
            alive = [r for r in self.rows.values() if r['kernel'].alive]
            if len(alive) >= MAX_SESSIONS:
                raise KernelError('At most %d sessions can run at once; stop one first' % MAX_SESSIONS)
            label = self._fresh_label(label)
            settings = dict(settings)
            if image:
                settings['image'] = image
            kernel = self.factory(settings, self.state_dir)
            kernel.start()
            row = {'id': 's-' + secrets.token_hex(4), 'label': label, 'settings': settings, 'image': image or settings.get('image') or 'base',
                   'kernel': kernel, 'created_at': time.time()}
            self.rows[row['id']] = row
            if self.active_id is None or not self.rows[self.active_id]['kernel'].alive:
                self.active_id = row['id']
            status = self.row_status(row)
        self._log('session.start', 'Session "%s" started (%s CPU, %s, image %s)' % (label, settings['cpus'], settings['memory'], status['image']),
                  {'id': row['id'], 'label': label, 'image': status['image'], 'cpus': settings['cpus'], 'memory': settings['memory']})
        return status

    def stop(self, ident=None):
        with self.lock:
            row = self.rows.get(ident or self.active_id)
            if row is None:
                return None
            row['kernel'].stop()
            status = self.row_status(row)
        self._log('session.stop', 'Session "%s" stopped' % row['label'], {'id': row['id'], 'label': row['label']})
        return status

    def interrupt(self, ident=None):
        row = self.get(ident) if ident else self.active()
        if row is None or not row['kernel'].alive:
            raise KernelError('No running session')
        ok = row['kernel'].interrupt()
        self._log('session.interrupt', 'Interrupt sent to "%s"' % row['label'], {'id': row['id'], 'delivered': bool(ok)})
        return ok, self.row_status(row)

    def select(self, ident):
        with self.lock:
            if ident not in self.rows:
                raise KernelError('No such session')
            self.active_id = ident
            return self.row_status(self.rows[ident])

    def forget(self, ident):
        """Remove a stopped session from the table."""
        with self.lock:
            row = self.rows.get(ident)
            if row is None:
                raise KernelError('No such session')
            if row['kernel'].alive:
                raise KernelError('Stop the session before removing it')
            del self.rows[ident]
            if self.active_id == ident:
                self.active_id = next((r['id'] for r in self.rows.values() if r['kernel'].alive), None)
        return True

    def stop_all(self):
        with self.lock:
            rows = list(self.rows.values())
        for row in rows:
            try:
                if row['kernel'].alive:
                    row['kernel'].stop()
            except Exception:
                pass
