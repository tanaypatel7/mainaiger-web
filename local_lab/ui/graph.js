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
  const nodeById = id => nodes().find(n => n.id === id);
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
  function buildNodes() {
    const root = $('nodes'); root.textContent = '';
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
        const area = document.createElement('textarea'); area.spellcheck = false; area.value = sourceOf(cellOf(node)); area.placeholder = node.kind === 'code' ? '# Python — Shift+Enter runs this cell' : 'Write a note…'; area.setAttribute('aria-label', node.kind === 'code' ? 'Python code' : 'Note'); el.append(area);
        if (node.kind === 'code') { const out = document.createElement('pre'); out.className = 'node-output'; el.append(out); const foot = document.createElement('div'); foot.className = 'node-foot'; const status = document.createElement('span'); status.className = 'status'; foot.append(status); const timing = document.createElement('span'); timing.className = 'timing'; foot.append(timing); const stale = document.createElement('span'); stale.className = 'stale'; stale.textContent = 'stale'; stale.title = 'The code changed after this output was produced'; stale.hidden = true; foot.append(stale); el.append(foot); }
      }
      if (node.kind === 'finish') { const foot = document.createElement('div'); foot.className = 'finish-note'; el.append(foot); }
      if (node.kind !== 'finish' && node.kind !== 'markdown') { const pout = document.createElement('span'); pout.className = 'port out'; pout.title = 'Drag to another cell to add a trigger'; el.append(pout); }
      root.append(el);
      updateNode(node.id);
      placeNode(node.id);
    }
    drawEdges();
  }
  function updateNode(id) {
    const node = nodeById(id), el = document.querySelector(`.node[data-id="${CSS.escape(id)}"]`); if (!node || !el) return;
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
  }
  function notebookPositions() {
    // Notebook view: the same cells, stacked in trigger order. Positions are computed, never saved.
    const out = {}; let y = 30; const x = 40;
    for (const id of state.order) { const n = nodeById(id); if (!n || n.kind === 'start' || n.kind === 'finish') continue; out[id] = {x, y}; y += box(id).h + 18; }
    for (const n of nodes()) if (!out[n.id] && n.kind !== 'start' && n.kind !== 'finish') { out[n.id] = {x, y}; y += box(n.id).h + 18; }
    return out;
  }
  function placeNode(id) {
    const el = document.querySelector(`.node[data-id="${CSS.escape(id)}"]`); if (!el) return;
    if (state.view === 'notebook') { const n = nodeById(id); if (n.kind === 'start' || n.kind === 'finish') { el.style.display = 'none'; return; } el.style.display = ''; const p = (state.notebookPos || {})[id] || {x: 40, y: 30}; el.style.left = p.x + 'px'; el.style.top = p.y + 'px'; return; }
    el.style.display = ''; const p = position(id); el.style.left = p.x + 'px'; el.style.top = p.y + 'px';
  }
  function layoutNotebook() { if (state.view !== 'notebook') return; state.notebookPos = {}; nodes().forEach(n => placeNode(n.id)); state.notebookPos = notebookPositions(); nodes().forEach(n => placeNode(n.id)); }
  function box(id) { const el = document.querySelector(`.node[data-id="${CSS.escape(id)}"]`), p = (state.view === 'notebook' && state.notebookPos && state.notebookPos[id]) || position(id); return {x: p.x, y: p.y, w: el ? el.offsetWidth : widthOf(nodeById(id)), h: el ? el.offsetHeight : 60}; }
  function curve(a, b) { const dy = Math.max(40, Math.abs(b.y - a.y) / 2); return `M${a.x} ${a.y} C${a.x} ${a.y + dy} ${b.x} ${b.y - dy} ${b.x} ${b.y}`; }
  function drawEdges() {
    const layer = $('edge-layer'); layer.textContent = '';
    if (state.view === 'notebook') return;
    const fired = new Set(state.run ? state.run.fired : []);
    const svgNS = 'http://www.w3.org/2000/svg';
    for (const edge of edges().filter(e => e.kind === 'control')) {
      const a = box(edge.source), b = box(edge.target);
      const path = document.createElementNS(svgNS, 'path');
      path.setAttribute('d', curve({x: a.x + a.w / 2, y: a.y + a.h}, {x: b.x + b.w / 2, y: b.y}));
      path.setAttribute('class', 'edge control' + (fired.has(edge.id) ? ' fired' : '') + (state.selected && state.selected.type === 'edge' && state.selected.id === edge.id ? ' selected' : ''));
      path.dataset.id = edge.id; layer.append(path);
    }
    if ($('show-data').checked) for (const edge of state.dataEdges) {
      const a = box(edge.source), b = box(edge.target);
      const path = document.createElementNS(svgNS, 'path');
      const start = {x: a.x + a.w - 28, y: a.y + a.h}, end = {x: b.x + b.w - 28, y: b.y};
      path.setAttribute('d', curve(start, end)); path.setAttribute('class', 'edge data'); layer.append(path);
      const label = document.createElementNS(svgNS, 'text'); label.setAttribute('class', 'edge-label');
      label.setAttribute('x', (start.x + end.x) / 2 + 6); label.setAttribute('y', (start.y + end.y) / 2); label.textContent = edge.name; layer.append(label);
    }
  }
  function applyViewport() { $('world').style.transform = `translate(${state.pan.x}px,${state.pan.y}px) scale(${state.zoom})`; $('zoom-label').textContent = Math.round(state.zoom * 100) + '%'; state.doc.layout.viewport = {x: state.pan.x, y: state.pan.y, zoom: state.zoom}; }
  function worldPoint(event) { const r = $('viewport').getBoundingClientRect(); return {x: (event.clientX - r.left - state.pan.x) / state.zoom, y: (event.clientY - r.top - state.pan.y) / state.zoom}; }

  // ---------------------------------------------------------------- view state from server
  function applyView(view) {
    if (view.document) state.doc = view.document;
    state.analysis = view.analysis || {}; state.dataEdges = view.data_edges || []; state.order = view.order || []; state.disconnected = view.disconnected || []; state.revision = view.revision;
    if (view.session) state.session = view.session; if (view.csrf) state.csrf = view.csrf; if ('run' in view && view.run) state.run = view.run;
  }
  function markDirty() { state.dirty = true; $('save-state').textContent = 'Unsaved'; $('save-state').dataset.state = 'dirty'; clearTimeout(state.saveTimer); state.saveTimer = setTimeout(save, 600); }
  async function save() {
    if (!state.dirty) return;
    try { const view = await api('/api/graph', {document: state.doc}); state.dirty = false; applyView(view); $('save-state').textContent = 'Saved'; $('save-state').dataset.state = ''; nodes().forEach(n => updateNode(n.id)); drawEdges(); renderInspector(); }
    catch (error) { $('save-state').textContent = error.status === 409 ? 'Waiting for run' : 'Not saved: ' + error.message; $('save-state').dataset.state = 'error'; if (error.status === 409) setTimeout(() => { state.dirty = true; save(); }, 1500); else toast(error.message); }
  }

  // ---------------------------------------------------------------- selection + inspector
  function select(type, id) { state.selected = type ? {type, id} : null; nodes().forEach(n => updateNode(n.id)); drawEdges(); renderInspector(); $('delete').disabled = !state.selected || (type === 'node' && (nodeById(id).kind === 'start' || nodeById(id).kind === 'finish')); const code = type === 'node' && nodeById(id).kind === 'code'; $('run-from').disabled = !code; $('run-one').disabled = !code; }
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
    document.querySelectorAll('.node textarea').forEach(a => { a.readOnly = !!running; });
  }

  // ---------------------------------------------------------------- editing
  function addNode(kind) {
    const id = freshId(kind === 'code' ? 'cell-' : 'note-');
    const number = nodes().filter(n => n.kind === 'code').length + 1;
    cells().push({id, cell_type: kind, metadata: {mainaiger: {title: kind === 'code' ? 'Cell ' + number : 'Note'}}, source: '', ...(kind === 'code' ? {execution_count: null, outputs: []} : {})});
    nodes().push({id, kind, cell_index: cells().length - 1});
    const sel = state.selected && state.selected.type === 'node' ? nodeById(state.selected.id) : null;
    const vp = $('viewport').getBoundingClientRect();
    const base = sel ? position(sel.id) : {x: (vp.width / 2 - state.pan.x) / state.zoom - NODE_W / 2, y: (vp.height / 3 - state.pan.y) / state.zoom};
    const spot = sel ? {x: base.x, y: base.y + box(sel.id).h + 80} : base;
    // Slide down until the new card does not overlap an existing one in this column.
    const overlaps = () => nodes().some(n => n.id !== id && state.doc.layout.nodes[n.id] && Math.abs(state.doc.layout.nodes[n.id].x - spot.x) < NODE_W && spot.y < state.doc.layout.nodes[n.id].y + box(n.id).h + 20 && spot.y + 160 > state.doc.layout.nodes[n.id].y);
    for (let i = 0; i < 40 && overlaps(); i++) spot.y += 60;
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
    let y = 40; const heights = {}; Object.keys(rows).map(Number).sort((a, b) => a - b).forEach(l => { const row = rows[l]; const total = row.reduce((w, n) => w + widthOf(n) + 60, -60); let x = Math.max(40, 420 - total / 2) + (l === -1 ? 900 : 0); let tallest = 0; for (const n of row) { state.doc.layout.nodes[n.id] = {x: x + (widthOf(n) === TERMINAL_W ? 0 : 0), y: l === -1 ? 40 : y}; x += widthOf(n) + 60; tallest = Math.max(tallest, box(n.id).h); } if (l !== -1) y += tallest + 90; });
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
    if (portOut) { const id = nodeEl.dataset.id; state.linking = {source: id}; viewport.classList.add('linking'); viewport.setPointerCapture(event.pointerId); const p = worldPoint(event); $('pending-edge').hidden = false; $('pending-edge').setAttribute('d', curve({x: box(id).x + box(id).w / 2, y: box(id).y + box(id).h}, p)); event.preventDefault(); return; }
    if (head) { const id = nodeEl.dataset.id; select('node', id); const p = worldPoint(event), pos = position(id); state.dragging = {id, dx: p.x - pos.x, dy: p.y - pos.y}; viewport.setPointerCapture(event.pointerId); event.preventDefault(); return; }
    if (nodeEl) { select('node', nodeEl.dataset.id); return; }  // clicks inside textarea/output keep their default behavior
    if (edgeEl) { select('edge', edgeEl.dataset.id); return; }
    select(null); state.panning = {x: event.clientX - state.pan.x, y: event.clientY - state.pan.y}; viewport.classList.add('panning'); viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener('pointermove', event => {
    if (state.dragging) { const p = worldPoint(event); const pos = position(state.dragging.id); pos.x = Math.round(p.x - state.dragging.dx); pos.y = Math.round(p.y - state.dragging.dy); placeNode(state.dragging.id); drawEdges(); return; }
    if (state.linking) { const p = worldPoint(event); const b = box(state.linking.source); $('pending-edge').setAttribute('d', curve({x: b.x + b.w / 2, y: b.y + b.h}, p)); document.querySelectorAll('.port.target').forEach(x => x.classList.remove('target')); const under = document.elementFromPoint(event.clientX, event.clientY); const target = under && under.closest('.node'); if (target && target.dataset.id !== state.linking.source) { const pin = target.querySelector('.port.in'); if (pin) pin.classList.add('target'); } return; }
    if (state.panning) { state.pan = {x: event.clientX - state.panning.x, y: event.clientY - state.panning.y}; applyViewport(); }
  });
  function endPointer(event) {
    if (state.dragging) { state.dragging = null; markDirty(); }
    if (state.linking) { const under = document.elementFromPoint(event.clientX, event.clientY); const target = under && under.closest('.node'); $('pending-edge').hidden = true; document.querySelectorAll('.port.target').forEach(x => x.classList.remove('target')); if (target) addControlEdge(state.linking.source, target.dataset.id); state.linking = null; viewport.classList.remove('linking'); }
    if (state.panning) { state.panning = null; viewport.classList.remove('panning'); markDirty(); }
  }
  viewport.addEventListener('pointerup', endPointer); viewport.addEventListener('pointercancel', endPointer);
  viewport.addEventListener('wheel', event => {
    if (event.target.closest('textarea, .node-output')) return;
    event.preventDefault();
    if (state.view === 'notebook') { state.pan.y = Math.min(0, state.pan.y - event.deltaY); applyViewport(); return; }
    const factor = Math.exp(-event.deltaY * 0.0015); const zoom = Math.max(0.2, Math.min(2.5, state.zoom * factor));
    const r = viewport.getBoundingClientRect(), mx = event.clientX - r.left, my = event.clientY - r.top;
    state.pan = {x: mx - (mx - state.pan.x) * (zoom / state.zoom), y: my - (my - state.pan.y) * (zoom / state.zoom)}; state.zoom = zoom; applyViewport();
  }, {passive: false});
  $('nodes').addEventListener('input', event => {
    const area = event.target.closest('textarea'); if (!area) return; const node = nodeById(area.closest('.node').dataset.id); cellOf(node).source = area.value; updateNode(node.id); markDirty();
  });
  $('nodes').addEventListener('click', event => {
    const button = event.target.closest('.node-run, .node-toggle'); if (!button) return; const id = button.closest('.node').dataset.id; const act = button.dataset.act;
    if (act === 'all') { reviewRun('all', []); return; }
    if (act === 'add') { select('node', id); addNode('code'); return; }
    if (button.classList.contains('node-toggle')) { const own = meta(cellOf(nodeById(id))); own.collapsed = own.collapsed || {}; own.collapsed[act] = !own.collapsed[act]; updateNode(id); drawEdges(); layoutNotebook(); markDirty(); return; }
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
  $('export-py').addEventListener('click', () => {
    // Export Python: cells reachable from Start, in trigger order. Branches are flattened serially — the same order the Lab runs them.
    const lines = ['# Exported from MainAIger Lab — cells in trigger order (serial).', '# Notes become comments; outputs are not included.', ''];
    const reach = new Set(); const stack = [nodes().find(n => n.kind === 'start').id]; while (stack.length) { const c = stack.pop(); if (reach.has(c)) continue; reach.add(c); edges().filter(e => e.kind === 'control' && e.source === c).forEach(e => stack.push(e.target)); }
    for (const id of state.order) { const n = nodeById(id); if (!n || !reach.has(id)) continue; if (n.kind === 'code') lines.push(`# ---- ${titleOf(n)} (${id}) ----`, sourceOf(cellOf(n)).replace(/\s+$/, ''), ''); else if (n.kind === 'markdown') lines.push(...sourceOf(cellOf(n)).split('\n').map(l => '# ' + l), ''); }
    const skipped = nodes().filter(n => n.kind === 'code' && !reach.has(n.id)); if (skipped.length) lines.push('# Not reachable from Start (not exported): ' + skipped.map(titleOf).join(', '));
    const blob = new Blob([lines.join('\n') + '\n'], {type: 'text/x-python'}); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'graph.py'; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $('clear-outputs').addEventListener('click', () => { for (const c of cells()) if (c.cell_type === 'code') { c.outputs = []; c.execution_count = null; delete meta(c).last_run; } state.run = null; nodes().forEach(n => updateNode(n.id)); drawEdges(); renderInspector(); markDirty(); });
  $('delete').addEventListener('click', deleteSelected); $('arrange').addEventListener('click', arrange); $('fit').addEventListener('click', fit);
  $('show-data').addEventListener('change', drawEdges);
  $('node-title').addEventListener('input', () => { const sel = state.selected && nodeById(state.selected.id); if (!sel || sel.kind !== 'code') return; meta(cellOf(sel)).title = $('node-title').value; updateNode(sel.id); markDirty(); });
  $('apply-override').addEventListener('click', () => { const sel = state.selected && nodeById(state.selected.id); if (!sel || sel.kind !== 'code') return; const own = meta(cellOf(sel)); const parse = v => v.split(',').map(s => s.trim()).filter(Boolean); const i = parse($('override-inputs').value), o = parse($('override-outputs').value); if (i.length) own.inputs = i; else delete own.inputs; if (o.length) own.outputs = o; else delete own.outputs; markDirty(); save(); });
  $('download').addEventListener('click', () => { const blob = new Blob([JSON.stringify(state.doc, null, 2) + '\n'], {type: 'application/json'}); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'graph.ipymb'; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
  $('open-file').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', async () => {
    const file = $('file-input').files[0]; $('file-input').value = ''; if (!file) return;
    try {
      if (file.size > 4000000) throw new Error('Choose a file smaller than 4 MB.');
      const data = JSON.parse(await file.text());
      const doc = data && data.format === 'mainaiger' ? data : fromNotebook(data);
      const view = await api('/api/graph', {document: doc}); applyView(view); state.run = null; state.dirty = false; $('save-state').textContent = 'Saved'; $('save-state').dataset.state = '';
      const vp = state.doc.layout.viewport || {}; state.pan = {x: vp.x || 40, y: vp.y || 30}; state.zoom = vp.zoom || 1; buildNodes(); select(null); applyViewport(); if (!Object.keys(state.doc.layout.nodes).length) arrange(); toast('Opened ' + file.name);
    } catch (error) { toast(error.message || 'The file could not be opened.'); }
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

  // ---------------------------------------------------------------- session
  $('session-start').addEventListener('click', () => { $('session-dialog').showModal(); $('session-cancel').focus(); });
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
  async function poll() {
    clearTimeout(state.pollTimer);
    try {
      const data = await api('/api/graph/run'); state.run = data.run; state.session = data.session || state.session; nodes().forEach(n => updateNode(n.id)); drawEdges(); renderInspector();
      if (state.run && (state.run.state === 'running' || state.run.state === 'pending')) { state.pollTimer = setTimeout(poll, 500); return; }
      const view = await api('/api/graph'); const run = state.run; applyView(view); state.run = run; nodes().forEach(n => updateNode(n.id)); layoutNotebook(); drawEdges(); renderInspector(); refreshVariables();
      toast(state.run ? `Run ${state.run.state}${state.run.error ? ': ' + state.run.error : ''}` : 'Run finished');
      if (state.dirty) save();
    } catch (error) { toast(error.message); }
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    try { const view = await api('/api/graph'); applyView(view); $('graph-main').hidden = false; const vp = state.doc.layout.viewport || {}; state.pan = {x: vp.x ?? 40, y: vp.y ?? 30}; state.zoom = vp.zoom || 1; buildNodes(); applyViewport(); renderInspector(); if (state.run && (state.run.state === 'running' || state.run.state === 'pending')) poll(); if (state.session.alive) refreshVariables(); }
    catch (error) { if (!$('locked').hidden) return; toast(error.message); }
  }
  window.addEventListener('resize', drawEdges);
  boot();
})();
