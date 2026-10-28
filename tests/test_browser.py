"""Browser scripts: the grammar, the store, a real headless run against a local site, captures into chat notes."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from local_lab import browser as b, memory as m, memory_import as mi

PAGES = {
    '/list': '<html><head><title>My chats</title></head><body><main><h1>Chats</h1><a href="/chat/1">First chat</a> <a href="/chat/2">Second chat</a> '
             '<a href="/chat/1">First again</a><a id="outside" href="http://localhost:{other}/outside">elsewhere</a></main><footer><a href="/about">about</a></footer></body></html>',
    '/chat/1': '<html><head><title>Haptics plan</title></head><body><h1>Haptics plan</h1><div class="msg">You: plan the glove</div><div class="msg">Assistant: calibrate each finger</div></body></html>',
    '/chat/2': '<html><head><title>Fraud talk</title></head><body><h1>Fraud talk</h1><div class="msg">You: quantum kernels?</div><div class="msg">Assistant: feature map then SVM</div></body></html>',
    '/about': '<html><head><title>About</title></head><body>about</body></html>',
    '/outside': '<html><head><title>Outside</title></head><body>should never be reached</body></html>',
}


class Site(BaseHTTPRequestHandler):
    other = 0
    hits = []
    def log_message(self, *args):
        pass
    def do_GET(self):
        Site.hits.append(self.path)
        page = PAGES.get(self.path)
        if page is None:
            self.send_response(404); self.end_headers(); return
        body = page.replace('{other}', str(Site.other)).encode('utf-8')
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


def script_for(port):
    return {'title': 'Local chats', 'allow': ['127.0.0.1'], 'start': 'http://127.0.0.1:%d/list' % port, 'params': [], 'budget_s': 60,
            'steps': [{'do': 'wait', 'selector': 'main a'}, {'do': 'links', 'selector': 'main a', 'as': 'items', 'limit': 10}, {'do': 'capture', 'as': 'list'},
                      {'do': 'click', 'selector': 'a#outside', 'optional': True}, {'do': 'wait', 'seconds': 0.3},
                      {'do': 'each', 'over': 'items', 'steps': [{'do': 'goto', 'url': '{{item}}'}, {'do': 'wait', 'selector': 'h1'}, {'do': 'capture', 'as': 'chat-{{index}}', 'selector': 'body'}]}]}


class Vault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve(); self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.home = patch.object(Path, 'home', return_value=self.root); self.home.start(); m.configure(self.state, str(self.root / 'Memory'))
    def tearDown(self):
        self.home.stop(); self.tmp.cleanup()


class GrammarAndStoreTests(Vault):
    def test_validate_normalises_and_refuses_plainly(self):
        ok = b.validate_script({'title': ' T ', 'allow': ['Example.com'], 'start': 'https://example.com/x', 'steps': [{'do': 'capture', 'as': 'p'}]})
        self.assertEqual((ok['title'], ok['allow'], ok['params'], ok['headed'], ok['budget_s']), ('T', ['example.com'], [], False, 300))
        bad = [
            ({'allow': ['example.com'], 'steps': [{'do': 'capture', 'as': 'p'}], 'extra': 1}, 'Unknown script fields'),
            ({'allow': [], 'steps': [{'do': 'capture', 'as': 'p'}]}, 'allow must list'),
            ({'allow': ['http://example.com'], 'steps': [{'do': 'capture', 'as': 'p'}]}, 'host names without http'),
            ({'allow': ['example.com'], 'start': 'https://other.org/', 'steps': [{'do': 'capture', 'as': 'p'}]}, 'not in the allowed sites'),
            ({'allow': ['example.com'], 'start': 'ftp://example.com/', 'steps': [{'do': 'capture', 'as': 'p'}]}, 'http://'),
            ({'allow': ['example.com'], 'steps': []}, 'steps must list'),
            ({'allow': ['example.com'], 'steps': [{'do': 'run', 'code': 'x'}]}, 'Step 1 must be'),
            ({'allow': ['example.com'], 'steps': [{'do': 'click'}]}, 'needs selector'),
            ({'allow': ['example.com'], 'steps': [{'do': 'click', 'selector': 'a', 'js': 1}]}, 'unknown fields: js'),
            ({'allow': ['example.com'], 'steps': [{'do': 'wait'}]}, 'needs a selector or seconds'),
            ({'allow': ['example.com'], 'steps': [{'do': 'wait', 'seconds': 999}]}, 'from 0 to 300'),
            ({'allow': ['example.com'], 'steps': [{'do': 'links', 'selector': 'a', 'as': 'my list'}]}, 'plain name'),
            ({'allow': ['example.com'], 'steps': [{'do': 'each', 'over': 'x', 'steps': [{'do': 'each', 'over': 'y', 'steps': [{'do': 'capture', 'as': 'p'}]}]}]}, 'cannot be nested'),
            ({'allow': ['example.com'], 'params': ['item'], 'steps': [{'do': 'capture', 'as': 'p'}]}, 'item and index'),
            ({'allow': ['example.com'], 'budget_s': 5, 'steps': [{'do': 'capture', 'as': 'p'}]}, 'budget_s'),
            ({'allow': ['example.com'], 'steps': [{'do': 'goto', 'url': 'https://evil.net/{{x}}'}]}, None),  # a placeholder url is checked at run time
        ]
        for script, message in bad:
            with self.subTest(script=script):
                if message is None:
                    b.validate_script(script)
                else:
                    with self.assertRaisesRegex(b.BrowserError, message):
                        b.validate_script(script)
        for name, template in b.TEMPLATES.items():
            with self.subTest(template=name):
                b.validate_script(template)
    def test_save_list_read_delete_and_names(self):
        self.assertEqual(b.listing(self.state), [])
        row = b.save_script(self.state, 'my-site', b.TEMPLATES['page-capture'])
        self.assertEqual((row['name'], row['steps'], row['captures'], row['allow']), ('my-site', 2, 1, ['example.com']))
        self.assertEqual([r['name'] for r in b.listing(self.state)], ['my-site']); self.assertEqual(b.read_script(self.state, 'my-site')['title'], 'Capture one page')
        self.assertTrue((self.root / 'Memory' / 'browser' / 'scripts' / 'my-site.json').is_file())
        for bad in ('My Site', '../x', 'a' * 50, ''):
            with self.assertRaisesRegex(b.BrowserError, 'lowercase'):
                b.save_script(self.state, bad, b.TEMPLATES['page-capture'])
        with self.assertRaisesRegex(b.BrowserError, 'No script named'):
            b.read_script(self.state, 'ghost')
        (self.root / 'Memory' / 'browser' / 'scripts' / 'broken.json').write_text('{"allow": []}', encoding='utf-8')
        rows = b.listing(self.state); self.assertEqual([r['name'] for r in rows], ['broken', 'my-site']); self.assertIn('allow must list', rows[0]['error'])
        self.assertEqual(b.delete_script(self.state, 'my-site'), 'my-site'); self.assertEqual([r['name'] for r in b.listing(self.state)], ['broken'])
        self.assertEqual(m.listing(self.state), [], 'scripts are not notes')
    def test_params_are_checked_against_the_script(self):
        b.save_script(self.state, 'q', dict(b.TEMPLATES['page-capture'], params=['query']))
        with self.assertRaisesRegex(b.BrowserError, 'needs the parameter "query"'):
            b._check_params(b.read_script(self.state, 'q'), {})
        self.assertEqual(b._check_params(b.read_script(self.state, 'q'), {'query': 7, 'extra': 'ignored'}), {'query': '7'})
        with self.assertRaisesRegex(b.BrowserError, 'text or a number'):
            b._check_params(b.read_script(self.state, 'q'), {'query': [1]})
    def test_availability_probe_reports_this_python(self):
        info = b.available(refresh=True)
        self.assertEqual(info['python'], sys.executable); self.assertIn('pip install playwright', info['install'][0]); self.assertIsInstance(info['ready'], bool)
        missing = b.available(python='/nonexistent/python', refresh=True); self.assertFalse(missing['ready']); self.assertTrue(missing['detail'])


READY = b.available()['ready']


@unittest.skipUnless(READY, 'Playwright Chromium not installed for this Python')
class RunTests(Vault):
    @classmethod
    def setUpClass(cls):
        cls.other = HTTPServer(('127.0.0.1', 0), Site); threading.Thread(target=cls.other.serve_forever, daemon=True).start()
        Site.other = cls.other.server_port
        cls.site = HTTPServer(('127.0.0.1', 0), Site); threading.Thread(target=cls.site.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for s in (cls.site, cls.other):
            s.shutdown(); s.server_close()
    def test_run_captures_follows_links_blocks_other_hosts_and_imports_chats(self):
        Site.hits = []
        b.save_script(self.state, 'local', script_for(self.site.server_port))
        lines = []
        class J:
            def write(self, kind, text, data=None): lines.append((kind, text, data))
        out = b.run(self.state, 'local', journal=J())
        self.assertTrue(out['ok'], out); self.assertEqual([p['as'] for p in out['pages']], ['list', 'chat-1', 'chat-2'])
        self.assertEqual(out['vars'], {'items': '2 item(s)'}, 'duplicates and the other host were dropped from the links')
        self.assertEqual(out['blocked'], ['http://localhost:%d/outside' % self.other.server_port], 'the click tried to leave; the navigation was refused')
        self.assertEqual(out['pages'][0]['url'], 'http://127.0.0.1:%d/list' % self.site.server_port); self.assertIn('First chat', (self.root / 'Memory' / out['folder'] / out['pages'][0]['file']).read_text(encoding='utf-8'))
        self.assertNotIn('/outside', Site.hits, 'the other server never saw a request')
        self.assertEqual([(p['title'], p['chars'] > 20) for p in out['pages'][1:]], [('Haptics plan', True), ('Fraud talk', True)])
        folder = self.root / 'Memory' / out['folder']; self.assertTrue((folder / 'capture.json').is_file()); self.assertTrue((folder / out['pages'][1]['file']).is_file())
        text = (folder / out['pages'][1]['file']).read_text(encoding='utf-8'); self.assertIn('title: "Haptics plan"', text); self.assertIn('calibrate each finger', text)
        self.assertEqual([l['do'] for l in out['log']], ['goto', 'wait', 'links', 'capture', 'click', 'wait', 'goto', 'wait', 'capture', 'goto', 'wait', 'capture', 'each'])
        self.assertTrue(all(l['ok'] for l in out['log']), out['log']); self.assertEqual(lines[0][0], 'browser.run'); self.assertIn('3 pages', lines[0][1])
        self.assertEqual(b.captures(self.state)[0]['pages'], 3); self.assertEqual(b.read_capture(self.state, out['folder'])['script'], 'local')
        page = b.read_page(self.state, out['folder'], out['pages'][2]['file']); self.assertEqual(page['title'], 'Fraud talk'); self.assertIn('quantum kernels', page['text'])
        for bad in ('browser/captures/../x', 'notes/a.md', out['folder'] + '/x'):
            with self.assertRaises(b.BrowserError): b.read_capture(self.state, bad)
        with self.assertRaises(b.BrowserError): b.read_page(self.state, out['folder'], '../capture.json')
        # Into memory: one chat note per page, the URL as its id, so importing twice adds nothing; the review loop then sees them.
        got = b.import_capture(self.state, out['folder']); self.assertEqual((got['imported'], got['skipped']), (3, 0))
        chats = m.listing(self.state, tier='chats'); self.assertEqual(sorted(c['title'] for c in chats), ['Fraud talk', 'Haptics plan', 'My chats'])
        note = m.read(self.state, [c for c in chats if c['title'] == 'Fraud talk'][0]['path'])
        self.assertEqual((note['front']['source'], note['front']['source_id'], note['front']['script']), ('browser', 'http://127.0.0.1:%d/chat/2' % self.site.server_port, 'local')); self.assertIn('feature map then SVM', note['body'])
        self.assertEqual(b.import_capture(self.state, out['folder'])['skipped'], 3); self.assertEqual(b.captures(self.state)[0]['imported']['notes'], 3)
        self.assertEqual(mi.review_status(self.state)['pending'], 3)
        # A refused goto (a parameter pointing elsewhere) stops the run with the reason; the capture is still recorded.
        b.save_script(self.state, 'param', {'title': 'p', 'allow': ['127.0.0.1'], 'params': ['url'], 'budget_s': 30, 'steps': [{'do': 'goto', 'url': '{{url}}'}, {'do': 'capture', 'as': 'x'}]})
        bad = b.run(self.state, 'param', params={'url': 'http://localhost:%d/outside' % self.other.server_port})
        self.assertFalse(bad['ok']); self.assertIn('not in the allowed sites', bad['error']); self.assertEqual(bad['pages'], []); self.assertNotIn('/outside', Site.hits)
        good = b.run(self.state, 'param', params={'url': 'http://127.0.0.1:%d/about' % self.site.server_port}); self.assertTrue(good['ok']); self.assertEqual(good['pages'][0]['title'], 'About')
        self.assertTrue((self.state / 'browser-profile').is_dir()); self.assertTrue(b.clear_profile(self.state)); self.assertFalse((self.state / 'browser-profile').exists())
    def test_missing_selector_and_budget_fail_plainly(self):
        b.save_script(self.state, 'slow', {'title': 's', 'allow': ['127.0.0.1'], 'start': 'http://127.0.0.1:%d/about' % self.site.server_port, 'budget_s': 10,
                                           'steps': [{'do': 'wait', 'selector': '#never', 'timeout_s': 2}, {'do': 'capture', 'as': 'x'}]})
        out = b.run(self.state, 'slow'); self.assertFalse(out['ok']); self.assertIn('Step 1 (wait) failed', out['error']); self.assertEqual(out['pages'], []); self.assertFalse(out['log'][-1]['ok'])
        b.save_script(self.state, 'opt', {'title': 's', 'allow': ['127.0.0.1'], 'start': 'http://127.0.0.1:%d/about' % self.site.server_port, 'budget_s': 10,
                                          'steps': [{'do': 'wait', 'selector': '#never', 'timeout_s': 1, 'optional': True}, {'do': 'capture', 'as': 'x'}]})
        out = b.run(self.state, 'opt'); self.assertTrue(out['ok'], out['error']); self.assertEqual(out['pages'][0]['title'], 'About')


class HttpTests(Vault):
    """The routes and a browser cell inside a graph run, against the Lab server with a real session."""
    @classmethod
    def setUpClass(cls):
        cls.other = HTTPServer(('127.0.0.1', 0), Site); threading.Thread(target=cls.other.serve_forever, daemon=True).start(); Site.other = cls.other.server_port
        cls.site = HTTPServer(('127.0.0.1', 0), Site); threading.Thread(target=cls.site.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for s in (cls.site, cls.other):
            s.shutdown(); s.server_close()
    def setUp(self):
        super().setUp()
        import http.client
        from local_lab.server import LabServer, initialize_state
        from tests.test_sessions import local_factory
        state, key = initialize_state(self.root / 'private'); self.key = key; self.lab_state = Path(state)
        m.configure(self.lab_state, str(self.root / 'Memory2'))
        self.server = LabServer(('127.0.0.1', 0), state, key, executor=lambda c, s: self.fail('one-shot'), kernel_factory=local_factory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_port; self.origin = f'http://127.0.0.1:{self.port}'; self.http = http.client
    def tearDown(self):
        self.server.kernels.stop_all(); self.server.shutdown(); self.server.server_close(); self.thread.join(); super().tearDown()
    def req(self, path, body=None, headers=None):
        c = self.http.HTTPConnection('127.0.0.1', self.port, timeout=120); h = {'Host': f'127.0.0.1:{self.port}'}; h.update(headers or {})
        if body is not None: h['Content-Type'] = 'application/json'
        c.request('POST' if body is not None else 'GET', path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); payload = r.read(); head = dict(r.getheaders()); c.close(); return r.status, json.loads(payload), head
    def login(self):
        status, data, head = self.req('/api/login', {'key': self.key}, {'Origin': self.origin})
        return {'Origin': self.origin, 'Cookie': head['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}
    def wait_browser(self, cookie):
        import time
        for _ in range(1200):
            st = self.req('/api/browser/run', headers=cookie)[1]
            if st['state'] != 'running': return st
            time.sleep(0.05)
        self.fail('browser run did not finish')
    def test_script_routes_refusals_and_status(self):
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        self.assertEqual(self.req('/api/browser')[0], 401)
        st = self.req('/api/browser', headers=cookie)[1]; self.assertEqual((st['scripts'], st['captures'], st['run']['state'], sorted(st['templates'])), ([], [], 'idle', ['list-and-each', 'login-by-hand', 'page-capture']))
        self.assertIn('pip install playwright', st['available']['install'][0]); self.assertIn('each', st['steps'])
        self.assertEqual(self.req('/api/browser/script', {'name': 'Bad Name', 'script': b.TEMPLATES['page-capture']}, auth)[0], 400)
        status, data, _ = self.req('/api/browser/script', {'name': 'cap', 'script': dict(b.TEMPLATES['page-capture'], steps=[{'do': 'eval', 'js': '1'}])}, auth); self.assertEqual(status, 400); self.assertIn('Step 1 must be', data['error'])
        row = self.req('/api/browser/script', {'name': 'cap', 'script': b.TEMPLATES['page-capture']}, auth)[1]; self.assertEqual((row['name'], row['steps']), ('cap', 2))
        self.assertEqual(self.req('/api/browser/script?name=cap', headers=cookie)[1]['script']['allow'], ['example.com']); self.assertEqual(self.req('/api/browser/script?name=ghost', headers=cookie)[0], 400)
        self.assertEqual([r['name'] for r in self.req('/api/browser', headers=cookie)[1]['scripts']], ['cap'])
        self.assertEqual(self.req('/api/browser/run', {'name': 'cap'}, auth)[0], 400, 'confirm required')
        with patch.object(b, 'available', lambda *a, **k: {'ready': False, 'playwright': False, 'chromium': False, 'install': ['x', 'y'], 'python': 'p', 'detail': ''}):
            status, data, _ = self.req('/api/browser/run', {'name': 'cap', 'confirm': True}, auth); self.assertEqual(status, 400); self.assertIn('Playwright is not installed', data['error'])
        self.assertEqual(self.req('/api/browser/capture?folder=browser/captures/nope', headers=cookie)[0], 400)
        self.assertEqual(self.req('/api/browser/profile/clear', {'confirm': True}, auth)[1], {'cleared': False})
        self.assertEqual(self.req('/api/browser/script/delete', {'name': 'cap'}, auth)[1], {'deleted': 'cap'}); self.assertEqual(self.req('/api/browser', headers=cookie)[1]['scripts'], [])
        kinds = [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]; self.assertIn('browser.script', kinds); self.assertIn('browser.script.deleted', kinds); self.assertIn('browser.profile.cleared', kinds)
        # Without a Memory folder nothing browser-related is reachable, and the page says why (409).
        (self.lab_state / 'memory.json').unlink(); self.assertEqual(self.req('/api/browser', headers=cookie)[1]['folder'], None); self.assertEqual(self.req('/api/browser/script', {'name': 'cap', 'script': b.TEMPLATES['page-capture']}, auth)[0], 409)
    @unittest.skipUnless(READY, 'Playwright Chromium not installed for this Python')
    def test_run_from_the_page_then_a_browser_cell_in_a_graph(self):
        from local_lab import graph as g
        from tests.test_nested import chain
        auth = self.login(); cookie = {'Cookie': auth['Cookie']}
        self.req('/api/browser/script', {'name': 'local', 'script': script_for(self.site.server_port)}, auth)
        status, st, _ = self.req('/api/browser/run', {'name': 'local', 'confirm': True}, auth); self.assertEqual((status, st['state'], st['job']['script']), (202, 'running', 'local'))
        self.assertEqual(self.req('/api/browser/run', {'name': 'local', 'confirm': True}, auth)[0], 409, 'one run at a time')
        st = self.wait_browser(cookie); self.assertEqual(st['state'], 'finished'); res = st['job']['result']; self.assertTrue(res['ok'], res['error']); self.assertEqual(len(res['pages']), 3)
        caps = self.req('/api/browser', headers=cookie)[1]['captures']; self.assertEqual((len(caps), caps[0]['pages'], caps[0]['script']), (1, 3, 'local'))
        page = self.req('/api/browser/page?folder=%s&file=%s' % (res['folder'], res['pages'][1]['file']), headers=cookie)[1]['page']; self.assertEqual(page['title'], 'Haptics plan')
        self.assertEqual(self.req('/api/browser/import', {'folder': res['folder']}, auth)[0], 400, 'confirm required')
        got = self.req('/api/browser/import', {'folder': res['folder'], 'confirm': True}, auth)[1]; self.assertEqual((got['imported'], got['skipped']), (3, 0))
        self.assertEqual(self.req('/api/memory/review', headers=cookie)[1]['pending'], 3, 'the review loop sees captured pages as chats')
        # A browser cell: `params` from the session, the capture bound to a variable the next cell reads.
        doc = chain([('p', 'params = {"url": "http://127.0.0.1:%d/chat/2"}' % self.site.server_port), ('web', ''), ('after', 'print(capture["pages"][0]["title"], len(capture["pages"]))')])
        doc['notebook']['cells'][1]['metadata']['mainaiger']['browser'] = {'script': 'param'}; doc = g.validate(doc)
        self.assertEqual(g.browser_cells(doc), [('web', 'param')]); self.assertEqual((g.analysis(doc)['web']['inputs'], g.analysis(doc)['web']['optional'], g.analysis(doc)['web']['outputs']), ([], ['params'], ['capture']))
        self.assertEqual(self.req('/api/graph', {'document': doc}, auth)[0], 200)
        check = self.req('/api/graph/preflight', {'mode': 'all', 'nodes': [], 'include_prerequisites': False}, auth)[1]
        self.assertEqual((check['browser'][0]['found'], check['browser_ready']), (False, False), 'the script does not exist yet')
        rev = self.req('/api/sessions', headers=cookie)[1]['revision']; self.req('/api/sessions/start', {'confirm': True, 'revision': rev}, auth)
        status, data, _ = self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth); self.assertEqual(status, 400); self.assertIn('not in the Memory folder: param', data['error'])
        self.req('/api/browser/script', {'name': 'param', 'script': {'title': 'p', 'allow': ['127.0.0.1'], 'params': ['url'], 'budget_s': 30, 'steps': [{'do': 'goto', 'url': '{{url}}'}, {'do': 'wait', 'selector': 'h1'}, {'do': 'capture', 'as': 'x'}]}}, auth)
        check = self.req('/api/graph/preflight', {'mode': 'all', 'nodes': [], 'include_prerequisites': False}, auth)[1]
        self.assertEqual((check['browser'][0]['allow'], check['browser'][0]['params'], check['browser_ready']), (['127.0.0.1'], ['url'], True))
        self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth)
        import time
        for _ in range(1200):
            run = self.req('/api/graph/run', headers=cookie)[1]['run']
            if run['state'] not in ('pending', 'running'): break
            time.sleep(0.05)
        self.assertEqual(run['state'], 'finished', run.get('error')); self.assertEqual(run['nodes']['after']['output'], 'Fraud talk 1\n'); self.assertIn('Browser param → capture', run['nodes']['web']['note'])
        self.assertEqual(len(self.req('/api/browser', headers=cookie)[1]['captures']), 2)
        kinds = [e['kind'] for e in self.req('/api/journal', headers=cookie)[1]['entries']]; self.assertEqual(kinds.count('browser.start'), 2); self.assertEqual(kinds.count('browser.run'), 2); self.assertIn('browser.import', kinds)
        # The same cell without `params` in the session: the script's parameter is missing, the run stops with the reason.
        doc2 = chain([('web', ''), ('after', 'print(capture)')]); doc2['notebook']['cells'][0]['metadata']['mainaiger']['browser'] = {'script': 'param', 'output': 'cap'}; doc2 = g.validate(doc2)
        self.req('/api/graph', {'document': doc2}, auth); self.server.kernels.active()['kernel'].execute('del params')
        self.req('/api/graph/run', {'mode': 'all', 'nodes': [], 'confirm': True, 'include_prerequisites': False}, auth)
        for _ in range(1200):
            run = self.req('/api/graph/run', headers=cookie)[1]['run']
            if run['state'] not in ('pending', 'running'): break
            time.sleep(0.05)
        self.assertEqual(run['state'], 'failed'); self.assertIn('needs the parameter "url"', run['nodes']['web']['error']['evalue'])


if __name__ == '__main__':
    unittest.main()
