"""Canvas benchmark for local_lab/ui/graph.js in headless Chromium.

Starts tests/perf/stub_server.py with N cells, opens /graph, and measures:
  drag   - main-thread cost per pointermove while dragging a node, and the frame
           rate the browser achieved during a 2 s real-mouse drag
  pan    - same, dragging the background
  select - cost of a click that selects a node
  typing - cost per keystroke in a cell's textarea
  save   - how many server round-trips (re-analysis) a burst of typing caused
  dom    - how many SVG elements the edge layer holds

Usage: python3 tests/perf/bench_canvas.py [cells] [label]
Writes tests/perf/results/<label>.json and prints a summary.
"""
import json, os, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
CELLS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
LABEL = sys.argv[2] if len(sys.argv) > 2 else 'run'
PORT = 8765

MID = max(1, CELLS // 2)
MEASURE_JS = r"""
() => {
  // Helpers installed once per page.
  window.__bench = {
    // Dispatch an event synchronously and return the handler time in ms (JS + any forced layout).
    cost(target, type, init) { const t = performance.now(); target.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true, button: 0, buttons: 1, ...init})); return performance.now() - t; },
    // Record requestAnimationFrame intervals for `ms` milliseconds; resolves with frame gaps.
    frames(ms) { return new Promise(resolve => { const gaps = []; let last = performance.now(); const start = last; const tick = () => { const now = performance.now(); gaps.push(now - last); last = now; if (now - start < ms) requestAnimationFrame(tick); else resolve(gaps); }; requestAnimationFrame(tick); }); },
    stats(xs) { const s = [...xs].sort((a, b) => a - b); const q = p => s[Math.min(s.length - 1, Math.floor(p * s.length))]; return {n: s.length, mean: +(s.reduce((a, b) => a + b, 0) / s.length).toFixed(2), p50: +q(0.5).toFixed(2), p95: +q(0.95).toFixed(2), max: +Math.max(...s).toFixed(2)}; },
  };
  return true;
}
"""


def main():
    server = subprocess.Popen([sys.executable, os.path.join(HERE, 'stub_server.py'), str(PORT), str(CELLS)])
    try:
        for _ in range(50):
            try: urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/graph', timeout=1); break
            except Exception: time.sleep(0.1)
        results = {'cells': CELLS, 'label': LABEL}
        with sync_playwright() as p:
            browser = p.chromium.launch(args=['--disable-gpu'])
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            page.goto(f'http://127.0.0.1:{PORT}/graph')
            page.wait_for_selector('.node.code')
            page.evaluate(MEASURE_JS); page.evaluate('m => { window.__MID = m[0]; window.__CELLS = m[1]; }', [MID, CELLS])
            cdp = page.context.new_cdp_session(page); cdp.send('Performance.enable')
            def metrics():
                return {m['name']: m['value'] for m in cdp.send('Performance.getMetrics')['metrics']}
            page.wait_for_timeout(300)

            results['dom'] = page.evaluate("() => ({edge_elements: document.getElementById('edge-layer').childElementCount, nodes: document.querySelectorAll('.node').length})")

            # ---- drag: synthetic events (handler cost), on a node in the middle of the graph
            m0 = metrics()
            results['drag_handler'] = page.evaluate(r"""() => {
              const el = document.querySelector('.node[data-id="cell-' + window.__MID + '"]'); const head = el.querySelector('.node-head b'); const vp = document.getElementById('viewport');
              const r = head.getBoundingClientRect(); const x0 = r.left + 5, y0 = r.top + 5;
              __bench.cost(head, 'pointerdown', {clientX: x0, clientY: y0});
              const costs = []; for (let i = 1; i <= 120; i++) costs.push(__bench.cost(vp, 'pointermove', {clientX: x0 + i * 2, clientY: y0 + i}));
              __bench.cost(vp, 'pointerup', {clientX: x0 + 240, clientY: y0 + 120});
              return __bench.stats(costs);
            }""")
            m1 = metrics(); results['drag_handler'].update({'layouts_per_move': round((m1['LayoutCount'] - m0['LayoutCount']) / 120, 2), 'style_recalcs_per_move': round((m1['RecalcStyleCount'] - m0['RecalcStyleCount']) / 120, 2), 'layout_ms_total': round((m1['LayoutDuration'] - m0['LayoutDuration']) * 1000, 1), 'script_ms_total': round((m1['ScriptDuration'] - m0['ScriptDuration']) * 1000, 1)})
            # ---- drag: real mouse over 2 s, measure achieved frame gaps
            box = page.evaluate("""() => { const r = document.querySelector('.node[data-id="cell-' + window.__MID + '"] .node-head b').getBoundingClientRect(); return {x: r.left + 5, y: r.top + 5}; }""")
            page.mouse.move(box['x'], box['y']); page.mouse.down()
            frames_promise = page.evaluate_handle("() => __bench.frames(1800)")
            t0 = time.time()
            for i in range(1, 121):
                page.mouse.move(box['x'] + i * 2, box['y'] + (i % 40), steps=1)
                time.sleep(max(0, (t0 + i * 0.015) - time.time()))
            page.mouse.up()
            gaps = frames_promise.json_value() if hasattr(frames_promise, 'json_value') else None
            gaps = page.evaluate("h => h", frames_promise)
            results['drag_frames'] = page.evaluate("gaps => ({...__bench.stats(gaps), fps: +(1000 / (gaps.reduce((a,b)=>a+b,0) / gaps.length)).toFixed(1), long_frames_over_20ms: gaps.filter(g => g > 20).length})", gaps)

            # ---- pan: synthetic events on the background
            results['pan_handler'] = page.evaluate(r"""() => {
              const vp = document.getElementById('viewport'); const r = vp.getBoundingClientRect(); const x0 = r.left + 20, y0 = r.bottom - 20;
              __bench.cost(vp, 'pointerdown', {clientX: x0, clientY: y0});
              const costs = []; for (let i = 1; i <= 120; i++) costs.push(__bench.cost(vp, 'pointermove', {clientX: x0 + i, clientY: y0 - i}));
              __bench.cost(vp, 'pointerup', {clientX: x0 + 120, clientY: y0 - 120});
              return __bench.stats(costs);
            }""")
            vp = page.evaluate("() => { const r = document.getElementById('viewport').getBoundingClientRect(); return {x: r.left + 20, y: r.bottom - 20}; }")
            page.mouse.move(vp['x'], vp['y']); page.mouse.down()
            frames_promise = page.evaluate_handle("() => __bench.frames(1800)")
            t0 = time.time()
            for i in range(1, 121):
                page.mouse.move(vp['x'] + i, vp['y'] - (i % 50), steps=1)
                time.sleep(max(0, (t0 + i * 0.015) - time.time()))
            page.mouse.up()
            gaps = page.evaluate("h => h", frames_promise)
            results['pan_frames'] = page.evaluate("gaps => ({...__bench.stats(gaps), fps: +(1000 / (gaps.reduce((a,b)=>a+b,0) / gaps.length)).toFixed(1), long_frames_over_20ms: gaps.filter(g => g > 20).length})", gaps)

            # ---- select: click 20 different nodes (pointerdown inside the card body)
            results['select_handler'] = page.evaluate(r"""() => {
              const costs = []; for (let i = 1; i <= Math.min(20, window.__CELLS); i++) { const el = document.querySelector(`.node[data-id="cell-${i}"] .node-foot`); const r = el.getBoundingClientRect(); costs.push(__bench.cost(el, 'pointerdown', {clientX: r.left + 3, clientY: r.top + 3})); }
              return __bench.stats(costs);
            }""")

            # ---- typing: 40 input events into one textarea (one character each)
            saves_before = json.load(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/__bench/stats'))['saves']
            results['typing_handler'] = page.evaluate(r"""() => {
              const area = document.querySelector('.node[data-id="cell-' + window.__MID + '"] textarea'); area.focus(); const costs = [];
              for (let i = 0; i < 40; i++) { area.value += 'x'; const t = performance.now(); area.dispatchEvent(new InputEvent('input', {bubbles: true})); costs.push(performance.now() - t); }
              return __bench.stats(costs);
            }""")
            page.wait_for_timeout(1500)
            st = json.load(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/__bench/stats'))
            results['save'] = {'round_trips_after_40_keystrokes': st['saves'] - saves_before, 'server_analysis_ms_mean': round(sum(st['analysis_ms']) / len(st['analysis_ms']), 1)}

            # ---- typing while keystrokes are spaced 100 ms apart (slower than the 600 ms debounce resets it)
            saves_before = st['saves']
            page.evaluate(r"""async () => { const area = document.querySelector('.node[data-id="cell-' + window.__MID + '"] textarea'); for (let i = 0; i < 10; i++) { area.value += 'y'; area.dispatchEvent(new InputEvent('input', {bubbles: true})); await new Promise(r => setTimeout(r, 100)); } }""")
            page.wait_for_timeout(1500)
            st = json.load(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/__bench/stats'))
            results['save']['round_trips_after_10_slow_keystrokes'] = st['saves'] - saves_before
            browser.close()
    finally:
        server.terminate()
    os.makedirs(os.path.join(HERE, 'results'), exist_ok=True)
    out = os.path.join(HERE, 'results', f'{LABEL}.json')
    json.dump(results, open(out, 'w'), indent=1)
    print(json.dumps(results, indent=1)); print('written', out)


if __name__ == '__main__':
    main()
