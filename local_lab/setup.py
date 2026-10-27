"""Fixed, owner-approved checks for the local setup Mainbook."""
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from .runner import IMAGE

CONTEXT = 'desktop-linux'

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
    docker = shutil.which('docker')
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

def status():
    docker, message = _local_docker()
    image_ready = False
    if docker:
        code, _ = _run([docker, '--context', CONTEXT, 'image', 'inspect', '--format', '{{.Id}}', IMAGE])
        image_ready = code == 0
    usage = shutil.disk_usage(Path.home())
    return {'device': platform.node() or 'This Mac', 'system': platform.system(),
            'architecture': platform.machine(), 'free_bytes': usage.free,
            'docker_ready': bool(docker), 'docker_message': message,
            'image': IMAGE, 'image_ready': image_ready,
            'image_integrity': 'Version tag only; a verified image digest has not been configured.'}

def pull():
    docker, message = _local_docker()
    if not docker:
        return {'ok': False, 'output': message}
    code, output = _run([docker, '--context', CONTEXT, 'pull', '--quiet', IMAGE], timeout=600)
    if code is None:
        return {'ok': False, 'output': 'Download timed out or failed to start. Docker may continue background activity; check Docker Desktop.'}
    return {'ok': code == 0, 'output': output if output else ('Download finished.' if code == 0 else 'Download failed.')}
