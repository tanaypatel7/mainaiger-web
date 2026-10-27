"""Local language models through Ollama, guided from the Lab with explicit approval.

Ollama runs *natively* on the Mac (not in Docker) so it can use the Apple
silicon GPU through Metal; Docker Desktop cannot reach that GPU. Everything
here talks to Ollama only on loopback (127.0.0.1:11434) and never exposes it
to the network. The Lab never accepts Ollama's terms, never installs anything
silently, and never sends prompts anywhere but the local Ollama process.

Steps, each a fixed operation the owner approves in the GUI:
1. device() — RAM, chip, free disk: what a model must fit into.
2. download_installer() — pinned release, pinned SHA-256, Developer ID
   signature + Apple notarization verified before the installer opens.
3. open_installer() / open_app() — the owner drags Ollama to Applications and
   starts it; the Lab only opens windows.
4. catalog() — curated models with approximate sizes and the RAM each needs,
   marked against this device.
5. pull() — downloads a chosen model through Ollama's local API with progress.
6. chat() — one request/response through Ollama's local API; the Lab's own
   /api/models/chat endpoint wraps it for a future chat interface.
"""
import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import setup as lab_setup

OLLAMA_VERSION = '0.40.12'
OLLAMA_URL = 'https://github.com/ollama/ollama/releases/download/v%s/Ollama.dmg' % OLLAMA_VERSION
OLLAMA_SHA256 = '348d75eac8370e69cdb9e85ef3fda8b2d9f8e01a01443860fd60bd552d42a433'  # from the release's sha256sum.txt
OLLAMA_HOSTS = ['github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com']
# Signing identity observed on the official 0.40.12 release (both Apple silicon and Intel):
#   Developer ID Application: Infra Technologies, Inc (3MU9H2V9Y9), bundle id com.electron.ollama.
# Re-verify these with `codesign -dv --verbose=2 Ollama.app` whenever the version pin changes.
OLLAMA_TEAM_ID = '3MU9H2V9Y9'
OLLAMA_BUNDLE_ID = 'com.electron.ollama'
OLLAMA_REQUIREMENT = '=anchor apple generic and identifier "%s" and certificate leaf[subject.OU] = "%s"' % (OLLAMA_BUNDLE_ID, OLLAMA_TEAM_ID)
OLLAMA_HOST, OLLAMA_PORT = '127.0.0.1', 11434
MODEL_NAME = re.compile(r'^[a-z0-9][a-z0-9._/-]{0,80}(:[a-z0-9][a-z0-9._-]{0,40})?$')
GiB = 1024 ** 3

# Curated starting points. Sizes are the default (4-bit) tags on ollama.com, approximate;
# Ollama reports the exact size when a download starts. "min_ram" is a practical floor:
# the model plus working memory, with the operating system still having room.
CATALOG = [
    {'name': 'llama3.2:1b', 'family': 'Llama 3.2', 'params': '1B', 'size_gb': 1.3, 'min_ram_gb': 4, 'use': 'Very fast replies, simple tasks'},
    {'name': 'llama3.2:3b', 'family': 'Llama 3.2', 'params': '3B', 'size_gb': 2.0, 'min_ram_gb': 8, 'use': 'Good general chat on most laptops'},
    {'name': 'qwen2.5:3b', 'family': 'Qwen 2.5', 'params': '3B', 'size_gb': 1.9, 'min_ram_gb': 8, 'use': 'General chat, strong multilingual'},
    {'name': 'gemma3:4b', 'family': 'Gemma 3', 'params': '4B', 'size_gb': 3.3, 'min_ram_gb': 8, 'use': 'General chat, image input'},
    {'name': 'phi4-mini', 'family': 'Phi-4 mini', 'params': '3.8B', 'size_gb': 2.5, 'min_ram_gb': 8, 'use': 'Reasoning and math at small size'},
    {'name': 'qwen2.5-coder:7b', 'family': 'Qwen 2.5 Coder', 'params': '7B', 'size_gb': 4.7, 'min_ram_gb': 16, 'use': 'Writing and explaining code'},
    {'name': 'llama3.1:8b', 'family': 'Llama 3.1', 'params': '8B', 'size_gb': 4.9, 'min_ram_gb': 16, 'use': 'Capable general assistant'},
    {'name': 'qwen2.5:7b', 'family': 'Qwen 2.5', 'params': '7B', 'size_gb': 4.7, 'min_ram_gb': 16, 'use': 'Capable general assistant'},
    {'name': 'gemma3:12b', 'family': 'Gemma 3', 'params': '12B', 'size_gb': 8.1, 'min_ram_gb': 24, 'use': 'Higher quality, slower'},
    {'name': 'qwen2.5:14b', 'family': 'Qwen 2.5', 'params': '14B', 'size_gb': 9.0, 'min_ram_gb': 24, 'use': 'Higher quality, slower'},
    {'name': 'gemma3:27b', 'family': 'Gemma 3', 'params': '27B', 'size_gb': 17.0, 'min_ram_gb': 48, 'use': 'Near frontier quality, needs a big Mac'},
    {'name': 'llama3.3:70b', 'family': 'Llama 3.3', 'params': '70B', 'size_gb': 43.0, 'min_ram_gb': 96, 'use': 'Largest practical local model'},
]


