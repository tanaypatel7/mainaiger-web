"""Browser scripts: fixed, reviewed step lists a browser on this machine follows (M6 of docs/MEMORY_PLAN.md).

A script is a small JSON file in the approved Memory folder (``browser/scripts/<name>.json``):
the sites it may visit, the parameters it takes, and its steps (goto, wait, click, fill, press,
scroll, capture, links, each). ``validate_script`` is the whole grammar; nothing else runs. A run
starts ``browser_runner.py`` in a subprocess with the Lab's own Python (Playwright must be
installed for it — ``available`` says whether it is and prints the two commands if not), waits
within the script's time budget, and writes the captured pages under
``browser/captures/<name>-<stamp>/`` with a ``capture.json`` index. ``import_capture`` turns a
capture's pages into read-only chat notes (``chats/browser/``) so the review loop can mine them
like an exported chat.

Honest limits (also in the UI): sites change their pages, log-ins and bot checks break scripts,
and some sites' terms restrict automated access. This is for sites without an export and for your
own tools; the official export stays the path for ChatGPT and Claude. Fragile by nature.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import memory

NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')
PARAM = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,40}$')
HOST = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$')
STEPS = {
    'goto': {'need': ('url',), 'may': ('timeout_s',)},
    'wait': {'need': (), 'may': ('selector', 'seconds', 'timeout_s', 'state')},
    'click': {'need': ('selector',), 'may': ('timeout_s',)},
    'fill': {'need': ('selector', 'text'), 'may': ('timeout_s',)},
    'press': {'need': ('key',), 'may': ('selector', 'timeout_s')},
    'scroll': {'need': (), 'may': ('times', 'pause_s')},
    'capture': {'need': ('as',), 'may': ('selector', 'timeout_s')},
    'links': {'need': ('selector', 'as'), 'may': ('limit',)},
    'each': {'need': ('over', 'steps'), 'may': ('limit',)},
}
MAX_STEPS = 200
MAX_INNER = 50
MIN_BUDGET, MAX_BUDGET, DEFAULT_BUDGET = 10, 1800, 300
MAX_CAPTURE_NOTE = 2 * 1024 * 1024
SCRIPTS = 'browser/scripts'
CAPTURES = 'browser/captures'
PROFILE = 'browser-profile'
RUNNER = Path(__file__).resolve().parent / 'browser_runner.py'
_AVAILABLE = {}

TEMPLATES = {
    'page-capture': {'title': 'Capture one page', 'allow': ['example.com'], 'start': 'https://example.com/', 'params': [],
                     'steps': [{'do': 'wait', 'selector': 'body'}, {'do': 'capture', 'as': 'page'}]},
    'list-and-each': {'title': 'Open every link in a list and capture each page', 'allow': ['example.com'], 'start': 'https://example.com/list', 'params': [],
                      'budget_s': 600,
                      'steps': [{'do': 'wait', 'selector': 'a'}, {'do': 'links', 'selector': 'main a', 'as': 'items', 'limit': 50},
                                {'do': 'each', 'over': 'items', 'limit': 50, 'steps': [{'do': 'goto', 'url': '{{item}}'}, {'do': 'wait', 'selector': 'body'}, {'do': 'capture', 'as': 'item-{{index}}'}]}]},
    'login-by-hand': {'title': 'Log in by hand (headed), then capture', 'allow': ['example.com'], 'start': 'https://example.com/login', 'params': [], 'headed': True, 'budget_s': 600,
                      'steps': [{'do': 'wait', 'selector': '[data-logged-in], .account', 'timeout_s': 300}, {'do': 'capture', 'as': 'after-login'}]},
}


class BrowserError(ValueError):
    pass


# ---------------------------------------------------------------- scripts
def _root(state_dir):
    return memory._root(state_dir)


def _scripts_dir(state_dir):
    return _root(state_dir) / SCRIPTS


def _captures_dir(state_dir):
    return _root(state_dir) / CAPTURES


def _check_name(name):
    if not isinstance(name, str) or not NAME.match(name):
        raise BrowserError('A script name is lowercase letters, digits and dashes, like chatgpt-list')
    return name


def _str(value, what, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BrowserError('%s must be text up to %d characters' % (what, limit))
    return value


def _number(value, what, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise BrowserError('%s must be a number from %s to %s' % (what, low, high))
    return value


def _check_url(url, allow, what):
    _str(url, what, 2000)
    if '{{' in url:
        return url  # filled at run time; the runner checks the scheme and host then
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        raise BrowserError('%s must start with http:// or https://' % what)
    host = (parts.hostname or '').lower()
    if not host or not any(host == a or host.endswith('.' + a) for a in allow):
        raise BrowserError('%s points at %s, which is not in the allowed sites' % (what, host or url))
    return url


def _check_step(step, allow, i, inner=False):
    if not isinstance(step, dict) or step.get('do') not in STEPS:
        raise BrowserError('Step %s must be {do: %s, …}' % (i, '|'.join(STEPS)))
    spec = STEPS[step['do']]
    extra = set(step) - set(spec['need']) - set(spec['may']) - {'do', 'optional', 'note'}
    if extra:
        raise BrowserError('Step %s (%s) has unknown fields: %s' % (i, step['do'], ', '.join(sorted(extra))))
    for key in spec['need']:
        if key not in step:
            raise BrowserError('Step %s (%s) needs %s' % (i, step['do'], key))
    if 'selector' in step:
        _str(step['selector'], 'Step %s selector' % i, 500)
    if 'timeout_s' in step:
        _number(step['timeout_s'], 'Step %s timeout_s' % i, 1, 600)
    if 'optional' in step and not isinstance(step['optional'], bool):
        raise BrowserError('Step %s optional must be true or false' % i)
    if 'note' in step:
        _str(step['note'], 'Step %s note' % i, 300)
    kind = step['do']
    if kind == 'goto':
        _check_url(step['url'], allow, 'Step %s url' % i)
    elif kind == 'wait':
        if 'seconds' in step:
            _number(step['seconds'], 'Step %s seconds' % i, 0, 300)
        if 'state' in step and step['state'] not in ('visible', 'attached', 'hidden', 'detached'):
            raise BrowserError('Step %s state must be visible, attached, hidden or detached' % i)
        if 'selector' not in step and 'seconds' not in step:
            raise BrowserError('Step %s (wait) needs a selector or seconds' % i)
    elif kind == 'fill':
        _str(step['text'], 'Step %s text' % i, 4000)
    elif kind == 'press':
        _str(step['key'], 'Step %s key' % i, 40)
    elif kind == 'scroll':
        if 'times' in step:
            _number(step['times'], 'Step %s times' % i, 1, 100)
        if 'pause_s' in step:
            _number(step['pause_s'], 'Step %s pause_s' % i, 0, 30)
    elif kind == 'capture':
        _str(step['as'], 'Step %s as' % i, 80)
    elif kind == 'links':
        _str(step['as'], 'Step %s as' % i, 80)
        if not PARAM.match(step['as']):
            raise BrowserError('Step %s as must be a plain name (letters, digits, underscores)' % i)
        if 'limit' in step:
            _number(step['limit'], 'Step %s limit' % i, 1, 500)
    elif kind == 'each':
        if inner:
            raise BrowserError('Step %s: each cannot be nested' % i)
        _str(step['over'], 'Step %s over' % i, 80)
        if 'limit' in step:
            _number(step['limit'], 'Step %s limit' % i, 1, 200)
        if not isinstance(step['steps'], list) or not 1 <= len(step['steps']) <= MAX_INNER:
            raise BrowserError('Step %s (each) needs 1 to %d inner steps' % (i, MAX_INNER))
        for j, sub in enumerate(step['steps'], 1):
            _check_step(sub, allow, '%s.%d' % (i, j), inner=True)
    return step


def validate_script(script):
    """The whole grammar. Returns the normalised script or raises BrowserError with a plain reason."""
    if not isinstance(script, dict):
        raise BrowserError('A script is a JSON object')
    allowed_keys = {'title', 'allow', 'start', 'params', 'headed', 'budget_s', 'steps', 'notes'}
    extra = set(script) - allowed_keys
    if extra:
        raise BrowserError('Unknown script fields: %s' % ', '.join(sorted(extra)))
    out = {'title': _str(script.get('title', 'Untitled'), 'title', 120).strip()}
    allow = script.get('allow')
    if not isinstance(allow, list) or not 1 <= len(allow) <= 20:
        raise BrowserError('allow must list 1 to 20 sites, like ["example.com"]')
    out['allow'] = []
    for host in allow:
        if not isinstance(host, str) or not HOST.match(host.lower()) or len(host) > 253:
            raise BrowserError('allow entries are host names without http://, like example.com')
        out['allow'].append(host.lower())
    params = script.get('params', [])
    if not isinstance(params, list) or len(params) > 10 or any(not isinstance(p, str) or not PARAM.match(p) for p in params):
        raise BrowserError('params is a list of up to 10 plain names')
    if any(p in ('item', 'index') for p in params):
        raise BrowserError('item and index are set by each; choose other parameter names')
    out['params'] = list(dict.fromkeys(params))
    if 'start' in script and script['start'] not in (None, ''):
        out['start'] = _check_url(script['start'], out['allow'], 'start')
    out['headed'] = bool(script.get('headed', False))
    out['budget_s'] = _number(script.get('budget_s', DEFAULT_BUDGET), 'budget_s', MIN_BUDGET, MAX_BUDGET)
    steps = script.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise BrowserError('steps must list 1 to %d steps' % MAX_STEPS)
    out['steps'] = [_check_step(s, out['allow'], i) for i, s in enumerate(steps, 1)]
    if 'notes' in script:
        out['notes'] = _str(script['notes'], 'notes', 4000)
    return out


def _summary(name, script, path=None):
    return {'name': name, 'title': script['title'], 'allow': script['allow'], 'params': script['params'], 'steps': len(script['steps']),
            'captures': sum(1 for s in script['steps'] if s['do'] == 'capture') + sum(1 for s in script['steps'] if s['do'] == 'each' for t in s['steps'] if t['do'] == 'capture'),
            'headed': script['headed'], 'budget_s': script['budget_s'], 'start': script.get('start'),
            'updated': path.stat().st_mtime if path else None}


def listing(state_dir):
    folder = _scripts_dir(state_dir)
    rows = []
    if folder.is_dir():
        for path in sorted(folder.glob('*.json')):
            if path.is_symlink() or not NAME.match(path.stem):
                continue
            try:
                script = validate_script(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError) as error:
                rows.append({'name': path.stem, 'title': path.stem, 'error': str(error), 'allow': [], 'params': [], 'steps': 0, 'captures': 0, 'headed': False, 'budget_s': 0, 'start': None, 'updated': path.stat().st_mtime})
                continue
            rows.append(_summary(path.stem, script, path))
    return rows


def read_script(state_dir, name):
    path = _scripts_dir(state_dir) / (_check_name(name) + '.json')
    if not path.is_file() or path.is_symlink():
        raise BrowserError('No script named %s' % name)
    try:
        return validate_script(json.loads(path.read_text(encoding='utf-8')))
    except ValueError as error:
        raise BrowserError('Script %s is not valid: %s' % (name, error))


def save_script(state_dir, name, script):
    script = validate_script(script)
    folder = _scripts_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (_check_name(name) + '.json')
    if path.is_symlink():
        raise BrowserError('Refusing to write through a symlink')
    text = json.dumps(script, indent=1, ensure_ascii=False) + '\n'
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write(text)
    os.replace(tmp, path)
    return _summary(name, script, path)


def delete_script(state_dir, name):
    path = _scripts_dir(state_dir) / (_check_name(name) + '.json')
    if not path.is_file() or path.is_symlink():
        raise BrowserError('No script named %s' % name)
    path.unlink()
    return name


# ---------------------------------------------------------------- availability
def available(python=None, refresh=False):
    """Whether the Lab's Python can import Playwright and finds its Chromium; the install commands when not."""
    python = python or sys.executable
    cached = _AVAILABLE.get(python)
    if cached and not refresh and time.time() - cached['checked'] < 300:
        return cached
    probe = ('import json, os\n'
             'try:\n    import playwright\n    from playwright.sync_api import sync_playwright\nexcept Exception as e:\n    print(json.dumps({"playwright": False, "chromium": False, "detail": str(e)[:200]})); raise SystemExit\n'
             'with sync_playwright() as p:\n    path = p.chromium.executable_path\n'
             'print(json.dumps({"playwright": True, "chromium": bool(path) and os.path.exists(path), "version": getattr(playwright, "__version__", ""), "detail": path}))\n')
    out = {'python': python, 'playwright': False, 'chromium': False, 'version': '', 'detail': '', 'checked': time.time(),
           'install': ['%s -m pip install playwright' % python, '%s -m playwright install chromium' % python]}
    try:
        done = subprocess.run([python, '-c', probe], capture_output=True, text=True, timeout=60)
        if done.returncode == 0 and done.stdout.strip():
            out.update(json.loads(done.stdout.strip().splitlines()[-1]))
        else:
            out['detail'] = (done.stderr or done.stdout).strip()[-300:]
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        out['detail'] = str(error)[:300]
    out['ready'] = bool(out['playwright'] and out['chromium'])
    _AVAILABLE[python] = out
    return out


