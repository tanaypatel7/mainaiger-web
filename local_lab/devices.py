"""Paired devices: other machines running this same Lab, reached through SSH port-forwards.

Pairing is three owner-visible facts kept in ``<state dir>/devices.json`` (mode 0600):
a *name* (``nexus``), the *SSH host* (an alias from the owner's own ``~/.ssh/config`` or
``user@host``), the *port* the remote Lab listens on, and the path of a private file holding
that Lab's owner key (copied by the owner, for example with ``scp``; the Lab never moves
keys). Nothing is sent anywhere at pairing time.

Connecting is a reviewed step: the exact command is shown first::

    ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes \\
        -L 127.0.0.1:8795:127.0.0.1:8795 nexus

The Lab starts that child process (``tunnel: "lab"``), waits for the port to answer, then
signs in to the remote Lab with the key. With ``tunnel: "manual"`` the owner runs the forward
(or Tailscale SSH) themselves and the Lab only signs in. ``BatchMode=yes`` means ssh never
prompts: keys or an agent must already work for that host, which is the state the owner
reported. Disconnect ends the child process. The remote Lab stays loopback-only on its own
device throughout; nothing opens a public port anywhere.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import remote as remote_module

FILE = 'devices.json'
NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,30}$')
HOST = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._@-]{0,120}$')
SSH = 'ssh'
TUNNEL_READY_SECONDS = 12
MAX_DEVICES = 16


class DeviceError(ValueError):
    pass


def read_key(path):
    """The remote owner key from a private file: owner-owned, regular, mode 0600, not a symlink."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise DeviceError('The key file path must be absolute')
    if p.is_symlink() or not p.is_file():
        raise DeviceError('The key file must be a plain file (no symlink)')
    info = p.stat()
    if info.st_uid != os.getuid() or (info.st_mode & 0o077):
        raise DeviceError('The key file must be owned by you with mode 0600 (chmod 600)')
    key = p.read_text(encoding='utf-8').strip()
    if not 32 <= len(key) <= 128 or any(c.isspace() for c in key):
        raise DeviceError('The key file does not look like a Lab owner key')
    return key


def tunnel_command(host, port):
    return [SSH, '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-o', 'StrictHostKeyChecking=yes',
            '-L', '127.0.0.1:%d:127.0.0.1:%d' % (port, port), host]


def _port_open(port, timeout=0.5):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            return True
    except OSError:
        return False


class Tunnel:
    def __init__(self, host, port, popen=None):
        self.host, self.port = host, port
        self.command = tunnel_command(host, port)
        self.proc = None
        self.error = None
        self._popen = popen or (lambda *a, **k: subprocess.Popen(*a, **k))

    def start(self):
        if _port_open(self.port):
            raise DeviceError('Port %d is already in use on this device; stop whatever listens there or choose another port for the remote Lab' % self.port)
        try:
            self.proc = self._popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
        except OSError as error:
            raise DeviceError('ssh could not be started: %s' % error)
        deadline = time.time() + TUNNEL_READY_SECONDS
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = (self.proc.stdout.read() or b'').decode('utf-8', 'replace').strip()
                self.proc.stdout.close()
                self.error = out[-600:] or 'ssh exited with code %s' % self.proc.returncode
                raise DeviceError('The SSH forward ended: %s' % self.error)
            if _port_open(self.port):
                return True
            time.sleep(0.25)
        self.stop()
        raise DeviceError('The SSH forward did not come up within %d seconds' % TUNNEL_READY_SECONDS)

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.proc is not None and self.proc.stdout:
            try:
                self.proc.stdout.close()
            except OSError:
                pass


