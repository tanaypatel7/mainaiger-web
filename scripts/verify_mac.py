"""Verify the Lab on a real machine and write one honest report file.

Why this exists: everything in the Lab is tested in CI with fake models, a
fake export and local pages. The four things only a real Mac can answer are a
session inside Docker, a real Ollama model driving the self-improvement loop,
a real ChatGPT/Claude export through the import parsers, and Playwright for
the Lab's own Python. This script runs those checks through the Lab's own
modules (not copies of them), labels every result ok / fail / skip with the
reason, and writes `verify-mac-<stamp>.json` and `.md` next to the checkout
so the report can be uploaded and read without the machine.

What it never does: it never reads an owner key, never writes into a Lab's
state folder (it uses its own `--state-dir`), never writes to the checkout,
never installs anything, never opens a network listener, and the Docker
container it may start has the same limits as a Lab session (no network,
read-only workspace). The SSH probe is opt-in and read-only (`--ssh-host`).

Run from the repository root:

    python3 scripts/verify_mac.py                 # probes + test suites only
    python3 scripts/verify_mac.py --docker        # + a real Docker session
    python3 scripts/verify_mac.py --docker --model auto   # + the self-improvement loop with a real model
    python3 scripts/verify_mac.py --export ~/Downloads/chatgpt-export.zip
    python3 scripts/verify_mac.py --ssh-host nexus
"""
import argparse
import datetime
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from local_lab import graph as g                      # noqa: E402
from local_lab import models as lab_models            # noqa: E402
from local_lab import decide as lab_decide            # noqa: E402
from local_lab import network as lab_network          # noqa: E402
from local_lab import browser as lab_browser          # noqa: E402
from local_lab import memory_import as lab_import     # noqa: E402
from local_lab.graph_run import GraphRun              # noqa: E402
from local_lab.kernel_session import KernelSession    # noqa: E402
from local_lab.runner import KERNEL_DIR, IMAGE, resolve_docker, stage_kernel, session_command, session_control  # noqa: E402

HOME = str(Path.home())
EXAMPLES = ROOT / 'examples'
PREFERRED_MODELS = ('qwen2.5-coder', 'qwen3-coder', 'deepseek-coder', 'codellama', 'llama3', 'qwen')


def redact(value):
    """Replace the home folder in any string so a report never carries a user name; recurse into lists and dicts."""
    if isinstance(value, str):
        return value.replace(HOME, '~')
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def run_cmd(args, timeout=60, cwd=None, env=None):
    """Run one child process and return (code, combined output) without ever raising."""
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, cwd=cwd, env=env)
        return proc.returncode, proc.stdout.decode('utf-8', 'replace')
    except subprocess.TimeoutExpired as e:
        return None, 'timed out after %ss\n%s' % (timeout, (e.stdout or b'').decode('utf-8', 'replace'))
    except OSError as e:
        return None, str(e)


class Report:
    def __init__(self):
        self.sections = []
        self.started = time.time()

    def add(self, name, status, summary, **detail):
        assert status in ('ok', 'fail', 'skip')
        self.sections.append({'name': name, 'status': status, 'summary': summary, 'detail': redact(detail)})
        print('[%s] %s: %s' % (status.upper(), name, summary), flush=True)

    def run(self, name, fn):
        """Run one probe; an exception becomes a fail with its traceback, never a crash of the whole report."""
        print('--- %s' % name, flush=True)
        try:
            fn(self)
        except Exception:
            self.add(name, 'fail', 'the probe itself crashed', traceback=traceback.format_exc()[-3000:])

    def to_json(self, args):
        return {'generated': datetime.datetime.now().astimezone().isoformat(timespec='seconds'), 'elapsed_s': round(time.time() - self.started, 1),
                'args': redact({k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}),
                'summary': {s: sum(1 for x in self.sections if x['status'] == s) for s in ('ok', 'fail', 'skip')}, 'sections': self.sections}

    def to_markdown(self, args):
        data = self.to_json(args)
        lines = ['# MainAIger Lab · verification report', '', 'Generated %s · %ss · ok %d · fail %d · skip %d' % (data['generated'], data['elapsed_s'], data['summary']['ok'], data['summary']['fail'], data['summary']['skip']), '',
                 '| Probe | Result | Summary |', '|---|---|---|']
        for s in self.sections:
            lines.append('| %s | %s | %s |' % (s['name'], s['status'], s['summary'].replace('|', '/')))
        lines.append('')
        for s in self.sections:
            if s['detail']:
                lines += ['## %s (%s)' % (s['name'], s['status']), '', '```json', json.dumps(s['detail'], indent=1, ensure_ascii=False)[:6000], '```', '']
        return '\n'.join(lines)


