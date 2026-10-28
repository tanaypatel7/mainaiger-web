"""Optional, owner-operated browser acquisition of rendered chat history.

The browser has its own private profile. The owner signs in manually; this code
never reads passwords, cookies, local storage or browser credentials. Exports
are requested only through visible controls. Fallback capture is intentionally
limited to selected, discovered chats and rendered text, not a complete export.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import stat
import shutil
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

PROVIDERS = {'chatgpt': ('https://chatgpt.com', '/c/'), 'claude': ('https://claude.ai', '/chat/')}
MAX_CHATS = 20
MAX_DISCOVERED = 500
MAX_TEXT = 4 * 1024 * 1024
MAX_MESSAGES = 1000
MAX_FILES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 30 * 1024 * 1024
CAPTURE_WARNING = ('Browser capture contains only currently rendered text. Hidden branches, unloaded history, '
                   'attachments, formatting and other export metadata may be missing. Review before keeping it.')


def _chat_url(provider, url):
    if provider not in PROVIDERS or not isinstance(url, str) or len(url) > 2048:
        raise ValueError('Choose a discovered chat from the selected provider')
    base, prefix = PROVIDERS[provider]
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != urlsplit(base).netloc or parsed.username or parsed.password:
            raise ValueError('Chat URLs must use the selected provider’s official HTTPS origin')
        if not re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_-]{1,160}/?', parsed.path):
            raise ValueError('This is not a supported chat URL')
        if parsed.query or parsed.fragment:
            raise ValueError('Chat URLs with query strings or fragments are not supported')
        return urlunsplit(('https', parsed.netloc, parsed.path.rstrip('/'), '', ''))
    except (TypeError, ValueError) as error:
        raise ValueError('Choose a supported chat URL on the selected provider') from error


def _official(provider, url):
    try:
        parsed = urlsplit(url)
        return parsed.scheme == 'https' and parsed.netloc == urlsplit(PROVIDERS[provider][0]).netloc
    except (KeyError, TypeError, ValueError):
        return False


def _private_directory(path):
    if path.is_symlink():
        raise ValueError('Browser profile directories cannot be symlinks')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Browser profile directories must be private and owned by this account')
    return path


class BrowserImportManager:
    def __init__(self, state_dir, *, _playwright_factory=None, on_capture=None):
        self.root = _private_directory(Path(state_dir)).resolve()
        self._factory = _playwright_factory
        self._on_capture = on_capture
        try:
            self.available = _playwright_factory is not None or importlib.util.find_spec('playwright.sync_api') is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            self.available = False
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._pw = self._context = self._page = self._cdp = None
        self._downloads = {}
        self._expected_attachment = None
        self._download_limit = MAX_FILE_BYTES
        self._state = {'available': self.available, 'running': False, 'busy': False, 'provider': None,
                       'chats': [], 'warnings': [], 'error': None, 'result': None, 'job': None}
        if not self.available:
            self._state['error'] = 'Browser capture needs optional Playwright and Chromium. Use ZIP/JSON import, or install playwright and run python -m playwright install chromium.'

    def status(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def _submit(self, action, payload=None):
        with self._lock:
            if not self.available:
                raise ValueError(self._state['error'])
            if self._stop.is_set():
                raise ValueError('The browser manager is shutting down; restart the Lab to reconnect')
            if self._state['busy']:
                raise ValueError('A browser job is already running; wait for it to finish')
            if action != 'open' and not self._state['running']:
                raise ValueError('Open a provider browser and sign in first')
            self._state.update(busy=True, error=None, warnings=[], result=None,
                               job={'id': str(time.time_ns()), 'action': action, 'state': 'running', 'completed': 0, 'total': 0})
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name='mainaiger-browser-import', daemon=True)
                self._thread.start()
            self._queue.put((action, payload))
            return self.status()

    def open(self, provider):
        if provider not in PROVIDERS:
            raise ValueError('Choose ChatGPT or Claude')
        return self._submit('open', provider)

    def scan(self):
        return self._submit('scan')

    def request_export(self):
        return self._submit('export')

    def capture(self, urls, capture_files=False):
        if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_CHATS or not isinstance(capture_files, bool):
            raise ValueError('Select between 1 and 20 discovered chats')
        with self._lock:
            provider = self._state['provider']
            known = {chat['url'] for chat in self._state['chats']}
            chosen = list(dict.fromkeys(_chat_url(provider, url) for url in urls))
            if any(url not in known for url in chosen):
                raise ValueError('Scan visible chats first, then select discovered chat links')
        return self._submit('capture', {'urls': chosen, 'capture_files': capture_files})

    def close_browser(self):
        """Close the dedicated browser while keeping the manager reusable."""
        return self._submit('close')

    def close(self):
        """Signal shutdown; browser APIs are always called by their owner thread."""
        self._stop.set()
        self._queue.put(None)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def _release(self):
        self._cdp = None
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        self._context = self._page = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None
        self._update(running=False)

    def _worker(self):
        try:
            while not self._stop.is_set():
                item = self._queue.get()
                if item is None:
                    break
                action, payload = item
                try:
                    result = getattr(self, '_do_' + action)(payload)
                    if action == 'capture' and self._on_capture is not None:
                        self._on_capture(result, self._state['job']['id'])
                    with self._lock:
                        self._state['job']['state'] = 'complete'
                        self._state['result'] = result
                except Exception as error:
                    # Browser errors can contain page text, navigation URLs or tokens.
                    # Only our bounded ValueErrors are exposed; never dump tracebacks.
                    message = str(error) if isinstance(error, ValueError) else 'The browser operation failed. Check the browser window and sign-in, or use ZIP/JSON import. Chromium must be installed for Playwright.'
                    with self._lock:
                        self._state['error'] = message[:500]
                        self._state['job']['state'] = 'failed'
                finally:
                    self._update(busy=False)
        finally:
            self._release()
            self._update(busy=False)

    def _do_open(self, provider):
        self._release()
        if self._factory is None:
            from playwright.sync_api import sync_playwright
            factory = sync_playwright
        else:
            factory = self._factory
        directory = _private_directory(self.root / 'browser-import')
        profile = _private_directory(directory / provider)
        self._pw = factory().start()
        self._context = self._pw.chromium.launch_persistent_context(str(profile), headless=False, accept_downloads=True)
        self._context.set_default_timeout(3000)
        self._context.set_default_navigation_timeout(15000)
        try:
            self._cdp = self._context.browser.new_browser_cdp_session()
            self._cdp.send('Browser.setDownloadBehavior', {'behavior': 'deny', 'eventsEnabled': True})
            self._cdp.on('Browser.downloadWillBegin', self._download_begin)
            self._cdp.on('Browser.downloadProgress', self._download_progress)
        except Exception:
            self._cdp = None
            # If bounded browser download control is unavailable, refuse all
            # downloads rather than silently removing the promised size guard.
            self._context.on('page', lambda page: page.on('download', lambda download: download.cancel()))
            for page in self._context.pages:
                page.on('download', lambda download: download.cancel())
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(PROVIDERS[provider][0], wait_until='domcontentloaded', timeout=15000)
        self._update(running=True, provider=provider, chats=[])
        return {'message': 'Browser opened. Sign in manually in that window, then request an export or scan the visible chat list. The dedicated profile retains your browser session until you clear it.'}

    def _active_page(self):
        if self._context is None:
            raise ValueError('Open the browser first')
        provider = self._state['provider']
        pages = self._context.pages
        # Prefer the active original tab; a manual login can open a new provider tab.
        candidates = ([self._page] if self._page in pages else []) + list(reversed(pages))
        for page in candidates:
            if not page.is_closed() and _official(provider, page.url):
                self._page = page
                return page
        raise ValueError('Navigate the dedicated browser to the selected provider and finish signing in first')

    def _do_scan(self, _):
        page = self._active_page()
        # DOM-backed links only: no private APIs, hidden application state or URL guesses.
        anchors = page.locator('a[href]').evaluate_all("els => els.filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden').map(e => ({url: e.href, title: (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0,300)})).slice(0,2000)")
        chats = {}
        for item in anchors:
            try:
                url = _chat_url(self._state['provider'], item.get('url'))
            except (AttributeError, ValueError):
                continue
            if url not in chats and len(chats) < MAX_DISCOVERED:
                chats[url] = {'url': url, 'title': item.get('title') or 'Untitled visible chat'}
        result = list(chats.values())
        warnings = ['Only currently visible chat links were discovered. Scroll or open more history in the browser and scan again.']
        # A rescan adds visible items rather than losing previously discovered links.
        with self._lock:
            merged = {c['url']: c for c in self._state['chats']}
            merged.update(chats)
            self._state['chats'] = list(merged.values())[:MAX_DISCOVERED]
        self._update(warnings=warnings)
        return {'found': len(result), 'message': 'Select the chats to capture. Prefer the provider export for a complete archive.'}

    def _visible_click(self, page, label):
        # Exact accessible names only. No coordinate guesses or broad text matches.
        button = page.get_by_role('button', name=re.compile(label, re.I))
        for index in range(min(button.count(), 20)):
            candidate = button.nth(index)
            if candidate.is_visible() and candidate.is_enabled():
                candidate.click(timeout=3000)
                return True
        return False

    def _do_export(self, _):
        page = self._active_page()
        steps = []
        # UI layouts vary. Advance only when the semantic control is actually
        # visible, and stop rather than guessing when it is not.
        for label, name in ((r'^Settings$', 'Settings'),
                            (r'^(Data controls|Privacy|Account)$', 'Data/privacy controls'),
                            (r'^(Export data|Export all data|Export)$', 'Export data'),
                            (r'^(Confirm export|Request export)$', 'Confirm export')):
            if not _official(self._state['provider'], page.url):
                raise ValueError('Export navigation left the selected provider; finish manually')
            if self._visible_click(page, label):
                steps.append(name)
        requested = 'Confirm export' in steps
        message = ('The visible export confirmation was clicked. Check the provider for confirmation and email/download delivery; MainAIger has not received an archive.' if requested else
                   'Complete the provider’s export flow in the browser: Settings → Data controls / Privacy → Export data. When the archive arrives, use Import ZIP / JSON. The export request could not be verified automatically.')
        self._update(warnings=['Provider export may arrive by email. This browser does not read email or automatically download export files.'])
        return {'requested': requested, 'verified_delivery': False, 'steps': steps, 'message': message}

    def _messages(self, page, provider):
        if provider == 'chatgpt':
            raw = page.locator('[data-message-author-role]').evaluate_all("els => els.filter(e => e.getClientRects().length).map(e => ({role:e.getAttribute('data-message-author-role'),text:e.innerText || ''}))")
        else:
            raw = page.locator('[data-testid="user-message"], .font-user-message, .font-claude-message').evaluate_all("els => els.filter(e => e.getClientRects().length && !els.some(p => p !== e && p.contains(e))).map(e => ({role:e.matches('.font-claude-message') ? 'assistant' : 'user',text:e.innerText || ''}))")
        fallback = False
        if not raw:
            main = page.locator('main').first
            if not main.count():
                raise ValueError('No rendered conversation content was found; use the provider export')
            raw = [{'role': 'unknown', 'text': main.inner_text(timeout=3000)}]
            fallback = True
        if len(raw) > MAX_MESSAGES:
            raise ValueError('A rendered chat exceeds 1,000 messages; use the provider export')
        messages = []
        for index, row in enumerate(raw):
            text = row.get('text')
            if not isinstance(text, str) or not text.strip():
                continue
            if len(text.encode('utf-8')) > 2 * 1024 * 1024:
                raise ValueError('A rendered message exceeds 2 MiB; use the provider export')
            role = row.get('role') if row.get('role') in ('user', 'assistant', 'system', 'tool') else 'unknown'
            messages.append({'source_id': 'rendered-%d-%s' % (index, hashlib.sha256(text.encode()).hexdigest()[:16]),
                             'role': role, 'text': text, 'parent_id': None})
        if not messages:
            raise ValueError('No rendered conversation text was found; wait for the chat to load and retry')
        return messages, fallback

    def _cancel_download(self, guid):
        try:
            self._cdp.send('Browser.cancelDownload', {'guid': guid})
        except Exception:
            pass

    def _download_begin(self, event):
        guid = event.get('guid', '')
        allowed = event.get('url') == self._expected_attachment and bool(self._expected_attachment)
        self._downloads[guid] = {'state': 'inProgress', 'blocked': not allowed}
        if not allowed:
            self._cancel_download(guid)

    def _download_progress(self, event):
        guid = event.get('guid', '')
        row = self._downloads.setdefault(guid, {'blocked': True})
        row.update(event)
        # CDP progress cancellation is best effort: network bytes can overshoot
        # a callback. Completed files are independently size checked below.
        if row['blocked'] or event.get('receivedBytes', 0) > self._download_limit or event.get('totalBytes', 0) > self._download_limit:
            row['blocked'] = True
            self._cancel_download(guid)

    def _attachment(self, page, locator, observed, source_url, remaining):
        url = observed.get('url', '')
        provider = self._state['provider']
        parsed = urlsplit(url)
        if not _official(provider, url) or not re.search(r'/(files|attachments)/', parsed.path):
            raise ValueError('The observed link is not a recognized same-provider file endpoint; download it manually')
        if self._cdp is None:
            raise ValueError('This browser does not expose bounded download control; download this file manually')
        if remaining <= 0:
            raise ValueError('The 30 MiB total file budget has been reached')
        parent = _private_directory(self.root / 'browser-import-files')
        ident = str(uuid.uuid4())
        stage = _private_directory(parent / ident)
        self._downloads = {}
        self._expected_attachment = url
        self._download_limit = min(MAX_FILE_BYTES, remaining)
        download = None
        keep = False
        try:
            self._cdp.send('Browser.setDownloadBehavior', {'behavior': 'allowAndName', 'downloadPath': str(stage), 'eventsEnabled': True})
            with page.expect_download(timeout=5000) as event:
                locator.nth(observed['index']).click(timeout=3000)
            download = event.value
            if download.url != url:
                raise ValueError('The attachment redirected to an unapproved download URL; download it manually')
            deadline = time.monotonic() + 30
            completed = None
            while time.monotonic() < deadline and not self._stop.is_set():
                for guid, progress in list(self._downloads.items()):
                    if progress.get('blocked') or progress.get('state') == 'canceled':
                        raise ValueError('The download was canceled or exceeded its 10 MiB limit')
                    if progress.get('state') == 'completed':
                        completed = guid
                        break
                if completed:
                    break
                page.wait_for_timeout(100)
            if not completed:
                raise ValueError('Attachment download did not finish within 30 seconds')
            if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', completed):
                raise ValueError('The browser returned an invalid download identifier')
            path = stage / completed
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > min(MAX_FILE_BYTES, remaining):
                raise ValueError('The completed file exceeds the size budget or is not a regular file')
            contents = path.read_bytes()
            if len(contents) > min(MAX_FILE_BYTES, remaining):
                raise ValueError('The completed file exceeds the size budget')
            final = stage / 'attachment.bin'
            path.rename(final)
            final.chmod(0o600)
            keep = True
            name = observed.get('name') or download.suggested_filename or 'Attachment'
            return {'id': ident, 'name': str(name)[:200], 'path': str(final), 'sha256': hashlib.sha256(contents).hexdigest(),
                    'size': len(contents), 'source_url': source_url, 'downloaded': True}
        finally:
            self._expected_attachment = None
            try:
                self._cdp.send('Browser.setDownloadBehavior', {'behavior': 'deny', 'eventsEnabled': True})
            except Exception:
                pass
            if download is not None and not keep:
                try:
                    download.cancel()
                except Exception:
                    pass
            if not keep:
                shutil.rmtree(stage, ignore_errors=True)

    def _do_capture(self, payload):
        page = self._active_page()
        provider = self._state['provider']
        conversations, skipped, attachments, files = [], [], [], []
        warnings = [CAPTURE_WARNING]
        if payload['capture_files']:
            warnings.append('Attachment capture uses only recognized visible same-provider file links: at most 10 files, 10 MiB each and 30 MiB saved total. Download cancellation is best effort; hidden files and redirected links may need manual export.')
        total_text = file_attempts = 0
        with self._lock:
            self._state['job']['total'] = len(payload['urls'])
            titles = {c['url']: c['title'] for c in self._state['chats']}
        for position, url in enumerate(payload['urls']):
            if self._stop.is_set():
                break
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=15000)
                if _chat_url(provider, page.url) != url:
                    raise ValueError('The selected chat redirected; finish signing in or use the export')
                # A SPA may mount after DOMContentLoaded. Wait for one known content
                # surface, without interacting with login/CAPTCHA or access controls.
                page.locator('main, [data-message-author-role], [data-testid="user-message"], .font-claude-message').first.wait_for(state='visible', timeout=10000)
                messages, fallback = self._messages(page, provider)
                size = sum(len(m['text'].encode()) for m in messages)
                if total_text + size > MAX_TEXT:
                    raise ValueError('Capture exceeds 4 MiB text; select fewer chats')
                total_text += size
                if fallback:
                    warnings.append('A chat used main-page text fallback with unknown speaker roles: ' + url)
                conversations.append({'provider': provider + '-browser', 'source_id': url, 'title': titles[url], 'messages': messages})
                file_links = page.locator('[data-message-author-role] a[download], [data-message-author-role] a[href*="/files/"], [data-message-author-role] a[href*="/attachments/"], [data-testid="user-message"] a[download], [data-testid="user-message"] a[href*="/files/"], .font-claude-message a[download], .font-claude-message a[href*="/files/"]')
                links = file_links.evaluate_all("els => els.map((e,index) => ({index,url:e.href,visible:!!e.getClientRects().length,name:(e.innerText || e.getAttribute('download') || 'Attachment').slice(0,200)})).filter(e => e.visible).slice(0,100)")
                # Do not store signed URLs, tokens or query strings in the ledger.
                for row in links:
                    record = {'source_url': url, 'name': row.get('name', 'Attachment'), 'downloaded': False}
                    if payload['capture_files'] and file_attempts < MAX_FILES:
                        file_attempts += 1
                        try:
                            file = self._attachment(page, file_links, row, url, MAX_TOTAL_FILE_BYTES - sum(f['size'] for f in files))
                            files.append(file)
                            record.update(id=file['id'], size=file['size'], sha256=file['sha256'], downloaded=True)
                        except Exception as error:
                            record['error'] = str(error)[:300] if isinstance(error, ValueError) else 'Download could not be completed; use manual provider export.'
                    else:
                        record['error'] = 'File capture not selected or the 10-file limit was reached.'
                    attachments.append(record)
            except Exception as error:
                skipped.append({'url': url, 'reason': str(error)[:300] if isinstance(error, ValueError) else 'Chat could not be captured; check sign-in and rendered content in the browser.'})
            with self._lock:
                self._state['job']['completed'] = position + 1
        if not conversations:
            raise ValueError('No selected chat could be captured. Check sign-in and loaded content, or import the provider ZIP/JSON export.')
        normalized = json.dumps(conversations, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        parsed = {'provider': provider + '-browser', 'conversations': conversations,
                  'warnings': warnings, 'sha256': hashlib.sha256(normalized).hexdigest()}
        self._update(warnings=warnings)
        return {'parsed': parsed, 'captured': len(conversations), 'skipped': skipped, 'attachments': attachments, 'files': files,
                'message': 'Rendered text captured for source-history import. It is not confirmed memory.', 'warnings': warnings}

    def _do_close(self, _):
        self._release()
        self._update(chats=[], provider=None)
        return {'message': 'Dedicated browser closed. Its private profile remains for your next sign-in session.'}
