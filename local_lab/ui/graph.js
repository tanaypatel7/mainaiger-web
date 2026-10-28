(() => {
  'use strict';
  // Graph canvas: edits a .ipymb document held by the local service, draws trigger
  // (control) and derived data edges, and runs nodes in the persistent session.
  const $ = id => document.getElementById(id);
  const NODE_W = 320, TERMINAL_W = 132;
  const state = {doc: null, analysis: {}, dataEdges: [], order: [], disconnected: [], revision: 0, run: null,
                 session: {alive: false, executing: false}, csrf: '', selected: null, pan: {x: 40, y: 30}, zoom: 1,
                 dirty: false, saveTimer: null, pollTimer: null, pendingRun: null, dragging: null, linking: null, panning: null, view: 'graph', notebookPos: null, variables: new Set()};
  let toastTimer = null;
  const toast = text => { $('toast').textContent = text; $('toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4000); };

  // ---------------------------------------------------------------- API
  async function api(path, body) {
    const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {'Content-Type': 'application/json', ...(state.csrf ? {'X-CSRF-Token': state.csrf} : {})}};
    if (body !== undefined) { options.method = 'POST'; options.body = JSON.stringify(body); }
    let response;
    try { response = await fetch(path, options); } catch { throw new Error('Unable to reach your local Lab.'); }
    let data = {}; try { data = await response.json(); } catch {}
    if (response.status === 401) { $('locked').hidden = false; $('graph-main').hidden = true; throw new Error('Unlock the Lab first.'); }
    if (!response.ok) { const error = new Error(typeof data.error === 'string' ? data.error : 'The local request could not be completed.'); error.status = response.status; throw error; }
    return data;
  }

  // ---------------------------------------------------------------- document helpers
  const nodes = () => state.doc.graph.nodes;
  const edges = () => state.doc.graph.edges;
  const cells = () => state.doc.notebook.cells;
  // Lookup caches. The document arrays are the source of truth; these maps only avoid
  // linear scans and querySelector calls on hot paths. buildNodes() refills them.
  const nodeIndex = new Map();   // node id -> node object
  const nodeEls = new Map();     // node id -> <article class="node">
  const sizes = new Map();       // node id -> {w, h} in world px, kept fresh by a ResizeObserver
  const edgePaths = new Map();   // edge key -> {path, label, edge}
  const nodeById = id => nodeIndex.get(id) || nodes().find(n => n.id === id);
  const elOf = id => nodeEls.get(id) || null;
  const cellOf = node => cells()[node.cell_index];
  const meta = cell => { cell.metadata = cell.metadata || {}; cell.metadata.mainaiger = cell.metadata.mainaiger || {}; return cell.metadata.mainaiger; };
  const sourceOf = cell => Array.isArray(cell.source) ? cell.source.join('') : (cell.source || '');
  const titleOf = node => node.kind === 'start' ? 'Start' : node.kind === 'finish' ? 'Finish' : (meta(cellOf(node)).title || (node.kind === 'markdown' ? 'Note' : node.id));
  const widthOf = node => (node.kind === 'start' || node.kind === 'finish') ? TERMINAL_W : NODE_W;
  function freshId(prefix) { let n = 1; const taken = new Set([...nodes().map(x => x.id), ...edges().map(x => x.id), ...cells().map(c => c.id)]); while (taken.has(prefix + n)) n++; return prefix + n; }
  function position(id) { const layout = state.doc.layout.nodes; if (!layout[id]) layout[id] = {x: 60 + Math.random() * 200, y: 60 + Math.random() * 200}; return layout[id]; }
  function controlReaches(from, to) { // is `to` downstream of `from`? (local cycle guard)
    const stack = [from], seen = new Set();
    while (stack.length) { const cur = stack.pop(); if (cur === to) return true; if (seen.has(cur)) continue; seen.add(cur); edges().filter(e => e.kind === 'control' && e.source === cur).forEach(e => stack.push(e.target)); }
    return false;
  }

  // ---------------------------------------------------------------- rendering
  function fnv(text) { let h = 0x811c9dc5; for (const byte of new TextEncoder().encode(text)) { h ^= byte; h = Math.imul(h, 0x01000193) >>> 0; } return h.toString(16).padStart(8, '0'); }
  function button(cls, act, title, text) { const b = document.createElement('button'); b.type = 'button'; b.className = cls; b.dataset.act = act; b.title = title; b.textContent = text; b.setAttribute('aria-label', title); return b; }
  function missingFor(id) {
    // Inputs of this cell that no upstream cell produces and that are not in the session yet.
    const info = state.analysis[id]; if (!info) return [];
    const fed = new Set(state.dataEdges.filter(e => e.target === id).map(e => e.name));
    return info.inputs.filter(n => !fed.has(n) && !state.variables.has(n));
  }
  // One observer for every card. When a card changes size (typing grows a textarea, output
  // arrives, code is collapsed) the browser tells us; we never ask with offsetWidth/offsetHeight
  // during a drag, because that forces a synchronous layout of the whole canvas.
  const sizeObserver = new ResizeObserver(entries => {
    const touched = [];
    for (const entry of entries) { const id = entry.target.dataset.id; const b = entry.borderBoxSize && entry.borderBoxSize[0]; const w = b ? b.inlineSize : entry.target.offsetWidth, h = b ? b.blockSize : entry.target.offsetHeight; if (!w && !h) continue; sizes.set(id, {w, h}); touched.push(id); }
    if (touched.length) { scheduleFrame({nodes: touched, layoutNotebook: state.view === 'notebook'}); }
  });
  function buildNodes() {
    const root = $('nodes'); root.textContent = '';
    nodeEls.clear(); sizes.clear(); sizeObserver.disconnect(); reindex();
    for (const node of nodes()) {
      const el = document.createElement('article');
      el.className = 'node ' + node.kind + ((node.kind === 'start' || node.kind === 'finish') ? ' terminal' : '');
      el.dataset.id = node.id;
      if (node.kind !== 'start') { const pin = document.createElement('span'); pin.className = 'port in'; pin.title = 'Trigger input'; el.append(pin); }
      const head = document.createElement('div'); head.className = 'node-head';
      if (node.kind === 'code') { const count = document.createElement('span'); count.className = 'count'; count.title = 'Execution count in this session'; head.append(count); }
      if (node.kind === 'start') head.append(button('node-run primary', 'all', 'Run the whole graph from Start', '▶'));
      const title = document.createElement('b'); head.append(title);
      if (node.kind === 'code') {
        head.append(button('node-run', 'one', 'Run only this cell (Shift+Enter)', '▶'));
        head.append(button('node-run', 'from', 'Run from this cell onward (Ctrl+Enter)', '▶▶'));
        head.append(button('node-toggle', 'code', 'Show or hide code', '{ }'));
        head.append(button('node-toggle', 'output', 'Show or hide output', '▤'));
        head.append(button('node-run', 'add', 'Add a cell triggered by this one', '+'));
      }
      el.append(head);
      if (node.kind === 'code') { const needs = document.createElement('div'); needs.className = 'node-needs'; needs.hidden = true; el.append(needs); }
      if (node.kind === 'code' || node.kind === 'markdown') {
        const area = document.createElement('textarea'); area.spellcheck = false; area.value = sourceOf(cellOf(node)); area.placeholder = node.kind === 'code' ? '# Python — Shift+Enter runs this cell' : 'Write a note…'; area.setAttribute('aria-label', node.kind === 'code' ? 'Python code' : 'Note'); el.append(area); if (node.kind === 'markdown') requestAnimationFrame(() => autosize(area));
        if (node.kind === 'code') { const out = document.createElement('pre'); out.className = 'node-output'; el.append(out); const foot = document.createElement('div'); foot.className = 'node-foot'; const status = document.createElement('span'); status.className = 'status'; foot.append(status); const timing = document.createElement('span'); timing.className = 'timing'; foot.append(timing); const stale = document.createElement('span'); stale.className = 'stale'; stale.textContent = 'stale'; stale.title = 'The code changed after this output was produced'; stale.hidden = true; foot.append(stale); el.append(foot); }
      }
      if (node.kind === 'finish') { const foot = document.createElement('div'); foot.className = 'finish-note'; el.append(foot); }
      if (node.kind !== 'finish' && node.kind !== 'markdown') { const pout = document.createElement('span'); pout.className = 'port out'; pout.title = 'Drag to another cell to add a trigger'; el.append(pout); }
      root.append(el); nodeEls.set(node.id, el); sizeObserver.observe(el, {box: 'border-box'});
      updateNode(node.id);
      placeNode(node.id);
    }
    drawEdges();
  }
  function updateNode(id) {
    const node = nodeById(id), el = elOf(id); if (!node || !el) return;
    el.querySelector('.node-head b').textContent = titleOf(node);
    el.classList.toggle('selected', state.selected && state.selected.type === 'node' && state.selected.id === id);
    el.classList.toggle('disconnected', state.disconnected.includes(id));
    if (node.kind === 'finish') { const r = state.run; el.querySelector('.finish-note').textContent = r && r.finished_at ? `${r.state} · ${Object.values(r.nodes).filter(n => n.status === 'ok').length}/${r.plan.length} ok` : ''; return; }
    if (node.kind !== 'code') return;
    const cell = cellOf(node), own = meta(cell), runNode = state.run && state.run.nodes[id];
    const last = own.last_run || null;
    const status = runNode ? runNode.status : (last ? last.status : null);
    el.dataset.status = status || '';
    const badge = el.querySelector('.status'); badge.textContent = status || 'not run'; badge.dataset.s = status || '';
    const elapsed = runNode && runNode.elapsed_ms != null ? runNode.elapsed_ms : (last ? last.elapsed_ms : null);
    el.querySelector('.timing').textContent = elapsed != null && status && status !== 'running' ? elapsed + ' ms' : '';
    el.querySelector('.count').textContent = 'In [' + (cell.execution_count == null ? ' ' : cell.execution_count) + ']';
    el.querySelector('.stale').hidden = !(last && last.source_hash && last.source_hash !== fnv(sourceOf(cell)) && !(runNode && runNode.status === 'running'));
    const needs = missingFor(id), needsEl = el.querySelector('.node-needs'); needsEl.hidden = !needs.length; needsEl.textContent = needs.length ? 'needs: ' + needs.join(', ') : '';
    const out = el.querySelector('.node-output'); let text = '', hasError = false;
    if (runNode && runNode.output !== undefined) { text = runNode.output || ''; if (runNode.error) { text += (text ? '\n' : '') + (runNode.error.traceback || []).join('\n'); hasError = true; } if (runNode.note) text += (text ? '\n' : '') + runNode.note; }
    else for (const o of cell.outputs || []) { if (o.output_type === 'stream') text += Array.isArray(o.text) ? o.text.join('') : o.text; else if (o.output_type === 'error') { text += (o.traceback || []).join('\n'); hasError = true; } }
    out.textContent = text.length > 4000 ? text.slice(0, 4000) + '\n…' : (text || (status === 'running' ? 'Running…' : ''));
    out.classList.toggle('has-error', hasError);
    const collapsed = own.collapsed || {};
    el.querySelector('textarea').hidden = !!collapsed.code; out.hidden = !!collapsed.output || (!text && status !== 'running');
    el.querySelector('.node-toggle[data-act="code"]').classList.toggle('off', !!collapsed.code); el.querySelector('.node-toggle[data-act="output"]').classList.toggle('off', !!collapsed.output);
    const area = el.querySelector('textarea'); if (area && document.activeElement !== area && area.value !== sourceOf(cell)) area.value = sourceOf(cell);
    if (area && !area.hidden) autosize(area);
  }
  function notebookPositions() {
    // Notebook view: the same cells, stacked in trigger order. Positions are computed, never saved.
    const out = {}; let y = 30; const x = 40;
    for (const id of state.order) { const n = nodeById(id); if (!n || n.kind === 'start' || n.kind === 'finish') continue; out[id] = {x, y}; y += box(id).h + 18; }
    for (const n of nodes()) if (!out[n.id] && n.kind !== 'start' && n.kind !== 'finish') { out[n.id] = {x, y}; y += box(n.id).h + 18; }
    return out;
  }
  function autosize(area) { area.style.height = 'auto'; area.style.height = Math.min(440, Math.max(56, area.scrollHeight + 2)) + 'px'; }
  function placeNode(id) {
    const el = elOf(id); if (!el) return;
    if (state.view === 'notebook') { const n = nodeById(id); if (n.kind === 'start' || n.kind === 'finish') { el.style.display = 'none'; return; } el.style.display = ''; const p = (state.notebookPos || {})[id] || {x: 40, y: 30}; el.style.left = p.x + 'px'; el.style.top = p.y + 'px'; return; }
    el.style.display = ''; const p = position(id); el.style.left = p.x + 'px'; el.style.top = p.y + 'px';
  }
  function layoutNotebook() { if (state.view !== 'notebook') return; state.notebookPos = notebookPositions(); nodes().forEach(n => placeNode(n.id)); }
  function box(id) {
    // Position comes from the document; size comes from the ResizeObserver cache. The one
    // offsetWidth read below happens only the first time a card is seen, before the observer fires.
    const p = (state.view === 'notebook' && state.notebookPos && state.notebookPos[id]) || position(id);
    let s = sizes.get(id);
    if (!s) { const el = elOf(id); s = el && el.offsetWidth ? {w: el.offsetWidth, h: el.offsetHeight} : {w: widthOf(nodeById(id)), h: 60}; if (el && el.offsetWidth) sizes.set(id, s); }
    return {x: p.x, y: p.y, w: s.w, h: s.h};
  }
  function curve(a, b) { const dy = Math.max(40, Math.abs(b.y - a.y) / 2); return `M${a.x} ${a.y} C${a.x} ${a.y + dy} ${b.x} ${b.y - dy} ${b.x} ${b.y}`; }
  const svgNS = 'http://www.w3.org/2000/svg';
  function edgeGeometry(entry) {
    // Where an edge starts and ends, in world px. Control edges leave the bottom centre of the
    // source; data edges leave near the right so the two kinds do not overlap.
    const a = box(entry.edge.source), b = box(entry.edge.target);
    if (entry.kind === 'control') return {start: {x: a.x + a.w / 2, y: a.y + a.h}, end: {x: b.x + b.w / 2, y: b.y}};
    return {start: {x: a.x + a.w - 28, y: a.y + a.h}, end: {x: b.x + b.w - 28, y: b.y}};
  }
  function positionEdge(entry) {
    const g = edgeGeometry(entry); entry.path.setAttribute('d', curve(g.start, g.end));
    if (entry.label) { entry.label.setAttribute('x', (g.start.x + g.end.x) / 2 + 6); entry.label.setAttribute('y', (g.start.y + g.end.y) / 2); }
  }
  function edgeClass(entry) {
    if (entry.kind !== 'control') return 'edge data';
    const fired = state.run && state.run.fired && state.run.fired.includes(entry.edge.id);
    const selected = state.selected && state.selected.type === 'edge' && state.selected.id === entry.edge.id;
    return 'edge control' + (fired ? ' fired' : '') + (selected ? ' selected' : '');
  }
  function drawEdges() {
    // Full rebuild: used when the set of edges changes (save, add/delete, data-edge toggle, view switch).
    // Moving nodes does not come here; it goes through updateEdgesFor().
    const layer = $('edge-layer'); layer.textContent = ''; edgePaths.clear();
    if (state.view === 'notebook') return;
    for (const edge of edges()) {
      if (edge.kind !== 'control') continue;
      const path = document.createElementNS(svgNS, 'path'); path.dataset.id = edge.id;
      const entry = {kind: 'control', edge, path, label: null}; edgePaths.set('c:' + edge.id, entry);
      path.setAttribute('class', edgeClass(entry)); positionEdge(entry); layer.append(path);
    }
    if ($('show-data').checked) state.dataEdges.forEach((edge, i) => {
      const path = document.createElementNS(svgNS, 'path'); path.setAttribute('class', 'edge data');
      const label = document.createElementNS(svgNS, 'text'); label.setAttribute('class', 'edge-label'); label.textContent = edge.name;
      const entry = {kind: 'data', edge, path, label}; edgePaths.set('d:' + i, entry);
      positionEdge(entry); layer.append(path, label);
    });
  }
  function updateEdgesFor(ids) {
    // Re-route only the edges that touch these nodes; everything else keeps its existing <path>.
    if (state.view === 'notebook') return;
    const set = ids instanceof Set ? ids : new Set(ids);
    for (const entry of edgePaths.values()) if (set.has(entry.edge.source) || set.has(entry.edge.target)) positionEdge(entry);
  }
  function restyleEdges() { for (const entry of edgePaths.values()) if (entry.kind === 'control') entry.path.setAttribute('class', edgeClass(entry)); }
  // One animation frame per burst of input. pointermove can arrive at 120+ Hz on a Mac; the
  // screen only shows 60-120 frames a second, so we note what changed and apply it once per frame.
  const frame = {pending: false, nodes: new Set(), viewport: false, layoutNotebook: false, link: null};
  function scheduleFrame(work) {
    if (work.nodes) for (const id of work.nodes) frame.nodes.add(id);
    if (work.viewport) frame.viewport = true;
    if (work.layoutNotebook) frame.layoutNotebook = true;
    if (work.link) frame.link = work.link;
    if (frame.pending) return;
    frame.pending = true;
    requestAnimationFrame(() => {
      frame.pending = false;
      const ids = [...frame.nodes]; frame.nodes.clear();
      if (frame.layoutNotebook) { frame.layoutNotebook = false; layoutNotebook(); }
      else for (const id of ids) placeNode(id);
      if (ids.length) updateEdgesFor(ids);
      if (frame.viewport) { frame.viewport = false; applyViewport(); }
      if (frame.link) { const l = frame.link; frame.link = null; const b = box(l.source); $('pending-edge').setAttribute('d', curve({x: b.x + b.w / 2, y: b.y + b.h}, l.point)); }
    });
  }
  function applyViewport() { $('world').style.transform = `translate(${state.pan.x}px,${state.pan.y}px) scale(${state.zoom})`; $('zoom-label').textContent = Math.round(state.zoom * 100) + '%'; state.doc.layout.viewport = {x: state.pan.x, y: state.pan.y, zoom: state.zoom}; }
  function worldPoint(event) { const r = $('viewport').getBoundingClientRect(); return {x: (event.clientX - r.left - state.pan.x) / state.zoom, y: (event.clientY - r.top - state.pan.y) / state.zoom}; }

  // ---------------------------------------------------------------- view state from server
  function reindex() { nodeIndex.clear(); for (const n of nodes()) nodeIndex.set(n.id, n); }
  function applyView(view) {
    if (view.document) { state.doc = view.document; reindex(); }
    state.analysis = view.analysis || {}; state.dataEdges = view.data_edges || []; state.order = view.order || []; state.disconnected = view.disconnected || []; state.revision = view.revision;
    if (view.session) state.session = view.session; if (view.csrf) state.csrf = view.csrf; if ('run' in view && view.run) state.run = view.run;
    if (view.library) state.libraryLinks = view.library;
  }
  function markDirty() { state.dirty = true; $('save-state').textContent = 'Unsaved'; $('save-state').dataset.state = 'dirty'; clearTimeout(state.saveTimer); state.saveTimer = setTimeout(save, 600); }
  async function save() {
    if (!state.dirty) return;
    try { const view = await api('/api/graph', {document: state.doc}); state.dirty = false; applyView(view); $('save-state').textContent = 'Saved'; $('save-state').dataset.state = ''; nodes().forEach(n => updateNode(n.id)); drawEdges(); renderInspector(); }
    catch (error) { $('save-state').textContent = error.status === 409 ? 'Waiting for run' : 'Not saved: ' + error.message; $('save-state').dataset.state = 'error'; if (error.status === 409) setTimeout(() => { state.dirty = true; save(); }, 1500); else toast(error.message); }
  }

  // ---------------------------------------------------------------- selection + inspector
  function select(type, id) {
    const before = state.selected; state.selected = type ? {type, id} : null;
    // Only the two cards whose selection state changed need repainting; edges change class only.
    if (before && before.type === 'node') updateNode(before.id);
    if (type === 'node') updateNode(id);
    restyleEdges(); renderInspector(); $('delete').disabled = !state.selected || (type === 'node' && (nodeById(id).kind === 'start' || nodeById(id).kind === 'finish')); const code = type === 'node' && nodeById(id).kind === 'code'; $('run-from').disabled = !code; $('run-one').disabled = !code; }
  function chips(container, names, missing) { container.textContent = ''; if (!names.length) { container.textContent = '—'; return; } for (const n of names) { const c = document.createElement('span'); c.className = 'chip' + (missing && missing.has(n) ? ' missing' : ''); c.textContent = n; container.append(c); } }
  function renderInspector() {
    const panel = $('node-panel'); const sel = state.selected && state.selected.type === 'node' ? nodeById(state.selected.id) : null;
    if (!sel || sel.kind !== 'code') { panel.hidden = true; }
    else {
      panel.hidden = false; const cell = cellOf(sel), info = state.analysis[sel.id] || {inputs: [], outputs: []};
      if (document.activeElement !== $('node-title')) $('node-title').value = meta(cell).title || '';
      chips($('node-inputs'), info.inputs, new Set(missingFor(sel.id))); chips($('node-outputs'), info.outputs);
      const by = edges().filter(e => e.kind === 'control' && e.target === sel.id).map(e => titleOf(nodeById(e.source))), to = edges().filter(e => e.kind === 'control' && e.source === sel.id).map(e => titleOf(nodeById(e.target)));
      $('node-links').textContent = `Triggered by: ${by.length ? by.join(', ') : '— (not connected)'} · Triggers: ${to.length ? to.join(', ') : '—'}`;
      $('node-analysis-note').textContent = info.error ? 'Analysis unavailable: ' + info.error : (info.declared ? 'Declared by you (overrides the automatic reading).' : 'Read automatically from the code; edit below to override.');
      const own = meta(cell); if (document.activeElement !== $('override-inputs')) $('override-inputs').value = (own.inputs || []).join(', '); if (document.activeElement !== $('override-outputs')) $('override-outputs').value = (own.outputs || []).join(', ');
      renderLibraryLink(sel.id);
      const r = state.run && state.run.nodes[sel.id]; const last = r || own.last_run;
      $('node-result').textContent = last && last.status ? `${last.status}${last.elapsed_ms != null ? ' · ' + last.elapsed_ms + ' ms' : ''}${r && r.error ? ' · ' + r.error.ename + (r.error.evalue ? ': ' + r.error.evalue : '') : ''}` : 'Not run.';
    }
    const list = $('run-list'); list.textContent = '';
    if (state.run) {
      $('run-summary').textContent = `${state.run.mode} · ${state.run.state}${state.run.error ? ' · ' + state.run.error : ''}`;
      for (const id of state.run.plan) { const li = document.createElement('li'); const n = nodeById(id); li.textContent = n ? titleOf(n) : id; const s = document.createElement('span'); s.className = 'status'; const rn = state.run.nodes[id] || {}; s.dataset.s = rn.status || ''; s.textContent = (rn.status || 'pending') + (rn.elapsed_ms != null ? ' ' + rn.elapsed_ms + ' ms' : ''); li.append(s); list.append(li); }
    } else $('run-summary').textContent = 'No run yet. Start a session, then choose Run all.';
    const alive = !!state.session.alive;
    $('session-state').textContent = alive ? (state.session.executing ? 'Session: running a cell' : 'Session: ready · Python ' + (state.session.python || '')) : (state.session.ended ? 'Session: stopped · ' + state.session.ended : 'Session: not started');
    $('session-state').dataset.state = alive ? (state.session.executing ? 'busy' : 'ready') : (state.session.ended ? 'dead' : '');
    $('session-start').disabled = alive; $('session-stop').disabled = !alive; $('session-interrupt').disabled = !alive || !state.session.executing;
    const running = state.run && (state.run.state === 'running' || state.run.state === 'pending');
    $('run-cancel').disabled = !running; $('run-all').disabled = !!running; $('variables-refresh').disabled = !alive || !!state.session.executing;
    if (state.readOnly !== !!running) { state.readOnly = !!running; document.querySelectorAll('.node textarea').forEach(a => { a.readOnly = !!running; }); }
  }

  // ---------------------------------------------------------------- editing
  function freeSpot(sel) {
    // Where a new card goes: below the selected card, or in the upper middle of the view; slid
    // down until it does not overlap an existing card in that column.
    const vp = $('viewport').getBoundingClientRect();
    const base = sel ? position(sel.id) : {x: (vp.width / 2 - state.pan.x) / state.zoom - NODE_W / 2, y: (vp.height / 3 - state.pan.y) / state.zoom};
    const spot = sel ? {x: base.x, y: base.y + box(sel.id).h + 80} : {...base};
    const overlaps = () => nodes().some(n => state.doc.layout.nodes[n.id] && Math.abs(state.doc.layout.nodes[n.id].x - spot.x) < NODE_W && spot.y < state.doc.layout.nodes[n.id].y + box(n.id).h + 20 && spot.y + 160 > state.doc.layout.nodes[n.id].y);
    for (let i = 0; i < 40 && overlaps(); i++) spot.y += 60;
    return {x: Math.round(spot.x), y: Math.round(spot.y)};
  }
  function addNode(kind) {
    const id = freshId(kind === 'code' ? 'cell-' : 'note-');
    const number = nodes().filter(n => n.kind === 'code').length + 1;
    const sel = state.selected && state.selected.type === 'node' ? nodeById(state.selected.id) : null;
    const spot = freeSpot(sel);
    cells().push({id, cell_type: kind, metadata: {mainaiger: {title: kind === 'code' ? 'Cell ' + number : 'Note'}}, source: '', ...(kind === 'code' ? {execution_count: null, outputs: []} : {})});
    nodes().push({id, kind, cell_index: cells().length - 1}); reindex();
    state.doc.layout.nodes[id] = spot;
    if (kind === 'code' && sel && sel.kind !== 'finish' && sel.kind !== 'markdown') {
      // Insert the new cell into the chain: S -> new, and if S pointed at Finish, new -> Finish instead.
      const finish = nodes().find(n => n.kind === 'finish');
      const toFinish = edges().find(e => e.kind === 'control' && e.source === sel.id && finish && e.target === finish.id);
      if (toFinish) { toFinish.target = id; edges().push({id: freshId('e'), kind: 'control', source: id, target: finish.id}); }
      else edges().push({id: freshId('e'), kind: 'control', source: sel.id, target: id});
    }
    buildNodes(); select('node', id); markDirty();
    const area = document.querySelector(`.node[data-id="${CSS.escape(id)}"] textarea`); if (area) area.focus();
  }
  function deleteSelected() {
    if (!state.selected) return;
    if (state.selected.type === 'edge') { state.doc.graph.edges = edges().filter(e => e.id !== state.selected.id); }
    else {
      const node = nodeById(state.selected.id); if (!node || node.kind === 'start' || node.kind === 'finish') return;
      const index = node.cell_index; cells().splice(index, 1);
      state.doc.graph.nodes = nodes().filter(n => n.id !== node.id); nodes().forEach(n => { if (n.cell_index != null && n.cell_index > index) n.cell_index -= 1; });
      state.doc.graph.edges = edges().filter(e => e.source !== node.id && e.target !== node.id); delete state.doc.layout.nodes[node.id];
    }
    select(null); buildNodes(); markDirty();
  }
  function addControlEdge(source, target) {
    if (source === target) return;
    const s = nodeById(source), t = nodeById(target); if (!s || !t) return;
    if (t.kind === 'start' || s.kind === 'finish' || s.kind === 'markdown' || t.kind === 'markdown') { toast('Triggers run from Start or a code cell into a code cell or Finish.'); return; }
    if (edges().some(e => e.kind === 'control' && e.source === source && e.target === target)) { toast('That trigger already exists.'); return; }
    if (controlReaches(target, source)) { toast('That would create a loop; loops are not supported yet.'); return; }
    edges().push({id: freshId('e'), kind: 'control', source, target}); drawEdges(); markDirty();
  }
  function arrange() {
    const layer = {}; for (const id of state.order) { const preds = edges().filter(e => e.kind === 'control' && e.target === id).map(e => layer[e.source] ?? 0); layer[id] = preds.length ? Math.max(...preds) + 1 : 0; }
    const finish = nodes().find(n => n.kind === 'finish'); const deepest = Math.max(0, ...Object.values(layer)); if (finish) layer[finish.id] = deepest;
    const rows = {}; for (const n of nodes()) { const l = state.disconnected.includes(n.id) || (n.kind === 'markdown') ? -1 : layer[n.id]; (rows[l] = rows[l] || []).push(n); }
    // Notes and disconnected cells form a column on the left (the plan before the code); the trigger graph is centered to its right.
    const side = rows[-1] || []; delete rows[-1];
    let sideY = 40; for (const n of side) { state.doc.layout.nodes[n.id] = {x: 40, y: sideY}; sideY += box(n.id).h + 40; }
    const shift = side.length ? NODE_W + 100 : 0;
    let y = 40; Object.keys(rows).map(Number).sort((a, b) => a - b).forEach(l => { const row = rows[l]; const total = row.reduce((w, n) => w + widthOf(n) + 60, -60); let x = Math.max(40, 560 - total / 2) + shift; let tallest = 0; for (const n of row) { state.doc.layout.nodes[n.id] = {x, y}; x += widthOf(n) + 60; tallest = Math.max(tallest, box(n.id).h); } y += tallest + 90; });
    nodes().forEach(n => placeNode(n.id)); drawEdges(); markDirty(); fit();
  }
  function fit() {
    const vp = $('viewport').getBoundingClientRect(); if (!vp.width || !nodes().length) return;
    let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
    for (const n of nodes()) { const b = box(n.id); left = Math.min(left, b.x); top = Math.min(top, b.y); right = Math.max(right, b.x + b.w); bottom = Math.max(bottom, b.y + b.h); }
    state.zoom = Math.max(0.25, Math.min(1.2, (vp.width - 60) / (right - left), (vp.height - 80) / (bottom - top)));
    state.pan = {x: (vp.width - (right - left) * state.zoom) / 2 - left * state.zoom, y: 30 - top * state.zoom + Math.max(0, (vp.height - 60 - (bottom - top) * state.zoom) / 2)};
    applyViewport();
  }

  // ---------------------------------------------------------------- pointer interactions
  const viewport = $('viewport');
  viewport.addEventListener('pointerdown', event => {
    const portOut = event.target.closest('.port.out'), head = event.target.closest('.node-head'), nodeEl = event.target.closest('.node'), edgeEl = event.target.closest('path.edge.control');
    if (event.button !== 0) return;
    if (event.target.closest('button')) return;  // header buttons keep their click; no drag, no capture
    if (state.view === 'notebook') { if (nodeEl) select('node', nodeEl.dataset.id); else if (!edgeEl) select(null); return; }  // notebook view: no dragging or linking
    if (portOut) { const id = nodeEl.dataset.id; state.linking = {source: id}; viewport.classList.add('linking'); viewport.setPointerCapture(event.pointerId); const p = worldPoint(event); $('pending-edge').classList.add('active'); $('pending-edge').setAttribute('d', curve({x: box(id).x + box(id).w / 2, y: box(id).y + box(id).h}, p)); event.preventDefault(); return; }
    if (head) { const id = nodeEl.dataset.id; select('node', id); const p = worldPoint(event), pos = position(id); state.dragging = {id, dx: p.x - pos.x, dy: p.y - pos.y}; viewport.setPointerCapture(event.pointerId); event.preventDefault(); return; }
    if (nodeEl) { select('node', nodeEl.dataset.id); return; }  // clicks inside textarea/output keep their default behavior
    if (edgeEl) { select('edge', edgeEl.dataset.id); return; }
    select(null); state.panning = {x: event.clientX - state.pan.x, y: event.clientY - state.pan.y}; viewport.classList.add('panning'); viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener('pointermove', event => {
    // Handlers only record where the pointer is; scheduleFrame() applies it once per frame.
    if (state.dragging) { const p = worldPoint(event); const pos = position(state.dragging.id); pos.x = Math.round(p.x - state.dragging.dx); pos.y = Math.round(p.y - state.dragging.dy); scheduleFrame({nodes: [state.dragging.id]}); return; }
    if (state.linking) {
      scheduleFrame({link: {source: state.linking.source, point: worldPoint(event)}});
      const under = document.elementFromPoint(event.clientX, event.clientY); const target = under && under.closest('.node'); const id = target && target.dataset.id !== state.linking.source ? target.dataset.id : null;
      if (id !== state.linking.hover) { const old = state.linking.hover && elOf(state.linking.hover); if (old) { const pin = old.querySelector('.port.in'); if (pin) pin.classList.remove('target'); } state.linking.hover = id; const pin = id && elOf(id).querySelector('.port.in'); if (pin) pin.classList.add('target'); }
      return;
    }
    if (state.panning) { state.pan = {x: event.clientX - state.panning.x, y: event.clientY - state.panning.y}; scheduleFrame({viewport: true}); }
  });
  function endPointer(event) {
    if (state.dragging) { const id = state.dragging.id; state.dragging = null; placeNode(id); updateEdgesFor([id]); markDirty(); }
    if (state.linking) { const under = document.elementFromPoint(event.clientX, event.clientY); const target = under && under.closest('.node'); $('pending-edge').classList.remove('active'); $('pending-edge').setAttribute('d', ''); frame.link = null; document.querySelectorAll('.port.target').forEach(x => x.classList.remove('target')); if (target) addControlEdge(state.linking.source, target.dataset.id); state.linking = null; viewport.classList.remove('linking'); }
    if (state.panning) { state.panning = null; viewport.classList.remove('panning'); applyViewport(); markDirty(); }
  }
  viewport.addEventListener('pointerup', endPointer); viewport.addEventListener('pointercancel', endPointer);
  viewport.addEventListener('wheel', event => {
    if (event.target.closest('textarea, .node-output')) return;
    event.preventDefault();
    if (state.view === 'notebook') { state.pan.y = Math.min(0, state.pan.y - event.deltaY); scheduleFrame({viewport: true}); return; }
    const factor = Math.exp(-event.deltaY * 0.0015); const zoom = Math.max(0.2, Math.min(2.5, state.zoom * factor));
    const r = viewport.getBoundingClientRect(), mx = event.clientX - r.left, my = event.clientY - r.top;
    state.pan = {x: mx - (mx - state.pan.x) * (zoom / state.zoom), y: my - (my - state.pan.y) * (zoom / state.zoom)}; state.zoom = zoom; scheduleFrame({viewport: true});
  }, {passive: false});
  $('nodes').addEventListener('input', event => {
    // A keystroke touches one card. autosize may change its height; the ResizeObserver then
    // re-routes only that card's edges (and re-stacks the notebook view) on the next frame.
    const area = event.target.closest('textarea'); if (!area) return; const node = nodeById(area.closest('.node').dataset.id); cellOf(node).source = area.value; updateNode(node.id); markDirty();
  });
  $('nodes').addEventListener('click', event => {
    const button = event.target.closest('.node-run, .node-toggle'); if (!button) return; const id = button.closest('.node').dataset.id; const act = button.dataset.act;
    if (act === 'all') { reviewRun('all', []); return; }
    if (act === 'add') { select('node', id); addNode('code'); return; }
    if (button.classList.contains('node-toggle')) { const own = meta(cellOf(nodeById(id))); own.collapsed = own.collapsed || {}; own.collapsed[act] = !own.collapsed[act]; updateNode(id); markDirty(); return; }
    select('node', id); reviewRun(act, [id]);
  });
  document.addEventListener('keydown', event => {
    const typing = event.target.closest('input, textarea');
    if (typing) {
      const cellEl = event.target.closest('.node.code');
      if (cellEl && event.key === 'Enter' && (event.shiftKey || event.ctrlKey || event.metaKey)) { event.preventDefault(); select('node', cellEl.dataset.id); reviewRun(event.shiftKey ? 'one' : 'from', [cellEl.dataset.id]); return; }
      if (event.key === 'Escape') event.target.blur(); return;
    }
    if (event.key === 'Delete' || event.key === 'Backspace') { if (state.selected) { event.preventDefault(); deleteSelected(); } }
    if (event.key === 'Escape') select(null);
  });

  // ---------------------------------------------------------------- toolbar
  $('add-cell').addEventListener('click', () => addNode('code')); $('add-note').addEventListener('click', () => addNode('markdown'));
  function setView(view) {
    state.view = view; document.body.dataset.view = view; $('view-graph').classList.toggle('active', view === 'graph'); $('view-notebook').classList.toggle('active', view === 'notebook');
    if (view === 'notebook') { state.savedPan = {pan: {...state.pan}, zoom: state.zoom}; state.zoom = 1; state.pan = {x: 0, y: 0}; applyViewport(); layoutNotebook(); }
    else { if (state.savedPan) { state.pan = state.savedPan.pan; state.zoom = state.savedPan.zoom; } nodes().forEach(n => placeNode(n.id)); applyViewport(); }
    drawEdges();
  }
  $('view-graph').addEventListener('click', () => setView('graph')); $('view-notebook').addEventListener('click', () => setView('notebook'));
  const exportPy = document.createElement('button'); exportPy.id = 'export-py'; exportPy.hidden = true; document.body.append(exportPy);
  $('export-py').addEventListener('click', () => {
    // Export Python: cells reachable from Start, in trigger order. Branches are flattened serially — the same order the Lab runs them.
    const lines = ['# Exported from MainAIger Lab — cells in trigger order (serial).', '# Notes become comments; outputs are not included.', ''];
    const reach = new Set(); const stack = [nodes().find(n => n.kind === 'start').id]; while (stack.length) { const c = stack.pop(); if (reach.has(c)) continue; reach.add(c); edges().filter(e => e.kind === 'control' && e.source === c).forEach(e => stack.push(e.target)); }
    for (const id of state.order) { const n = nodeById(id); if (!n || !reach.has(id)) continue; if (n.kind === 'code') lines.push(`# ---- ${titleOf(n)} (${id}) ----`, sourceOf(cellOf(n)).replace(/\s+$/, ''), ''); else if (n.kind === 'markdown') lines.push(...sourceOf(cellOf(n)).split('\n').map(l => '# ' + l), ''); }
    const skipped = nodes().filter(n => n.kind === 'code' && !reach.has(n.id)); if (skipped.length) lines.push('# Not reachable from Start (not exported): ' + skipped.map(titleOf).join(', '));
    const blob = new Blob([lines.join('\n') + '\n'], {type: 'text/x-python'}); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = fileStem() + '.py'; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $('clear-outputs').addEventListener('click', () => { for (const c of cells()) if (c.cell_type === 'code') { c.outputs = []; c.execution_count = null; delete meta(c).last_run; } state.run = null; nodes().forEach(n => updateNode(n.id)); drawEdges(); renderInspector(); markDirty(); });
  $('delete').addEventListener('click', deleteSelected); $('arrange').addEventListener('click', arrange); $('fit').addEventListener('click', fit);
  $('show-data').addEventListener('change', drawEdges);
  $('node-title').addEventListener('input', () => { const sel = state.selected && nodeById(state.selected.id); if (!sel || sel.kind !== 'code') return; meta(cellOf(sel)).title = $('node-title').value; updateNode(sel.id); markDirty(); });
  $('apply-override').addEventListener('click', () => { const sel = state.selected && nodeById(state.selected.id); if (!sel || sel.kind !== 'code') return; const own = meta(cellOf(sel)); const parse = v => v.split(',').map(s => s.trim()).filter(Boolean); const i = parse($('override-inputs').value), o = parse($('override-outputs').value); if (i.length) own.inputs = i; else delete own.inputs; if (o.length) own.outputs = o; else delete own.outputs; markDirty(); save(); });
  function downloadBlob(text, type, filename) { const blob = new Blob([text], {type}); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = filename; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  const fileStem = () => (docName() || 'graph').replace(/[^A-Za-z0-9 _-]/g, '').trim() || 'graph';
  $('export-menu').addEventListener('change', () => { const kind = $('export-menu').value; $('export-menu').value = ''; if (kind === 'ipymb') downloadBlob(JSON.stringify(state.doc, null, 2) + '\n', 'application/json', fileStem() + '.ipymb'); else if (kind === 'ipynb') $('export-ipynb').click(); else if (kind === 'py') $('export-py').click(); });
  $('help-toggle').addEventListener('click', () => { const open = $('help-hint').hidden; $('help-hint').hidden = !open; $('help-toggle').setAttribute('aria-expanded', String(open)); });
  $('open-file').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', async () => {
    const file = $('file-input').files[0]; $('file-input').value = ''; if (!file) return;
    try {
      if (file.size > 4000000) throw new Error('Choose a file smaller than 4 MB.');
      const data = JSON.parse(await file.text());
      let doc = data, report = null;
      if (!(data && data.format === 'mainaiger')) { const imported = await api('/api/graph/import', {notebook: data}); doc = imported.document; report = imported.report; }
      await openDocument(doc, file.name);
      if (report) showImportReport(file.name, report);
    } catch (error) { toast(error.message || 'The file could not be opened.'); }
  });
  async function openDocument(doc, label) {
    const view = await api('/api/graph', {document: doc}); applyView(view); state.run = null; state.dirty = false; $('save-state').textContent = 'Saved'; $('save-state').dataset.state = '';
    refreshEnvironment();
    const fresh = !Object.keys(state.doc.layout.nodes || {}).length;  // examples and imports carry no positions
    const vp = state.doc.layout.viewport || {}; state.pan = {x: vp.x || 40, y: vp.y || 30}; state.zoom = vp.zoom || 1; buildNodes(); select(null); applyViewport(); if (fresh) arrange(); else fit(); layoutNotebook(); renderInspector(); toast('Opened ' + label);
  }
  function showImportReport(name, report) {
    $('import-summary').textContent = `${name}: ${report.code_cells} code cell${report.code_cells === 1 ? '' : 's'}, ${report.notes} note${report.notes === 1 ? '' : 's'}` + (report.dropped ? `, ${report.dropped} unreadable cell(s) dropped` : '') + '.';
    const list = $('import-warnings'); list.textContent = '';
    if (!report.warnings.length) { const li = document.createElement('li'); li.textContent = 'No hidden-state or unsupported constructs found.'; list.append(li); }
    for (const w of report.warnings) { const li = document.createElement('li'); const k = document.createElement('span'); k.className = 'kind'; k.textContent = w.kind === 'input' ? 'HIDDEN STATE' : w.kind.toUpperCase(); li.append(k); const n = nodeById(w.cell); li.append(`${n ? titleOf(n) : w.cell}: ${w.detail}`); list.append(li); }
    $('import-dialog').showModal(); $('import-ok').focus();
  }
  $('import-ok').addEventListener('click', () => $('import-dialog').close());
  // ---- saved Mainbooks (owner-approved folder)
  let books = {configured: false, folder: null, default: '', mainbooks: []};
  async function refreshMainbooks() {
    try { books = await api('/api/mainbooks'); const select = $('open-mainbook'); select.textContent = ''; const first = document.createElement('option'); first.value = ''; first.textContent = books.configured ? (books.mainbooks.length ? 'Open Mainbook…' : 'Open Mainbook… (none saved yet)') : 'Open Mainbook… (choose a folder first)'; select.append(first);
      for (const m of books.mainbooks) { const o = document.createElement('option'); o.value = m.name; o.textContent = `${m.name}${m.cells != null ? ' (' + m.cells + ' cells)' : ''}`; select.append(o); } } catch {}
  }
  function docName() { const own = state.doc && state.doc.notebook.metadata && state.doc.notebook.metadata.mainaiger; return own && own.name ? own.name : ''; }
  $('save-mainbook').addEventListener('click', async () => {
    await refreshMainbooks();
    $('folder-section').hidden = books.configured; $('folder-input').value = books.folder || books.default || ''; $('folder-note').textContent = books.configured ? '' : 'Default shown; you can change it. The folder is created if it does not exist.';
    $('name-input').value = docName(); $('save-note').textContent = books.configured ? `Saved into ${books.folder}. Letters, digits, spaces, dashes and underscores.` : 'Letters, digits, spaces, dashes and underscores. The file becomes name.ipymb.';
    $('save-dialog').showModal(); (books.configured ? $('name-input') : $('folder-input')).focus();
  });
  $('save-cancel').addEventListener('click', () => $('save-dialog').close());
  $('save-confirm').addEventListener('click', async () => {
    const name = $('name-input').value.trim(); if (!name) { $('save-note').textContent = 'Enter a name.'; return; }
    try {
      if (!books.configured) { books = await api('/api/mainbooks/folder', {folder: $('folder-input').value, confirm: true}); $('folder-section').hidden = true; }
      if (state.dirty) { clearTimeout(state.saveTimer); await save(); }
      let result;
      try { result = await api('/api/mainbooks/save', {name, overwrite: false}); }
      catch (error) { if (error.status === 409 && window.confirm(`Replace the existing Mainbook "${name}"?`)) result = await api('/api/mainbooks/save', {name, overwrite: true}); else throw error; }
      $('save-dialog').close(); const view = await api('/api/graph'); const run = state.run; applyView(view); state.run = run; $('save-state').textContent = 'Saved · ' + name; toast(`Saved ${name}.ipymb in ${books.folder}`); refreshMainbooks();
    } catch (error) { $('save-note').textContent = error.message; }
  });
  $('open-mainbook').addEventListener('change', async () => {
    const name = $('open-mainbook').value; $('open-mainbook').value = ''; if (!name) return;
    try { const data = await api('/api/mainbooks/' + encodeURIComponent(name)); await openDocument(data.document, name + '.ipymb'); $('save-state').textContent = 'Saved · ' + name; } catch (error) { toast(error.message); }
  });
  document.addEventListener('keydown', event => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); $('save-mainbook').click(); } });
  $('open-example').addEventListener('change', async () => {
    const name = $('open-example').value; $('open-example').value = ''; if (!name) return;
    try { const data = await api('/examples/' + encodeURIComponent(name)); await openDocument(data.document, name); } catch (error) { toast(error.message); }
  });
  const exportIpynb = document.createElement('button'); exportIpynb.id = 'export-ipynb'; exportIpynb.hidden = true; document.body.append(exportIpynb);
  $('export-ipynb').addEventListener('click', async () => {
    if (state.dirty) { clearTimeout(state.saveTimer); await save(); }
    try { const data = await api('/api/graph/export'); const blob = new Blob([JSON.stringify(data.notebook, null, 1) + '\n'], {type: 'application/x-ipynb+json'}); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = fileStem() + '.ipynb'; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast(data.note); } catch (error) { toast(error.message); }
  });
  function fromNotebook(nb) {
    if (!nb || nb.nbformat !== 4 || !Array.isArray(nb.cells)) throw new Error('Choose a .ipymb graph or an nbformat 4 .ipynb notebook.');
    const used = new Set(); const nodeList = [{id: 'start', kind: 'start'}]; const edgeList = []; let previous = 'start'; let count = 0;
    const cellsOut = nb.cells.map((cell, index) => {
      let id = typeof cell.id === 'string' && cell.id && !used.has(cell.id) ? cell.id : 'cell-' + (index + 1); while (used.has(id) || id === 'start' || id === 'finish') id += '-x'; used.add(id);
      const type = cell.cell_type === 'raw' ? 'markdown' : cell.cell_type; const copy = {...cell, id, cell_type: type, metadata: {...(cell.metadata || {})}};
      copy.metadata.mainaiger = {...(copy.metadata.mainaiger || {})}; if (!copy.metadata.mainaiger.title) copy.metadata.mainaiger.title = type === 'code' ? 'Cell ' + (++count) : 'Note';
      if (type === 'code') { copy.outputs = Array.isArray(copy.outputs) ? copy.outputs : []; copy.execution_count = Number.isInteger(copy.execution_count) ? copy.execution_count : null; }
      nodeList.push({id, kind: type, cell_index: index}); if (type === 'code') { edgeList.push({id: 'e' + edgeList.length, kind: 'control', source: previous, target: id}); previous = id; } return copy;
    });
    nodeList.push({id: 'finish', kind: 'finish'}); edgeList.push({id: 'e' + edgeList.length, kind: 'control', source: previous, target: 'finish'});
    return {format: 'mainaiger', schema_version: 2, notebook: {...nb, cells: cellsOut}, graph: {nodes: nodeList, edges: edgeList}, layout: {nodes: {}, viewport: {x: 40, y: 30, zoom: 1}}};
  }

  // ---------------------------------------------------------------- cell library
  // One global folder of reusable cells (local_lab/library.py). The server owns the files; the
  // browser lists, searches, inserts, saves and pulls through /api/library/*. A cell inserted from
  // the library carries metadata.mainaiger.library = {name, version, source_hash}; every graph view
  // reports, per node, whether it is current, behind, modified locally, or missing from the library.
  let library = {configured: false, folder: null, default: '', cells: [], starters: []};
  state.libraryLinks = {};
  async function refreshLibrary() {
    try { library = await api('/api/library' + ($('library-search').value.trim() ? '?q=' + encodeURIComponent($('library-search').value.trim()) : '')); } catch (error) { $('library-note').textContent = error.message; return; }
    renderLibrary();
  }
  function renderLibrary() {
    const list = $('library-list'); list.textContent = '';
    $('library-folder').hidden = library.configured; $('library-seed').hidden = !library.configured || !library.starters.length;
    if (!library.configured) $('library-note').textContent = 'Reusable cells shared by every Mainbook. Approve a Library folder to start.';
    else if (!library.cells.length) $('library-note').textContent = library.query ? 'No cells match.' : `Library folder: ${library.folder} (empty). Save a cell from a graph or add the starter cells.`;
    else $('library-note').textContent = `${library.count} cell${library.count === 1 ? '' : 's'} in ${library.folder}. Drag onto the canvas or press Insert.`;
    for (const c of library.cells) {
      const li = document.createElement('li'); li.dataset.name = c.name; li.draggable = !c.unreadable;
      const head = document.createElement('div'); head.className = 'lib-title'; const b = document.createElement('b'); b.textContent = c.title; b.title = c.name; head.append(b);
      if (!c.unreadable) head.append(button('quiet small lib-insert', 'insert', 'Insert this cell after the selected cell (or into the view)', 'Insert'));
      li.append(head);
      const meta = document.createElement('div'); meta.className = 'lib-meta'; meta.textContent = `${c.name} v${c.version ?? '?'}` + (c.tags.length ? ' · ' + c.tags.join(', ') : '') + (c.inputs.length || c.outputs.length ? ` · ${c.inputs.join(', ') || '—'} → ${c.outputs.join(', ') || '—'}` : ''); li.append(meta);
      if (c.description) { const d = document.createElement('div'); d.className = 'lib-desc'; d.textContent = c.description; li.append(d); }
      list.append(li);
    }
  }
  function renderLibraryLink(id) {
    const link = state.libraryLinks[id]; const el = $('node-library'); el.dataset.state = link ? link.state : '';
    $('lib-pull').hidden = !link || link.state !== 'behind'; $('lib-push').hidden = !link || link.state !== 'modified';
    $('lib-save').textContent = link ? 'Save as new library cell…' : 'Save to Library…';
    if (!link) { el.textContent = 'Not from the library.'; return; }
    const text = {current: `${link.name} v${link.version} · up to date`, behind: `${link.name} v${link.version} · the library has v${link.latest}`, modified: `${link.name} v${link.version} · edited here since it was inserted`, missing: `${link.name} v${link.version} · no longer in the library`};
    el.textContent = text[link.state] || `${link.name} v${link.version}`;
  }
  async function afterLibraryChange(view, selectId) {
    const run = state.run; applyView(view); state.run = run; state.dirty = false; $('save-state').textContent = 'Saved'; $('save-state').dataset.state = '';
    buildNodes(); if (selectId) select('node', selectId); else renderInspector(); layoutNotebook();
  }
  async function insertFromLibrary(name, position, after) {
    if (state.dirty) { clearTimeout(state.saveTimer); await save(); if (state.dirty) return; }
    try { const data = await api('/api/library/insert', {name, position, after: after || null}); await afterLibraryChange(data.view, data.node); toast(`Inserted ${name} from the Library.`); }
    catch (error) { toast(error.message); }
  }
  function insertSelectedLibraryCell(name) {
    const sel = state.selected && state.selected.type === 'node' ? nodeById(state.selected.id) : null;
    const chain = sel && (sel.kind === 'start' || sel.kind === 'code');
    insertFromLibrary(name, freeSpot(chain ? sel : null), chain ? sel.id : null);
  }
  $('library-list').addEventListener('click', event => { const b = event.target.closest('.lib-insert'); if (!b) return; insertSelectedLibraryCell(b.closest('li').dataset.name); });
  $('library-list').addEventListener('dragstart', event => { const li = event.target.closest('li[draggable="true"]'); if (!li) return; event.dataTransfer.setData('text/x-mainaiger-cell', li.dataset.name); event.dataTransfer.effectAllowed = 'copy'; li.classList.add('dragging'); });
  $('library-list').addEventListener('dragend', event => { const li = event.target.closest('li'); if (li) li.classList.remove('dragging'); });
  viewport.addEventListener('dragover', event => { if (![...event.dataTransfer.types].includes('text/x-mainaiger-cell') || state.view === 'notebook') return; event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; viewport.classList.add('drop-target'); });
  viewport.addEventListener('dragleave', () => viewport.classList.remove('drop-target'));
  viewport.addEventListener('drop', event => {
    viewport.classList.remove('drop-target'); const name = event.dataTransfer.getData('text/x-mainaiger-cell'); if (!name || state.view === 'notebook') return; event.preventDefault();
    const p = worldPoint(event); insertFromLibrary(name, {x: Math.round(p.x - NODE_W / 2), y: Math.round(p.y - 20)}, null);
  });
  let searchTimer = null; $('library-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(refreshLibrary, 250); });
  $('library-refresh').addEventListener('click', refreshLibrary);
  // Folder approval (once).
  function openLibraryFolderDialog(then) {
    $('library-folder-input').value = library.folder || library.default || ''; $('library-folder-note').textContent = 'Default shown; you can change it. The folder is created if it does not exist.';
    state.afterLibraryFolder = then || null; $('library-folder-dialog').showModal(); $('library-folder-input').focus();
  }
  $('library-folder').addEventListener('click', () => openLibraryFolderDialog(null));
  $('library-folder-cancel').addEventListener('click', () => $('library-folder-dialog').close());
  $('library-folder-confirm').addEventListener('click', async () => {
    try { library = await api('/api/library/folder', {folder: $('library-folder-input').value, confirm: true}); $('library-folder-dialog').close(); await refreshLibrary(); const then = state.afterLibraryFolder; state.afterLibraryFolder = null; if (then) then(); }
    catch (error) { $('library-folder-note').textContent = error.message; }
  });
  // Starter cells.
  $('library-seed').addEventListener('click', () => { $('library-seed-dialog').showModal(); $('library-seed-cancel').focus(); });
  $('library-seed-cancel').addEventListener('click', () => $('library-seed-dialog').close());
  $('library-seed-confirm').addEventListener('click', async () => {
    $('library-seed-dialog').close();
    try { const r = await api('/api/library/seed', {confirm: true}); toast(r.added.length ? `Added ${r.added.length} starter cells.` : 'The starter cells were already in your Library.'); refreshLibrary(); } catch (error) { toast(error.message); }
  });
  // Save / push a cell of this graph.
  function openLibrarySaveDialog(push) {
    const sel = state.selected && nodeById(state.selected.id); if (!sel || sel.kind !== 'code') return;
    if (!library.configured) { openLibraryFolderDialog(() => openLibrarySaveDialog(push)); return; }
    const link = state.libraryLinks[sel.id]; const own = meta(cellOf(sel));
    state.librarySave = {node: sel.id, push: !!push && !!link};
    $('library-save-intro').textContent = push && link ? `Saves the edited code of "${titleOf(sel)}" as ${link.name} v${link.latest + 1}. Other Mainbooks that use it will show "the library has v${link.latest + 1}".` : `Saves "${titleOf(sel)}" (code, title, inputs → outputs) as a new library cell.`;
    $('library-name').value = push && link ? link.name : (own.title || sel.id).toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60); $('library-name').readOnly = !!(push && link);
    const existing = link && library.cells.find(c => c.name === link.name);
    $('library-tags').value = existing ? existing.tags.join(', ') : ''; $('library-description').value = existing ? existing.description : '';
    $('library-save-note').textContent = 'Letters, digits, dashes and underscores. The file becomes name.cell.json.';
    $('library-save-dialog').showModal(); (push && link ? $('library-description') : $('library-name')).focus();
  }
  $('lib-save').addEventListener('click', () => openLibrarySaveDialog(false));
  $('lib-push').addEventListener('click', () => openLibrarySaveDialog(true));
  $('library-save-cancel').addEventListener('click', () => $('library-save-dialog').close());
  $('library-save-confirm').addEventListener('click', async () => {
    const job = state.librarySave; if (!job) return;
    const name = $('library-name').value.trim(); if (!name) { $('library-save-note').textContent = 'Enter a name.'; return; }
    const tags = $('library-tags').value.split(',').map(t => t.trim()).filter(Boolean);
    if (state.dirty) { clearTimeout(state.saveTimer); await save(); if (state.dirty) return; }
    try {
      let result;
      try { result = await api('/api/library/save', {node: job.node, name, tags, description: $('library-description').value, overwrite: job.push}); }
      catch (error) { if (error.status === 409 && window.confirm(`A library cell named "${name}" exists. Save this as its next version?`)) result = await api('/api/library/save', {node: job.node, name, tags, description: $('library-description').value, overwrite: true}); else throw error; }
      $('library-save-dialog').close(); await afterLibraryChange(result.view, job.node); toast(`Saved ${result.name} v${result.version} to the Library.`); refreshLibrary();
    } catch (error) { $('library-save-note').textContent = error.message; }
  });
  $('lib-pull').addEventListener('click', async () => {
    const sel = state.selected && nodeById(state.selected.id); if (!sel) return; const link = state.libraryLinks[sel.id]; if (!link) return;
    if (!window.confirm(`Replace the code of "${titleOf(sel)}" with ${link.name} v${link.latest} from the Library? Outputs are cleared.`)) return;
    if (state.dirty) { clearTimeout(state.saveTimer); await save(); if (state.dirty) return; }
    try { const data = await api('/api/library/pull', {node: sel.id}); await afterLibraryChange(data.view, sel.id); toast(`Updated to ${link.name} v${link.latest}.`); } catch (error) { toast(error.message); }
  });

  // ---------------------------------------------------------------- environment (derived image)
  // The Mainbook declares notebook.metadata.mainaiger.environment.packages; the server turns that into
  // a Dockerfile, an image tag and the exact docker build command (local_lab/environment.py). Building
  // is one reviewed step; the session then uses the derived image with every limit unchanged.
  let environment = {derived: false, ready: true, image: '', packages: [], build: null};
  function envPackagesInDoc() { const own = state.doc && state.doc.notebook.metadata && state.doc.notebook.metadata.mainaiger; return own && own.environment && Array.isArray(own.environment.packages) ? own.environment.packages : []; }
  async function refreshEnvironment() {
    try { environment = await api('/api/environment'); } catch (error) { $('env-status').textContent = error.message; return; }
    renderEnvironment();
    if (environment.build && environment.build.state === 'running') { clearTimeout(state.envTimer); state.envTimer = setTimeout(refreshEnvironment, 1500); }
  }
  function renderEnvironment() {
    if (document.activeElement !== $('env-packages')) $('env-packages').value = envPackagesInDoc().join('\n');
    const b = environment.build, status = $('env-status');
    $('env-build').hidden = !environment.derived || environment.ready || (b && b.state === 'running');
    if (!environment.derived) { status.textContent = `Base image ${environment.base_image || environment.image} (standard library only). Add packages to build a derived image.`; status.dataset.state = ''; }
    else if (b && b.state === 'running' && b.image === environment.image) { status.textContent = `Building ${environment.image}…`; status.dataset.state = 'running'; }
    else if (environment.ready) { status.textContent = `Image ${environment.image} is built: ${environment.packages.join(', ')}. Sessions started now use it.`; status.dataset.state = 'ready'; }
    else if (b && b.state === 'failed' && b.image === environment.image) { status.textContent = `Build failed: ${b.error}`; status.dataset.state = 'failed'; }
    else { status.textContent = `Image ${environment.image} is not built yet; sessions cannot start until it is.`; status.dataset.state = 'missing'; }
    const log = $('env-log'); const show = b && (b.state === 'running' || b.state === 'failed' || (b.state === 'done' && b.image === environment.image));
    log.hidden = !show; if (show) { log.textContent = (b.log_truncated ? '…\n' : '') + (b.log || '(waiting for output)'); log.scrollTop = log.scrollHeight; }
  }
  $('env-apply').addEventListener('click', async () => {
    const packages = $('env-packages').value.split(/[\n,]/).map(p => p.trim()).filter(Boolean);
    const own = state.doc.notebook.metadata.mainaiger = state.doc.notebook.metadata.mainaiger || {};
    if (packages.length) own.environment = {packages}; else delete own.environment;
    markDirty(); clearTimeout(state.saveTimer); await save();
    if (state.dirty) { renderEnvironment(); return; }  // the server said why (toast + save-state); the list stays as typed
    await refreshEnvironment(); toast(packages.length ? 'Package list saved in this Mainbook.' : 'No packages: this Mainbook uses the base image.');
  });
  $('env-build').addEventListener('click', () => {
    $('env-dockerfile').textContent = environment.dockerfile || ''; $('env-command').textContent = (environment.command || []).join(' ');
    $('env-dialog-note').textContent = `Creates image ${environment.image}. Builds can take minutes; the log appears in the Environment section.`;
    $('env-dialog').showModal(); $('env-cancel').focus();
  });
  $('env-cancel').addEventListener('click', () => $('env-dialog').close());
  $('env-confirm').addEventListener('click', async () => {
    $('env-dialog').close();
    try { environment = await api('/api/environment/build', {confirm: true, image: environment.image}); renderEnvironment(); toast('Build started.'); clearTimeout(state.envTimer); state.envTimer = setTimeout(refreshEnvironment, 1500); }
    catch (error) { toast(error.message); }
  });

  // ---------------------------------------------------------------- session
  $('session-start').addEventListener('click', () => { $('session-image').textContent = environment.derived ? `Image: ${environment.image} (${environment.packages.join(', ')})` + (environment.ready ? '' : ' — not built yet; build the environment first.') : `Image: ${environment.image} (base, standard library only).`; $('session-dialog').showModal(); $('session-cancel').focus(); });
  $('session-cancel').addEventListener('click', () => $('session-dialog').close());
  $('session-confirm').addEventListener('click', async () => { $('session-dialog').close(); try { const st = await api('/api/state'); state.csrf = st.csrf; state.session = await api('/api/kernel/start', {confirm: true, revision: st.revision}); toast('Session started.'); renderInspector(); refreshVariables(); } catch (error) { toast(error.message); } });
  $('session-stop').addEventListener('click', async () => { try { state.session = await api('/api/kernel/stop', {}); toast('Session stopped; variables are gone.'); renderInspector(); $('variables').hidden = true; $('variables-note').textContent = 'Start a session to inspect variables.'; } catch (error) { toast(error.message); } });
  $('session-interrupt').addEventListener('click', async () => { try { await api('/api/kernel/interrupt', {}); toast('Interrupt sent.'); } catch (error) { toast(error.message); } });
  $('variables-refresh').addEventListener('click', refreshVariables);
  async function refreshVariables() {
    if (!state.session.alive || state.session.executing) return;
    try { const data = await api('/api/kernel/variables'); state.variables = new Set((data.variables || []).map(v => v.name)); nodes().forEach(n => updateNode(n.id)); const body = $('variables').querySelector('tbody'); body.textContent = ''; for (const v of data.variables || []) { const tr = document.createElement('tr'); for (const k of ['name', 'type', 'summary']) { const td = document.createElement('td'); td.textContent = v[k]; tr.append(td); } body.append(tr); } $('variables').hidden = false; $('variables-note').textContent = (data.variables || []).length ? 'Names defined in this session.' : 'No variables defined yet.'; }
    catch (error) { $('variables-note').textContent = error.message; }
  }

  // ---------------------------------------------------------------- runs
  async function reviewRun(mode, ids) {
    if (state.dirty) { clearTimeout(state.saveTimer); await save(); if (state.dirty) return; }
    let check; try { check = await api('/api/graph/preflight', {mode, nodes: ids, include_prerequisites: false}); } catch (error) { toast(error.message); return; }
    state.pendingRun = {mode, nodes: ids, check};
    $('run-dialog-intro').textContent = mode === 'all' ? 'Every cell reachable from Start runs in order, one at a time, in your session.' : mode === 'from' ? 'The selected cell and everything it triggers run in order.' : 'Only the selected cell runs.';
    const plan = $('run-dialog-plan'); plan.textContent = '';
    for (const id of check.plan) { const li = document.createElement('li'); const n = nodeById(id); li.textContent = n ? titleOf(n) : id; plan.append(li); }
    const missing = $('run-dialog-missing'), list = $('run-dialog-missing-list'); list.textContent = '';
    if (check.missing.length) { missing.hidden = false; for (const m of check.missing) { const li = document.createElement('li'); const n = nodeById(m.node); li.textContent = `${n ? titleOf(n) : m.node} needs "${m.name}"` + (m.producers.length ? ` — produced by ${m.producers.map(p => titleOf(nodeById(p))).join(', ')}` : ' — no cell in this graph defines it'); list.append(li); } $('include-prereqs').checked = check.prerequisites.length > 0; $('include-prereqs').disabled = !check.prerequisites.length; } else missing.hidden = true;
    const notes = []; if (check.from_session.length) notes.push('Already in the session: ' + [...new Set(check.from_session.map(x => x.name))].join(', ') + '.'); if (check.unresolvable.length) notes.push('Inputs nobody produces will fail at run time unless they already exist in the session.'); if (!check.session_alive) notes.push('No session is running — start one first.');
    $('run-dialog-note').textContent = notes.join(' '); $('run-dialog-confirm').disabled = !check.session_alive || !check.plan.length;
    $('run-dialog').showModal(); $('run-dialog-cancel').focus();
  }
  $('run-all').addEventListener('click', () => reviewRun('all', []));
  $('run-from').addEventListener('click', () => state.selected && reviewRun('from', [state.selected.id]));
  $('run-one').addEventListener('click', () => state.selected && reviewRun('one', [state.selected.id]));
  $('run-dialog-cancel').addEventListener('click', () => $('run-dialog').close());
  $('run-dialog-confirm').addEventListener('click', async () => {
    $('run-dialog').close(); const pending = state.pendingRun; if (!pending) return;
    try { const data = await api('/api/graph/run', {mode: pending.mode, nodes: pending.nodes, confirm: true, include_prerequisites: $('include-prereqs').checked && !$('include-prereqs').disabled}); state.run = data.run; nodes().forEach(n => updateNode(n.id)); drawEdges(); renderInspector(); poll(); }
    catch (error) { toast(error.message); }
  });
  $('run-cancel').addEventListener('click', async () => { try { await api('/api/graph/cancel', {}); toast('Cancelling after the current cell…'); } catch (error) { toast(error.message); } });
  function applyRunSnapshot(run) {
    // Repaint only the cards whose run entry changed since the last snapshot, and restyle edges
    // (the "fired" class) without rebuilding them.
    const before = state.run; state.run = run;
    const changed = new Set();
    const ids = new Set([...(before ? Object.keys(before.nodes) : []), ...(run ? Object.keys(run.nodes) : [])]);
    for (const id of ids) { const a = before && before.nodes[id], b = run && run.nodes[id]; if (JSON.stringify(a) !== JSON.stringify(b)) changed.add(id); }
    const finish = nodes().find(n => n.kind === 'finish'); if (finish) changed.add(finish.id);
    for (const id of changed) updateNode(id);
    restyleEdges();
    return changed.size > 1;
  }
  async function poll() {
    clearTimeout(state.pollTimer);
    try {
      const data = await api('/api/graph/run'); state.session = data.session || state.session; const moved = applyRunSnapshot(data.run); renderInspector();
      if (state.run && (state.run.state === 'running' || state.run.state === 'pending')) {
        // Poll quickly right after something changed (a cell just finished), back off while a long cell runs.
        state.pollDelay = moved ? 250 : Math.min(1500, (state.pollDelay || 250) * 1.5);
        state.pollTimer = setTimeout(poll, state.pollDelay); return;
      }
      state.pollDelay = 250;
      const view = await api('/api/graph'); const run = state.run; applyView(view); state.run = run; nodes().forEach(n => updateNode(n.id)); layoutNotebook(); drawEdges(); renderInspector(); refreshVariables();
      toast(state.run ? `Run ${state.run.state}${state.run.error ? ': ' + state.run.error : ''}` : 'Run finished');
      if (state.dirty) save();
    } catch (error) { toast(error.message); }
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    try { const view = await api('/api/graph'); applyView(view); $('graph-main').hidden = false; refreshMainbooks(); refreshLibrary(); refreshEnvironment(); try { const ex = await api('/api/examples'); for (const e of ex.examples || []) { const o = document.createElement('option'); o.value = e.name; o.textContent = `${e.title} (${e.cells} cells)`; $('open-example').append(o); } } catch {} const vp = state.doc.layout.viewport || {}; state.pan = {x: vp.x ?? 40, y: vp.y ?? 30}; state.zoom = vp.zoom || 1; buildNodes(); applyViewport(); renderInspector(); if (state.run && (state.run.state === 'running' || state.run.state === 'pending')) poll(); if (state.session.alive) refreshVariables(); }
    catch (error) { if (!$('locked').hidden) return; toast(error.message); }
  }
  window.addEventListener('resize', () => { drawEdges(); layoutNotebook(); });
  // Keep the session pill truthful when the session is started or stopped from another tab or page.
  setInterval(async () => { if (document.hidden || (state.run && (state.run.state === 'running' || state.run.state === 'pending'))) return; try { const s = await api('/api/kernel'); const was = state.session.alive; state.session = s; if (was !== s.alive) { renderInspector(); if (s.alive) refreshVariables(); } else renderInspector(); } catch {} }, 5000);
  boot();
})();