# ---------------------------------------------------------------- probes
def probe_environment(r):
    code, head = run_cmd(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'])
    _, branch = run_cmd(['git', '-C', str(ROOT), 'branch', '--show-current'])
    _, dirty = run_cmd(['git', '-C', str(ROOT), 'status', '--porcelain'])
    r.add('environment', 'ok' if code == 0 else 'fail', '%s %s, Python %s, commit %s on %s%s' % (platform.system(), platform.machine(), platform.python_version(), head.strip() or '?', branch.strip() or '?', ', uncommitted changes' if dirty.strip() else ''),
          python=sys.executable, platform=platform.platform(), commit=head.strip(), branch=branch.strip(), dirty_files=dirty.strip().splitlines()[:20])


def probe_docker(r):
    docker, env, error = resolve_docker()
    if error:
        return r.add('docker', 'fail' if sys.platform == 'darwin' else 'skip', error)
    if sys.platform != 'darwin':  # on a Mac resolve_docker has already verified the Desktop engine
        code, out = run_cmd([docker, 'info', '--format', '{{.ServerVersion}}'], env=env, timeout=30)
        if code != 0:
            return r.add('docker', 'skip', 'the docker command exists but no engine answers here', output=out[-300:])
    code, out = run_cmd([docker, 'image', 'inspect', IMAGE, '--format', '{{.Id}}'], env=env, timeout=30)
    if code != 0:
        return r.add('docker', 'fail', 'Docker runs but the session image %s is not pulled (docker pull %s)' % (IMAGE, IMAGE), output=out[-500:])
    r.add('docker', 'ok', 'Docker Desktop local engine verified; %s present' % IMAGE, docker=docker, image_id=out.strip()[:30])


def probe_ollama(r):
    st = lab_models.status()
    names = [m['name'] for m in st['models']]
    if not st['running']:
        return r.add('ollama', 'fail' if st['installed'] else 'skip', 'Ollama is installed but not answering on 127.0.0.1:11434' if st['installed'] else 'Ollama is not installed (Models page → Get Ollama)', installed=st['installed'])
    r.add('ollama', 'ok' if names else 'fail', 'Ollama %s running with %d model(s): %s' % (st['version'], len(names), ', '.join(names) or 'none installed (Models page → Download)'),
          version=st['version'], models=[{'name': m['name'], 'gb': round((m['size_bytes'] or 0) / 1e9, 1)} for m in st['models']], ram_gb=round(st['device']['ram_bytes'] / 1e9, 1))


def pick_model(r, wanted):
    """`auto` picks the first installed coder-like model; a name must be installed."""
    names = [m['name'] for m in lab_models.installed_models()] if lab_models.running() else []
    if wanted != 'auto':
        return wanted if wanted in names else None
    for prefix in PREFERRED_MODELS:
        for n in names:
            if n.startswith(prefix):
                return n
    return names[0] if names else None


def probe_playwright(r):
    info = lab_browser.available(sys.executable, refresh=True)
    if info['ready']:
        return r.add('playwright', 'ok', 'Playwright %s with Chromium for %s' % (info['version'], info['python']), **{k: info[k] for k in ('version', 'detail')})
    r.add('playwright', 'skip', 'Playwright is not ready for this Python: ' + (info['detail'] or 'not installed'), install=info['install'])


def probe_tailscale(r):
    net = lab_network.tailnet()
    if not net['available']:
        return r.add('tailscale', 'skip', net['note'])
    peers = [{'name': p['name'], 'os': p.get('os'), 'online': p['online']} for p in net['peers']]  # names and state only, no addresses
    r.add('tailscale', 'ok' if net['logged_in'] else 'fail', net['note'] + ' %d peer(s), %d online.' % (len(peers), sum(1 for p in peers if p['online'])) if net['logged_in'] else net['note'], peers=peers, tailnet=net.get('tailnet'))


def probe_ssh(r, host):
    if not host:
        return r.add('nexus-ssh', 'skip', 'no --ssh-host given; the Devices pairing needs ssh to work non-interactively to that host')
    remote = 'echo python=$(python3 --version 2>&1); echo docker=$(command -v docker || echo missing); test -d ~/mainaiger-web && echo repo=$(git -C ~/mainaiger-web rev-parse --short HEAD 2>&1) branch=$(git -C ~/mainaiger-web branch --show-current 2>&1) || echo repo=missing; echo lab=$( (curl -s -m 3 http://127.0.0.1:8795/api/state >/dev/null && echo listening-on-8795) || echo not-listening-on-8795)'
    args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes', host, remote]
    print('  running: ' + ' '.join(args[:-1]) + ' <read-only probe>', flush=True)
    code, out = run_cmd(args, timeout=30)
    if code != 0:
        return r.add('nexus-ssh', 'fail', 'ssh %s did not work without a prompt (exit %s): the Lab\'s Connect uses exactly these options' % (host, code), output=out[-800:])
    facts = dict(line.split('=', 1) for line in out.strip().splitlines() if '=' in line)
    r.add('nexus-ssh', 'ok', 'ssh %s works non-interactively; %s' % (host, ', '.join('%s %s' % kv for kv in facts.items())), **facts)


def probe_tests(r, skip):
    if skip:
        return r.add('tests', 'skip', '--no-tests given')
    code, out = run_cmd([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'], cwd=str(ROOT), timeout=1800)
    m = re.search(r'Ran (\d+) tests? in ([\d.]+)s', out)
    py = 'Python: %s tests in %ss, %s' % (m.group(1), m.group(2), 'OK' if code == 0 else 'FAILED') if m else 'Python: no result (exit %s)' % code
    ncode, nout = run_cmd(['node', '--test'] + sorted(str(p) for p in (ROOT / 'tests').glob('*.test.js')), cwd=str(ROOT), timeout=600)
    passed = re.search(r'# pass (\d+)', nout); failed = re.search(r'# fail (\d+)', nout)
    node = 'Node: %s pass, %s fail' % (passed.group(1) if passed else '?', failed.group(1) if failed else '?') if ncode is not None else 'Node: could not run (%s)' % nout[:100]
    status = 'ok' if code == 0 and ncode == 0 else 'fail'
    r.add('tests', status, py + '; ' + node, python_tail=out[-4000:] if code != 0 else out[-300:], node_tail=nout[-2000:] if ncode != 0 else '', skipped=re.findall(r'skipped=(\d+)', out))


def docker_kernel(state_dir, workspace):
    """The same construction as server.docker_kernel, with this script's private state folder."""
    docker, env, error = resolve_docker()
    if error:
        raise RuntimeError(error)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    kernel_dir = stage_kernel(state_dir)
    name = 'mainaiger-lab-verify-' + secrets.token_hex(6)
    settings = {'workspace': workspace, 'cpus': '1', 'memory': '512m'}
    args = session_command(settings, name, kernel_dir); args[0] = docker
    control = session_control(name)
    for cmd in control.values():
        cmd[0] = docker
    return KernelSession(args, env=env, interrupt_args=control['interrupt'], kill_args=control['kill'], label=name), ' '.join(args)


def probe_docker_session(r, args):
    if not args.docker:
        return r.add('docker-session', 'skip', 'add --docker to start a real session container and run a cell in it')
    kernel, command = docker_kernel(args.state_dir, str(ROOT))
    started = time.time()
    try:
        kernel.start(timeout=60)
    except Exception as e:
        return r.add('docker-session', 'fail', 'the session container did not start: %s' % e, command=command)
    try:
        t0 = time.time()
        cell = kernel.execute("import os, sys\nprint('python', sys.version.split()[0])\nprint('workspace files', sorted(os.listdir('/workspace'))[:5])\ntry:\n    open('/workspace/verify-write-test', 'w')\n    print('WRITABLE')\nexcept OSError as e:\n    print('workspace read-only:', type(e).__name__)\nimport socket\ntry:\n    socket.create_connection(('127.0.0.1', 11434), timeout=2); print('NETWORK REACHED')\nexcept OSError as e:\n    print('no network:', type(e).__name__)\nx = 21 * 2\n")
        cond = kernel.evaluate('x == 42')
        slow = kernel.execute('import time\nfor i in range(10):\n    time.sleep(0.2)\nprint("slow cell done")\n', timeout=10)
        out = cell.get('output', '')
        problems = [p for p, bad in (('cell failed: %s' % cell.get('error'), cell.get('status') != 'ok'), ('workspace was writable', 'WRITABLE' in out), ('container reached the network', 'NETWORK REACHED' in out),
                                     ('evaluate did not answer true: %s' % cond, cond.get('truth') is not True), ('second cell: %s' % slow.get('status'), slow.get('status') != 'ok')) if bad]
        r.add('docker-session', 'fail' if problems else 'ok', ('; '.join(problems)) if problems else 'container started in %.1fs; cell ran in %d ms; workspace read-only; no network; evaluate works' % (t0 - started, cell.get('elapsed_ms') or 0),
              command=command, output=out[-1500:], evaluate=cond, started_s=round(t0 - started, 1))
    finally:
        kernel.stop()


def real_ask_model(log):
    """A model cell's question to the real Ollama, exactly as server.ask_model does it; every call is logged for the report."""
    def ask(cfg, prompt):
        t0 = time.time()
        if cfg.get('schema'):
            res = lab_decide.ask(cfg['model'], prompt, '', cfg['schema'], attempts=cfg.get('attempts', 2))
            out = {'ok': bool(res.get('ok')), 'value': res.get('decision'), 'raw': res.get('raw'), 'error': res.get('error'), 'elapsed_ms': res.get('elapsed_ms'), 'attempts': res.get('attempts'), 'model': res.get('model', cfg['model'])}
        else:
            res = lab_models.chat(cfg['model'], [{'role': 'user', 'content': prompt[:50000]}])
            out = {'ok': bool(res.get('ok')), 'value': res.get('reply'), 'raw': res.get('reply'), 'error': None if res.get('ok') else res.get('output'), 'elapsed_ms': res.get('elapsed_ms'), 'attempts': 1, 'model': res.get('model', cfg['model'])}
        log.append({'output': cfg.get('output'), 'ok': out['ok'], 'attempts': out.get('attempts'), 'seconds': round(time.time() - t0, 1), 'error': (out.get('error') or '')[:300],
                    'prompt_chars': len(prompt), 'answer': (json.dumps(out['value'])[:600] if out['ok'] else (out.get('raw') or '')[:600])})
        return out
    return ask


def run_example(r, args, name, model, label):
    doc = g.validate(json.loads((EXAMPLES / (name + '.ipymb')).read_text(encoding='utf-8')))
    for cell in doc['notebook']['cells']:          # point every model cell at the chosen installed model
        own = cell.get('metadata', {}).get('mainaiger', {})
        if 'model' in own and isinstance(own['model'], dict):
            own['model']['model'] = model
    if args.docker:
        kernel, _ = docker_kernel(args.state_dir, str(ROOT))
    else:
        kernel = KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')])
    kernel.start(timeout=60)
    calls = []
    try:
        run = GraphRun(doc, g.plan(doc, 'all'), kernel, on_result=lambda i, res: g.record_result(doc, i, res), ask_model=real_ask_model(calls)).start()
        outcome = run.wait(args.loop_timeout)
        snap = run.snapshot()
        statuses = {k: v['status'] for k, v in snap['nodes'].items()}
        loop = snap['edges'].get('e-judge-propose', {})
        report = snap['nodes'].get('report', {}).get('output', '')
        ok = outcome == 'finished' and all(s in ('ok', 'idle') for s in statuses.values())
        headline = next((ln.strip() for ln in report.splitlines() if re.search(r'[A-Za-z]', ln)), '')
        summary = '%s: run %s; model %s; %d model call(s), %d failed; loop fired %s×, %s; %s' % (
            name, outcome, model, len(calls), sum(1 for c in calls if not c['ok']), loop.get('fires'), loop.get('state'), ('Report: ' + headline) if headline else 'no report output')
        r.add(label, 'ok' if ok else 'fail', summary, outcome=outcome, error=snap.get('error'), statuses=statuses, steps=snap.get('steps'), loop=loop, calls=calls, report=report[-2500:],
              apply_output=snap['nodes'].get('apply', {}).get('output', '')[-1500:])
    finally:
        kernel.stop()


def probe_self_improve(r, args):
    if not args.model:
        return r.add('self-improve-loop', 'skip', 'add --model auto (or --model NAME) to run examples/self-improve.ipymb with a real Ollama model')
    model = pick_model(r, args.model)
    if not model:
        return r.add('self-improve-loop', 'fail', 'no usable model: Ollama not running or %s not installed' % args.model)
    run_example(r, args, 'self-improve', model, 'self-improve-loop')
    if args.repo_loop:
        run_example(r, args, 'self-improve-repo', model, 'self-improve-repo-loop')
    else:
        r.add('self-improve-repo-loop', 'skip', 'add --repo-loop to also run the repository loop (three unittest runs inside the container; several minutes)')


def probe_export(r, path):
    if not path:
        return r.add('memory-import', 'skip', 'add --export PATH to inspect a real ChatGPT or Claude export zip (nothing is imported; titles are not written to the report)')
    try:
        info = lab_import.inspect_zip(str(path))
    except Exception as e:
        return r.add('memory-import', 'fail', 'inspect failed on the real export: %s: %s' % (type(e).__name__, e), error=traceback.format_exc()[-2500:])
    status = 'ok' if info['conversations'] and not info['errors'] else 'fail'
    r.add('memory-import', status, 'detected %s: %d conversation(s), %d message(s), %d parse error(s), %d other file(s) in the zip, %s → %s' % (info['source'], info['conversations'], info['messages'], len(info['errors']), len(info['other_files']), info['first'][:10], info['last'][:10]),
          source=info['source'], conversations=info['conversations'], messages=info['messages'], errors=info['errors'], other_files_sample=info['other_files'][:10], bytes=info['bytes'])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--docker', action='store_true', help='start a real session container and run cells in it')
    p.add_argument('--model', default=None, help='auto, or an installed Ollama model name: run the self-improvement loop with it')
    p.add_argument('--repo-loop', action='store_true', help='also run examples/self-improve-repo.ipymb (needs --model; slow)')
    p.add_argument('--export', type=Path, default=None, help='a ChatGPT or Claude export zip to inspect with the real parsers')
    p.add_argument('--ssh-host', default=None, help='an ssh host alias (for example nexus) to probe read-only, the way Devices → Connect will')
    p.add_argument('--no-tests', action='store_true', help='skip the two test suites')
    p.add_argument('--loop-timeout', type=int, default=1200, help='seconds to allow a loop run with a real model')
    p.add_argument('--state-dir', type=Path, default=Path.home() / '.local/share/mainaiger-lab-verify', help='private folder for this script only (staged kernel); never a Lab\'s own state')
    p.add_argument('--out', type=Path, default=ROOT, help='folder for the report files')
    args = p.parse_args()

    r = Report()
    r.run('environment', probe_environment)
    r.run('docker', probe_docker)
    r.run('ollama', probe_ollama)
    r.run('playwright', probe_playwright)
    r.run('tailscale', probe_tailscale)
    r.run('nexus-ssh', lambda rep: probe_ssh(rep, args.ssh_host))
    r.run('tests', lambda rep: probe_tests(rep, args.no_tests))
    r.run('docker-session', lambda rep: probe_docker_session(rep, args))
    r.run('self-improve-loop', lambda rep: probe_self_improve(rep, args))
    r.run('memory-import', lambda rep: probe_export(rep, args.export))

    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    args.out.mkdir(parents=True, exist_ok=True)
    jpath, mpath = args.out / ('verify-mac-%s.json' % stamp), args.out / ('verify-mac-%s.md' % stamp)
    jpath.write_text(json.dumps(r.to_json(args), indent=1, ensure_ascii=False), encoding='utf-8')
    mpath.write_text(r.to_markdown(args), encoding='utf-8')
    data = r.to_json(args)
    print('\nDone: ok %d · fail %d · skip %d\nReport: %s\n        %s' % (data['summary']['ok'], data['summary']['fail'], data['summary']['skip'], jpath, mpath), flush=True)
    return 1 if data['summary']['fail'] else 0


if __name__ == '__main__':
    sys.exit(main())
