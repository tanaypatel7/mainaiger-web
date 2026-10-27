"""Build a fixed, limited Docker invocation; never execute Python on the host."""
from pathlib import Path
import os
import secrets
import shutil
import subprocess
import sys
import threading

IMAGE = 'python:3.13.18-slim'
CPUS = {'0.5', '1', '2'}
MEMORY = {'256m', '512m', '1g'}
MAX_OUTPUT = 65536
MAX_CODE = 20000
TIMEOUT = 20


def validate_settings(data, state_dir):
    if set(data) != {'workspace', 'cpus', 'memory'}:
        raise ValueError('Invalid settings')
    workspace, cpus, memory = data['workspace'], data['cpus'], data['memory']
    if not isinstance(workspace, str) or len(workspace) > 1024 or cpus not in CPUS or memory not in MEMORY:
        raise ValueError('Choose one of the supported resource limits')
    if not workspace:
        return {'workspace': None, 'cpus': cpus, 'memory': memory}
    if any(x in workspace for x in (',', '\n', '\r', '\x00')):
        raise ValueError('Folder path contains unsupported characters')
    path = Path(workspace).expanduser()
    if not path.is_absolute():
        raise ValueError('Folder path must be absolute')
    try:
        root = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError('Folder does not exist') from None
    if not root.is_dir() or root == Path('/'):
        raise ValueError('Choose a specific directory')
    state = Path(state_dir).resolve(strict=True)
    if root == state or root in state.parents or state in root.parents:
        raise ValueError('Folder must not overlap private Lab state')
    return {'workspace': str(root), 'cpus': cpus, 'memory': memory}


KERNEL_DIR = Path(__file__).resolve().parent / 'kernel'
KERNEL_MOUNT = '/opt/mainaiger'


def _docker_prefix():
    return ['docker'] + (['--context', 'desktop-linux'] if sys.platform == 'darwin' else [])


def _base(settings, name):
    args = _docker_prefix() + ['run', '--rm', '--name', name, '--pull=never', '--network=none',
            '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=64', '--cpus='+settings['cpus'],
            '--memory='+settings['memory'], '--memory-swap='+settings['memory'], '--user=65534:65534', '--interactive']
    if settings['workspace']:
        args += ['--mount', 'type=bind,src='+settings['workspace']+',dst=/workspace,readonly', '--workdir=/workspace']
    else:
        args += ['--workdir=/tmp']
    return args


def command(settings, name):
    """One-shot run: a fresh container reads the program from stdin and exits."""
    return _base(settings, name) + [IMAGE, 'python', '-I', '-']


def session_command(settings, name, kernel_dir=KERNEL_DIR):
    """Persistent session: the same container limits, but the process is the kernel.

    The kernel source directory is mounted read-only; the container cannot alter it.
    """
    return _base(settings, name) + ['--mount', 'type=bind,src='+str(kernel_dir)+',dst='+KERNEL_MOUNT+',readonly',
                                    IMAGE, 'python', '-I', KERNEL_MOUNT+'/kernel.py']


def session_control(name):
    """Commands the host uses to interrupt or remove a session container."""
    return {'interrupt': _docker_prefix() + ['kill', '--signal=INT', name],
            'kill': _docker_prefix() + ['rm', '-f', name]}


def resolve_docker(find=shutil.which):
    """Return (docker_executable, env, error). On a Mac only Docker Desktop's local engine is accepted."""
    if sys.platform == 'darwin':
        from .setup import _env, _local_docker
        docker, message = _local_docker()
        if not docker:
            return None, None, message
        return docker, _env(), None
    if not find('docker'):
        return None, None, 'Docker was not found. Install and start Docker Desktop first.'
    return 'docker', None, None


def preview(settings):
    return command(settings, '<generated-container-name>') + ['<Python source via stdin>']


def session_preview(settings):
    return session_command(settings, '<generated-container-name>')


def execute(code, settings, popen=subprocess.Popen, run=subprocess.run, find=shutil.which):
    if not isinstance(code, str) or not code.strip() or len(code.encode('utf-8')) > MAX_CODE:
        raise ValueError('Enter Python code up to 20 KB')
    env = None
    if sys.platform == 'darwin':
        from .setup import _env, _local_docker
        docker, message = _local_docker()
        if not docker:
            return {'output': '', 'exit_code': None, 'error': message}
        env = _env()
    elif not find('docker'):
        return {'output': '', 'exit_code': None, 'error': 'Docker was not found. Install and start Docker Desktop first.'}
    name = 'mainaiger-lab-'+secrets.token_hex(12)
    args = command(settings, name)
    if env is not None: args[0] = docker
    proc = popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, **({'env': env} if env is not None else {}))
    chunks, size, overflow = [], [0], [False]
    def read_output():
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk: break
            remaining = MAX_OUTPUT-size[0]
            chunks.append(chunk[:max(0, remaining)])
            size[0] += len(chunk)
            if size[0] > MAX_OUTPUT:
                overflow[0] = True
                proc.kill()
                break
    reader=threading.Thread(target=read_output, daemon=True)
    reader.start()
    def write_input():
        try:
            proc.stdin.write(code.encode('utf-8'))
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
    writer=threading.Thread(target=write_input, daemon=True)
    writer.start()
    timed_out=False
    try:
        try:
            proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out=True
            proc.kill()
            proc.wait(timeout=5)
    finally:
        # Killing a Docker client does not necessarily stop its container.
        try:
            cleanup = ([docker, '--context', 'desktop-linux'] if env is not None else ['docker']) + ['rm', '-f', name]
            run(cleanup, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False, **({'env': env} if env is not None else {}))
        except (OSError, subprocess.TimeoutExpired):
            pass
        reader.join(timeout=5)
        writer.join(timeout=5)
        if not reader.is_alive(): proc.stdout.close()
    output=b''.join(chunks).decode('utf-8','replace')
    if overflow[0] or timed_out:
        output+='\n[Output or time limit reached. Cleanup was attempted; check Docker for any container named mainaiger-lab-*.]'
    return {'output': output, 'exit_code': proc.returncode, 'error': None}
