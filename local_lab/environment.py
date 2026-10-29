"""Derived session images: a Mainbook declares the packages it needs, the Lab builds one image.

The Mainbook carries ``notebook.metadata.mainaiger.environment = {"packages": [...]}``. Each
package is an exact pin (``name==version``) so the same list always means the same image.
The image is derived from the base image the Lab already uses::

    FROM python:3.13.18-slim
    RUN pip install --no-cache-dir --disable-pip-version-check --require-virtualenv=false \\
        numpy==2.3.3 scikit-learn==1.7.2

and tagged ``mainaiger-lab-env:<12 hex of sha256 of the sorted list>``, so the tag names the
package list and nothing else. Building is the one step that uses the network: ``docker build``
runs with Docker's default build network so pip can download; the session container that later
runs the cells keeps ``--network=none`` and every other limit unchanged. The build is a fixed,
reviewed command: the browser shows the Dockerfile and the exact command and the owner approves
it; the server never takes package names from anywhere but the validated document.

Nothing here runs Python on the host; the only child processes are ``docker build`` and
``docker image inspect``.
"""
import hashlib
import re
import subprocess
import sys
import threading
import time

from .runner import IMAGE, _docker_prefix, resolve_docker

NAME = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,78}[A-Za-z0-9])?$')
EXTRAS = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:,[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)*$')
VERSION = re.compile(r'^[0-9][A-Za-z0-9.+!]{0,39}$')
MAX_PACKAGES = 50
TAG_PREFIX = 'mainaiger-lab-env'
BUILD_TIMEOUT = 900      # seconds
MAX_LOG = 65536          # bytes of build output kept


class EnvironmentSpecError(ValueError):
    """The package list or the build request is not acceptable."""


def parse_packages(values):
    """Validate ``["numpy==2.3.3", "pandas[excel]==2.3.2"]``; returns the sorted, de-duplicated list."""
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_PACKAGES:
        raise EnvironmentSpecError('packages must be a list of at most %d entries' % MAX_PACKAGES)
    out = {}
    for item in values:
        if not isinstance(item, str) or len(item) > 160 or any(c.isspace() for c in item):
            raise EnvironmentSpecError('Each package is one word like name==1.2.3')
        if '==' not in item:
            raise EnvironmentSpecError('"%s": pin an exact version, name==1.2.3, so the build is reproducible' % item)
        spec, version = item.split('==', 1)
        name, extras = spec, None
        if '[' in spec:
            if not spec.endswith(']') or spec.count('[') != 1:
                raise EnvironmentSpecError('"%s": extras look like name[extra1,extra2]' % item)
            name, extras = spec[:-1].split('[', 1)
            if not EXTRAS.match(extras):
                raise EnvironmentSpecError('"%s": extras look like name[extra1,extra2]' % item)
        if not NAME.match(name):
            raise EnvironmentSpecError('"%s": package names use letters, digits, dots, dashes and underscores' % item)
        if not VERSION.match(version):
            raise EnvironmentSpecError('"%s": the version must start with a digit (for example 2.3.3)' % item)
        key = name.lower().replace('_', '-').replace('.', '-')
        if key in out:
            raise EnvironmentSpecError('"%s" is listed twice' % name)
        out[key] = item
    return [out[k] for k in sorted(out)]


def packages_of(doc):
    own = doc.get('notebook', {}).get('metadata', {}).get('mainaiger', {})
    env = own.get('environment') if isinstance(own, dict) else None
    return parse_packages(env.get('packages')) if isinstance(env, dict) else []


def image_tag(packages):
    packages = parse_packages(packages)
    if not packages:
        return IMAGE
    digest = hashlib.sha256('\n'.join(packages).encode('utf-8')).hexdigest()[:12]
    return '%s:%s' % (TAG_PREFIX, digest)


