"""Behavioural checks for the graph canvas (local_lab/ui/graph.js) in headless Chromium.

These run only when Playwright and its Chromium are installed (python3 -m playwright
install chromium); otherwise they are skipped so the plain unittest run stays green.
They use tests/perf/stub_server.py, which serves the real UI with a fake /api/graph.
"""
import json, os, subprocess, sys, time, unittest, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8791
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch().close()
    HAVE_BROWSER = True
except Exception:
    HAVE_BROWSER = False


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class CanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = subprocess.Popen([sys.executable, os.path.join(HERE, 'perf', 'stub_server.py'), str(PORT), '6'])
        for _ in range(50):
            try: urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/graph', timeout=1); break
            except Exception: time.sleep(0.1)
        cls.pw = sync_playwright().start(); cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.server.terminate(); cls.server.wait(timeout=10)

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 1400, 'height': 900})
        self.page.goto(f'http://127.0.0.1:{PORT}/graph'); self.page.wait_for_selector('.node.code'); self.page.wait_for_timeout(200)
        self.page.click('#fit'); self.page.wait_for_timeout(100)  # bring every card on screen (zoom may be below 1)

    def tearDown(self):
        self.page.close()

    def edge_d(self, source, target):
        return self.page.evaluate("""([s, t]) => { const p = [...document.querySelectorAll('#edge-layer path.control')].find(p => { const id = p.dataset.id; return id === `e-${s}-${t}`; }); return p && p.getAttribute('d'); }""", [source, target])

    def card_rect(self, cid):
        return self.page.evaluate("""id => { const r = document.querySelector(`.node[data-id="${id}"]`).getBoundingClientRect(); return {x: r.left, y: r.top, w: r.width, h: r.height}; }""", cid)

    def test_drag_moves_card_and_only_its_edges(self):
        before_own = self.edge_d('cell-1', 'cell-2'); before_other = self.edge_d('cell-1', 'cell-3'); card0 = self.card_rect('cell-2')
        r = self.page.evaluate("""() => { const r = document.querySelector('.node[data-id="cell-2"] .node-head b').getBoundingClientRect(); return {x: r.left + 4, y: r.top + 4}; }""")
        self.page.mouse.move(r['x'], r['y']); self.page.mouse.down()
        for i in range(1, 21): self.page.mouse.move(r['x'] + i * 5, r['y'] + i * 3)
        self.page.mouse.up(); self.page.wait_for_timeout(100)
        rect = self.card_rect('cell-2')
        self.assertAlmostEqual(rect['x'], card0['x'] + 100, delta=3); self.assertAlmostEqual(rect['y'], card0['y'] + 60, delta=3)  # screen px follow the pointer 1:1 at any zoom
        self.assertNotEqual(before_own, self.edge_d('cell-1', 'cell-2'), 'edge into the dragged card must re-route')
        self.assertEqual(before_other, self.edge_d('cell-1', 'cell-3'), 'an edge not touching the dragged card must keep its path')
        # The edge ends at the top centre of the moved card (SVG is in world px; zoom is 1 and pan is known).
        d = self.edge_d('cell-1', 'cell-2'); end = d.split(' ')[-2:]
        world = self.page.evaluate("""() => { const vp = document.getElementById('viewport').getBoundingClientRect(); const m = new DOMMatrix(getComputedStyle(document.getElementById('world')).transform); return {x: vp.left + m.e, y: vp.top + m.f, z: m.a}; }""")
        self.assertAlmostEqual(float(end[0]), (rect['x'] + rect['w'] / 2 - world['x']) / world['z'], delta=2)
        self.assertAlmostEqual(float(end[1]), (rect['y'] - world['y']) / world['z'], delta=2)

    def test_typing_grows_card_and_reroutes_outgoing_edges(self):
        before = self.edge_d('cell-1', 'cell-2'); h0 = self.card_rect('cell-1')['h']
        area = self.page.locator('.node[data-id="cell-1"] textarea'); area.click(); area.press('End'); area.type('\n' * 8 + 'x = 1')
        self.page.wait_for_timeout(150)
        self.assertGreater(self.card_rect('cell-1')['h'], h0 + 40)
        self.assertNotEqual(before, self.edge_d('cell-1', 'cell-2'), 'edges leaving a card that grew must move down with it')

    def test_select_marks_exactly_one_card(self):
        self.page.locator('.node[data-id="cell-3"] .node-foot').click(); self.page.wait_for_timeout(50)
        self.assertEqual(self.page.evaluate("() => [...document.querySelectorAll('.node.selected')].map(e => e.dataset.id)"), ['cell-3'])
        self.page.locator('.node[data-id="cell-4"] .node-foot').click(); self.page.wait_for_timeout(50)
        self.assertEqual(self.page.evaluate("() => [...document.querySelectorAll('.node.selected')].map(e => e.dataset.id)"), ['cell-4'])
        self.assertFalse(self.page.locator('#node-panel').is_hidden())

    def test_port_drag_creates_trigger_and_preview_follows_pointer(self):
        out = self.page.evaluate("""() => { const r = document.querySelector('.node[data-id="cell-3"] .port.out').getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }""")
        dest = self.page.evaluate("""() => { const r = document.querySelector('.node[data-id="cell-5"] .node-foot').getBoundingClientRect(); return {x: r.left + 10, y: r.top + 5}; }""")
        self.page.mouse.move(out['x'], out['y']); self.page.mouse.down()
        self.page.mouse.move((out['x'] + dest['x']) / 2, (out['y'] + dest['y']) / 2, steps=5); self.page.wait_for_timeout(60)
        self.assertEqual(self.page.evaluate("() => getComputedStyle(document.getElementById('pending-edge')).display"), 'inline')
        d_mid = self.page.get_attribute('#pending-edge', 'd')
        self.page.mouse.move(dest['x'], dest['y'], steps=5); self.page.wait_for_timeout(60)
        self.assertNotEqual(d_mid, self.page.get_attribute('#pending-edge', 'd'), 'preview must follow the pointer')
        self.assertEqual(self.page.evaluate("() => [...document.querySelectorAll('.port.target')].length"), 1, 'hovered input port is highlighted')
        self.page.mouse.up(); self.page.wait_for_timeout(100)
        self.assertEqual(self.page.evaluate("() => getComputedStyle(document.getElementById('pending-edge')).display"), 'none', 'preview is removed after the drop')
        self.assertTrue(self.page.evaluate("() => !![...document.querySelectorAll('#edge-layer path.control')].find(p => p.dataset.id.startsWith('e') && !p.dataset.id.includes('-cell'))"), 'a new control edge path exists')
        self.page.wait_for_timeout(900)  # save debounce -> server re-analysis
        view = json.load(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/graph'))
        self.assertTrue(any(e['source'] == 'cell-3' and e['target'] == 'cell-5' for e in view['document']['graph']['edges']))

    def test_pan_and_zoom_apply_next_frame(self):
        t0 = self.page.evaluate("() => getComputedStyle(document.getElementById('world')).transform")
        vp = self.page.evaluate("() => { const r = document.getElementById('viewport').getBoundingClientRect(); return {x: r.left + 30, y: r.bottom - 30}; }")
        self.page.mouse.move(vp['x'], vp['y']); self.page.mouse.down(); self.page.mouse.move(vp['x'] + 80, vp['y'] - 40, steps=4); self.page.mouse.up(); self.page.wait_for_timeout(80)
        t1 = self.page.evaluate("() => getComputedStyle(document.getElementById('world')).transform"); self.assertNotEqual(t0, t1)
        self.page.mouse.wheel(0, -300); self.page.wait_for_timeout(80)
        self.assertNotEqual(self.page.inner_text('#zoom-label'), '100%')


if __name__ == '__main__':
    unittest.main()


class RealServerCase(unittest.TestCase):
    """A canvas page served by the real local service (in-process), signed in with the owner key."""
    @classmethod
    def setUpClass(cls):
        import tempfile, threading
        from pathlib import Path
        from unittest.mock import patch
        from local_lab.server import LabServer, initialize_state
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name).resolve()
        state, key = initialize_state(cls.root / 'private'); cls.key = key
        cls.home = patch.object(Path, 'home', return_value=cls.root); cls.home.start()
        cls.server = LabServer(('127.0.0.1', 0), state, key)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'
        cls.pw = sync_playwright().start(); cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.home.stop(); cls.tmp.cleanup()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 1400, 'height': 900})
        self.page.goto(self.origin + '/graph')
        # Sign in from inside the page (same-origin, so the cookie and CSRF flow are the real ones), then reload the canvas.
        self.page.evaluate("key => fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key})}).then(r => r.json())", self.key)
        self.page.goto(self.origin + '/graph'); self.page.wait_for_selector('.node.code'); self.page.wait_for_timeout(300)

    def tearDown(self):
        self.page.close()


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class LibraryPanelTests(RealServerCase):
    """The Library panel: approve folder, starter cells, search, insert, edit, push, pull, drag."""
    def test_approve_folder_seed_insert_edit_push_pull(self):
        p = self.page
        # 1. Not configured: the panel offers to choose a folder; approving uses the default next to Mainbooks.
        self.assertFalse(p.locator('#library-folder').is_hidden()); self.assertTrue(p.locator('#library-seed').is_hidden())
        p.click('#library-folder'); p.wait_for_selector('#library-folder-dialog[open]')
        self.assertTrue(p.input_value('#library-folder-input').endswith('Library'))
        p.click('#library-folder-confirm'); p.wait_for_selector('#library-seed:not([hidden])')
        self.assertIn('(empty)', p.inner_text('#library-note'))
        # 2. Starter cells need an explicit approval, then appear in the list.
        p.click('#library-seed'); p.wait_for_selector('#library-seed-dialog[open]'); p.click('#library-seed-confirm')
        p.wait_for_function("() => document.querySelectorAll('#library-list li').length === 6", timeout=10000)
        p.fill('#library-search', 'logistic'); p.wait_for_function("() => document.querySelectorAll('#library-list li').length === 1")
        self.assertEqual(p.inner_text('#library-list li b'), 'Train classifier')
        p.fill('#library-search', ''); p.wait_for_function("() => document.querySelectorAll('#library-list li').length === 6")
        # 3. Insert after the selected cell: new node between cell-1 and Finish, linked and current.
        p.locator('.node[data-id="cell-1"] .node-foot').click()
        p.locator('#library-list li[data-name="load-data"] .lib-insert').click()
        p.wait_for_selector('.node[data-id="load-data"]')
        self.assertEqual(p.inner_text('.node[data-id="load-data"] .node-head b'), 'Load data')
        self.assertEqual(p.inner_text('#node-library'), 'load-data v1 · up to date')
        pairs = p.evaluate("() => [...document.querySelectorAll('#edge-layer path.control')].map(x => x.dataset.id)")
        self.assertTrue(any('cell-1' in e and 'load-data' in e or e.startswith('e') for e in pairs))
        self.assertTrue(p.locator('#lib-pull').is_hidden()); self.assertTrue(p.locator('#lib-push').is_hidden())
        # 4. Edit the inserted cell: the link reports "edited here" and offers Push.
        area = p.locator('.node[data-id="load-data"] textarea'); area.click(); area.press('End'); area.type('\nprint("edited")')
        p.wait_for_function("() => document.getElementById('node-library').dataset.state === 'modified'", timeout=5000)
        self.assertFalse(p.locator('#lib-push').is_hidden())
        p.click('#lib-push'); p.wait_for_selector('#library-save-dialog[open]')
        self.assertEqual(p.input_value('#library-name'), 'load-data'); self.assertIn('v2', p.inner_text('#library-save-intro'))
        p.fill('#library-description', 'edited in a test'); p.click('#library-save-confirm')
        p.wait_for_function("() => document.getElementById('node-library').textContent.includes('v2 · up to date')")
        p.wait_for_function("() => [...document.querySelectorAll('#library-list li')].some(li => li.textContent.includes('load-data v2'))")
        # 5. Another graph with v1 is "behind": simulate by inserting from the list after dropping the link to v1 on the server copy.
        self.server.graph_doc['notebook']['cells'][-1]['metadata']['mainaiger']['library']['version'] = 1
        self.server.graph_doc['notebook']['cells'][-1]['metadata']['mainaiger']['library']['source_hash'] = 'ffffffff'
        self.server.graph_doc['notebook']['cells'][-1]['source'] = 'x'  # matches nothing, so it would be "modified"; set hash to match instead
        import local_lab.graph as g
        self.server.graph_doc['notebook']['cells'][-1]['metadata']['mainaiger']['library']['source_hash'] = g.source_hash('x')
        p.reload(); p.wait_for_selector('.node[data-id="load-data"]'); p.locator('.node[data-id="load-data"] .node-foot').click()
        p.wait_for_function("() => document.getElementById('node-library').dataset.state === 'behind'")
        self.assertFalse(p.locator('#lib-pull').is_hidden())
        p.once('dialog', lambda d: d.accept()); p.click('#lib-pull')
        p.wait_for_function("() => document.getElementById('node-library').textContent.includes('v2 · up to date')")
        self.assertIn('print("edited")', p.input_value('.node[data-id="load-data"] textarea'))
        # 6. Drag a library cell onto the canvas: a new unchained node appears where it was dropped.
        before = p.evaluate("() => document.querySelectorAll('.node').length")
        src = p.locator('#library-list li[data-name="report"]'); src.drag_to(p.locator('#viewport'), target_position={'x': 700, 'y': 500})
        p.wait_for_function("n => document.querySelectorAll('.node').length === n + 1", arg=before)
        self.assertIsNotNone(p.query_selector('.node[data-id="report"]'))
        rect = p.evaluate("() => { const r = document.querySelector('.node[data-id=\"report\"]').getBoundingClientRect(); const v = document.getElementById('viewport').getBoundingClientRect(); return {x: r.left - v.left, y: r.top - v.top}; }")
        self.assertLess(abs(rect['x'] + 160 - 700), 60); self.assertLess(abs(rect['y'] + 20 - 500), 60)


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class EnvironmentPanelTests(RealServerCase):
    """The Environment panel: declare packages, review the build, watch it finish, start a session on the image."""
    def test_declare_build_and_session_uses_image(self):
        import subprocess, sys
        from unittest.mock import patch
        from local_lab import environment as env
        p = self.page; ready = set(); self.server.image_ready = lambda tag: tag in ready
        self.assertIn('Base image', p.inner_text('#env-status')); self.assertTrue(p.locator('#env-build').is_hidden())
        # A bad list is refused by the server with the reason; nothing is built.
        p.fill('#env-packages', 'numpy'); p.click('#env-apply'); p.wait_for_function("() => document.getElementById('toast').textContent.includes('pin an exact version')")
        self.assertTrue(p.locator('#env-build').is_hidden())
        # A good list is saved into the Mainbook; the image is not built yet, so Build appears and sessions are refused.
        p.fill('#env-packages', 'scikit-learn==1.7.2\nnumpy==2.3.3'); p.click('#env-apply')
        p.wait_for_function("() => document.getElementById('env-status').dataset.state === 'missing'")
        tag = env.image_tag(['numpy==2.3.3', 'scikit-learn==1.7.2'])
        self.assertIn(tag, p.inner_text('#env-status')); self.assertFalse(p.locator('#env-build').is_hidden())
        self.assertEqual(self.server.graph_doc['notebook']['metadata']['mainaiger']['environment']['packages'], ['numpy==2.3.3', 'scikit-learn==1.7.2'])
        p.click('#session-start'); p.wait_for_selector('#session-dialog[open]')
        self.assertIn('not built yet', p.inner_text('#session-image')); p.click('#session-cancel')
        # Review: the dialog shows the Dockerfile and the exact command; approving starts the (fake) build and streams its log.
        p.click('#env-build'); p.wait_for_selector('#env-dialog[open]')
        self.assertIn('FROM python:', p.inner_text('#env-dockerfile')); self.assertIn('numpy==2.3.3 scikit-learn==1.7.2', p.inner_text('#env-dockerfile'))
        self.assertIn('docker', p.inner_text('#env-command')); self.assertIn(tag, p.inner_text('#env-command'))
        real_popen = subprocess.Popen
        script = "import sys, time; sys.stdin.read(); print('Step 1/2 : FROM python'); sys.stdout.flush(); time.sleep(1.2); print('Successfully tagged'); sys.exit(0)"
        with patch.object(env, 'resolve_docker', return_value=('docker', None, None)), patch.object(subprocess, 'Popen', lambda a, **k: real_popen([sys.executable, '-c', script], **k)):
            p.click('#env-confirm')
            p.wait_for_function("() => document.getElementById('env-status').dataset.state === 'running'")
            p.wait_for_function("() => document.getElementById('env-log').textContent.includes('Step 1/2')")
            p.wait_for_function("() => document.getElementById('env-status').dataset.state === 'ready'", timeout=15000)
        self.assertIn('Successfully tagged', p.inner_text('#env-log')); self.assertTrue(p.locator('#env-build').is_hidden())
        p.click('#session-start'); p.wait_for_selector('#session-dialog[open]')
        self.assertIn(tag, p.inner_text('#session-image')); self.assertNotIn('not built', p.inner_text('#session-image')); p.click('#session-cancel')
        # Clearing the list goes back to the base image.
        p.fill('#env-packages', ''); p.click('#env-apply'); p.wait_for_function("() => document.getElementById('env-status').textContent.includes('Base image')")
        self.assertNotIn('environment', self.server.graph_doc['notebook']['metadata']['mainaiger'])
