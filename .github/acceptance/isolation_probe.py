# Runs INSIDE the session container as a cell. Reports what the sandbox allows.
import json, os, urllib.request
r = {}
try:
    open('/workspace/new-file.txt', 'w').write('x'); r['write_workspace'] = 'allowed'
except OSError as e:
    r['write_workspace'] = type(e).__name__
try:
    r['read_workspace'] = open('/workspace/hello.txt').read().strip()
except OSError as e:
    r['read_workspace'] = type(e).__name__
try:
    urllib.request.urlopen('http://example.com', timeout=5); r['network'] = 'allowed'
except Exception as e:
    r['network'] = type(e).__name__
r['uid'] = os.getuid()
r['docker_sock'] = os.path.exists('/var/run/docker.sock')
try:
    open('/tmp/probe.txt', 'w').write('x'); r['tmp_write'] = 'ok'
except OSError as e:
    r['tmp_write'] = type(e).__name__
try:
    open('/opt/mainaiger/kernel.py', 'a'); r['kernel_write'] = 'allowed'
except OSError as e:
    r['kernel_write'] = type(e).__name__
try:
    open('/etc/probe', 'w'); r['root_write'] = 'allowed'
except OSError as e:
    r['root_write'] = type(e).__name__
r['cpu_count'] = os.cpu_count()
for p in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
    try:
        r['memory_limit'] = open(p).read().strip(); break
    except OSError:
        pass
for p in ('/sys/fs/cgroup/cpu.max',):
    try:
        r['cpu_max'] = open(p).read().strip()
    except OSError:
        pass
for p in ('/sys/fs/cgroup/pids.max',):
    try:
        r['pids_max'] = open(p).read().strip()
    except OSError:
        pass
print(json.dumps(r))
