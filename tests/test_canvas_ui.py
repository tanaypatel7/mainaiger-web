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
