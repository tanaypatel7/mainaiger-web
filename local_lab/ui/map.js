(() => {
  'use strict';
  // Code map page: a folder's modules, classes, functions, routes and UI files as nodes; calls, imports,
  // routes and UI calls as edges. Data from GET /api/codemap (local_lab/codemap.py); source snippets from
  // GET /api/codemap/source. Layout is columns per module; the canvas pans and zooms like the graph page.
  const $ = id => document.getElementById(id);
  const COL_W = 250, COL_GAP = 70, ROW_H = 28, TOP = 40;
  const state = {map: null, root: 'self', pan: {x: 30, y: 20}, zoom: 1, panning: null, selected: null, collapsed: new Set(), pos: {}, visible: new Set(), search: ''};
  let toastTimer = null; const toast = t => { $('toast').textContent = t; $('toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4000); };
  async function api(path) {
    let response; try { response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', redirect: 'error'}); } catch { throw new Error('Unable to reach your local Lab.'); }
    let data = {}; try { data = await response.json(); } catch {}
    if (response.status === 401) { $('locked').hidden = false; $('map-main').hidden = true; throw new Error('Unlock the Lab first.'); }
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : 'The local request could not be completed.');
    return data;
  }
  // ---- data
  const byId = () => state.map.index;
  async function load(root) {
    $('map-stats').textContent = 'Reading…';
    const map = await api('/api/codemap?root=' + encodeURIComponent(root)); state.map = map; state.root = root;
    map.index = Object.fromEntries(map.nodes.map(n => [n.id, n]));
    map.out = {}; map.in = {}; for (const e of map.edges) { (map.out[e.source] = map.out[e.source] || []).push(e); (map.in[e.target] = map.in[e.target] || []).push(e); }
    const sel = $('map-root'); sel.textContent = ''; for (const r of map.roots) { const o = document.createElement('option'); o.value = r; o.textContent = r === 'self' ? 'This Lab (local_lab)' : r; o.selected = r === root; sel.append(o); }
    const members = map.nodes.filter(n => n.kind !== 'module' && n.kind !== 'route' && n.kind !== 'ui').length;
    state.collapsed = new Set(members > 600 ? map.nodes.filter(n => n.kind === 'module').map(n => n.id) : []);
    $('map-stats').textContent = `${map.stats.nodes} nodes · ${map.stats.edges} edges · ${map.stats.python} Python files · ${map.stats.ui} UI files`;
    $('map-summary').textContent = `${map.root}: ${map.stats.python} Python and ${map.stats.ui} UI files read; ${map.stats.unresolved_calls} calls to names outside this folder were counted, not drawn.` + (map.stats.syntax_errors ? ` ${map.stats.syntax_errors} file(s) had syntax errors.` : '');
    const sk = $('map-skipped'); sk.textContent = ''; for (const s of map.stats.skipped) { const li = document.createElement('li'); li.textContent = s; sk.append(li); }
    state.selected = null; layout(); render(); fit(); renderSelection();
  }
  // ---- layout: one column per module in path order; routes first, UI last
  function layout() {
    const map = state.map; state.pos = {}; state.visible = new Set();
    const modules = map.nodes.filter(n => n.kind === 'module').sort((a, b) => a.path.localeCompare(b.path));
    const routes = map.nodes.filter(n => n.kind === 'route').sort((a, b) => a.name.localeCompare(b.name));
    const uis = map.nodes.filter(n => n.kind === 'ui').sort((a, b) => a.path.localeCompare(b.path));
    let x = 0; state.cols = [];
    const column = (title, nodes) => { let y = TOP; state.cols.push({x, title}); for (const n of nodes) { state.pos[n.id] = {x, y, w: COL_W, h: n.kind === 'module' ? 26 : 24}; state.visible.add(n.id); y += ROW_H; } x += COL_W + COL_GAP; };
    if (routes.length) { const half = Math.ceil(routes.length / 2); if (routes.length > 24) { column('routes', routes.slice(0, half)); column('routes (cont.)', routes.slice(half)); } else column('routes', routes); }
    for (const m of modules) {
      const members = []; if (!state.collapsed.has(m.id)) for (const e of (map.out[m.id] || []).filter(e => e.kind === 'contains')) { const c = byId()[e.target]; members.push(c); if (c.kind === 'class') for (const e2 of (map.out[c.id] || []).filter(e => e.kind === 'contains')) members.push(byId()[e2.target]); }
      column(m.path, [m, ...members]);
    }
    if (uis.length) column('ui', uis);
    state.width = x; state.height = Math.max(...Object.values(state.pos).map(p => p.y + p.h), 200) + 40;
  }
  // ---- render
  function render() {
    const root = $('nodes'); root.textContent = '';
    for (const c of state.cols) { const t = document.createElement('div'); t.className = 'mcol-title'; t.style.left = c.x + 'px'; t.style.top = (TOP - 22) + 'px'; t.textContent = c.title; root.append(t); }
    for (const id of state.visible) { const n = byId()[id], p = state.pos[id]; const el = document.createElement('div'); el.className = 'mnode ' + n.kind + (state.collapsed.has(id) ? ' collapsed' : ''); el.dataset.id = id; el.style.left = p.x + 'px'; el.style.top = p.y + 'px'; el.style.width = p.w + 'px'; el.textContent = n.kind === 'module' ? n.path : (n.kind === 'method' ? n.name.split('.').slice(1).join('.') : n.name); el.title = `${n.kind} ${n.name} · ${n.path}:${n.line}` + (n.doc ? '\n' + n.doc : ''); root.append(el); }
    drawEdges(); applyViewport(); applySearch();
  }
  const enabled = () => ({calls: $('edge-calls').checked, imports: $('edge-imports').checked, routes: $('edge-routes').checked, ui: $('edge-ui').checked});
  function endpoint(id, side) {
    // Edges leave a node from its right or left edge, whichever faces the other column.
    const p = state.pos[id]; if (!p) return null; return side === 'r' ? {x: p.x + p.w, y: p.y + p.h / 2} : {x: p.x, y: p.y + p.h / 2};
  }
  function visibleTarget(id) {
    // A collapsed module stands in for its members.
    if (state.visible.has(id)) return id; const n = byId()[id]; if (!n) return null;
    let cur = n; while (cur && cur.parent) { cur = byId()[cur.parent]; if (cur && state.visible.has(cur.id)) return cur.id; } return null;
  }
  function drawEdges() {
    const layer = $('edge-layer'); layer.textContent = ''; const on = enabled(); const ns = 'http://www.w3.org/2000/svg'; const sel = state.selected;
    for (const e of state.map.edges) {
      if (e.kind === 'contains' || !on[e.kind]) continue;
      const a = visibleTarget(e.source), b = visibleTarget(e.target); if (!a || !b || a === b) continue;
      const pa = state.pos[a], pb = state.pos[b]; const left = pa.x <= pb.x;
      const s = endpoint(a, left ? 'r' : 'l'), t = endpoint(b, left ? 'l' : 'r'); const dx = Math.max(40, Math.abs(t.x - s.x) / 2);
      const path = document.createElementNS(ns, 'path'); path.setAttribute('d', `M${s.x} ${s.y} C${s.x + (left ? dx : -dx)} ${s.y} ${t.x - (left ? dx : -dx)} ${t.y} ${t.x} ${t.y}`);
      path.setAttribute('class', 'map-' + e.kind + (sel ? (a === sel || b === sel ? ' lit' : ' faded') : '')); layer.append(path);
    }
  }
  function applyViewport() { $('world').style.transform = `translate(${state.pan.x}px,${state.pan.y}px) scale(${state.zoom})`; $('zoom-label').textContent = Math.round(state.zoom * 100) + '%'; }
  function fit() {
    // Fit to the width (columns side by side, text still readable); tall columns are reached by panning down.
    const vp = $('viewport').getBoundingClientRect(); if (!vp.width) return;
    state.zoom = Math.max(0.3, Math.min(1, (vp.width - 60) / state.width)); state.pan = {x: 30, y: 20}; applyViewport();
  }
  function applySearch() {
    const q = state.search.trim().toLowerCase(); for (const el of document.querySelectorAll('.mnode')) { const n = byId()[el.dataset.id]; const hit = q && (n.name.toLowerCase().includes(q) || n.path.toLowerCase().includes(q)); el.classList.toggle('hit', !!hit); el.classList.toggle('dim', !!q && !hit); }
  }
  // ---- selection + source
  async function select(id) {
    state.selected = id; for (const el of document.querySelectorAll('.mnode')) el.classList.toggle('selected', el.dataset.id === id); drawEdges(); renderSelection();
  }
  async function renderSelection() {
    const id = state.selected; const n = id && byId()[id];
    if (!n) { $('sel-title').textContent = 'Click a node.'; $('sel-meta').textContent = ''; $('sel-doc').textContent = ''; $('sel-links').textContent = ''; $('sel-source').hidden = true; return; }
    $('sel-title').textContent = `${n.kind}: ${n.name}`; $('sel-meta').textContent = `${n.path}:${n.line}-${n.end_line}`; $('sel-doc').textContent = n.doc || (n.routes_called ? 'calls: ' + n.routes_called.join(', ') : '');
    const links = $('sel-links'); links.textContent = '';
    const group = (title, items, pick) => { if (!items.length) return; const b = document.createElement('b'); b.textContent = title; links.append(b); for (const e of items.slice(0, 40)) { const t = byId()[pick(e)]; if (!t) continue; const a = document.createElement('a'); a.textContent = `${t.kind === 'method' || t.kind === 'function' || t.kind === 'class' ? t.name : t.name} · ${t.path}`; a.dataset.id = t.id; links.append(a); } if (items.length > 40) { const m = document.createElement('span'); m.className = 'muted'; m.textContent = `… ${items.length - 40} more`; links.append(m); } };
    const out = state.map.out[id] || [], inn = state.map.in[id] || [];
    group('Calls', out.filter(e => e.kind === 'calls'), e => e.target); group('Called by', inn.filter(e => e.kind === 'calls'), e => e.source);
    group('Imports', out.filter(e => e.kind === 'imports'), e => e.target); group('Imported by', inn.filter(e => e.kind === 'imports'), e => e.source);
    group('Handles routes', inn.filter(e => e.kind === 'routes'), e => e.source); group('Handled by', out.filter(e => e.kind === 'routes'), e => e.target);
    group('Called from UI', inn.filter(e => e.kind === 'ui'), e => e.source); group('Calls routes', out.filter(e => e.kind === 'ui'), e => e.target);
    group('Contains', out.filter(e => e.kind === 'contains'), e => e.target);
    const src = $('sel-source'); src.hidden = false; src.textContent = 'Loading source…';
    try { const data = await api(`/api/codemap/source?root=${encodeURIComponent(state.root)}&path=${encodeURIComponent(n.path)}&line=${n.line}&end_line=${n.end_line}`); if (state.selected !== id) return; src.textContent = data.text + (data.truncated ? '\n…' : ''); } catch (error) { src.textContent = error.message; }
  }
  $('sel-links').addEventListener('click', event => { const a = event.target.closest('a[data-id]'); if (!a) return; const id = a.dataset.id; if (!state.visible.has(id)) { const n = byId()[id]; let cur = n; while (cur && cur.parent) { cur = byId()[cur.parent]; if (cur && cur.kind === 'module') state.collapsed.delete(cur.id); } layout(); render(); } select(id); });
  // ---- interactions
  const viewport = $('viewport');
  $('nodes').addEventListener('click', event => { const el = event.target.closest('.mnode'); if (!el) return; const id = el.dataset.id; const n = byId()[id];
    if (n.kind === 'module' && state.selected === id) { if (state.collapsed.has(id)) state.collapsed.delete(id); else state.collapsed.add(id); layout(); render(); select(id); return; }
    select(id); });
  viewport.addEventListener('pointerdown', event => { if (event.button !== 0 || event.target.closest('.mnode')) return; state.panning = {x: event.clientX - state.pan.x, y: event.clientY - state.pan.y}; viewport.setPointerCapture(event.pointerId); viewport.classList.add('panning'); });
  viewport.addEventListener('pointermove', event => { if (!state.panning) return; state.pan = {x: event.clientX - state.panning.x, y: event.clientY - state.panning.y}; requestAnimationFrame(applyViewport); });
  const endPan = () => { state.panning = null; viewport.classList.remove('panning'); }; viewport.addEventListener('pointerup', endPan); viewport.addEventListener('pointercancel', endPan);
  viewport.addEventListener('wheel', event => { event.preventDefault(); const factor = Math.exp(-event.deltaY * 0.0015); const zoom = Math.max(0.1, Math.min(2.5, state.zoom * factor)); const r = viewport.getBoundingClientRect(), mx = event.clientX - r.left, my = event.clientY - r.top; state.pan = {x: mx - (mx - state.pan.x) * (zoom / state.zoom), y: my - (my - state.pan.y) * (zoom / state.zoom)}; state.zoom = zoom; applyViewport(); }, {passive: false});
  for (const id of ['edge-calls', 'edge-imports', 'edge-routes', 'edge-ui']) $(id).addEventListener('change', drawEdges);
  $('map-fit').addEventListener('click', fit);
  $('map-expand').addEventListener('click', () => { state.collapsed.clear(); layout(); render(); });
  $('map-collapse').addEventListener('click', () => { state.collapsed = new Set(state.map.nodes.filter(n => n.kind === 'module').map(n => n.id)); layout(); render(); });
  $('map-search').addEventListener('input', () => { state.search = $('map-search').value; applySearch(); });
  $('map-root').addEventListener('change', () => load($('map-root').value).catch(e => toast(e.message)));
  $('map-reload').addEventListener('click', () => load(state.root).catch(e => toast(e.message)));
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !event.target.closest('input')) select(null); });
  (async () => { try { await api('/api/state'); $('map-main').hidden = false; await load('self'); } catch (error) { if ($('locked').hidden) toast(error.message); } })();
})();
