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
        cls.browser.close(); cls.pw.stop(); cls.server.terminate()

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
