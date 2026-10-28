"""Runs one browser script on this machine. Standalone: stdlib + Playwright, no Lab imports.

The Lab starts this file in a subprocess (``local_lab/browser.py``) with one JSON job on stdin:
{script, params, out_dir, profile_dir, headed, budget_s}. The script is the fixed, validated step
list from the approved folder; this runner only interprets it. The last line on stdout is the
result as JSON. Rules enforced here, whatever the script says:

- the browser only *navigates* to hosts in the script's ``allow`` list (top-frame navigations to
  any other host are refused and recorded; pages may still load their own assets from anywhere);
- one time budget for the whole run; every wait has a timeout;
- captured text is cut at MAX_TEXT characters, at most MAX_PAGES pages, lists at most MAX_LIST;
- nothing is executed from the page: steps click, type, wait, scroll and read text and links.

Playwright keeps a persistent profile in ``profile_dir`` (cookies, logins), so a headed run in
which you log in by hand makes later headless runs work until the site logs you out.
"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

MAX_TEXT = 500_000
MAX_PAGES = 200
MAX_LIST = 500
MAX_EACH = 200
PLACEHOLDER = re.compile(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}')


class StepError(Exception):
    pass


def host_allowed(url, allow):
    host = (urlsplit(url).hostname or '').lower()
    if not host:
        return False
    return any(host == a or host.endswith('.' + a) for a in allow)


def slug(text):
    s = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return s[:60] or 'page'


def fill_in(text, values):
    def one(match):
        name = match.group(1)
        if name not in values:
            raise StepError('No value for {{%s}}' % name)
        value = values[name]
        return value if isinstance(value, str) else json.dumps(value)
    return PLACEHOLDER.sub(one, text)


class Runner:
    def __init__(self, job):
        self.script = job['script']
        self.allow = [a.lower() for a in self.script['allow']]
        self.values = dict(job.get('params') or {})
        self.out = Path(job['out_dir'])
        self.profile = Path(job['profile_dir'])
        self.headed = bool(job.get('headed')) or bool(self.script.get('headed'))
        self.budget = float(job.get('budget_s') or self.script.get('budget_s') or 300)
        self.deadline = time.time() + self.budget
        self.pages = []
        self.log = []
        self.blocked = []
        self.page = None

    # ---- helpers ------------------------------------------------------
    def left_ms(self, cap=None):
        left = max(0.0, self.deadline - time.time()) * 1000
        return int(min(left, cap * 1000) if cap else left)

    def check_budget(self):
        if time.time() > self.deadline:
            raise StepError('Time budget of %d s spent' % self.budget)

    def record(self, n, step, ok, started, note=''):
        self.log.append({'n': n, 'do': step['do'], 'ok': ok, 'ms': int((time.time() - started) * 1000), 'note': note[:300]})

    def _route(self, route, request):
        if request.is_navigation_request() and request.frame == self.page.main_frame and not host_allowed(request.url, self.allow):
            self.blocked.append(request.url[:300])
            route.abort()
        else:
            route.continue_()

    # ---- steps --------------------------------------------------------
    def do_goto(self, step):
        url = fill_in(step['url'], self.values)
        if urlsplit(url).scheme not in ('http', 'https') or not host_allowed(url, self.allow):
            raise StepError('Refused: %s is not in the allowed sites' % (urlsplit(url).hostname or url))
        self.page.goto(url, timeout=self.left_ms(step.get('timeout_s', 60)), wait_until='domcontentloaded')
        return url

    def do_wait(self, step):
        if step.get('selector'):
            self.page.wait_for_selector(fill_in(step['selector'], self.values), timeout=self.left_ms(step.get('timeout_s', 30)), state=step.get('state', 'visible'))
            return 'found ' + step['selector']
        seconds = float(step.get('seconds', 1))
        time.sleep(min(seconds, max(0.0, self.deadline - time.time())))
        return 'waited %.1f s' % seconds

    def do_click(self, step):
        self.page.click(fill_in(step['selector'], self.values), timeout=self.left_ms(step.get('timeout_s', 30)))
        return step['selector']

    def do_fill(self, step):
        self.page.fill(fill_in(step['selector'], self.values), fill_in(step['text'], self.values), timeout=self.left_ms(step.get('timeout_s', 30)))
        return step['selector']

    def do_press(self, step):
        if step.get('selector'):
            self.page.press(fill_in(step['selector'], self.values), step['key'], timeout=self.left_ms(step.get('timeout_s', 30)))
        else:
            self.page.keyboard.press(step['key'])
        return step['key']

    def do_scroll(self, step):
        times = int(step.get('times', 5))
        for _ in range(times):
            self.check_budget()
            self.page.mouse.wheel(0, 4000)
            time.sleep(float(step.get('pause_s', 0.5)))
        return '%d scroll(s)' % times

    def do_capture(self, step):
        if len(self.pages) >= MAX_PAGES:
            raise StepError('At most %d pages per run' % MAX_PAGES)
        if step.get('selector'):
            text = self.page.inner_text(fill_in(step['selector'], self.values), timeout=self.left_ms(step.get('timeout_s', 30)))
        else:
            text = self.page.evaluate('() => document.body ? document.body.innerText : ""')
        cut = len(text) > MAX_TEXT
        text = text[:MAX_TEXT]
        title = (self.page.title() or '').strip()[:200]
        name = fill_in(step['as'], self.values)
        n = len(self.pages) + 1
        rel = 'pages/%03d-%s.md' % (n, slug(title or name))
        when = time.strftime('%Y-%m-%dT%H:%M:%S')
        head = '---\ntitle: %s\nurl: %s\ncaptured: %s\nas: %s\n---\n\n' % (json.dumps(title or name, ensure_ascii=False), self.page.url, when, name)
        (self.out / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.out / rel).write_text(head + text + ('\n\n[cut at %d characters]\n' % MAX_TEXT if cut else '\n'), encoding='utf-8')
        self.pages.append({'n': n, 'as': name, 'title': title or name, 'url': self.page.url, 'chars': len(text), 'cut': cut, 'file': rel, 'captured': when})
        return '%s (%d chars)' % (rel, len(text))

    def do_links(self, step):
        hrefs = self.page.eval_on_selector_all(fill_in(step['selector'], self.values), 'els => els.map(e => e.href || "")')
        seen, out = set(), []
        for href in hrefs:
            if not href or href in seen or not host_allowed(href, self.allow):
                continue
            seen.add(href)
            out.append(href)
            if len(out) >= min(int(step.get('limit', 100)), MAX_LIST):
                break
        self.values[step['as']] = out
        return '%d link(s) → %s' % (len(out), step['as'])

    def do_each(self, step, n):
        items = self.values.get(step['over'])
        if not isinstance(items, list):
            raise StepError('"%s" is not a list' % step['over'])
        limit = min(int(step.get('limit', 50)), MAX_EACH)
        for index, item in enumerate(items[:limit]):
            self.values['item'] = item
            self.values['index'] = index + 1
            for sub in step['steps']:
                self.run_step(sub, '%s.%d' % (n, index + 1))
        return '%d item(s) of %s' % (min(len(items), limit), step['over'])

    def run_step(self, step, n):
        self.check_budget()
        started = time.time()
        try:
            if step['do'] == 'each':
                note = self.do_each(step, n)
            else:
                note = getattr(self, 'do_' + step['do'])(step)
        except StepError:
            self.record(n, step, False, started)
            raise
        except Exception as error:  # Playwright timeouts and the like: the step's own message
            message = str(error).split('\n')[0][:300]
            self.record(n, step, False, started, message)
            if step.get('optional'):
                return
            raise StepError('Step %s (%s) failed: %s' % (n, step['do'], message))
        self.record(n, step, True, started, note)

    # ---- the run --------------------------------------------------------
    def run(self):
        from playwright.sync_api import sync_playwright
        started = time.time()
        self.out.mkdir(parents=True, exist_ok=True)
        self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        error = None
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(str(self.profile), headless=not self.headed, viewport={'width': 1280, 'height': 900},
                                                             accept_downloads=False, ignore_https_errors=False)
            try:
                self.page = context.pages[0] if context.pages else context.new_page()
                context.route('**/*', self._route)
                if self.script.get('start'):
                    self.run_step({'do': 'goto', 'url': self.script['start']}, 0)
                for i, step in enumerate(self.script['steps'], 1):
                    self.run_step(step, str(i))
            except StepError as err:
                error = str(err)
            except Exception as err:  # the browser itself failed
                error = 'Browser error: ' + str(err).split('\n')[0][:300]
            finally:
                try:
                    context.close()
                except Exception:
                    pass
        shown = {k: (v if isinstance(v, (str, int, float, bool)) else '%d item(s)' % len(v) if isinstance(v, list) else str(type(v).__name__)) for k, v in self.values.items() if k not in ('item', 'index')}
        return {'ok': error is None, 'error': error, 'pages': self.pages, 'vars': shown, 'log': self.log[-400:], 'blocked': self.blocked[:50],
                'elapsed_ms': int((time.time() - started) * 1000), 'headed': self.headed}


def main():
    job = json.load(sys.stdin)
    try:
        result = Runner(job).run()
    except ImportError as error:
        result = {'ok': False, 'error': 'Playwright is not installed for %s: %s' % (sys.executable, error), 'pages': [], 'vars': {}, 'log': [], 'blocked': [], 'elapsed_ms': 0}
    sys.stdout.write('\n' + json.dumps(result, ensure_ascii=False) + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