# ---------------------------------------------------------------- device
def device():
    """What a model has to fit into. Unknown values stay unknown rather than guessed."""
    system = platform.system()
    ram = None
    chip = platform.processor() or platform.machine()
    if system == 'Darwin':
        code, out = lab_setup._run(['/usr/sbin/sysctl', '-n', 'hw.memsize'])
        if code == 0 and out.strip().isdigit():
            ram = int(out.strip())
        code, out = lab_setup._run(['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'])
        if code == 0 and out.strip():
            chip = out.strip()
    elif system == 'Linux':
        try:
            for line in Path('/proc/meminfo').read_text().splitlines():
                if line.startswith('MemTotal:'):
                    ram = int(line.split()[1]) * 1024
        except (OSError, ValueError):
            ram = None
    usage = shutil.disk_usage(Path.home())
    return {'system': system, 'architecture': platform.machine(), 'chip': chip, 'ram_bytes': ram,
            'free_bytes': usage.free,
            'gpu_note': ('Apple silicon: Ollama uses the GPU through Metal with the same unified memory as RAM.' if system == 'Darwin' and platform.machine() == 'arm64'
                         else 'GPU acceleration depends on the machine; Ollama reports it after it starts.')}


def catalog(ram_bytes, free_bytes=None, installed=()):
    """The curated models, each marked for this device: fits / tight / too large."""
    ram_gb = (ram_bytes or 0) / GiB
    free_gb = (free_bytes or 0) / GiB
    rows = []
    for item in CATALOG:
        row = dict(item)
        if not ram_bytes:
            row['fit'] = 'unknown'
        elif ram_gb >= item['min_ram_gb'] * 1.5:
            row['fit'] = 'fits'
        elif ram_gb * 1.05 >= item['min_ram_gb']:  # 7.8 GiB reported for an 8 GB machine still counts
            row['fit'] = 'tight'
        else:
            row['fit'] = 'too_large'
        row['disk_ok'] = None if free_bytes is None else free_gb >= item['size_gb'] + 2
        row['installed'] = item['name'] in set(installed)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- Ollama process
def _ollama_app():
    for p in (Path('/Applications/Ollama.app'), Path.home() / 'Applications/Ollama.app'):
        if p.exists():
            return p
    return None


def _connect(timeout=10):
    return http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=timeout)


def _request(method, path, body=None, timeout=10, stream=False):
    """One request to the local Ollama API. Returns (status, response) — response is parsed JSON unless stream=True."""
    conn = _connect(timeout)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers={'Content-Type': 'application/json'} if payload else {})
        response = conn.getresponse()
        if stream:
            return response.status, response, conn
        data = response.read(8 * 1024 * 1024)
        conn.close()
        try:
            return response.status, json.loads(data) if data else {}
        except ValueError:
            return response.status, {'raw': data[:500].decode('utf-8', 'replace')}
    except (OSError, http.client.HTTPException):
        conn.close()
        return None, None


def running():
    status, data = _request('GET', '/api/version', timeout=3)
    if status == 200 and isinstance(data, dict):
        return str(data.get('version', ''))[:40]
    return None


def installed_models():
    status, data = _request('GET', '/api/tags', timeout=10)
    if status != 200 or not isinstance(data, dict):
        return []
    rows = []
    for item in data.get('models', [])[:200]:
        if isinstance(item, dict) and isinstance(item.get('name'), str):
            rows.append({'name': item['name'][:120], 'size_bytes': item.get('size') if isinstance(item.get('size'), int) else None,
                         'modified': str(item.get('modified_at', ''))[:40]})
    return rows