class DeviceManager:
    def __init__(self, state_dir, local_port=None, journal=None, popen=None):
        self.state_dir = Path(state_dir).resolve(strict=True)
        self.local_port = local_port
        self.journal = journal
        self._popen = popen
        self.lock = threading.Lock()
        self.devices = self._load()
        self.connections = {}  # name -> {'tunnel': Tunnel|None, 'lab': RemoteLab, 'connected_at': float}
        self.errors = {}

    # ---- storage -------------------------------------------------------------
    def _path(self):
        return self.state_dir / FILE

    def _load(self):
        p = self._path()
        if not p.is_file() or p.is_symlink():
            return []
        try:
            rows = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        return [r for r in rows if isinstance(r, dict) and isinstance(r.get('name'), str)] if isinstance(rows, list) else []

    def _save(self):
        tmp = self._path().with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(self.devices, out, indent=1)
        os.replace(tmp, self._path())

    def _log(self, kind, text, data=None):
        if self.journal is not None:
            self.journal.write(kind, text, data)

    # ---- pairing -------------------------------------------------------------
    def pair(self, name, host, port, key_file, tunnel='lab'):
        if not isinstance(name, str) or not NAME.match(name):
            raise DeviceError('Device names are short lowercase words: letters, digits, dashes')
        if not isinstance(host, str) or not HOST.match(host):
            raise DeviceError('The SSH host is an alias from your ssh config or user@host, without spaces')
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            raise DeviceError('The port must be between 1024 and 65535')
        if self.local_port and port == self.local_port:
            raise DeviceError('Port %d is this Lab\'s own port; run the remote Lab on another port (for example %d)' % (port, port + 1))
        if tunnel not in ('lab', 'manual'):
            raise DeviceError('tunnel must be "lab" (the Lab starts ssh) or "manual" (you run the forward)')
        read_key(key_file)  # validated now; read again at connect time
        with self.lock:
            if any(d['name'] == name for d in self.devices):
                raise DeviceError('A device named "%s" is already paired' % name)
            if len(self.devices) >= MAX_DEVICES:
                raise DeviceError('At most %d devices can be paired' % MAX_DEVICES)
            self.devices.append({'name': name, 'host': host, 'port': port, 'key_file': str(Path(key_file).expanduser()), 'tunnel': tunnel, 'paired_at': time.time()})
            self._save()
        self._log('device.pair', 'Paired device "%s" (%s, port %d)' % (name, host, port), {'name': name, 'host': host, 'port': port, 'tunnel': tunnel})
        return self.describe(name)

    def forget(self, name):
        self.disconnect(name)
        with self.lock:
            before = len(self.devices)
            self.devices = [d for d in self.devices if d['name'] != name]
            if len(self.devices) == before:
                raise DeviceError('No device named "%s"' % name)
            self._save()
        self._log('device.forget', 'Forgot device "%s"' % name, {'name': name})
        return True

    def get(self, name):
        with self.lock:
            return next((d for d in self.devices if d['name'] == name), None)

    # ---- connecting ----------------------------------------------------------
    def connect(self, name):
        device = self.get(name)
        if device is None:
            raise DeviceError('No device named "%s"' % name)
        if name in self.connections:
            return self.describe(name)
        key = read_key(device['key_file'])
        tunnel = None
        if device['tunnel'] == 'lab':
            tunnel = Tunnel(device['host'], device['port'], popen=self._popen)
            tunnel.start()
        lab = remote_module.RemoteLab(device['port'], key)
        try:
            lab.login()
            state = lab.state()
        except remote_module.RemoteError as error:
            if tunnel:
                tunnel.stop()
            self.errors[name] = str(error)
            raise DeviceError('Connected to port %d but the Lab there refused the sign-in: %s' % (device['port'], error))
        self.connections[name] = {'tunnel': tunnel, 'lab': lab, 'connected_at': time.time(), 'state': state}
        self.errors.pop(name, None)
        self._log('device.connect', 'Connected to device "%s"' % name, {'name': name, 'tunnel': device['tunnel']})
        return self.describe(name)

    def disconnect(self, name):
        conn = self.connections.pop(name, None)
        if conn is None:
            return False
        if conn['tunnel']:
            conn['tunnel'].stop()
        self._log('device.disconnect', 'Disconnected from device "%s"' % name, {'name': name})
        return True

    def lab(self, name):
        conn = self.connections.get(name)
        if conn is None:
            raise DeviceError('Device "%s" is not connected' % name)
        if conn['tunnel'] and not conn['tunnel'].alive:
            self.connections.pop(name, None)
            self.errors[name] = 'The SSH forward ended' + ((': ' + conn['tunnel'].error) if conn['tunnel'].error else '')
            raise DeviceError('The connection to "%s" dropped (%s); connect again' % (name, self.errors[name]))
        return conn['lab']

    def connected(self):
        return [name for name in list(self.connections) if self._still_connected(name)]

    def _still_connected(self, name):
        try:
            self.lab(name)
            return True
        except DeviceError:
            return False

    # ---- reporting -----------------------------------------------------------
    def describe(self, name):
        device = self.get(name)
        if device is None:
            raise DeviceError('No device named "%s"' % name)
        conn = self.connections.get(name) if self._still_connected(name) else None
        out = {k: device[k] for k in ('name', 'host', 'port', 'tunnel', 'key_file', 'paired_at')}
        out['command'] = tunnel_command(device['host'], device['port']) if device['tunnel'] == 'lab' else None
        out['connected'] = conn is not None
        out['connected_at'] = conn['connected_at'] if conn else None
        out['error'] = self.errors.get(name)
        out['remote'] = None
        if conn:
            try:
                st = conn['lab'].state()
                out['remote'] = {'workspace': st.get('workspace'), 'limits': st.get('limits'), 'docker_available': st.get('docker_available'),
                                 'environment': st.get('environment'), 'session': st.get('session')}
            except remote_module.RemoteError as error:
                out['error'] = str(error)
        return out

    def list(self):
        with self.lock:
            names = [d['name'] for d in self.devices]
        return [self.describe(n) for n in names]

    def close(self):
        for name in list(self.connections):
            self.disconnect(name)