# ---------------------------------------------------------------- runs
def _check_params(script, params):
    params = params if isinstance(params, dict) else {}
    out = {}
    for name in script['params']:
        if name not in params:
            raise BrowserError('The script needs the parameter "%s" (set params = {%r: …} in an earlier cell, or fill it in on the Browser page)' % (name, name))
        value = params[name]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise BrowserError('Parameter "%s" must be text or a number' % name)
        out[name] = value if isinstance(value, str) else str(value)
        if len(out[name]) > 4000:
            raise BrowserError('Parameter "%s" is longer than 4000 characters' % name)
    return out


def run(state_dir, name, params=None, headed=None, python=None, journal=None):
    """Run one saved script to completion; returns the capture (also written as capture.json in its folder)."""
    script = read_script(state_dir, name)
    values = _check_params(script, params)
    headed = script['headed'] if headed is None else bool(headed)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    rel = '%s/%s-%s' % (CAPTURES, name, stamp)
    out_dir = _root(state_dir) / rel
    n = 2
    while out_dir.exists():
        rel = '%s/%s-%s-%d' % (CAPTURES, name, stamp, n)
        out_dir = _root(state_dir) / rel
        n += 1
    out_dir.mkdir(parents=True)
    profile = Path(state_dir).resolve(strict=True) / PROFILE
    job = {'script': script, 'params': values, 'out_dir': str(out_dir), 'profile_dir': str(profile), 'headed': headed, 'budget_s': script['budget_s']}
    python = python or sys.executable
    started = time.time()
    result = {'ok': False, 'error': None, 'pages': [], 'vars': {}, 'log': [], 'blocked': [], 'elapsed_ms': 0}
    try:
        done = subprocess.run([python, str(RUNNER)], input=json.dumps(job), capture_output=True, text=True, timeout=script['budget_s'] + 90)
        lines = [l for l in done.stdout.splitlines() if l.strip()]
        if lines:
            try:
                result.update(json.loads(lines[-1]))
            except ValueError:
                result['error'] = 'The runner did not report a result: %s' % (done.stderr.strip()[-300:] or lines[-1][:300])
        else:
            result['error'] = 'The runner stopped without a result: %s' % (done.stderr.strip()[-300:] or 'exit %d' % done.returncode)
    except subprocess.TimeoutExpired:
        result['error'] = 'The run did not finish within its budget of %d s and was stopped' % script['budget_s']
    except OSError as error:
        result['error'] = 'Could not start the runner: %s' % error
    result['elapsed_ms'] = result.get('elapsed_ms') or int((time.time() - started) * 1000)
    result.update({'script': name, 'title': script['title'], 'params': values, 'folder': rel, 'when': time.time(), 'headed': headed, 'allow': script['allow']})
    with open(out_dir / 'capture.json', 'w', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=1)
    if journal:
        journal.write('browser.run', 'Browser script %s: %s · %d page%s in %d ms%s' % (name, 'ok' if result['ok'] else 'failed', len(result['pages']), '' if len(result['pages']) == 1 else 's', result['elapsed_ms'], (' · ' + result['error']) if result['error'] else ''),
                      {'script': name, 'ok': result['ok'], 'pages': len(result['pages']), 'folder': rel, 'blocked': len(result['blocked']), 'error': result['error'], 'headed': headed})
    return result