def status(state_dir=None):
    version = running()
    models = installed_models() if version else []
    dev = device()
    staged = bool(state_dir and (Path(state_dir) / 'installers' / 'Ollama.dmg').is_file())
    return {'device': dev, 'installed': _ollama_app() is not None, 'app_path': str(_ollama_app()) if _ollama_app() else None,
            'running': bool(version), 'version': version, 'endpoint': 'http://%s:%d' % (OLLAMA_HOST, OLLAMA_PORT),
            'models': models, 'catalog': catalog(dev['ram_bytes'], dev['free_bytes'], [m['name'] for m in models]),
            'installer': {'version': OLLAMA_VERSION, 'url': OLLAMA_URL, 'sha256': OLLAMA_SHA256, 'staged': staged,
                          'verification': 'Pinned release SHA-256, pinned Developer ID Team ID %s for %s, and Apple notarization checked before opening.' % (OLLAMA_TEAM_ID, OLLAMA_BUNDLE_ID)},
            'chat_endpoint': '/api/models/chat (loopback, owner session only)'}


# ---------------------------------------------------------------- installer
def _verify_ollama(dmg, stage):
    """Mount read-only, require Ollama.app signed by a Developer ID naming Ollama and accepted by Gatekeeper."""
    tmp = Path(tempfile.mkdtemp(prefix='verify-ollama-', dir=stage))
    mount = tmp / 'volume'
    mount.mkdir()
    attached, verified, message = False, False, 'The Ollama installer could not be verified.'
    try:
        code, output = lab_setup._run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-noautoopen', '-mountpoint', str(mount), str(dmg)], timeout=120)
        if code != 0:
            message = 'Could not mount the Ollama installer for verification. ' + output[:300]
        else:
            attached = True
            app = mount / 'Ollama.app'
            if not app.is_dir() or app.is_symlink():
                message = 'The image did not contain Ollama.app.'
            else:
                # Same pattern as the Docker installer: a strict signature check against the pinned
                # bundle identifier and Team ID, then Gatekeeper's own assessment (notarization).
                code, output = lab_setup._run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', OLLAMA_REQUIREMENT, str(app)], timeout=180)
                if code != 0:
                    code2, details = lab_setup._run(['/usr/bin/codesign', '-dv', '--verbose=2', str(app)], timeout=60)
                    identity = ' | '.join(l.strip() for l in details.splitlines() if l.strip().startswith(('Authority=', 'TeamIdentifier=', 'Identifier=')))
                    message = ('Ollama.app signature or signing identity verification failed (expected Team ID %s, bundle %s). ' % (OLLAMA_TEAM_ID, OLLAMA_BUNDLE_ID)) + (identity or output[:300])[:400]
                else:
                    code, assess = lab_setup._run(['/usr/sbin/spctl', '--assess', '--type', 'execute', str(app)], timeout=120)
                    if code != 0:
                        message = 'Gatekeeper did not accept Ollama.app (notarization check failed). ' + assess[:300]
                    else:
                        verified, message = True, 'SHA-256, pinned Ollama signing identity (Team ID %s) and Apple notarization verified.' % OLLAMA_TEAM_ID
    finally:
        if attached:
            code, _ = lab_setup._run(['/usr/bin/hdiutil', 'detach', str(mount)], timeout=30)
            if code != 0:
                verified, message = False, 'The verification volume could not be detached. Eject it in Finder before retrying.'
        try:
            mount.rmdir(); tmp.rmdir()
        except OSError:
            pass
    return verified, message


def download_installer(state_dir, progress_callback=None):
    progress = progress_callback or (lambda event: None)
    if _ollama_app() is not None:
        return {'ok': False, 'output': 'Ollama.app is already present. Existing installations are not overwritten.'}
    if platform.system() != 'Darwin':
        return {'ok': False, 'output': 'The guided Ollama installer is for macOS; on Linux install Ollama from ollama.com and the Lab will detect it.'}
    try:
        stage = lab_setup._staging(state_dir)
        final = lab_setup.fetch_verified(OLLAMA_URL, OLLAMA_SHA256, stage, 'Ollama.dmg', progress, OLLAMA_HOSTS, 'Ollama')
        ok, message = _verify_ollama(final, stage)
        if not ok:
            try: final.unlink()
            except OSError: pass
            return {'ok': False, 'output': message}
        return {'ok': True, 'output': message + ' Ready to open the installer.'}
    except (ValueError, OSError, TimeoutError, http.client.HTTPException) as error:
        return {'ok': False, 'output': 'Ollama installer preparation failed: ' + str(error)[:400]}


