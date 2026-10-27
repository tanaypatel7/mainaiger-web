"""Fixed, owner-approved checks for the local setup Mainbook."""
import os
import platform
import hashlib
import http.client
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from .runner import IMAGE

CONTEXT = 'desktop-linux'
INSTALLER_VERSION = '4.96.3'
INSTALLER_BUILD = '244472'
INSTALLERS = {
    'arm64': ('arm64', 'a3ac78b7de67af169ca58f33a4fa4c26007840870574c80c59a7ab0c7f68d954'),
    'x86_64': ('amd64', '9d3409c689628d4f42972e4773a700817ff3526b1810a30a8de33fc1379e016b'),
}
MAX_DOWNLOAD = 2 * 1024 * 1024 * 1024

def _installer_spec():
    spec = INSTALLERS.get(platform.machine())
    if platform.system() != 'Darwin' or not spec:
        raise ValueError('Docker installer is supported only on Apple silicon or Intel Macs.')
    arch, digest = spec
    return f'https://desktop.docker.com/mac/main/{arch}/{INSTALLER_BUILD}/Docker.dmg', digest

def _installed():
    return any(p.exists() for p in (Path('/Applications/Docker.app'), Path.home()/'Applications/Docker.app'))

def _find_docker():
    # Docker Desktop defaults to a user-level CLI link outside many GUI app PATHs.
    for path in (Path.home()/'.docker/bin/docker', Path('/Applications/Docker.app/Contents/Resources/bin/docker'), Path.home()/'Applications/Docker.app/Contents/Resources/bin/docker'):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which('docker')

def _staging(state_dir):
    root = Path(state_dir).resolve(strict=True)
    path = root/'installers'
    if path.is_symlink(): raise ValueError('Installer directory cannot be a symlink.')
    path.mkdir(mode=0o700, exist_ok=True)
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077 or not path.is_dir():
        raise ValueError('Installer directory must be private and owned by this account.')
    return path

def _digest_matches(path, expected):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest() == expected

def _verify_signature(dmg, stage):
    # Attach invisibly and read-only, verify the code signature and Docker Team ID,
    # then detach. The user sees the standard installer only after verification.
    tmp = Path(tempfile.mkdtemp(prefix='verify-', dir=stage))
    mount = tmp/'volume'
    mount.mkdir()
    attached = False
    verified = False
    message = 'The installer could not be verified.'
    try:
        code, output = _run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-noautoopen', '-mountpoint', str(mount), str(dmg)], timeout=120)
        if code != 0:
            message = 'Could not mount the downloaded installer for signature verification. ' + output[:500]
        else:
            attached = True
            app = mount/'Docker.app'
            if not app.is_dir() or app.is_symlink():
                message = 'The image did not contain the expected Docker.app.'
            else:
                requirement = '=anchor apple generic and identifier "com.docker.docker" and certificate leaf[subject.OU] = "9BNSXJN65R"'
                code, output = _run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', requirement, str(app)], timeout=180)
                verified = code == 0
                message = ('SHA-256 and Docker app signing identity verified.' if verified else
                           'Docker app signature or signing identity verification failed. ' + output[:500])
    finally:
        if attached:
            code, _ = _run(['/usr/bin/hdiutil', 'detach', str(mount)], timeout=30)
            if code != 0:
                verified = False
                message = 'The verification volume could not be detached. Please eject it in Finder before retrying.'
        # Do not recursively remove a directory that might still be a mountpoint.
        try: mount.rmdir(); tmp.rmdir()
        except OSError: pass
    return verified, message

class _DockerRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        u = urlsplit(newurl)
        if u.scheme != 'https' or u.hostname != 'desktop.docker.com' or u.username or u.password or u.port not in (None, 443):
            raise ValueError('Docker download redirected to an unapproved address.')
        return super().redirect_request(request, fp, code, msg, headers, newurl)

def download_installer(state_dir, progress_callback=None):
    progress = progress_callback or (lambda event: None)
    if _installed():
        return {'ok': False, 'output': 'Docker.app is already present. Existing installations are not overwritten.'}
    try:
        url, expected = _installer_spec()
        stage = _staging(state_dir)
        fd, tmpname = tempfile.mkstemp(prefix='docker-', suffix='.part', dir=stage)
        tmp = Path(tmpname)
        try:
            digest = hashlib.sha256()
            received = 0
            deadline = time.monotonic() + 1800
            opener = urllib.request.build_opener(_DockerRedirect())
            with os.fdopen(fd, 'wb') as out, opener.open(url, timeout=30) as response:
                declared = response.headers.get('Content-Length')
                total = int(declared) if declared and declared.isdigit() else None
                if total is not None and total > MAX_DOWNLOAD: raise ValueError('Installer download exceeds the size limit.')
                progress({'phase':'downloading', 'bytes':0, 'total':total})
                while True:
                    if time.monotonic() > deadline: raise TimeoutError('Installer download timed out.')
                    chunk = response.read(1024 * 1024)
                    if not chunk: break
                    received += len(chunk)
                    if received > MAX_DOWNLOAD: raise ValueError('Installer download exceeds the size limit.')
                    digest.update(chunk); out.write(chunk)
                    progress({'phase':'downloading', 'bytes':received, 'total':total})
                if total is not None and received != total: raise ValueError('Installer download was incomplete.')
            if digest.hexdigest() != expected: raise ValueError('Installer checksum did not match the pinned Docker release.')
            progress({'phase':'verifying', 'bytes':received, 'total':total})
            ok, message = _verify_signature(tmp, stage)
            if not ok: return {'ok':False, 'output':message}
            if _installed(): return {'ok':False, 'output':'Docker was installed during download; the staged installer was not retained.'}
            os.replace(tmp, stage/'Docker.dmg')
            return {'ok':True, 'output':message + ' Ready to open the native installer.'}
        finally:
            try: tmp.unlink(missing_ok=True)
            except OSError: pass
    except (ValueError, OSError, TimeoutError, http.client.HTTPException) as error:
        return {'ok':False, 'output':'Installer preparation failed: ' + str(error)[:500]}

