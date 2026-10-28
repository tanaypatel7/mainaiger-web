(() => {
  'use strict';
  // Memory page: notes (persistent memory) and chats (imported, read-only) from the approved Memory folder,
  // GET/POST /api/memory/* (local_lab/memory.py). List or link graph on the left, one note open on the right.
  const $ = id => document.getElementById(id);
  const state = {status: null, tier: 'notes', notes: [], selected: null, note: null, view: 'list', graph: null, pan: {x: 30, y: 20}, zoom: 1, panning: null, pos: {}};
  let csrf = '';
  let toastTimer = null; const toast = t => { $('toast').textContent = t; $('toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4000); };
  async function api(path, body) {
    const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {'Content-Type': 'application/json', ...(csrf ? {'X-CSRF-Token': csrf} : {})}};
    if (body !== undefined) { options.method = 'POST'; options.body = JSON.stringify(body); }
    let response; try { response = await fetch(path, options); } catch { throw new Error('Unable to reach your local Lab.'); }
    let data = {}; try { data = await response.json(); } catch {}
    if (response.status === 401) { $('locked').hidden = false; $('memory-main').hidden = true; throw new Error('Unlock the Lab first.'); }
    if (!response.ok) { const e = new Error(typeof data.error === 'string' ? data.error : 'The local request could not be completed.'); e.status = response.status; throw e; }
    return data;
  }
  const fmtTime = t => t ? new Date(t * 1000).toLocaleString() : '';
  // ---- status + list
  async function refreshStatus() {
    state.status = await api('/api/memory');
    const st = state.status;
    $('memory-folder').textContent = st.configured ? st.folder : 'not chosen'; $('memory-choose').textContent = st.configured ? 'Change folder…' : 'Choose folder…';
    $('memory-stats').textContent = st.configured ? `${st.counts.notes} note${st.counts.notes === 1 ? '' : 's'} · ${st.counts.chats} chat${st.counts.chats === 1 ? '' : 's'} · search: ${st.search}` : 'No Memory folder yet';
    $('memory-summary').textContent = st.configured ? `${st.folder} — notes/ holds what you keep (${st.counts.notes}), chats/ holds imported conversations (${st.counts.chats}). Search uses ${st.search === 'fts5' ? 'an SQLite full-text index in the private state folder' : 'a plain scan (SQLite FTS5 is not available here)'}.` : 'Approve a folder to start keeping notes.';
    for (const sel of [$('filter-kind'), $('note-kind'), $('new-kind')]) { const keep = sel.value; sel.textContent = ''; if (sel.id === 'filter-kind') { const o = document.createElement('option'); o.value = ''; o.textContent = 'any kind'; sel.append(o); } for (const k of st.kinds) { if (k === 'chat' && sel.id !== 'filter-kind') continue; const o = document.createElement('option'); o.value = k; o.textContent = k; sel.append(o); } if (keep) sel.value = keep; }
    $('note-new').disabled = !st.configured; $('idea-draft').disabled = !st.configured || !$('idea-text').value.trim();
  }
  async function refreshList() {
    if (!state.status || !state.status.configured) { $('list-note').textContent = 'Choose a Memory folder to begin.'; $('note-list').textContent = ''; return; }
    const q = $('memory-search').value.trim();
    try {
      if (q) { const data = await api(`/api/memory/search?q=${encodeURIComponent(q)}&tier=${state.tier}`); state.notes = data.hits; }
      else { const params = new URLSearchParams({tier: state.tier}); if ($('filter-kind').value) params.set('kind', $('filter-kind').value); if ($('filter-status').value) params.set('status', $('filter-status').value); state.notes = (await api('/api/memory/notes?' + params)).notes; }
    } catch (error) { $('list-note').textContent = error.message; return; }
    $('list-note').textContent = state.notes.length ? (q ? `${state.notes.length} hit${state.notes.length === 1 ? '' : 's'} for "${q}"` : `${state.notes.length} ${state.tier}`) : (q ? 'Nothing matches.' : state.tier === 'notes' ? 'No notes yet. Write one with New note…, or let an idea or an import create some.' : 'No chats imported yet.');
    const list = $('note-list'); list.textContent = '';
    for (const n of state.notes) {
      const li = document.createElement('li'); li.dataset.path = n.path; li.classList.toggle('selected', n.path === state.selected);
      const head = document.createElement('div'); head.className = 'row-head'; const b = document.createElement('b'); b.textContent = n.title; const kind = document.createElement('span'); kind.className = 'kind'; kind.textContent = n.kind; const status = document.createElement('span'); status.className = 'status'; status.dataset.s = n.status; status.textContent = n.status; head.append(b, kind, status); li.append(head);
      if (n.snippet) { const sn = document.createElement('p'); sn.className = 'snippet'; const parts = n.snippet.split(/\[(.*?)\]/); parts.forEach((part, i) => { if (i % 2) { const m = document.createElement('mark'); m.textContent = part; sn.append(m); } else sn.append(part); }); li.append(sn); }
      else if (n.preview) { const p = document.createElement('p'); p.className = 'preview'; p.textContent = n.preview; li.append(p); }
      if (n.tags.length) { const t = document.createElement('div'); t.className = 'tags'; t.textContent = n.tags.map(x => '#' + x).join(' '); li.append(t); }
      list.append(li);
    }
  }
  $('note-list').addEventListener('click', event => { const li = event.target.closest('li[data-path]'); if (li) open(li.dataset.path); });
  // ---- one note
  async function open(path) {
    try { state.note = (await api('/api/memory/note?path=' + encodeURIComponent(path))).note; } catch (error) { toast(error.message); return; }
    state.selected = path; for (const li of document.querySelectorAll('#note-list li')) li.classList.toggle('selected', li.dataset.path === path);
    for (const el of document.querySelectorAll('.mnode')) el.classList.toggle('selected', el.dataset.path === path);
    renderNote();
  }
  function renderNote() {
    const n = state.note; $('note-empty').hidden = !!n; $('note-editor').hidden = !n; if (!n) return;
    const chat = n.tier === 'chats';
    $('note-title').value = n.title; $('note-kind').value = chat ? 'fact' : n.kind; $('note-status').value = n.status; $('note-tags').value = n.tags.join(', '); $('note-visibility').value = n.visibility; $('note-body').value = n.body;
    $('note-mainbook-row').hidden = n.kind !== 'mainbook'; $('note-mainbook').value = n.mainbook || '';
    for (const id of ['note-title', 'note-kind', 'note-status', 'note-tags', 'note-visibility', 'note-body', 'note-mainbook', 'note-add-heading', 'note-add-text']) $(id).disabled = chat;
    for (const id of ['note-save', 'note-stable', 'note-add']) $(id).disabled = chat; $('note-stable').hidden = n.status === 'stable';
    $('note-meta').textContent = `${n.path} · id ${n.id || '—'} · updated ${fmtTime(n.updated)} · ${n.bytes} bytes${chat ? ' · imported chat, read-only' : ''}`;
    const links = $('note-links'); links.textContent = ''; if (n.links.length) { links.append('Links: '); for (const l of n.links) { const a = document.createElement('a'); a.textContent = '[[' + l + ']]'; a.dataset.link = l; links.append(a); } }
    $('note-sources').textContent = n.sources.length ? 'Sources: ' + n.sources.join(' · ') : '';
    $('note-result').textContent = '';
  }
  $('note-kind').addEventListener('change', () => { $('note-mainbook-row').hidden = $('note-kind').value !== 'mainbook'; });
  $('note-links').addEventListener('click', async event => {
    const a = event.target.closest('a[data-link]'); if (!a) return;
    const target = a.dataset.link.toLowerCase(); const hit = state.notes.find(n => n.title.toLowerCase() === target || (n.id && n.id.toLowerCase() === target));
    if (hit) open(hit.path); else { $('memory-search').value = a.dataset.link; await refreshList(); const h = state.notes.find(n => n.title.toLowerCase() === target); if (h) open(h.path); else toast(`No note titled "${a.dataset.link}" yet; create it with New note….`); }
  });
  async function saveNote(extraFront) {
    const n = state.note; if (!n || n.tier === 'chats') return;
    const front = {title: $('note-title').value.trim(), kind: $('note-kind').value, status: $('note-status').value, tags: $('note-tags').value.split(',').map(s => s.trim()).filter(Boolean), visibility: $('note-visibility').value, ...(extraFront || {})};
    if (front.kind === 'mainbook') front.mainbook = $('note-mainbook').value.trim();
    try { const r = await api('/api/memory/note/update', {path: n.path, body: $('note-body').value, front, expected_hash: n.hash}); state.note = r.note; renderNote(); $('note-result').textContent = 'Saved.'; await refreshStatus(); await refreshList(); }
    catch (error) { $('note-result').textContent = error.message; if (error.status === 409) toast(error.message); }
  }
  $('note-save').addEventListener('click', () => saveNote());
  $('note-stable').addEventListener('click', () => { $('note-status').value = 'stable'; saveNote({status: 'stable'}); });
  $('note-add').addEventListener('click', async () => {
    const n = state.note; if (!n) return;
    try { const r = await api('/api/memory/note/append', {path: n.path, heading: $('note-add-heading').value.trim() || 'Added', text: $('note-add-text').value}); state.note = r.note; $('note-add-text').value = ''; renderNote(); $('note-result').textContent = 'Added a dated section.'; await refreshList(); }
    catch (error) { $('note-result').textContent = error.message; }
  });
  $('note-delete').addEventListener('click', () => { if (!state.note) return; $('delete-text').textContent = `${state.note.title} (${state.note.path})`; $('delete-dialog').showModal(); $('delete-cancel').focus(); });
  $('delete-cancel').addEventListener('click', () => $('delete-dialog').close());
  $('delete-confirm').addEventListener('click', async () => {
    $('delete-dialog').close(); const n = state.note; if (!n) return;
    try { await api('/api/memory/note/delete', {path: n.path, confirm: true}); state.note = null; state.selected = null; renderNote(); toast('Deleted.'); await refreshStatus(); await refreshList(); if (state.view === 'graph') await drawGraph(); } catch (error) { toast(error.message); }
  });
  // ---- ideas: describe -> draft (model) -> review -> save as new or grow an existing idea
  let ideaDraft = null;
  $('idea-text').addEventListener('input', () => { $('idea-draft').disabled = !$('idea-text').value.trim() || !(state.status && state.status.configured); });
  $('idea-draft').addEventListener('click', async () => {
    const text = $('idea-text').value.trim(); if (!text) return;
    $('idea-draft').disabled = true; $('idea-note').textContent = 'Asking your local model…';
    try { ideaDraft = await api('/api/memory/idea/draft', {text}); } catch (error) { $('idea-note').textContent = error.message; $('idea-draft').disabled = false; return; }
    $('idea-draft').disabled = false; $('idea-note').textContent = '';
    const d = ideaDraft.draft;
    $('idea-dialog-meta').textContent = ideaDraft.ok ? `Drafted by ${ideaDraft.model} in ${(ideaDraft.elapsed_ms / 1000).toFixed(1)} s (${ideaDraft.attempts} attempt${ideaDraft.attempts === 1 ? '' : 's'}). Edit anything; nothing is saved until you confirm.` : `${ideaDraft.error || 'No model answer'} — the card below is built from your words; edit it by hand.`;
    $('idea-title').value = d.title; $('idea-summary').value = d.summary; $('idea-tags').value = d.tags.join(', '); $('idea-tasks').value = d.tasks.join('\n');
    const box = $('idea-similar'), list = $('idea-similar-list'); list.textContent = ''; box.hidden = !ideaDraft.similar.length;
    for (const s of ideaDraft.similar) { const label = document.createElement('label'); label.className = 'toggle'; const r = document.createElement('input'); r.type = 'radio'; r.name = 'idea-where'; r.value = s.path; label.append(r, ` Add to "${s.title}" (${s.status})`); const snip = document.createElement('div'); snip.className = 'snip'; snip.textContent = (s.snippet || '').replace(/[\[\]]/g, '').slice(0, 140); list.append(label, snip); }
    box.querySelector('input[value=""]').checked = true;
    $('idea-dialog').showModal(); $('idea-title').focus();
  });
  $('idea-cancel').addEventListener('click', () => $('idea-dialog').close());
  $('idea-confirm').addEventListener('click', async () => {
    const text = $('idea-text').value.trim(); const where = (document.querySelector('input[name="idea-where"]:checked') || {}).value || '';
    const card = {title: $('idea-title').value, summary: $('idea-summary').value, tags: $('idea-tags').value.split(',').map(s => s.trim()).filter(Boolean), tasks: $('idea-tasks').value.split('\n').map(s => s.trim()).filter(Boolean)};
    try {
      let note;
      if (where) note = (await api('/api/memory/idea/grow', {path: where, text})).note;
      else note = (await api('/api/memory/idea', {text, draft: card, model: ideaDraft && ideaDraft.model, confirm: true})).note;
      $('idea-dialog').close(); $('idea-text').value = ''; $('idea-draft').disabled = true; toast(where ? 'Added to the idea.' : 'Idea saved as a draft note.');
      await refreshStatus(); state.tier = 'notes'; setTier(); await refreshList(); open(note.path);
    } catch (error) { toast(error.message); }
  });
  // ---- new note
  $('note-new').addEventListener('click', () => { $('new-title').value = ''; $('new-tags').value = ''; $('new-body').value = ''; $('new-dialog').showModal(); $('new-title').focus(); });
  $('new-cancel').addEventListener('click', () => $('new-dialog').close());
  $('new-confirm').addEventListener('click', async () => {
    const title = $('new-title').value.trim(); if (!title) { toast('Give the note a title.'); return; }
    try { const r = await api('/api/memory/note', {title, kind: $('new-kind').value, tags: $('new-tags').value.split(',').map(s => s.trim()).filter(Boolean), body: $('new-body').value}); $('new-dialog').close(); await refreshStatus(); state.tier = 'notes'; setTier(); await refreshList(); open(r.note.path); toast('Note created.'); }
    catch (error) { toast(error.message); }
  });
  // ---- folder
  $('memory-choose').addEventListener('click', () => { $('folder-input').value = state.status && (state.status.folder || state.status.default) || ''; $('folder-dialog').showModal(); $('folder-cancel').focus(); });
  $('folder-cancel').addEventListener('click', () => $('folder-dialog').close());
  $('folder-confirm').addEventListener('click', async () => {
    try { await api('/api/memory/folder', {folder: $('folder-input').value, confirm: true}); $('folder-dialog').close(); toast('Memory folder approved.'); await refreshStatus(); await refreshList(); } catch (error) { toast(error.message); }
  });
  // ---- filters, tiers, views
  function setTier() { $('tier-notes').classList.toggle('active', state.tier === 'notes'); $('tier-chats').classList.toggle('active', state.tier === 'chats'); }
  $('tier-notes').addEventListener('click', () => { state.tier = 'notes'; setTier(); refreshList(); if (state.view === 'graph') drawGraph(); });
  $('tier-chats').addEventListener('click', () => { state.tier = 'chats'; setTier(); refreshList(); if (state.view === 'graph') drawGraph(); });
  $('filter-kind').addEventListener('change', refreshList); $('filter-status').addEventListener('change', refreshList);
  let searchTimer = null; $('memory-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(refreshList, 250); });
  function setView(view) { state.view = view; $('view-list').classList.toggle('active', view === 'list'); $('view-graph').classList.toggle('active', view === 'graph'); $('list-view').hidden = view !== 'list'; $('viewport').hidden = view !== 'graph'; if (view === 'graph') drawGraph(); }
  $('view-list').addEventListener('click', () => setView('list')); $('view-graph').addEventListener('click', () => setView('graph'));
  // ---- graph of [[links]]: notes grouped in columns by kind
  const COL_W = 230, ROW_H = 30;
  async function drawGraph() {
    try { state.graph = await api('/api/memory/graph' + (state.tier === 'chats' ? '?tier=chats' : '')); } catch (error) { toast(error.message); return; }
    const nodes = $('nodes'); nodes.textContent = ''; state.pos = {};
    const kinds = [...new Set(state.graph.nodes.map(n => n.kind))].sort();
    kinds.forEach((kind, col) => {
      const title = document.createElement('div'); title.className = 'mcol-title'; title.textContent = kind; title.style.left = (col * COL_W + 20) + 'px'; title.style.top = '12px'; nodes.append(title);
      state.graph.nodes.filter(n => n.kind === kind).forEach((n, row) => {
        const el = document.createElement('div'); el.className = `mnode ${n.kind} status-${n.status}`; el.dataset.path = n.path; el.textContent = n.title; el.title = `${n.kind} · ${n.status}`; el.style.left = (col * COL_W + 20) + 'px'; el.style.top = (40 + row * ROW_H) + 'px'; el.style.width = (COL_W - 30) + 'px';
        el.classList.toggle('selected', n.path === state.selected); nodes.append(el); state.pos[n.path] = {x: col * COL_W + 20, y: 40 + row * ROW_H, w: COL_W - 30, h: 24};
      });
    });
    const layer = $('edge-layer'); layer.textContent = '';
    for (const e of state.graph.edges) {
      const a = state.pos[e.source], b = state.pos[e.target]; if (!a || !b) continue;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('class', 'map-calls');
      const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2; const dx = Math.max(40, Math.abs(x2 - x1) / 2);
      path.setAttribute('d', a.x <= b.x ? `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}` : `M${a.x} ${y1} C${a.x - dx} ${y1} ${x2 + b.w + dx} ${y2} ${x2 + b.w} ${y2}`); layer.append(path);
    }
    applyViewport();
  }
  $('nodes').addEventListener('click', event => { const el = event.target.closest('.mnode'); if (el) open(el.dataset.path); });
  const viewport = $('viewport');
  function applyViewport() { $('world').style.transform = `translate(${state.pan.x}px,${state.pan.y}px) scale(${state.zoom})`; $('zoom-label').textContent = Math.round(state.zoom * 100) + '%'; }
  viewport.addEventListener('pointerdown', event => { if (event.button !== 0 || event.target.closest('.mnode')) return; state.panning = {x: event.clientX - state.pan.x, y: event.clientY - state.pan.y}; viewport.setPointerCapture(event.pointerId); });
  viewport.addEventListener('pointermove', event => { if (!state.panning) return; state.pan = {x: event.clientX - state.panning.x, y: event.clientY - state.panning.y}; requestAnimationFrame(applyViewport); });
  const endPan = () => { state.panning = null; }; viewport.addEventListener('pointerup', endPan); viewport.addEventListener('pointercancel', endPan);
  viewport.addEventListener('wheel', event => { event.preventDefault(); const factor = Math.exp(-event.deltaY * 0.0015); const zoom = Math.max(0.2, Math.min(2.5, state.zoom * factor)); const r = viewport.getBoundingClientRect(), mx = event.clientX - r.left, my = event.clientY - r.top; state.pan = {x: mx - (mx - state.pan.x) * (zoom / state.zoom), y: my - (my - state.pan.y) * (zoom / state.zoom)}; state.zoom = zoom; applyViewport(); }, {passive: false});
  // ---- boot
  (async () => { try { const st = await api('/api/state'); csrf = st.csrf; $('memory-main').hidden = false; await refreshStatus(); await refreshList(); } catch (error) { if ($('locked').hidden) toast(error.message); } })();
})();