def dockerfile(packages):
    packages = parse_packages(packages)
    lines = ['FROM ' + IMAGE]
    if packages:
        lines.append('RUN pip install --no-cache-dir --disable-pip-version-check \\')
        lines.append('    ' + ' '.join(packages))
    return '\n'.join(lines) + '\n'


def build_command(packages):
    """The exact command the owner reviews. The Dockerfile arrives on stdin (``-``), so there is
    no build context: nothing from the Mac is sent to the build."""
    return _docker_prefix() + ['build', '--tag', image_tag(packages), '-']


def describe(packages):
    packages = parse_packages(packages)
    return {'packages': packages, 'image': image_tag(packages), 'base_image': IMAGE, 'derived': bool(packages),
            'dockerfile': dockerfile(packages), 'command': build_command(packages),
            'network': 'Only this build step may use the network (pip downloads through Docker\'s build network). Sessions keep --network=none.' if packages else 'No build needed: the base image is used as is.'}


def image_ready(tag, run=subprocess.run):
    """True when Docker reports the image locally. Never pulls."""
    docker, env, error = resolve_docker()
    if error:
        return False
    args = _docker_prefix() + ['image', 'inspect', '--format', '{{.Id}}', tag]
    args[0] = docker
    try:
        proc = run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20, **({'env': env} if env is not None else {}))
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


class Build:
    """One background ``docker build``. Holds the bounded log and the outcome."""

    def __init__(self, packages, popen=None):
        self.packages = parse_packages(packages)
        self.image = image_tag(self.packages)
        self.command = build_command(self.packages)
        self.state = 'pending'           # pending | running | done | failed
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.error = None
        self.log = bytearray()
        self.truncated = False
        self._popen = popen or (lambda *a, **k: subprocess.Popen(*a, **k))  # looked up at call time so tests can substitute a fake build
        self._lock = threading.Lock()
        self.thread = None

    def start(self):
        if not self.packages:
            raise EnvironmentSpecError('No packages are declared; the base image needs no build')
        docker, env, error = resolve_docker()
        if error:
            raise EnvironmentSpecError(error)
        args = list(self.command)
        args[0] = docker
        self.state = 'running'
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._run, args=(args, env), daemon=True)
        self.thread.start()

    def _run(self, args, env):
        try:
            proc = self._popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, **({'env': env} if env is not None else {}))
        except OSError as error:
            self._finish(None, 'docker build could not start: %s' % error)
            return
        try:
            proc.stdin.write(dockerfile(self.packages).encode('utf-8'))
            proc.stdin.close()
        except OSError:
            pass
        deadline = time.time() + BUILD_TIMEOUT
        timed_out = False
        reader = threading.Thread(target=self._read, args=(proc,), daemon=True)
        reader.start()
        while proc.poll() is None:
            if time.time() > deadline:
                timed_out = True
                proc.kill()
                break
            time.sleep(0.2)
        reader.join(timeout=5)
        proc.wait()
        try:
            proc.stdout.close()
        except OSError:
            pass
        self._finish(proc.returncode, 'The build exceeded %d minutes and was stopped' % (BUILD_TIMEOUT // 60) if timed_out else None)

    def _read(self, proc):
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                return
            with self._lock:
                room = MAX_LOG - len(self.log)
                if room > 0:
                    self.log += chunk[:room]
                if len(chunk) > room:
                    self.truncated = True

    def _finish(self, code, error):
        self.exit_code = code
        self.finished_at = time.time()
        if error:
            self.error = error
            self.state = 'failed'
        elif code == 0:
            self.state = 'done'
        else:
            self.error = 'docker build exited with code %s' % code
            self.state = 'failed'

    def snapshot(self):
        with self._lock:
            log = self.log.decode('utf-8', 'replace')
        return {'state': self.state, 'image': self.image, 'packages': self.packages, 'command': self.command,
                'started_at': self.started_at, 'finished_at': self.finished_at, 'exit_code': self.exit_code,
                'error': self.error, 'log': log[-8000:], 'log_truncated': self.truncated or len(log) > 8000}