def open_installer(state_dir):
    if _ollama_app() is not None:
        return {'ok': False, 'output': 'Ollama.app is already present.'}
    try:
        stage = lab_setup._staging(state_dir)
        dmg = stage / 'Ollama.dmg'
        if dmg.is_symlink() or not dmg.is_file():
            raise ValueError('Download and verify the Ollama installer first.')
        if not lab_setup._digest_matches(dmg, OLLAMA_SHA256):
            raise ValueError('Staged Ollama installer checksum changed. Download it again.')
        ok, message = _verify_ollama(dmg, stage)
        if not ok:
            return {'ok': False, 'output': message}
        code, output = lab_setup._run(['/usr/bin/open', str(dmg)], timeout=30)
        if code != 0:
            return {'ok': False, 'output': 'Could not open the installer. ' + output[:300]}
        return {'ok': True, 'output': 'Installer opened. Drag Ollama to Applications, then open it; the Lab keeps checking until it answers.'}
    except (ValueError, OSError) as error:
        return {'ok': False, 'output': str(error)[:400]}


def open_app():
    app = _ollama_app()
    if app is None:
        return {'ok': False, 'output': 'Ollama is not installed yet.'}
    if platform.system() != 'Darwin':
        return {'ok': False, 'output': 'Opening Ollama automatically is supported on macOS only.'}
    code, output = lab_setup._run(['/usr/bin/open', '-a', str(app)], timeout=30)
    if code != 0:
        return {'ok': False, 'output': 'Ollama could not be opened. ' + output[:300]}
    return {'ok': True, 'output': 'Ollama is starting; it serves models on 127.0.0.1:11434 only.'}


# ---------------------------------------------------------------- models
def pull(model, progress_callback=None):
    """Download a model through Ollama's local API, reporting bytes as Ollama streams them."""
    progress = progress_callback or (lambda event: None)
    if not isinstance(model, str) or not MODEL_NAME.match(model):
        return {'ok': False, 'output': 'Choose a model name like llama3.2:3b.'}
    if not running():
        return {'ok': False, 'output': 'Ollama is not running. Open it first.'}
    result = _request('POST', '/api/pull', {'model': model, 'stream': True}, timeout=60, stream=True)
    if result[0] is None:
        return {'ok': False, 'output': 'Ollama did not accept the download request.'}
    status_code, response, conn = result
    try:
        if status_code != 200:
            return {'ok': False, 'output': 'Ollama refused the download (HTTP %s).' % status_code}
        last, deadline = 'starting', time.monotonic() + 4 * 3600
        total = completed = None
        while True:
            if time.monotonic() > deadline:
                return {'ok': False, 'output': 'The model download took longer than four hours and was abandoned.'}
            line = response.readline(65536)
            if not line:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if 'error' in event:
                return {'ok': False, 'output': 'Ollama reported: ' + str(event['error'])[:300]}
            last = str(event.get('status', last))[:120]
            if isinstance(event.get('total'), int): total = event['total']
            if isinstance(event.get('completed'), int): completed = event['completed']
            progress({'phase': 'downloading' if 'pulling' in last else last, 'bytes': completed or 0, 'total': total, 'detail': last})
        if last.startswith('success'):
            return {'ok': True, 'output': 'Model %s is ready.' % model}
        return {'ok': False, 'output': 'Download ended with status: ' + last}
    finally:
        try: conn.close()
        except OSError: pass


def chat(model, messages, timeout=300):
    """One chat completion from the local Ollama. Returns {'ok', 'reply', 'model', 'timing'} or an error."""
    if not isinstance(model, str) or not MODEL_NAME.match(model):
        return {'ok': False, 'output': 'Choose an installed model.'}
    if not isinstance(messages, list) or not 0 < len(messages) <= 100:
        return {'ok': False, 'output': 'Send between 1 and 100 messages.'}
    clean = []
    for item in messages:
        if not isinstance(item, dict) or item.get('role') not in ('system', 'user', 'assistant') or not isinstance(item.get('content'), str) or len(item['content']) > 50000:
            return {'ok': False, 'output': 'Each message needs a role (system/user/assistant) and text under 50,000 characters.'}
        clean.append({'role': item['role'], 'content': item['content']})
    names = {m['name'] for m in installed_models()}
    if model not in names and model.split(':')[0] + ':latest' not in names:
        return {'ok': False, 'output': 'Model %s is not downloaded yet.' % model}
    started = time.monotonic()
    code, data = _request('POST', '/api/chat', {'model': model, 'messages': clean, 'stream': False}, timeout=timeout)
    if code != 200 or not isinstance(data, dict) or not isinstance(data.get('message'), dict):
        return {'ok': False, 'output': 'Ollama did not answer (HTTP %s). %s' % (code, str((data or {}).get('error', ''))[:300])}
    return {'ok': True, 'model': str(data.get('model', model))[:120], 'reply': str(data['message'].get('content', ''))[:200000],
            'elapsed_ms': int((time.monotonic() - started) * 1000),
            'tokens': {'prompt': data.get('prompt_eval_count'), 'reply': data.get('eval_count')}}
