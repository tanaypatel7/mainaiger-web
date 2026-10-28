"""Optional browser acquisition tested with DOM/download doubles, never accounts."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_lab import browser_import as browser


CHAT = 'https://chatgpt.com/c/chat-one'
FILE = 'https://chatgpt.com/backend-api/files/file-one/download'


class FakeCDP:
    def __init__(self):
        self.callbacks = {}; self.calls = []; self.path = None
    def send(self, method, data):
        self.calls.append((method, data))
        if method == 'Browser.setDownloadBehavior' and data.get('downloadPath'):
            self.path = Path(data['downloadPath'])
    def on(self, name, callback): self.callbacks[name] = callback
    def emit(self, name, data): self.callbacks[name](data)


class FakeDownload:
    url = FILE
    suggested_filename = '../../unsafe.txt'
    canceled = False
    def cancel(self): self.canceled = True


class DownloadEvent:
    def __init__(self, download): self.value = download
    def __enter__(self): return self
    def __exit__(self, *args): pass


class FakeLocator:
    def __init__(self, page, selector): self.page = page; self.selector = selector
    @property
    def first(self): return self
    def nth(self, index): return self
    def count(self): return 1 if self.selector == 'main' else 0
    def wait_for(self, **kwargs): pass
    def is_visible(self): return False
    def is_enabled(self): return False
    def inner_text(self, **kwargs): return 'Fallback page text — speaker unknown'
    def evaluate_all(self, script):
        if self.selector == 'a[href]': return self.page.links
        if 'a[download]' in self.selector: return self.page.files
        return self.page.messages
    def click(self, **kwargs):
        self.page.thread_ids.append(threading.get_ident())
        cdp = self.page.cdp
        cdp.emit('Browser.downloadWillBegin', {'guid': 'file-guid', 'url': self.page.download.url})
        cdp.path.joinpath('file-guid').write_bytes(self.page.file_content)
        cdp.emit('Browser.downloadProgress', {'guid': 'file-guid', 'receivedBytes': len(self.page.file_content), 'totalBytes': self.page.reported_size or len(self.page.file_content), 'state': 'completed'})


class FakePage:
    def __init__(self, cdp):
        self.url = 'https://chatgpt.com'; self.cdp = cdp; self.thread_ids = []
        self.links = [{'url': CHAT, 'title': 'Research'}, {'url': 'https://evil.example/c/chat-two', 'title': 'External'}]
        self.messages = [{'role': 'user', 'text': 'Question'}, {'role': 'assistant', 'text': 'Answer'}]
        self.files = []; self.file_content = b'attachment text'; self.download = FakeDownload(); self.reported_size = None
    def goto(self, url, **kwargs): self.url = url; self.thread_ids.append(threading.get_ident())
    def is_closed(self): return False
    def locator(self, selector): return FakeLocator(self, selector)
    def get_by_role(self, *args, **kwargs): return FakeLocator(self, 'button')
    def on(self, *args): pass
    def wait_for_timeout(self, ms): time.sleep(min(ms / 1000, .001))
    def expect_download(self, **kwargs): return DownloadEvent(self.download)


class FakeContext:
    def __init__(self):
        self.cdp = FakeCDP(); self.page = FakePage(self.cdp); self.pages = [self.page]; self.browser = self; self.closed = False
    def new_browser_cdp_session(self): return self.cdp
    def set_default_timeout(self, timeout): pass
    def set_default_navigation_timeout(self, timeout): pass
    def close(self): self.closed = True
    def on(self, *args): pass


class FakePlaywright:
    def __init__(self): self.chromium = self; self.context = FakeContext(); self.thread_ids = []; self.stopped = False
    def start(self): self.thread_ids.append(threading.get_ident()); return self
    def launch_persistent_context(self, profile, **kwargs):
        self.profile = profile; self.options = kwargs; self.thread_ids.append(threading.get_ident()); return self.context
    def stop(self): self.thread_ids.append(threading.get_ident()); self.stopped = True


class BrowserImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.fake = FakePlaywright()
        self.manager = browser.BrowserImportManager(self.temp.name, _playwright_factory=lambda: self.fake)
    def tearDown(self): self.manager.close(); self.temp.cleanup()
    def finish(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.manager.status()
            if not state['busy']: return state
            time.sleep(.005)
        self.fail('worker did not finish')
    def opened(self):
        self.manager.open('chatgpt'); self.assertIsNone(self.finish()['error'])
        self.manager.scan(); self.assertIsNone(self.finish()['error'])

    def test_missing_optional_dependency_does_not_break_lab(self):
        with patch.object(browser.importlib.util, 'find_spec', side_effect=ModuleNotFoundError):
            manager = browser.BrowserImportManager(self.temp.name)
        self.assertFalse(manager.status()['available'])
        with self.assertRaisesRegex(ValueError, 'Playwright'): manager.open('chatgpt')
        manager.close()

    def test_private_profile_and_browser_apis_live_on_one_worker(self):
        self.opened(); self.manager.close()
        self.assertTrue(self.fake.context.closed)
        ids = self.fake.thread_ids + self.fake.context.page.thread_ids
        self.assertEqual(len(set(ids)), 1); self.assertNotEqual(ids[0], threading.get_ident())
        self.assertEqual(Path(self.fake.profile).stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.fake.options['headless'])

    def test_capture_persists_before_any_status_poll_or_next_action(self):
        from types import SimpleNamespace
        from local_lab.memory import MemoryStore
        from local_lab import memory_browser
        store = MemoryStore(self.temp.name)
        journal = SimpleNamespace(write=lambda *args: None)
        server = SimpleNamespace(state_dir=Path(self.temp.name), lock=threading.RLock(), memory=store, journal=journal)
        persisted = threading.Event()
        callback_threads = []
        def completed(capture, job_id):
            memory_browser.persist_capture(server, capture, job_id)
            callback_threads.append(threading.get_ident())
            persisted.set()
        self.manager.close()
        self.manager = browser.BrowserImportManager(self.temp.name, _playwright_factory=lambda: self.fake, on_capture=completed)
        try:
            self.opened()
            self.manager.capture([CHAT])
            # No browser status endpoint or manager status read after capture.
            self.assertTrue(persisted.wait(3), 'worker did not persist its capture')
            self.assertEqual(store.overview()['counts']['conversations'], 1)
            self.assertTrue((Path(self.temp.name) / 'browser-capture.json').is_file())
            self.assertEqual(len(list((Path(self.temp.name) / 'browser-captures').glob('*.json'))), 1)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with self.manager._lock:
                    if not self.manager._state['busy']:
                        break
                time.sleep(.005)
            self.manager.close_browser()
            self.assertFalse(self.finish()['running'])
            self.assertEqual(store.overview()['counts']['messages'], 2)
            self.assertEqual(callback_threads, self.fake.thread_ids[:1])
        finally:
            self.manager.close()
            store.close()

    def test_persistence_failure_does_not_report_capture_complete(self):
        def fail(capture, job_id):
            raise ValueError('Capture storage unavailable')
        self.manager.close()
        self.manager = browser.BrowserImportManager(self.temp.name, _playwright_factory=lambda: self.fake, on_capture=fail)
        self.opened()
        self.manager.capture([CHAT])
        result = self.finish()
        self.assertEqual(result['job']['state'], 'failed')
        self.assertEqual(result['error'], 'Capture storage unavailable')
        self.assertIsNone(result['result'])

    def test_open_and_capture_url_allowlist(self):
        for provider in ('evil', 'https://chatgpt.com', None):
            with self.assertRaises(ValueError): self.manager.open(provider)
        self.opened()
        for url in ('https://evil.example/c/one', 'http://chatgpt.com/c/one', 'https://chatgpt.com@evil.example/c/one', 'https://chatgpt.com/c/../account', CHAT + '?token=secret', 'https://chatgpt.com/c/undiscovered'):
            with self.subTest(url=url), self.assertRaises(ValueError): self.manager.capture([url])
        self.assertEqual([c['url'] for c in self.manager.status()['chats']], [CHAT])

    def test_capture_retains_provider_url_and_stable_digest_without_confirmation(self):
        self.opened(); self.manager.capture([CHAT]); result = self.finish()['result']
        parsed = result['parsed']; conversation = parsed['conversations'][0]
        self.assertEqual(conversation['provider'], 'chatgpt-browser')
        self.assertEqual(conversation['source_id'], CHAT)
        self.assertEqual([m['text'] for m in conversation['messages']], ['Question', 'Answer'])
        self.assertNotIn('status', conversation)
        self.manager.capture([CHAT]); self.assertEqual(self.finish()['result']['parsed']['sha256'], parsed['sha256'])

    def test_fallback_is_labeled_unknown_and_incomplete(self):
        self.opened(); self.fake.context.page.messages = []; self.manager.capture([CHAT])
        result = self.finish()['result']
        self.assertEqual(result['parsed']['conversations'][0]['messages'][0]['role'], 'unknown')
        self.assertTrue(any('fallback' in w for w in result['warnings']))

    def test_export_missing_controls_does_not_claim_success(self):
        self.opened(); self.manager.request_export(); result = self.finish()['result']
        self.assertFalse(result['requested']); self.assertFalse(result['verified_delivery'])
        self.assertIn('Import ZIP', result['message'])

    def test_no_attachment_download_without_explicit_selection(self):
        self.opened(); page = self.fake.context.page
        page.files = [{'index': 0, 'url': FILE, 'name': 'document.txt'}]
        self.manager.capture([CHAT], capture_files=False); result = self.finish()['result']
        self.assertEqual(result['files'], []); self.assertFalse(result['attachments'][0]['downloaded'])
        self.assertFalse((Path(self.temp.name) / 'browser-import-files').exists())

    def test_attachment_download_private_uuid_path_and_digest(self):
        self.opened(); page = self.fake.context.page
        page.files = [{'index': 0, 'url': FILE, 'name': '../../untrusted-name.html'}]
        self.manager.capture([CHAT], capture_files=True); state = self.finish()
        self.assertIsNone(state['error']); file = state['result']['files'][0]
        path = Path(file['path'])
        self.assertEqual(path.name, 'attachment.bin'); self.assertTrue(path.is_relative_to(Path(self.temp.name) / 'browser-import-files'))
        self.assertEqual(path.read_bytes(), page.file_content); self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(file['source_url'], CHAT); self.assertEqual(file['size'], len(page.file_content))

    def test_external_file_links_are_inventoried_not_downloaded(self):
        self.opened(); self.fake.context.page.files = [{'index': 0, 'url': 'https://evil.example/files/secret', 'name': 'external'}]
        self.manager.capture([CHAT], capture_files=True); result = self.finish()['result']
        self.assertEqual(result['files'], []); self.assertIn('same-provider', result['attachments'][0]['error'])

    def test_oversized_progress_cancels_and_removes_partial_file(self):
        self.opened(); page = self.fake.context.page
        page.files = [{'index': 0, 'url': FILE, 'name': 'large'}]; page.reported_size = browser.MAX_FILE_BYTES + 1
        self.manager.capture([CHAT], capture_files=True); result = self.finish()['result']
        self.assertEqual(result['files'], [])
        self.assertTrue(any(call[0] == 'Browser.cancelDownload' for call in page.cdp.calls))
        self.assertEqual(list((Path(self.temp.name) / 'browser-import-files').iterdir()), [])

    def test_unavailable_cdp_refuses_files_and_close_is_reusable(self):
        self.opened(); page = self.fake.context.page
        page.files = [{'index': 0, 'url': FILE, 'name': 'file'}]; self.manager._cdp = None
        self.manager.capture([CHAT], capture_files=True); result = self.finish()['result']
        self.assertEqual(result['files'], []); self.assertIn('bounded download', result['attachments'][0]['error'])
        self.manager.close_browser(); self.assertFalse(self.finish()['running'])
        self.manager.open('claude'); state = self.finish(); self.assertTrue(state['running']); self.assertEqual(state['provider'], 'claude')


if __name__ == '__main__': unittest.main()
