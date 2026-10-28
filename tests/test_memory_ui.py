"""Memory UI checks against the real local HTTP service, with synthetic data only.

Requires optional Playwright + Chromium. No model, provider login, or Docker is
used. Run: python -m unittest discover -s tests -p test_memory_ui.py
"""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from local_lab.chat_import import parse_archive
from local_lab.server import LabServer, initialize_state

try:
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as _pw:
        _pw.chromium.launch().close()
    HAVE_BROWSER = True
except Exception:
    HAVE_BROWSER = False


@unittest.skipUnless(HAVE_BROWSER, 'Optional Playwright Chromium is not installed')
class MemoryUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state, key = initialize_state(Path(self.tmp.name) / 'private')
        self.server = LabServer(('127.0.0.1', 0), state, key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch()
        self.context = self.browser.new_context(viewport={'width': 1380, 'height': 940})
        self.origin = 'http://127.0.0.1:%s' % self.server.server_port
        response = self.context.request.post(self.origin + '/api/login', data={'key': key}, headers={'Origin': self.origin})
        self.assertEqual(response.status, 200)
        self.page = self.context.new_page()
        self.model_requests = []
        self.page.on('request', lambda request: self.model_requests.append(request.url) if '/models/chat' in request.url or '/memory/propose' in request.url else None)

    def tearDown(self):
        self.context.close()
        self.browser.close()
        self.pw.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def open_page(self):
        self.page.goto(self.origin + '/memory')
        expect(self.page.locator('#memory-main')).to_be_visible()

    def test_manual_review_edit_and_search_without_model_calls(self):
        self.open_page()
        self.page.click('#new-entry')
        self.page.fill('#entry-title', 'Research procedure')
        self.page.fill('#entry-body', 'Use the reviewed method.')
        self.page.click('#entry-save')
        expect(self.page.locator('#entry-confirm')).to_be_enabled()
        self.page.click('#entry-confirm')
        expect(self.page.locator('#entry-status')).to_have_text('confirmed')
        self.page.fill('#search-query', 'reviewed')
        self.page.locator('#search-form button').click()
        expect(self.page.locator('#memory-list')).to_contain_text('Research procedure')
        expect(self.page.locator('#search-cost')).to_contain_text('0 model calls')
        self.page.fill('#entry-body', 'Updated reviewed method.')
        self.page.click('#entry-save')
        expect(self.page.locator('#entry-status')).to_have_text('draft')
        self.page.locator('#search-form button').click()
        expect(self.page.locator('#memory-list')).to_contain_text('No confirmed memories matched')
        self.assertEqual(self.model_requests, [])
        self.assertEqual(len(self.server.memory.entry(self.server.memory.overview()['entries'][0]['id'])['history']), 3)

    def test_source_selection_preserves_provenance_and_treats_html_as_text(self):
        source = [{'uuid': 'chat-1', 'name': 'Imported source', 'chat_messages': [
            {'uuid': 'msg-1', 'sender': 'human', 'text': '<img src=x onerror="window.injected=1"> A useful method'},
            {'uuid': 'msg-2', 'sender': 'assistant', 'text': 'Unselected source text'},
        ]}]
        self.server.memory.import_chats(parse_archive(json.dumps(source).encode(), 'conversations.json'))
        self.open_page()
        self.page.click('#tab-chats')
        self.page.locator('#memory-list button').first.click()
        expect(self.page.locator('#messages')).to_contain_text('<img src=x')
        self.assertEqual(self.page.locator('#messages img').count(), 0)
        self.assertIsNone(self.page.evaluate('window.injected'))
        self.page.locator('#messages input').first.check()
        self.page.click('#draft-selection')
        self.page.click('#entry-save')
        expect(self.page.locator('#entry-confirm')).to_be_enabled()
        overview = self.server.memory.overview()
        memory = self.server.memory.entry(overview['entries'][0]['id'])
        self.assertEqual(memory['status'], 'draft')
        self.assertEqual(len(memory['sources']), 1)
        self.assertNotIn('Unselected', memory['body'])
        self.assertEqual(self.model_requests, [])

    def test_pagination_and_small_screen_layout(self):
        for i in range(202):
            self.server.memory.save_entry({'title': 'Draft %03d' % i, 'body': 'Synthetic memory'})
        self.open_page()
        self.page.click('#tab-draft')
        expect(self.page.locator('#count-drafts')).to_have_text('202')
        self.assertEqual(self.page.locator('#memory-list button').count(), 200)
        self.page.click('#load-more')
        expect(self.page.locator('#memory-list button')).to_have_count(202)
        expect(self.page.locator('#load-more')).to_be_hidden()
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertEqual(self.model_requests, [])
