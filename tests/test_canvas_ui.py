"""Behavioural checks for the graph canvas (local_lab/ui/graph.js) in headless Chromium.

These run only when Playwright and its Chromium are installed (python3 -m playwright
install chromium); otherwise they are skipped so the plain unittest run stays green.
They use tests/perf/stub_server.py, which serves the real UI with a fake /api/graph.
"""
import json, os, subprocess, sys, time, unittest, urllib.request
from unittest.mock import patch

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


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class PanelsOnRealKernelTests(RealServerCase):
    """Resources, Run on, Files and Log panels, plus the starter ML flow, on a real kernel process (no Docker)."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sys
        from local_lab.kernel_session import KernelSession
        from local_lab.runner import KERNEL_DIR
        def factory(settings, state_dir):
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')
        cls.server.kernel_factory = factory; cls.server.kernels.factory = factory

    @classmethod
    def tearDownClass(cls):
        cls.server.kernels.stop_all(); super().tearDownClass()

    def test_sessions_run_on_library_flow_files_and_log(self):
        p = self.page
        # 1. Resources: start "main" and "worker" (2 CPUs) from the panel; main is active and the header says so.
        self.assertTrue(p.locator('#sessions-table').is_hidden())
        p.click('#session-new'); p.wait_for_selector('#session-new-dialog[open]'); p.fill('#session-label', 'main'); p.click('#session-new-confirm')
        p.wait_for_function("() => document.querySelectorAll('#sessions-table tbody tr').length === 1")
        p.wait_for_function("() => document.getElementById('session-state').textContent.includes('\"main\": ready')")
        p.click('#session-new'); p.wait_for_selector('#session-new-dialog[open]'); p.fill('#session-label', 'worker'); p.select_option('#session-cpus', '2'); p.click('#session-new-confirm')
        p.wait_for_function("() => document.querySelectorAll('#sessions-table tbody tr').length === 2")
        rows = p.evaluate("() => [...document.querySelectorAll('#sessions-table tbody tr')].map(r => [...r.children].slice(0, 4).map(td => td.textContent))")
        self.assertEqual(rows, [['main ★', 'this Mac', '0.5', '256m'], ['worker', 'this Mac', '2', '256m']])
        # 1b. Terminal: a command runs inside the chosen session and shows its exit code.
        p.wait_for_function("() => document.getElementById('term-session').options.length === 2")
        p.select_option('#term-session', label='worker'); p.fill('#term-input', 'echo from-worker && exit 5'); p.press('#term-input', 'Enter')
        p.wait_for_function("() => document.getElementById('term-output').textContent.includes('[exit 5]')")
        self.assertIn('from-worker', p.inner_text('#term-output'))
        # 2. Run on: cell-1 names "worker"; the card shows it; the run review lists it; the cell runs there.
        p.locator('.node[data-id="cell-1"] .node-foot').click(); p.wait_for_selector('#node-panel:not([hidden])')
        self.assertEqual(p.evaluate("() => [...document.querySelectorAll('#node-session option')].map(o => o.textContent)"), ['Active session', 'main', 'worker'])
        p.select_option('#node-session', 'worker'); p.wait_for_function("() => document.querySelector('.node[data-id=\"cell-1\"] .node-session').textContent === 'on worker'")
        p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        p.click('#run-all'); p.wait_for_selector('#run-dialog[open]')
        self.assertFalse(p.locator('#run-dialog-sessions').is_hidden()); self.assertIn('runs on "worker"', p.inner_text('#run-dialog-sessions-list'))
        p.click('#run-dialog-confirm'); p.wait_for_function("() => document.getElementById('run-summary').textContent.includes('finished')", timeout=30000)
        p.wait_for_function("() => document.querySelector('.node[data-id=\"cell-1\"] .status').textContent === 'ok'")
        self.assertEqual([v['name'] for v in self.server.kernels.by_label('main')['kernel'].variables()], [], 'main stayed empty')
        # 3. The six starter ML cells, inserted in order after cell-1 (which prints only), run end to end in the active session.
        p.select_option('#node-session', ''); p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        p.click('#library-folder'); p.wait_for_selector('#library-folder-dialog[open]'); p.click('#library-folder-confirm'); p.wait_for_selector('#library-seed:not([hidden])')
        p.click('#library-seed'); p.wait_for_selector('#library-seed-dialog[open]'); p.click('#library-seed-confirm'); p.wait_for_function("() => document.querySelectorAll('#library-list li').length === 6", timeout=10000)
        for name in ['load-data', 'extract-features', 'split-train-test', 'train-classifier', 'evaluate', 'report']:
            p.locator(f'#library-list li[data-name="{name}"] .lib-insert').click(); p.wait_for_selector(f'.node[data-id="{name}"]'); p.wait_for_function(f"() => document.querySelector('.node.selected') && document.querySelector('.node.selected').dataset.id === '{name}'")
        p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        p.click('#run-all'); p.wait_for_selector('#run-dialog[open]')
        self.assertEqual(p.evaluate("() => document.querySelectorAll('#run-dialog-plan li').length"), 7); self.assertTrue(p.locator('#run-dialog-missing').is_hidden(), 'data edges connect the flow; nothing is missing')
        p.click('#run-dialog-confirm'); p.wait_for_function("() => document.getElementById('run-summary').textContent.includes('running')")
        p.wait_for_function("() => /finished|failed|cancelled/.test(document.getElementById('run-summary').textContent)", timeout=60000)
        self.assertIn('accuracy', p.inner_text('.node[data-id="report"] .node-output')); self.assertIn('Classification report', p.inner_text('.node[data-id="report"] .node-output'))
        self.assertEqual(p.evaluate("() => [...document.querySelectorAll('.node.code .status')].map(s => s.textContent)"), ['ok'] * 7)
        # 4. Files: the Library folder is browsable read-only; a cell file previews as text.
        p.wait_for_function("() => document.getElementById('files-root').options.length >= 1")
        p.select_option('#files-root', 'library'); p.wait_for_function("() => document.querySelectorAll('#files-list li[data-path]').length === 6")
        p.locator('#files-list li[data-path="report.cell.json"]').click(); p.wait_for_selector('#file-dialog[open]')
        self.assertIn('"name": "report"', p.inner_text('#file-dialog-text')); self.assertTrue(p.locator('#file-dialog-open').is_hidden()); p.click('#file-dialog-close')
        # 5. Log: the journal shows the story, newest first.
        p.wait_for_function("() => document.querySelectorAll('#log-list li').length > 10")
        kinds = p.evaluate("() => [...document.querySelectorAll('#log-list li')].map(li => li.dataset.kind)")
        self.assertEqual(kinds[0], 'run.finished'); self.assertIn('cell.ok', kinds); self.assertIn('session.start', kinds); self.assertIn('library.insert', kinds)
        # 6. Stop worker from the table; it stays listed as stopped; cells naming it are refused at review time.
        p.once('dialog', lambda d: d.accept()); p.locator('#sessions-table tbody tr:nth-child(2) button[data-act="stop"]').click()
        p.wait_for_function("() => document.querySelector('#sessions-table tbody tr:nth-child(2) .status').textContent === 'stopped'")


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class ModelCellPanelTests(RealServerCase):
    """A cell becomes a model cell from the inspector; it runs on a real kernel with a fake model."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sys
        from local_lab.kernel_session import KernelSession
        from local_lab.runner import KERNEL_DIR
        def factory(settings, state_dir):
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')
        cls.server.kernel_factory = factory; cls.server.kernels.factory = factory
        cls.asked = []
        def fake_ask(cfg, prompt):
            cls.asked.append(prompt); return {'ok': True, 'value': {'action': 'keep', 'confidence': 0.75}, 'raw': '', 'error': None, 'elapsed_ms': 5, 'attempts': 1, 'model': cfg['model']}
        cls.server.ask_model = fake_ask

    @classmethod
    def tearDownClass(cls):
        cls.server.kernels.stop_all(); super().tearDownClass()

    def test_configure_model_cell_and_run(self):
        p = self.page
        # The starter cell prints; a second cell becomes a model cell judging that output; a third uses the answer.
        p.locator('.node[data-id="cell-1"] .node-foot').click(); p.wait_for_selector('#node-panel:not([hidden])')
        p.fill('#node-title', 'Evaluate'); p.wait_for_function("() => document.querySelector('.node[data-id=\"cell-1\"] .node-head b').textContent === 'Evaluate'")
        p.click('#add-cell'); p.wait_for_selector('.node[data-id="cell-2"]')
        area = p.locator('.node[data-id="cell-2"] textarea'); area.fill('Given this output, keep or revert?\n{{output:Evaluate}}')
        p.check('#model-enabled'); p.wait_for_selector('#model-fields:not([hidden])')
        p.fill('#model-name', 'llama3.2:3b'); p.fill('#model-output', 'decision'); p.fill('#model-schema', '{"type":"object","properties":{"action":{"type":"string","enum":["keep","revert"]},"confidence":{"type":"number"}}}')
        p.click('#model-apply'); p.wait_for_function("() => document.querySelector('.node[data-id=\"cell-2\"] .node-model').textContent === 'model llama3.2:3b → decision'")
        p.wait_for_function("() => document.getElementById('model-note').textContent.includes('checked against the shape')")
        self.assertEqual(p.inner_text('#node-outputs'), 'decision', 'the inspector shows the model output as the cell\'s output name')
        p.click('#add-cell'); p.wait_for_selector('.node[data-id="cell-3"]'); p.locator('.node[data-id="cell-3"] textarea').fill('print("decided", decision["action"], decision["confidence"])')
        p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        self.assertTrue(p.evaluate("() => [...document.querySelectorAll('#edge-layer .edge-label')].some(t => t.textContent === 'decision')"), 'a data edge carries the model answer to the next cell')
        p.click('#session-start'); p.wait_for_selector('#session-dialog[open]'); p.click('#session-confirm'); p.wait_for_function("() => document.getElementById('session-state').textContent.includes('ready')")
        p.click('#run-all'); p.wait_for_selector('#run-dialog[open]'); self.assertTrue(p.locator('#run-dialog-missing').is_hidden()); p.click('#run-dialog-confirm')
        p.wait_for_function("() => /finished|failed/.test(document.getElementById('run-summary').textContent)", timeout=30000)
        self.assertEqual(p.evaluate("() => [...document.querySelectorAll('.node.code .status')].map(s => s.textContent)"), ['ok', 'ok', 'ok'])
        self.assertIn('Hello from the graph', self.asked[0]); self.assertNotIn('{{', self.asked[0])
        self.assertEqual(p.inner_text('.node[data-id="cell-3"] .node-output').strip(), 'decided keep 0.75')
        self.assertIn('"action": "keep"', p.inner_text('.node[data-id="cell-2"] .node-output'))
        # Turning the model off makes it a plain code cell again.
        p.click('#arrange'); p.wait_for_timeout(150); p.locator('.node[data-id="cell-2"] .node-foot').click(); p.wait_for_function("() => document.querySelector('.node.selected') && document.querySelector('.node.selected').dataset.id === 'cell-2'")
        p.uncheck('#model-enabled'); p.wait_for_function("() => document.querySelector('.node[data-id=\"cell-2\"] .node-model').textContent === ''")


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class LoopArrowPanelTests(RealServerCase):
    """A loop arrow drawn backwards on the canvas, given a condition and a budget in the inspector, run on a real kernel."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sys
        from local_lab.kernel_session import KernelSession
        from local_lab.runner import KERNEL_DIR
        def factory(settings, state_dir):
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')
        cls.server.kernel_factory = factory; cls.server.kernels.factory = factory

    @classmethod
    def tearDownClass(cls):
        cls.server.kernels.stop_all(); super().tearDownClass()

    def point_on_edge(self, edge_id):
        return self.page.evaluate("""id => { const path = document.querySelector(`#edge-layer path.edge[data-id="${id}"]`); const pt = path.getPointAtLength(path.getTotalLength() * 0.5); const svg = document.getElementById('edges').getBoundingClientRect(); const m = new DOMMatrix(getComputedStyle(document.getElementById('world')).transform); return {x: svg.left + pt.x * m.a, y: svg.top + pt.y * m.d}; }""", edge_id)

    def test_loop_arrow_with_condition_and_budget_then_an_untaken_branch(self):
        p = self.page
        # Three chained cells: init -> bump -> after.
        p.locator('.node[data-id="cell-1"] .node-foot').click(); p.wait_for_selector('#node-panel:not([hidden])'); p.fill('#node-title', 'Init')
        p.locator('.node[data-id="cell-1"] textarea').fill('n = 0')
        p.click('#add-cell'); p.wait_for_selector('.node[data-id="cell-2"]'); p.fill('#node-title', 'Bump'); p.locator('.node[data-id="cell-2"] textarea').fill('n += 1\nprint("n =", n)')
        p.click('#add-cell'); p.wait_for_selector('.node[data-id="cell-3"]'); p.fill('#node-title', 'After'); p.locator('.node[data-id="cell-3"] textarea').fill('print("after", n)')
        p.click('#arrange'); p.click('#fit'); p.wait_for_timeout(150); p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        # Drag After's out port backwards onto Bump: a loop arrow, selected, with its label and panel.
        out = p.evaluate("""() => { const r = document.querySelector('.node[data-id="cell-3"] .port.out').getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }""")
        dest = p.evaluate("""() => { const r = document.querySelector('.node[data-id="cell-2"] .node-foot').getBoundingClientRect(); return {x: r.left + 10, y: r.top + 5}; }""")
        p.mouse.move(out['x'], out['y']); p.mouse.down(); p.mouse.move(dest['x'], dest['y'], steps=8); p.mouse.up(); p.wait_for_timeout(100)
        p.wait_for_selector('#edge-panel:not([hidden])')
        self.assertEqual(p.inner_text('#edge-title'), 'After → Bump (loop arrow)'); self.assertFalse(p.locator('#edge-loop-fields').is_hidden()); self.assertEqual(p.input_value('#edge-max'), '3')
        self.assertTrue(p.evaluate("() => [...document.querySelectorAll('#edge-layer .arrow-label.loop')].some(t => t.textContent === '\u21bb 0/3')"), 'the loop label shows count/budget')
        loop_id = p.evaluate("() => document.querySelector('#edge-layer path.loop').dataset.id")
        self.assertTrue(p.evaluate("id => document.querySelector(`#edge-layer path.edge[data-id='${id}']`).classList.contains('selected')", loop_id))
        p.fill('#edge-when', 'n < 3'); p.fill('#edge-max', '5'); p.click('#edge-apply'); p.wait_for_function("() => document.getElementById('toast').textContent.includes('Arrow saved')")
        edge = next(e for e in self.server.graph_doc['graph']['edges'] if e['id'] == loop_id); self.assertEqual((edge['loop'], edge['max_fires'], edge['when']), (True, 5, 'n < 3'))
        # Run: the review names the loop; the body runs three times; the label counts; the panel explains the last decision.
        p.click('#session-start'); p.wait_for_selector('#session-dialog[open]'); p.click('#session-confirm'); p.wait_for_function("() => document.getElementById('session-state').textContent.includes('ready')")
        p.click('#run-all'); p.wait_for_selector('#run-dialog[open]')
        self.assertEqual(p.inner_text('#run-dialog-arrows-list'), 'Loop After → Bump: runs the body again at most 5 times when n < 3'); p.click('#run-dialog-confirm')
        p.wait_for_function("() => /finished|failed|cancelled/.test(document.getElementById('run-summary').textContent)", timeout=30000)
        self.assertIn('finished', p.inner_text('#run-summary')); self.assertEqual(p.inner_text('.node[data-id="cell-2"] .node-output').strip(), 'n = 3')
        self.assertEqual(p.evaluate("() => [...document.querySelectorAll('#edge-layer .arrow-label.loop')].map(t => t.textContent)"), ['\u21bb 2/5 \u00b7 when n < 3'], 'the label counts the firings and shows the condition')
        self.assertIn('7 steps', p.inner_text('.node.finish .finish-note'))
        self.assertTrue(p.evaluate("id => document.querySelector(`#edge-layer path.edge[data-id='${id}']`).classList.contains('not-fired')", loop_id), 'the last decision on the loop was "not fired"')
        pt = self.point_on_edge(loop_id); p.mouse.click(pt['x'], pt['y']); p.wait_for_selector('#edge-panel:not([hidden])')
        self.assertEqual(p.inner_text('#edge-result'), 'not fired · 2/5 · condition gave False')
        # A condition on a forward arrow that is false leaves the cells behind it idle.
        first = next(e['id'] for e in self.server.graph_doc['graph']['edges'] if e['source'] == 'cell-1' and e['target'] == 'cell-2')
        pt = self.point_on_edge(first); p.mouse.click(pt['x'], pt['y']); p.wait_for_function("() => document.getElementById('edge-title').textContent === 'Init → Bump'")
        p.fill('#edge-when', 'n > 100'); p.click('#edge-apply'); p.wait_for_function("() => document.getElementById('toast').textContent.includes('Arrow saved')")
        self.assertTrue(p.evaluate("() => [...document.querySelectorAll('#edge-layer .arrow-label')].some(t => t.textContent === 'when n > 100')"))
        p.click('#run-all'); p.wait_for_selector('#run-dialog[open]'); self.assertIn('Init → Bump fires only when n > 100', p.inner_text('#run-dialog-arrows-list')); p.click('#run-dialog-confirm')
        p.wait_for_function("() => document.querySelector('.node.finish .finish-note').textContent.includes('idle')", timeout=30000)
        self.assertEqual(p.evaluate("() => [...document.querySelectorAll('.node.code .status')].map(s => s.textContent)"), ['ok', 'idle', 'idle'])
        self.assertTrue(p.evaluate("id => document.querySelector(`#edge-layer path.edge[data-id='${id}']`).classList.contains('not-fired')", first))
        self.assertIn('1/3 ok · 2 idle', p.inner_text('.node.finish .finish-note'))
        kinds = p.evaluate("() => [...document.querySelectorAll('#log-list li')].map(li => li.dataset.kind)"); self.assertIn('arrow.not_fired', kinds); self.assertIn('arrow.fired', kinds)


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class StartEventsPanelTests(RealServerCase):
    """Sources on Start, the arm review, Fire now, and a fired run showing up on the canvas; a cell edit disarms."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sys
        from local_lab.kernel_session import KernelSession
        from local_lab.runner import KERNEL_DIR
        def factory(settings, state_dir):
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')
        cls.server.kernel_factory = factory; cls.server.kernels.factory = factory

    @classmethod
    def tearDownClass(cls):
        cls.server.kernels.stop_all(); super().tearDownClass()

    def test_add_sources_arm_fire_and_disarm_on_edit(self):
        p = self.page
        self.assertTrue(p.locator('#events-arm').is_disabled(), 'nothing to arm yet')
        p.locator('.node[data-id="cell-1"] textarea').fill('print("fired by", event["kind"], "at", round(event["at"]) > 0)')
        p.select_option('#event-kind', 'timer'); p.fill('#event-arg', '5'); p.click('#event-add'); p.wait_for_function("() => document.getElementById('toast').textContent.includes('10 to 86400')")
        p.fill('#event-arg', '600'); p.click('#event-add'); p.wait_for_function("() => document.querySelectorAll('#events-list li').length === 1")
        p.select_option('#event-kind', 'manual'); self.assertTrue(p.locator('#event-arg').is_hidden()); p.click('#event-add'); p.wait_for_function("() => document.querySelectorAll('#events-list li').length === 2")
        self.assertEqual(p.evaluate("() => [...document.querySelectorAll('#events-list li span')].map(e => e.textContent)"), ['Timer: every 600 s', 'The Fire now button'])
        p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        self.assertEqual(self.server.graph_doc['graph']['nodes'][0]['events'], [{'kind': 'timer', 'every': 600}, {'kind': 'manual'}])
        p.wait_for_function("() => !document.getElementById('events-arm').disabled")
        # Arm: the dialog says exactly what may fire the Mainbook and the budget.
        p.click('#events-arm'); p.wait_for_selector('#events-dialog[open]')
        self.assertEqual(p.evaluate("() => [...document.querySelectorAll('#events-dialog-list li')].map(e => e.textContent)"), ['Every 10 min while armed (bound as now)', 'When you press Fire now (payload bound as event)'])
        self.assertIn('at most 500 cell executions', p.inner_text('#events-dialog-budget')); p.click('#events-dialog-confirm')
        p.wait_for_function("() => document.getElementById('events-state').textContent.startsWith('Armed: every 600 s, Fire now')")
        self.assertEqual(p.inner_text('.node.start .start-note'), '\u26a1 armed: every 600 s, Fire now'); self.assertFalse(p.locator('#events-fire').is_hidden())
        # Fire now without a session: dropped and said so; with a session: the run appears on the canvas.
        p.click('#events-fire'); p.wait_for_function("() => document.getElementById('events-recent').textContent.includes('dropped: Start a session')")
        p.click('#session-start'); p.wait_for_selector('#session-dialog[open]'); p.click('#session-confirm'); p.wait_for_function("() => document.getElementById('session-state').textContent.includes('ready')")
        p.click('#events-fire'); p.wait_for_function("() => document.querySelector('.node[data-id=\"cell-1\"] .node-output').textContent.includes('fired by manual at True')", timeout=15000)
        p.wait_for_function("() => document.getElementById('run-summary').textContent.includes('finished')")
        p.wait_for_function("() => document.getElementById('events-state').textContent.includes('fired 1 time')")
        kinds = p.evaluate("() => [...document.querySelectorAll('#log-list li')].map(li => li.dataset.kind)"); self.assertIn('event.fired', kinds); self.assertIn('events.arm', kinds)
        # Editing a cell disarms by itself; the panel and the Start card say so.
        p.locator('.node[data-id="cell-1"] textarea').fill('print("edited")'); p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        p.wait_for_function("() => document.getElementById('events-state').textContent.startsWith('Not armed')", timeout=10000)
        self.assertEqual(p.inner_text('.node.start .start-note'), ''); self.assertFalse(self.server.events.status()['armed'])


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class NestedMainbookPanelTests(RealServerCase):
    """A cell is turned into "runs a saved Mainbook" from the inspector; the run shows the inner summary on the card."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sys
        from local_lab import graph as g, mainbooks
        from local_lab.kernel_session import KernelSession
        from local_lab.runner import KERNEL_DIR
        def factory(settings, state_dir):
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')
        cls.server.kernel_factory = factory; cls.server.kernels.factory = factory
        mainbooks.configure(cls.server.state_dir, str(cls.root / 'books'))
        inner = g.new_document(); inner['notebook']['cells'][0]['source'] = 'total = base * 2\nprint("inner", total)'; inner['notebook']['metadata']['mainaiger'] = {'name': 'double'}
        mainbooks.save(cls.server.state_dir, 'double', g.validate(inner))

    @classmethod
    def tearDownClass(cls):
        cls.server.kernels.stop_all(); super().tearDownClass()

    def test_memory_cell_from_the_inspector_searches_real_notes(self):
        from local_lab import graph as g, memory
        p = self.page
        memory.configure(self.server.state_dir, str(self.root / 'Documents' / 'MainAIger Lab' / 'Memory')); memory.create(self.server.state_dir, 'Haptic glove setup', 'Calibrate each finger.', status_='stable')
        self.server.set_graph(g.new_document()); p.goto(self.origin + '/graph'); p.wait_for_selector('.node.code'); p.wait_for_timeout(300)  # a fresh document (the tests share one server)
        p.locator('.node[data-id="cell-1"] textarea').fill('question = "haptic glove"'); p.locator('.node[data-id="cell-1"] .node-foot').click(); p.wait_for_selector('#node-panel:not([hidden])')
        before = set(p.evaluate("() => [...document.querySelectorAll('.node.code')].map(n => n.dataset.id)"))
        p.click('#add-cell'); p.wait_for_function("n => document.querySelectorAll('.node.code').length === n + 1", arg=len(before))
        mem_id = p.evaluate("() => document.querySelector('.node.selected').dataset.id"); self.assertNotIn(mem_id, before)
        p.check('#mem-enabled'); p.wait_for_selector('#mem-fields:not([hidden])'); p.select_option('#mem-op', 'search'); self.assertEqual(p.input_value('#mem-output'), 'hits'); p.click('#mem-apply')
        p.wait_for_function("id => document.querySelector(`.node[data-id='${id}'] .node-model`).textContent === 'memory search → hits'", arg=mem_id); p.wait_for_function("() => document.getElementById('mem-note').textContent.includes('Reads question')")
        p.click('#add-cell'); p.wait_for_function("n => document.querySelectorAll('.node.code').length === n + 2", arg=len(before))
        last_id = p.evaluate("() => document.querySelector('.node.selected').dataset.id"); p.locator(f'.node[data-id="{last_id}"] textarea').fill('print(hits[0]["title"], hits[0]["status"])')
        p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        if not p.locator('#session-start').is_disabled(): p.click('#session-start'); p.wait_for_selector('#session-dialog[open]'); p.click('#session-confirm')
        p.wait_for_function("() => document.getElementById('session-state').textContent.includes('ready')")
        p.click('#run-all'); p.wait_for_selector('#run-dialog[open]'); self.assertIn('uses Memory: search', p.inner_text('#run-dialog-arrows-list')); p.click('#run-dialog-confirm')
        p.wait_for_function("id => document.querySelector(`.node[data-id='${id}'] .node-output`).textContent.includes('Haptic glove setup stable')", arg=last_id, timeout=30000)
        self.assertEqual(p.inner_text(f'.node[data-id="{mem_id}"] .status'), 'ok')

    def test_cell_runs_a_saved_mainbook(self):
        p = self.page
        p.locator('.node[data-id="cell-1"] textarea').fill('base = 21'); p.locator('.node[data-id="cell-1"] .node-foot').click(); p.wait_for_selector('#node-panel:not([hidden])')
        p.click('#add-cell'); p.wait_for_selector('.node[data-id="cell-2"]'); p.wait_for_function("() => document.querySelector('.node.selected') && document.querySelector('.node.selected').dataset.id === 'cell-2'")
        p.check('#book-enabled'); p.wait_for_selector('#book-fields:not([hidden])'); p.wait_for_function("() => [...document.querySelectorAll('#book-name option')].some(o => o.value === 'double')")
        p.select_option('#book-name', 'double'); p.click('#book-apply')
        p.wait_for_function("() => document.querySelector('.node[data-id=\"cell-2\"] .node-model').textContent.includes('Mainbook: double')")
        p.wait_for_function("() => document.getElementById('book-note').textContent.includes('Runs \"double\" (1 cells)')")
        self.assertIn('reads base and defines total', p.inner_text('#book-note')); self.assertEqual(p.inner_text('.node[data-id="cell-2"] .node-head b'), 'double')
        self.assertTrue(p.locator('.node[data-id="cell-2"] textarea').is_hidden(), 'the code box is hidden: the Mainbook is the code')
        p.click('#add-cell'); p.wait_for_selector('.node[data-id="cell-3"]'); p.locator('.node[data-id="cell-3"] textarea').fill('print("outer", total + 1)')
        p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        self.assertTrue(p.evaluate("() => [...document.querySelectorAll('#edge-layer .edge-label')].some(t => t.textContent === 'total')"), 'a data edge flows out of the nested Mainbook')
        if not p.locator('#session-start').is_disabled(): p.click('#session-start'); p.wait_for_selector('#session-dialog[open]'); p.click('#session-confirm')
        p.wait_for_function("() => document.getElementById('session-state').textContent.includes('ready')")
        p.click('#run-all'); p.wait_for_selector('#run-dialog[open]'); self.assertIn('runs the Mainbook "double" (1 cell, in this session)', p.inner_text('#run-dialog-arrows-list')); p.click('#run-dialog-confirm')
        p.wait_for_function("() => /finished|failed/.test(document.getElementById('run-summary').textContent)", timeout=30000)
        self.assertEqual(p.evaluate("() => [...document.querySelectorAll('.node.code .status')].map(s => s.textContent)"), ['ok', 'ok', 'ok'])
        out = p.inner_text('.node[data-id="cell-2"] .node-output'); self.assertIn('Mainbook "double": finished · 1/1 ok', out); self.assertIn('inner 42', out)
        self.assertEqual(p.inner_text('.node[data-id="cell-3"] .node-output').strip(), 'outer 43')
        kinds = p.evaluate("() => [...document.querySelectorAll('#log-list li')].map(li => li.textContent)"); self.assertTrue(any('double / cell-1: ok' in k for k in kinds), 'inner cells are in the Log under the Mainbook name')


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class MemoryPageTests(RealServerCase):
    """The Memory page: approve a folder, write notes that link, search, grow a note, mark it stable, see the link graph."""
    def test_folder_notes_links_search_grow_and_graph(self):
        p = self.page
        p.goto(self.origin + '/memory'); p.wait_for_selector('#memory-main:not([hidden])'); p.wait_for_function("() => document.getElementById('memory-stats').textContent !== 'Reading…'")
        if 'No Memory folder' in p.inner_text('#memory-stats'):  # the tests share one server; another test may have approved the folder already
            self.assertTrue(p.locator('#note-new').is_disabled())
            p.click('#memory-choose'); p.wait_for_selector('#folder-dialog[open]'); self.assertTrue(p.input_value('#folder-input').endswith('MainAIger Lab/Memory')); p.click('#folder-confirm')
            p.wait_for_function("() => document.getElementById('memory-stats').textContent.startsWith('0 notes')")
        self.assertTrue((self.root / 'Documents' / 'MainAIger Lab' / 'Memory' / 'notes').is_dir())
        # Two notes that link to each other.
        p.click('#note-new'); p.wait_for_selector('#new-dialog[open]'); p.fill('#new-title', 'Quantum kernels'); p.select_option('#new-kind', 'fact'); p.fill('#new-tags', 'quantum, ml'); p.fill('#new-body', 'Kernel methods map features into Hilbert space. Used by [[Fraud idea]].'); p.click('#new-confirm')
        p.wait_for_function("() => document.getElementById('note-meta').textContent.includes('notes/quantum-kernels.md')"); self.assertEqual(p.input_value('#note-title'), 'Quantum kernels')
        p.click('#note-new'); p.wait_for_selector('#new-dialog[open]'); p.fill('#new-title', 'Fraud idea'); p.select_option('#new-kind', 'idea'); p.fill('#new-body', 'Try [[Quantum kernels]] on the fraud data.'); p.click('#new-confirm')
        p.wait_for_function("() => document.getElementById('note-meta').textContent.includes('notes/fraud-idea.md')")
        p.wait_for_function("() => [...document.querySelectorAll('#note-list li b')].filter(b => ['Quantum kernels', 'Fraud idea'].includes(b.textContent)).length === 2")
        # Links open the other note; search finds by words; a dated section grows a draft into growing; mark stable.
        p.locator('#note-links a[data-link="Quantum kernels"]').click(); p.wait_for_function("() => document.getElementById('note-title').value === 'Quantum kernels'")
        p.fill('#memory-search', 'hilbert'); p.wait_for_function("() => document.querySelectorAll('#note-list li').length === 1 && document.querySelector('#note-list li mark')")
        self.assertIn('Hilbert', p.inner_text('#note-list li .snippet')); p.fill('#memory-search', ''); p.wait_for_function("() => document.querySelectorAll('#note-list li').length >= 2")
        p.locator('#note-list li[data-path="notes/fraud-idea.md"]').click(); p.wait_for_function("() => document.getElementById('note-title').value === 'Fraud idea'")
        self.assertEqual(p.input_value('#note-status'), 'draft')
        p.click('text=Add a dated section'); p.fill('#note-add-heading', 'Asked again'); p.fill('#note-add-text', 'Also try amplitude encoding.'); p.click('#note-add')
        p.wait_for_function("() => document.getElementById('note-status').value === 'growing'"); self.assertIn('## Asked again', p.input_value('#note-body'))
        p.click('#note-stable'); p.wait_for_function("() => document.getElementById('note-status').value === 'stable' && document.getElementById('note-result').textContent === 'Saved.'")
        self.assertIn('status: stable', (self.root / 'Documents' / 'MainAIger Lab' / 'Memory' / 'notes' / 'fraud-idea.md').read_text())
        p.select_option('#filter-status', 'stable'); p.wait_for_function("() => [...document.querySelectorAll('#note-list li b')].some(b => b.textContent === 'Fraud idea') && ![...document.querySelectorAll('#note-list li b')].some(b => b.textContent === 'Quantum kernels')"); p.select_option('#filter-status', '')
        # The graph view draws the notes with their links.
        p.click('#view-graph'); p.wait_for_function("() => document.querySelectorAll('.mnode').length >= 2 && document.querySelectorAll('#edge-layer path').length >= 2")
        p.locator('.mnode[data-path="notes/quantum-kernels.md"]').click(); p.wait_for_function("() => document.getElementById('note-title').value === 'Quantum kernels'")
        p.click('#view-list'); p.click('#tier-chats'); p.wait_for_function("() => document.getElementById('list-note').textContent.includes('chats') || document.getElementById('list-note').textContent.includes('No chats')")
        kinds = [e['kind'] for e in self.server.journal.tail(0, 200)]; self.assertIn('memory.create', kinds); self.assertIn('memory.folder', kinds); self.assertIn('memory.append', kinds)

    def test_idea_box_drafts_reviews_saves_and_grows(self):
        from local_lab import ideas
        p = self.page
        p.goto(self.origin + '/memory'); p.wait_for_selector('#memory-main:not([hidden])')
        if 'No Memory folder' in p.inner_text('#memory-stats'):
            p.click('#memory-choose'); p.wait_for_selector('#folder-dialog[open]'); p.click('#folder-confirm'); p.wait_for_function("() => !document.getElementById('memory-stats').textContent.includes('No Memory folder')")
        self.assertTrue(p.locator('#idea-draft').is_disabled())
        # No model is running in this test: the draft comes from the words and says so; the card is editable before saving.
        with patch.object(ideas.models, 'installed_models', return_value=[]):
            p.fill('#idea-text', 'I want MainAIger to export my ChatGPT chats and keep the useful parts as memory'); p.click('#idea-draft'); p.wait_for_selector('#idea-dialog[open]')
            self.assertIn('No local model', p.inner_text('#idea-dialog-meta')); self.assertEqual(p.input_value('#idea-title'), 'I want MainAIger to export my ChatGPT chats and keep the useful parts as memory'); self.assertTrue(p.locator('#idea-similar').is_hidden())
            p.fill('#idea-title', 'Chat export into memory'); p.fill('#idea-tags', 'memory, chats'); p.fill('#idea-tasks', 'Parse the export zip\nShow what to keep'); p.click('#idea-confirm')
            p.wait_for_function("() => document.getElementById('note-title').value === 'Chat export into memory'"); self.assertEqual(p.input_value('#note-kind'), 'idea'); self.assertIn('- [ ] Parse the export zip', p.input_value('#note-body')); self.assertEqual(p.input_value('#note-tags'), 'memory, chats')
            # Asking about it again offers the existing idea; choosing it grows the note instead of making another.
            p.fill('#idea-text', 'Also export my Claude chats into memory'); p.click('#idea-draft'); p.wait_for_selector('#idea-dialog[open]'); p.wait_for_selector('#idea-similar:not([hidden])')
            self.assertIn('Add to "Chat export into memory"', p.inner_text('#idea-similar-list')); p.locator('#idea-similar-list input[type=radio]').first.check(); p.click('#idea-confirm')
            p.wait_for_function("() => document.getElementById('note-status').value === 'growing'"); self.assertIn('Claude chats', p.input_value('#note-body'))
            self.assertEqual(len([n for n in self.server.journal.tail(0, 300) if n['kind'] == 'memory.idea']), 1, 'one idea note, grown once')

    def test_import_an_export_and_review_a_chat_into_memory(self):
        import zipfile
        from local_lab import memory_import
        from tests.test_memory_import import chatgpt_rows
        p = self.page
        p.goto(self.origin + '/memory'); p.wait_for_selector('#memory-main:not([hidden])')
        if 'No Memory folder' in p.inner_text('#memory-stats'):
            p.click('#memory-choose'); p.wait_for_selector('#folder-dialog[open]'); p.click('#folder-confirm'); p.wait_for_function("() => !document.getElementById('memory-stats').textContent.includes('No Memory folder')")
        z = self.root / 'chatgpt-export.zip'
        with zipfile.ZipFile(z, 'w') as a: a.writestr('conversations.json', json.dumps(chatgpt_rows()))
        p.wait_for_function("() => !document.getElementById('import-open').disabled"); p.click('#import-open'); p.wait_for_selector('#import-dialog[open]')
        p.fill('#import-path', str(z)); p.click('#import-inspect'); p.wait_for_function("() => document.getElementById('import-summary').textContent.includes('ChatGPT export: 2 conversations')")
        self.assertEqual(p.inner_text('#import-titles'), 'Fraud model talk\nCode chat'); p.click('#import-confirm')
        p.wait_for_function("() => document.getElementById('import-result').textContent.includes('Imported 2')"); p.wait_for_function("() => document.querySelectorAll('#note-list li').length === 2")
        p.wait_for_function("() => document.getElementById('review-state').textContent.includes('2 to go')")
        p.locator('#note-list li[data-path="chats/chatgpt/fraud-model-talk.md"]').click(); p.wait_for_function("() => document.getElementById('note-meta').textContent.includes('read-only')"); self.assertTrue(p.locator('#note-body').is_disabled())
        # Review with a scripted model: one item proposed; edit its title, keep it; the note links to the chat.
        def chat(model, messages, timeout=300, format=None):  # what Ollama would answer, in the shape decide.ask asks for
            return {'ok': True, 'model': model, 'reply': json.dumps({'items': [{'kind': 'fact', 'title': 'Quantum kernel fraud approach', 'text': 'Feature map, then an SVM.', 'tags': ['quantum'], 'messages': [2]}]}), 'elapsed_ms': 30, 'tokens': {'prompt': 50, 'reply': 40}}
        with patch.object(type(self.server), 'chat', lambda self_, *a, **k: chat(*a, **k)), patch.object(memory_import.models, 'installed_models', return_value=[{'name': 'fake:1b'}]):
            p.click('#review-next'); p.wait_for_selector('#review-dialog[open]')
            self.assertIn('1 proposal by fake:1b', p.inner_text('#review-meta')); self.assertEqual(p.input_value('#review-items .review-item .title'), 'Quantum kernel fraud approach')
            p.fill('#review-items .review-item .title', 'Quantum kernels for fraud'); p.click('#review-keep')
            p.wait_for_function("() => document.getElementById('review-state').textContent.includes('1 reviewed') && document.getElementById('review-state').textContent.includes('1 item kept')")
        p.click('#tier-notes'); p.wait_for_function("() => [...document.querySelectorAll('#note-list li b')].some(b => b.textContent === 'Quantum kernels for fraud')")
        p.locator('#note-list li b:has-text("Quantum kernels for fraud")').click(); p.wait_for_function("() => document.getElementById('note-title').value === 'Quantum kernels for fraud'")
        self.assertIn('chat: chats/chatgpt/fraud-model-talk.md#2', p.inner_text('#note-sources')); self.assertIn('[[Fraud model talk]]', p.inner_text('#note-links'))
        # Skip the remaining chat; nothing left to review.
        with patch.object(memory_import.models, 'installed_models', return_value=[]):
            p.click('#review-next'); p.wait_for_selector('#review-dialog[open]'); self.assertIn('No local model', p.inner_text('#review-meta')); p.click('#review-skip')
        p.wait_for_function("() => document.getElementById('review-state').textContent.includes('0 to go')"); self.assertTrue(p.locator('#review-next').is_disabled())

    def test_ask_shows_memory_hits_two_costs_and_answers(self):
        from local_lab import ask as ask_module, memory
        p = self.page
        p.goto(self.origin + '/memory'); p.wait_for_selector('#memory-main:not([hidden])')
        if 'No Memory folder' in p.inner_text('#memory-stats'):
            p.click('#memory-choose'); p.wait_for_selector('#folder-dialog[open]'); p.click('#folder-confirm'); p.wait_for_function("() => !document.getElementById('memory-stats').textContent.includes('No Memory folder')")
        memory.create(self.server.state_dir, 'Haptic glove setup', 'Calibrate each finger, then run the demo scene.', status_='stable', tags=['haptics'])
        p.fill('#costs-watts', '30'); p.click('#costs-save'); p.wait_for_function("() => document.getElementById('toast').textContent.includes('Device draw saved')")
        p.fill('#ask-text', 'how do I set up the haptic glove?')
        with patch.object(ask_module.models, 'installed_models', return_value=[{'name': 'fake:1b'}]):
            p.press('#ask-text', 'Enter'); p.wait_for_selector('#ask-result:not([hidden])')
            self.assertIn('Haptic glove setup', p.inner_text('#ask-hits')); self.assertIn('0 tokens · instant', p.inner_text('#cost-reuse-text')); self.assertIn('Wh (approx.)', p.inner_text('#cost-regen-text')); self.assertIn('default rates', p.inner_text('#cost-regen-text'))
            self.assertIn('Recommended: use memory', p.inner_text('#ask-recommend')); self.assertTrue(p.evaluate("() => document.getElementById('cost-reuse').classList.contains('recommended')"))
            p.click('#ask-reuse'); p.wait_for_selector('#ask-answer:not([hidden])'); self.assertIn('Calibrate each finger', p.inner_text('#ask-answer-text')); self.assertIn('no model ran', p.inner_text('#ask-answer-meta'))
            # Ask the model instead: a scripted chat answers; the answer is saved as a draft note with the note used as a source.
            def chat(model, messages, timeout=300, format=None):
                return {'ok': True, 'model': model, 'reply': 'Calibrate each finger; run the demo scene.', 'elapsed_ms': 800, 'tokens': {'prompt': 120, 'reply': 12}}
            with patch.object(type(self.server), 'chat', lambda self_, *a, **k: chat(*a, **k)):
                p.click('#ask-model'); p.wait_for_function("() => document.getElementById('ask-answer-meta').textContent.includes('saved as a draft answer note')")
            self.assertIn('fake:1b · 0.8 s', p.inner_text('#ask-answer-meta')); self.assertIn('12 reply tokens', p.inner_text('#ask-answer-meta')); self.assertIn('Wh', p.inner_text('#ask-answer-meta'))
            p.wait_for_function("() => document.getElementById('note-title').value.startsWith('Q: how do I set up the haptic glove')"); self.assertEqual(p.input_value('#note-kind'), 'answer'); self.assertIn('note: notes/haptic-glove-setup.md', p.inner_text('#note-sources'))
        self.assertIn('fake:1b:', p.inner_text('#costs-summary'))

    def test_mainbook_note_runs_through_the_graph_page(self):
        from local_lab import graph as g, mainbooks, memory
        p = self.page
        mainbooks.configure(self.server.state_dir, str(self.root / 'books'))
        doc = g.new_document(); doc['notebook']['cells'][0]['source'] = 'print("from memory")'; doc['notebook']['metadata']['mainaiger'] = {'name': 'hello loop'}; mainbooks.save(self.server.state_dir, 'hello loop', g.validate(doc))
        memory.configure(self.server.state_dir, str(self.root / 'Documents' / 'MainAIger Lab' / 'Memory'))
        note = memory.create(self.server.state_dir, 'Say hello', 'Runs the hello loop.', kind='mainbook', extra={'mainbook': 'hello loop'})
        p.goto(self.origin + '/memory'); p.wait_for_selector('#memory-main:not([hidden])'); p.wait_for_function("() => document.querySelectorAll('#note-list li').length > 0")
        p.locator(f'#note-list li[data-path="{note["path"]}"]').click(); p.wait_for_function("() => document.getElementById('note-title').value === 'Say hello'")
        self.assertFalse(p.locator('#note-mainbook-row').is_hidden()); self.assertEqual(p.input_value('#note-mainbook'), 'hello loop')
        p.click('#note-run'); p.wait_for_selector('#run-dialog[open]', timeout=15000)
        self.assertEqual(p.url, self.origin + '/graph'); self.assertEqual(p.input_value('.node[data-id="cell-1"] textarea'), 'print("from memory")'); self.assertIn('hello loop', p.inner_text('#save-state'))
        p.click('#run-dialog-cancel')


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class FormCellPanelTests(RealServerCase):
    """A form on a card: define fields in the inspector, fill them on the card, run with those values."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sys
        from local_lab.kernel_session import KernelSession
        from local_lab.runner import KERNEL_DIR
        def factory(settings, state_dir):
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')
        cls.server.kernel_factory = factory; cls.server.kernels.factory = factory

    @classmethod
    def tearDownClass(cls):
        cls.server.kernels.stop_all(); super().tearDownClass()

    def test_define_fill_and_run_form(self):
        p = self.page
        p.locator('.node[data-id="cell-1"] .node-foot').click(); p.wait_for_selector('#node-panel:not([hidden])')
        p.locator('.node[data-id="cell-1"] textarea').fill('print("hello", name, times * 2, speed, loud)')
        p.check('#form-enabled'); p.wait_for_selector('#form-fields:not([hidden])')
        p.fill('#form-spec', 'bad line here'); p.click('#form-apply'); p.wait_for_function("() => document.getElementById('form-note').textContent.includes('Cannot read')")
        p.fill('#form-spec', 'name: text = world\ntimes: integer = 2\nspeed: choice = fast | slow\nloud: flag'); p.click('#form-apply')
        p.wait_for_function("() => document.getElementById('form-note').textContent.includes('4 fields')")
        self.assertFalse(p.locator('.node[data-id="cell-1"] .node-form').is_hidden())
        self.assertEqual(p.input_value('#form-cell-1-name'), 'world'); self.assertEqual(p.input_value('#form-cell-1-times'), '2')
        # Fill the card: a new name, a choice and the flag; values are saved in the Mainbook.
        p.fill('#form-cell-1-name', 'Tanay'); p.select_option('#form-cell-1-speed', 'slow'); p.check('#form-cell-1-loud'); p.fill('#form-cell-1-times', '5'); p.press('#form-cell-1-times', 'Tab')
        import time as _t
        for _ in range(100):
            if self.server.graph_doc['notebook']['cells'][0]['metadata']['mainaiger'].get('form', {}).get('values', {}).get('times') == 5: break
            _t.sleep(0.05)
        self.assertEqual(self.server.graph_doc['notebook']['cells'][0]['metadata']['mainaiger']['form']['values'], {'name': 'Tanay', 'speed': 'slow', 'loud': True, 'times': 5})
        self.assertEqual(p.inner_text('#node-outputs'), 'loud\nname\nspeed\ntimes', 'the inspector lists the fields as outputs')
        p.click('#session-start'); p.wait_for_selector('#session-dialog[open]'); p.click('#session-confirm'); p.wait_for_function("() => document.getElementById('session-state').textContent.includes('ready')")
        p.locator('.node[data-id="cell-1"] .form-run').click(); p.wait_for_selector('#run-dialog[open]'); self.assertIn('selected cell and everything it triggers', p.inner_text('#run-dialog-intro')); p.click('#run-dialog-confirm')
        p.wait_for_function("() => /finished|failed/.test(document.getElementById('run-summary').textContent)", timeout=30000)
        out = p.inner_text('.node[data-id="cell-1"] .node-output'); self.assertEqual(out.splitlines()[0], 'hello Tanay 10 slow True'); self.assertIn('Form values bound', out)
        # Reload: the values persist on the card.
        p.reload(); p.wait_for_selector('.node[data-id="cell-1"] .node-form:not([hidden])'); self.assertEqual(p.input_value('#form-cell-1-name'), 'Tanay'); self.assertTrue(p.is_checked('#form-cell-1-loud'))


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class DevicesPageTests(RealServerCase):
    """Pair a second in-process Lab on the Devices page, start a session there, see it in Resources, run a cell on it."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import os, sys, threading
        from local_lab.kernel_session import KernelSession
        from local_lab.runner import KERNEL_DIR
        from local_lab.server import LabServer, initialize_state
        def factory(settings, state_dir):
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')
        cls.server.kernel_factory = factory; cls.server.kernels.factory = factory
        rstate, rkey = initialize_state(cls.root / 'remote-private')
        cls.remote = LabServer(('127.0.0.1', 0), rstate, rkey, kernel_factory=factory); threading.Thread(target=cls.remote.serve_forever, daemon=True).start()
        cls.key_file = cls.root / 'nexus.key'; cls.key_file.write_text(rkey); os.chmod(cls.key_file, 0o600)

    @classmethod
    def tearDownClass(cls):
        cls.server.kernels.stop_all(); cls.remote.kernels.stop_all(); cls.remote.shutdown(); cls.remote.server_close(); super().tearDownClass()

    def test_pair_connect_session_and_run_on_device(self):
        p = self.page
        p.goto(self.origin + '/devices'); p.wait_for_selector('#devices-main:not([hidden])'); p.wait_for_function("() => document.getElementById('paired-state').textContent === 'None'")
        self.assertIn('python3 -m local_lab', p.text_content('#install-output'))
        p.click('#pair'); p.wait_for_selector('#pair-dialog[open]')
        p.fill('#pair-name', 'nexus'); p.fill('#pair-host', 'nexus'); p.fill('#pair-port', str(self.remote.server_port)); p.fill('#pair-key', '/nope.key'); p.click('#pair-confirm')
        p.wait_for_function("() => document.getElementById('pair-note').textContent.includes('plain file')")
        p.fill('#pair-key', str(self.key_file)); p.select_option('#pair-tunnel', 'manual'); p.click('#pair-confirm')
        p.wait_for_function("() => document.querySelectorAll('#paired tbody tr').length === 1")
        self.assertIn('not connected', p.inner_text('#paired tbody tr'))
        p.locator('#paired button[data-act="connect"]').click(); p.wait_for_selector('#connect-dialog[open]'); self.assertIn('manual forward', p.inner_text('#connect-command')); p.click('#connect-confirm')
        p.wait_for_function("() => document.querySelector('#paired tbody tr').textContent.includes('connected')")
        p.locator('#paired button[data-act="session"]').click(); p.wait_for_selector('#remote-session-dialog[open]'); p.fill('#remote-session-label', 'main'); p.click('#remote-session-confirm')
        p.wait_for_function("() => document.getElementById('paired-note').textContent.includes('nexus/main')")
        # The Graph page shows the device session in Resources; a cell can run on it.
        p.goto(self.origin + '/graph'); p.wait_for_selector('.node.code'); p.wait_for_function("() => document.querySelectorAll('#sessions-table tbody tr').length === 1")
        row = p.evaluate("() => [...document.querySelector('#sessions-table tbody tr').children].slice(0, 2).map(td => td.textContent)"); self.assertEqual(row, ['nexus/main', 'nexus'], 'a device session is listed but never made active by itself')
        p.locator('.node[data-id="cell-1"] .node-foot').click(); p.wait_for_selector('#node-panel:not([hidden])')
        self.assertIn('nexus/main', p.evaluate("() => [...document.querySelectorAll('#node-session option')].map(o => o.value)"))
        p.select_option('#node-session', 'nexus/main'); p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        p.click('#run-all')
        try: p.wait_for_selector('#run-dialog[open]', timeout=8000)
        except Exception: self.fail('run review did not open: toast=%r save=%r' % (p.inner_text('#toast'), p.inner_text('#save-state')))
        self.assertIn('runs on "nexus/main"', p.inner_text('#run-dialog-sessions-list')); p.click('#run-dialog-confirm')
        p.wait_for_function("() => /finished|failed/.test(document.getElementById('run-summary').textContent)", timeout=30000)
        self.assertEqual(p.inner_text('.node[data-id="cell-1"] .status'), 'ok'); self.assertEqual(len(self.remote.kernels.list()), 1); self.assertEqual(self.remote.kernels.list()[0]['count'], 1, 'the cell ran on the remote Lab')
        # Terminal on the device session.
        p.wait_for_function("() => document.getElementById('term-session').options.length >= 1"); p.fill('#term-input', 'echo via-device'); p.press('#term-input', 'Enter')
        p.wait_for_function("() => document.getElementById('term-output').textContent.includes('via-device')")


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class MapPageTests(RealServerCase):
    """The code map of the Lab itself renders, selects, shows source, searches, collapses."""
    def test_map_of_self(self):
        p = self.page
        p.goto(self.origin + '/map'); p.wait_for_selector('#map-main:not([hidden])'); p.wait_for_function("() => document.querySelectorAll('.mnode').length > 300")
        self.assertIn('nodes', p.inner_text('#map-stats')); self.assertGreater(p.evaluate("() => document.querySelectorAll('#edge-layer path').length"), 300)
        p.locator('.mnode[data-id="def:graph.py:validate"]').click(); p.wait_for_function("() => document.getElementById('sel-source').textContent.includes('def validate(document)')")
        self.assertIn('function: validate', p.inner_text('#sel-title')); self.assertIn('Called by', p.inner_text('#sel-links')); self.assertGreater(p.evaluate("() => document.querySelectorAll('#edge-layer path.lit').length"), 3)
        p.locator('#sel-links a[data-id="def:server.py:LabServer.set_graph"]').first.click(); p.wait_for_function("() => document.getElementById('sel-title').textContent.includes('set_graph')")
        p.fill('#map-search', 'library'); p.wait_for_function("() => document.querySelectorAll('.mnode.hit').length > 5 && document.querySelectorAll('.mnode.dim').length > 100")
        p.fill('#map-search', '')
        p.click('#map-collapse'); p.wait_for_function("() => document.querySelectorAll('.mnode.method').length === 0 && document.querySelectorAll('.mnode.module').length > 15")
        p.click('#map-expand'); p.wait_for_function("() => document.querySelectorAll('.mnode.method').length > 100")
        p.uncheck('#edge-calls'); p.wait_for_function("() => document.querySelectorAll('#edge-layer path.map-calls').length === 0")
        p.screenshot(path='/tmp/claude-0/-home-claude/a8759d54-ed59-512b-84e2-204b2e346c72/scratchpad/map.png')


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class MapToCellTests(RealServerCase):
    """Map page -> "Make a cell from this…" -> the Graph page adds a form cell that calls the function; run it."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sys
        from local_lab.kernel_session import KernelSession
        from local_lab.runner import KERNEL_DIR
        from tests.test_codemap import sample_package
        def factory(settings, state_dir):
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')
        cls.server.kernel_factory = factory; cls.server.kernels.factory = factory
        cls.ws = cls.root / 'ws'; cls.ws.mkdir(); sample_package(cls.ws); cls.server.settings['workspace'] = str(cls.ws)

    @classmethod
    def tearDownClass(cls):
        cls.server.kernels.stop_all(); super().tearDownClass()

    def test_make_a_cell_from_a_function_and_run_it(self):
        p = self.page
        p.goto(self.origin + '/map'); p.wait_for_selector('#map-main:not([hidden])'); p.wait_for_function("() => document.querySelectorAll('.mnode').length > 300")
        p.select_option('#map-root', 'workspace'); p.wait_for_selector('.mnode[data-id="def:tools/ops.py:scale"]')
        p.locator('.mnode[data-id="mod:tools/ops.py"]').click(); p.wait_for_function("() => document.getElementById('sel-title').textContent.includes('module')"); self.assertTrue(p.locator('#sel-cell').is_hidden(), 'modules do not become cells')
        p.locator('.mnode[data-id="def:tools/ops.py:scale"]').click(); p.wait_for_selector('#sel-cell:not([hidden])')
        self.assertIn('A form with 7 fields (values, factor, round_to, label, verbose, seed, cb)', p.inner_text('#sel-cell-note'))
        p.click('#sel-cell'); p.wait_for_function("() => document.getElementById('sel-cell-note').textContent.includes('Queued for the Graph page')")
        # The Graph page picks it up, chained after the selected cell, with the form on the card.
        p.goto(self.origin + '/graph'); p.wait_for_selector('.node[data-id="cell-2"]'); p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        self.assertEqual(p.inner_text('.node[data-id="cell-2"] .node-head b'), 'scale'); self.assertIn('from tools.ops import scale', p.input_value('.node[data-id="cell-2"] textarea'))
        self.assertEqual(p.evaluate("() => [...document.querySelectorAll('.node[data-id=\"cell-2\"] .node-form [data-field]')].map(i => i.dataset.field)"), ['values', 'factor', 'round_to', 'label', 'verbose', 'seed', 'cb'])
        self.assertEqual(p.input_value('#form-cell-2-factor'), '2'); self.assertEqual(self.server.cell_inbox, [], 'taken')
        self.assertTrue(any(e['source'] == 'cell-1' and e['target'] == 'cell-2' for e in self.server.graph_doc['graph']['edges']))
        p.fill('#form-cell-2-values', '[10, 20]'); p.fill('#form-cell-2-factor', '0.5'); p.press('#form-cell-2-factor', 'Tab'); p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        if not p.locator('#session-start').is_disabled(): p.click('#session-start'); p.wait_for_selector('#session-dialog[open]'); p.click('#session-confirm')
        p.wait_for_function("() => document.getElementById('session-state').textContent.includes('ready')")
        p.click('#run-all'); p.wait_for_selector('#run-dialog[open]'); p.click('#run-dialog-confirm')
        p.wait_for_function("() => /finished|failed/.test(document.getElementById('run-summary').textContent)", timeout=30000)
        self.assertEqual(p.inner_text('.node[data-id="cell-2"] .node-output').strip().splitlines()[0], '[5.0, 10.0]')

    def test_edit_a_function_on_the_map_review_allow_write_and_run_tests(self):
        p = self.page
        (self.ws / 'tests').mkdir(exist_ok=True); (self.ws / 'tests' / 'test_ops.py').write_text('import unittest\nfrom tools.ops import scale\nclass T(unittest.TestCase):\n    def test_scale(self):\n        self.assertEqual(scale([1], factor=3), [3])\n')
        p.goto(self.origin + '/map'); p.wait_for_selector('#map-main:not([hidden])'); p.wait_for_function("() => document.querySelectorAll('.mnode').length > 300")
        p.locator('.mnode[data-id="def:graph.py:validate"]').click(); p.wait_for_function("() => document.getElementById('sel-source').textContent.includes('def validate')")
        self.assertTrue(p.locator('#sel-edit').is_hidden(), 'the running app is not edited in place')
        p.select_option('#map-root', 'workspace'); p.wait_for_selector('.mnode[data-id="def:tools/ops.py:scale"]')
        p.locator('.mnode[data-id="def:tools/ops.py:scale"]').click(); p.wait_for_function("() => document.getElementById('sel-source').textContent.includes('def scale')"); p.wait_for_selector('#sel-edit:not([hidden])')
        p.click('#sel-edit'); p.wait_for_selector('#sel-editor:not([hidden])'); self.assertIn('Nothing is written until you review', p.inner_text('#sel-edit-note'))
        text = p.input_value('#sel-text'); p.fill('#sel-text', text.replace('Scale values.', 'Scale values, rounded.'))
        p.click('#sel-review'); p.wait_for_selector('#write-dialog[open]')
        self.assertIn('+    """Scale values, rounded."""', p.inner_text('#write-dialog-diff')); self.assertIn('not allowed yet', p.inner_text('#write-dialog-note'))
        p.click('#write-dialog-confirm'); p.wait_for_selector('#allow-dialog[open]'); self.assertIn(str(self.ws), p.inner_text('#allow-dialog-text'))
        p.click('#allow-dialog-confirm'); p.wait_for_function("() => document.getElementById('toast').textContent.includes('Wrote tools/ops.py')")
        self.assertIn('Scale values, rounded.', (self.ws / 'tools' / 'ops.py').read_text())
        p.wait_for_function("() => document.getElementById('sel-doc').textContent === 'Scale values, rounded.'", timeout=10000)  # the map re-read the file and kept the selection
        self.assertTrue(p.locator('#sel-editor').is_hidden())
        # A broken edit is refused before anything is written.
        p.click('#sel-edit'); p.wait_for_selector('#sel-editor:not([hidden])'); p.fill('#sel-text', 'def scale(:\n    pass'); p.click('#sel-review')
        p.wait_for_function("() => document.getElementById('toast').textContent.includes('would not parse')"); p.click('#sel-edit-cancel')
        # The folder's tests run inside the session.
        p.goto(self.origin + '/graph'); p.wait_for_selector('.node.code')
        if not p.locator('#session-start').is_disabled(): p.click('#session-start'); p.wait_for_selector('#session-dialog[open]'); p.click('#session-confirm')
        p.wait_for_function("() => document.getElementById('session-state').textContent.includes('ready')")
        p.goto(self.origin + '/map'); p.wait_for_selector('#map-main:not([hidden])'); p.select_option('#map-root', 'workspace'); p.wait_for_selector('.mnode[data-id="def:tools/ops.py:scale"]')
        p.click('#map-tests'); p.wait_for_function("() => document.getElementById('map-tests-output').textContent.includes('Ran 1 test')", timeout=30000)
        self.assertIn('OK', p.inner_text('#map-tests-output'))


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class MapPatchTests(RealServerCase):
    """The Map page's PATCH box: a unified diff is checked and shown, the folder allowed once, the write atomic."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from tests.test_codemap import sample_package
        cls.ws = cls.root / 'ws'; cls.ws.mkdir(); sample_package(cls.ws); cls.server.settings['workspace'] = str(cls.ws)
    def test_patch_review_allow_write(self):
        from tests.test_codemap import PATCH
        p = self.page
        p.goto(self.origin + '/map'); p.wait_for_selector('#map-main:not([hidden])'); p.wait_for_function("() => document.querySelectorAll('.mnode').length > 300")
        p.fill('#patch-text', PATCH); self.assertTrue(p.locator('#patch-review').is_disabled(), 'the running app is never patched'); self.assertIn('not patched in place', p.inner_text('#patch-note'))
        p.select_option('#map-root', 'workspace'); p.wait_for_selector('.mnode[data-id="def:tools/ops.py:scale"]'); p.wait_for_function("() => document.getElementById('patch-note').textContent === ''")
        p.fill('#patch-text', 'nonsense'); p.wait_for_function("() => !document.getElementById('patch-review').disabled"); p.click('#patch-review'); p.wait_for_function("() => document.getElementById('patch-note').textContent.includes('No hunks found')")
        p.fill('#patch-text', PATCH); p.click('#patch-review'); p.wait_for_selector('#write-dialog[open]')
        self.assertIn('tools/ops.py (2 hunks): every hunk matches', p.inner_text('#write-dialog-intro')); self.assertIn('+# Scales a list of numbers.', p.inner_text('#write-dialog-diff'))
        p.click('#write-dialog-confirm'); p.wait_for_selector('#allow-dialog[open]'); p.click('#allow-dialog-confirm')
        p.wait_for_function("() => document.getElementById('patch-note').textContent.startsWith('Patched tools/ops.py')")
        self.assertIn('# Scales a list of numbers.', (self.ws / 'tools' / 'ops.py').read_text()); self.assertEqual(p.input_value('#patch-text'), '')
        p.locator('.mnode[data-id="def:tools/ops.py:scale"]').click(); p.wait_for_function("() => document.getElementById('sel-doc').textContent.includes('Scale values, rounded.')")


@unittest.skipUnless(HAVE_BROWSER, 'Playwright Chromium not installed')
class BrowserPageTests(RealServerCase):
    """The Browser page: a script from a template, the reviewed run against the Lab's own page, the capture into chats; the Graph inspector's BROWSER section."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from local_lab.kernel_session import KernelSession
        from local_lab.runner import KERNEL_DIR
        def factory(settings, state_dir):
            return KernelSession([sys.executable, '-I', str(KERNEL_DIR / 'kernel.py')], label='local')
        cls.server.kernel_factory = factory; cls.server.kernels.factory = factory
    @classmethod
    def tearDownClass(cls):
        cls.server.kernels.stop_all(); super().tearDownClass()
    def test_script_run_capture_import_and_browser_cell(self):
        from local_lab import memory, memory_import
        p = self.page
        memory.configure(self.server.state_dir, str(self.root / 'Documents' / 'MainAIger Lab' / 'Memory'))
        p.goto(self.origin + '/browser'); p.wait_for_selector('#browser-main:not([hidden])'); p.wait_for_function("() => document.getElementById('browser-stats').textContent !== 'Reading…'")
        self.assertIn('Playwright', p.inner_text('#runner-state')); self.assertTrue(p.locator('#script-new').is_enabled())
        # New from a template: the name comes from a prompt; the JSON is edited to point at the Lab itself (a site on 127.0.0.1).
        p.once('dialog', lambda d: d.accept('lab-home')); p.select_option('#script-template', 'page-capture'); p.click('#script-new'); p.wait_for_selector('#script-editor:not([hidden])')
        self.assertEqual(p.inner_text('#script-name'), 'lab-home'); self.assertIn('"example.com"', p.input_value('#script-json'))
        p.fill('#script-json', '{"title": "The Lab front page", "allow": ["127.0.0.1"], "start": "%s/", "budget_s": 60, "steps": [{"do": "wait", "selector": "body"}, {"do": "capture", "as": "home"}]}' % self.origin)
        p.click('#script-save'); p.wait_for_function("() => document.getElementById('script-result').textContent.startsWith('Saved: 2 steps')")
        p.wait_for_function("() => document.querySelectorAll('#script-list li[data-name=\"lab-home\"]').length === 1"); self.assertIn('may visit 127.0.0.1', p.inner_text('#script-list li[data-name="lab-home"] .preview'))
        # A bad script is refused with the step named, and nothing is saved.
        p.fill('#script-json', '{"title": "x", "allow": ["127.0.0.1"], "steps": [{"do": "eval", "js": "alert(1)"}]}'); p.click('#script-save'); p.wait_for_function("() => document.getElementById('script-result').textContent.includes('Step 1 must be')")
        p.click('#script-list li[data-name="lab-home"]'); p.wait_for_function("() => document.getElementById('script-json').value.includes('The Lab front page')")
        # The reviewed run: sites and steps are listed; confirm; the page polls to the result.
        p.click('#script-run'); p.wait_for_selector('#run-dialog[open]'); self.assertEqual(p.inner_text('#run-dialog-allow'), '127.0.0.1'); self.assertIn('read the page as "home"', p.inner_text('#run-dialog-steps')); self.assertFalse(p.is_checked('#run-dialog-headed'))
        p.click('#run-confirm'); p.wait_for_function("() => document.getElementById('run-state').textContent.includes('lab-home: finished')", timeout=60000)
        self.assertIn('1 page captured', p.inner_text('#run-summary')); self.assertIn('MainAIger', p.inner_text('#run-pages li b')); self.assertTrue(p.locator('#run-blocked').is_hidden())
        p.wait_for_function("() => document.querySelectorAll('#captures-list li').length === 1"); self.assertIn('1 page', p.inner_text('#captures-list li .preview'))
        # Import the capture: one chat note, visible to the review loop; a second import adds nothing.
        p.click('#run-import'); p.wait_for_selector('#import-dialog[open]'); self.assertIn('1 page from browser/captures/lab-home-', p.inner_text('#import-dialog-text')); p.click('#import-confirm')
        p.wait_for_function("() => document.getElementById('toast').textContent.startsWith('1 chat note added')"); p.wait_for_function("() => document.querySelector('#captures-list li .preview').textContent.includes('1 chat note imported')")
        chats = memory.listing(self.server.state_dir, tier='chats'); self.assertEqual(len(chats), 1); self.assertEqual(memory.read(self.server.state_dir, chats[0]['path'])['front']['source'], 'browser')
        self.assertEqual(memory_import.review_status(self.server.state_dir)['pending'], 1)
        p.click('#captures-list li button[data-folder]'); p.wait_for_selector('#import-dialog[open]'); p.click('#import-confirm'); p.wait_for_function("() => document.getElementById('toast').textContent.startsWith('0 chat notes added, 1 already there')")
        # A profile now exists; forgetting it is a reviewed step too.
        p.wait_for_function("() => !document.getElementById('profile-clear').disabled"); p.click('#profile-clear'); p.wait_for_selector('#profile-dialog[open]'); p.click('#profile-confirm'); p.wait_for_function("() => document.getElementById('profile-clear').disabled")
        # On the Graph page: a cell drives the script; the run review names the sites.
        from local_lab import graph as g
        self.server.set_graph(g.new_document()); p.goto(self.origin + '/graph'); p.wait_for_selector('.node.code'); p.wait_for_timeout(300)
        p.locator('.node[data-id="cell-1"] .node-foot').click(); p.wait_for_selector('#node-panel:not([hidden])')
        p.check('#web-enabled'); p.wait_for_selector('#web-fields:not([hidden])'); p.wait_for_function("() => [...document.querySelectorAll('#web-script option')].some(o => o.value === 'lab-home')")
        p.select_option('#web-script', 'lab-home'); self.assertEqual(p.input_value('#web-output'), 'capture'); p.click('#web-apply')
        p.wait_for_function("() => document.querySelector('.node[data-id=\"cell-1\"] .node-model').textContent === 'browser lab-home → capture'"); p.wait_for_function("() => document.getElementById('web-note').textContent.includes('may visit 127.0.0.1')"); self.assertEqual(p.inner_text('.node[data-id="cell-1"] .node-head b'), 'Browser lab-home')
        p.wait_for_function("() => document.getElementById('save-state').textContent === 'Saved'")
        if not p.locator('#session-start').is_disabled(): p.click('#session-start'); p.wait_for_selector('#session-dialog[open]'); p.click('#session-confirm')
        p.wait_for_function("() => document.getElementById('session-state').textContent.includes('ready')")
        p.click('#run-all'); p.wait_for_selector('#run-dialog[open]'); lines = p.inner_text('#run-dialog-arrows-list'); self.assertIn('drives a browser on this Mac with "lab-home": may visit 127.0.0.1; 2 steps', lines); self.assertIn('fragile by nature', lines)
        self.assertFalse(p.locator('#run-dialog-confirm').is_disabled()); p.click('#run-dialog-confirm')
        p.wait_for_function("() => /finished|failed/.test(document.getElementById('run-summary').textContent)", timeout=90000)
        self.assertEqual(p.inner_text('.node[data-id="cell-1"] .status'), 'ok'); out = p.inner_text('.node[data-id="cell-1"] .node-output'); self.assertIn('"pages"', out); self.assertIn('browser/captures/lab-home-', out)
        self.assertTrue(any('Browser script lab-home started' in k for k in p.evaluate("() => [...document.querySelectorAll('#log-list li')].map(li => li.textContent)")), 'the run is in the Log')