def open_installer(state_dir):
    if _installed():
        return {'ok':False, 'output':'Docker.app is already present. Existing installations are not overwritten.'}
    try:
        _, expected = _installer_spec()
        stage = _staging(state_dir)
        dmg = stage/'Docker.dmg'
        if dmg.is_symlink() or not dmg.is_file(): raise ValueError('Download and verify the installer first.')
        if not _digest_matches(dmg, expected): raise ValueError('Staged installer checksum changed. Download it again.')
        ok, message = _verify_signature(dmg, stage)
        if not ok: return {'ok':False, 'output':message}
        code, output = _run(['/usr/bin/open', str(dmg)], timeout=30)
        if code != 0: return {'ok':False, 'output':'Could not open the installer. ' + output[:500]}
        return {'ok':True, 'output':'Installer opened. Follow Docker’s native installation and terms prompts, then return to check readiness.'}
    except (ValueError, OSError) as error:
        return {'ok':False, 'output':str(error)[:500]}

def _env():
    env = os.environ.copy()
    for name in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_API_VERSION'):
        env.pop(name, None)
    return env

def _run(args, timeout=10):
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=_env())
    except OSError:
        return None, 'The check failed to start.'
    output = bytearray()
    overflow = [False]
    def read():
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            if len(output) + len(chunk) > 8192:
                overflow[0] = True
                proc.kill()
                break
            output.extend(chunk)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        reader.join(timeout=5)
        return None, 'The check timed out.'
    reader.join(timeout=5)
    if overflow[0] or reader.is_alive():
        return None, 'The operation produced too much output or did not close its output.'
    return proc.returncode, output.decode('utf-8', 'replace')

def _local_docker():
    docker = _find_docker()
    if not docker:
        return None, 'Docker command not found. Install and start Docker Desktop.'
    if platform.system() != 'Darwin':
        return None, 'Automatic image setup currently supports macOS only.'
    code, endpoint = _run([docker, 'context', 'inspect', CONTEXT, '--format', '{{.Endpoints.docker.Host}}'])
    expected = 'unix://' + str(Path.home() / '.docker/run/docker.sock')
    if code != 0 or endpoint.strip() != expected:
        return None, 'The Docker Desktop local socket could not be verified. No image download was started.'
    code, details = _run([docker, '--context', CONTEXT, 'info', '--format', '{{.OperatingSystem}}|{{.OSType}}'])
    if code != 0 or details.strip() != 'Docker Desktop|linux':
        return None, 'Start Docker Desktop and retry. Its local engine could not be verified.'
    return docker, 'Docker Desktop local engine detected.'

def status(state_dir=None):
    docker, message = _local_docker()
    image_ready = False
    if docker:
        code, _ = _run([docker, '--context', CONTEXT, 'image', 'inspect', '--format', '{{.Id}}', IMAGE])
        image_ready = code == 0
    usage = shutil.disk_usage(Path.home())
    try:
        installer_url, _ = _installer_spec()
    except ValueError:
        installer_url = None
    installer_ready = bool(state_dir and (Path(state_dir)/'installers'/'Docker.dmg').is_file())
    return {'device': platform.node() or 'This Mac', 'system': platform.system(),
            'architecture': platform.machine(), 'free_bytes': usage.free,
            'docker_ready': bool(docker), 'docker_message': message,
            'image': IMAGE, 'image_ready': image_ready,
            'image_integrity': 'Version tag only; a verified image digest has not been configured.',
            'installer_version':INSTALLER_VERSION, 'installer_url':installer_url,
            'installer_ready':installer_ready, 'installer_installed':_installed(),
            'installer_verification':'Pinned Docker release SHA-256 and Docker app signing identity; checked before opening.'}

def pull():
    docker, message = _local_docker()
    if not docker:
        return {'ok': False, 'output': message}
    code, output = _run([docker, '--context', CONTEXT, 'pull', '--quiet', IMAGE], timeout=600)
    if code is None:
        return {'ok': False, 'output': 'Download timed out or failed to start. Docker may continue background activity; check Docker Desktop.'}
    return {'ok': code == 0, 'output': output if output else ('Download finished.' if code == 0 else 'Download failed.')}