def captures(state_dir, limit=50):
    folder = _captures_dir(state_dir)
    rows = []
    if folder.is_dir():
        for path in sorted(folder.iterdir(), reverse=True):
            index = path / 'capture.json'
            if path.is_symlink() or not path.is_dir() or not index.is_file():
                continue
            try:
                data = json.loads(index.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            rows.append({'folder': '%s/%s' % (CAPTURES, path.name), 'script': data.get('script'), 'title': data.get('title'), 'ok': data.get('ok'), 'error': data.get('error'),
                         'pages': len(data.get('pages') or []), 'when': data.get('when'), 'elapsed_ms': data.get('elapsed_ms'), 'imported': data.get('imported')})
            if len(rows) >= limit:
                break
    return rows


def read_capture(state_dir, rel):
    root = _root(state_dir)
    if not isinstance(rel, str) or not rel.startswith(CAPTURES + '/') or '..' in rel.split('/') or rel.count('/') != 2:
        raise BrowserError('Not a capture folder')
    folder = root / rel
    index = folder / 'capture.json'
    if folder.is_symlink() or not index.is_file():
        raise BrowserError('No capture at %s' % rel)
    try:
        data = json.loads(index.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise BrowserError('The capture index could not be read')
    data['folder'] = rel
    return data


def read_page(state_dir, rel, file):
    folder = _root(state_dir) / read_capture(state_dir, rel)['folder']
    if not isinstance(file, str) or not re.match(r'^pages/\d{3}-[a-z0-9-]+\.md$', file):
        raise BrowserError('Not a captured page')
    path = folder / file
    if path.is_symlink() or not path.is_file():
        raise BrowserError('No such page')
    front, body = memory.parse(path.read_text(encoding='utf-8', errors='replace'))
    return {'file': file, 'title': front.get('title'), 'url': front.get('url'), 'captured': front.get('captured'), 'text': body}


def import_capture(state_dir, rel):
    """Each captured page becomes a read-only chat note under chats/browser/ (same URL twice adds nothing)."""
    from . import memory_import
    data = read_capture(state_dir, rel)
    folder = _root(state_dir) / data['folder']
    known = memory_import._existing_ids(state_dir, 'browser')
    imported, skipped, errors = [], 0, []
    for page in data.get('pages') or []:
        if page.get('url') in known:
            skipped += 1
            continue
        try:
            got = read_page(state_dir, rel, page['file'])
        except BrowserError as error:
            errors.append('%s: %s' % (page.get('file'), error))
            continue
        body = 'Source: browser script %s · %s · captured %s\n\n%s' % (data.get('script'), got['url'], got['captured'], got['text'])
        if len(body.encode('utf-8')) > MAX_CAPTURE_NOTE:
            body = body.encode('utf-8')[:MAX_CAPTURE_NOTE].decode('utf-8', 'ignore') + '\n\n[truncated]\n'
        extra = {'source': 'browser', 'source_id': got['url'], 'messages': None, 'chat_created': got['captured'], 'script': data.get('script')}
        try:
            row = memory.create(state_dir, got['title'] or page.get('as') or 'Captured page', body, kind='chat', tier='chats', subfolder='browser', status_='stable',
                                sources=['browser: %s · %s' % (data.get('script'), rel)], extra=extra)
        except memory.MemoryError as error:
            errors.append('%s: %s' % (got['title'], error))
            continue
        known.add(got['url'])
        imported.append({'path': row['path'], 'title': row['title']})
    before = data.get('imported') if isinstance(data.get('imported'), dict) else {}
    data['imported'] = {'at': time.time(), 'notes': int(before.get('notes') or 0) + len(imported), 'skipped': skipped}
    with open(folder / 'capture.json', 'w', encoding='utf-8') as out:
        json.dump(data, out, ensure_ascii=False, indent=1)
    return {'folder': rel, 'imported': len(imported), 'skipped': skipped, 'errors': errors[:50], 'notes': imported[:200]}


def clear_profile(state_dir):
    """Forget the browser's cookies and logins (the persistent profile)."""
    import shutil
    profile = Path(state_dir).resolve(strict=True) / PROFILE
    if profile.is_symlink():
        raise BrowserError('The profile folder is a symlink; not touching it')
    existed = profile.is_dir()
    if existed:
        shutil.rmtree(profile)
    return existed
