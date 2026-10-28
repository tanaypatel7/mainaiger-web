(() => {
  'use strict';
  // Models Mainbook: device → Ollama install → model choice → download → chat API test.
  const $ = id => document.getElementById(id);
  const GiB = 1024 ** 3;
  let csrf = '', latest = null, busy = false, pending = null, watchTimer = null, pollTimer = null;
  const gb = bytes => Number.isFinite(bytes) ? (bytes / GiB).toFixed(1) + ' GB' : 'unknown';
  const state = (id, text, kind = '') => { const el = $(id); el.textContent = text; el.dataset.state = kind; };
  async function api(path, body) {
    const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {'Content-Type': 'application/json', ...(csrf ? {'X-CSRF-Token': csrf} : {})}};
    if (body !== undefined) { options.method = 'POST'; options.body = JSON.stringify(body); }
    let response; try { response = await fetch(path, options); } catch { throw new Error('Unable to reach your local Lab.'); }
    let data = {}; try { data = await response.json(); } catch {}
    if (response.status === 401) { $('locked').hidden = false; $('models-main').hidden = true; throw new Error('Unlock the Lab first.'); }
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : 'The local request could not be completed.');
    return data;
  }
  function controls() {
    const l = latest;
    $('ollama-download').disabled = busy || !l || l.installed || l.device.system !== 'Darwin';
    $('ollama-open-installer').disabled = busy || !l || l.installed || !l.installer.staged;
    $('ollama-open-app').disabled = busy || !l || !l.installed || l.running;
    $('ollama-recheck').disabled = busy;
    document.querySelectorAll('#catalog button').forEach(b => { b.disabled = busy || !l || !l.running || b.dataset.nodisk === '1'; });
    $('chat-send').disabled = busy || !l || !l.running || !l.models.length;
  }
  function renderDevice(d) {
    state('device-state', 'Complete', 'success');
    $('device-output').textContent = `Chip: ${d.chip}\nSystem: ${d.system} · ${d.architecture}\nMemory: ${gb(d.ram_bytes)}\nFree disk in home: ${gb(d.free_bytes)}\n${d.gpu_note}`;
  }
  function renderOllama(l) {
    $('ollama-source').textContent = `Ollama ${l.installer.version}: ${l.installer.url} (SHA-256 ${l.installer.sha256.slice(0, 12)}…)`;
    if (l.running) { state('ollama-state', 'Running · ' + l.version, 'success'); $('ollama-output').textContent = `Ollama ${l.version} is answering on ${l.endpoint}. Installed models: ${l.models.length ? l.models.map(m => m.name).join(', ') : 'none yet'}.`; }
    else if (l.installed) { state('ollama-state', 'Installed, not running'); $('ollama-output').textContent = `Ollama is installed at ${l.app_path} but is not answering. Open it (it may ask you to confirm its menu-bar helper), then check again.`; }
    else if (l.installer.staged) { state('ollama-state', 'Downloaded'); $('ollama-output').textContent = 'The verified installer is staged. Open it, drag Ollama to Applications, then open Ollama.'; }
    else { state('ollama-state', l.device.system === 'Darwin' ? 'Needs action' : 'Manual'); $('ollama-output').textContent = l.device.system === 'Darwin' ? 'Review the download to get Ollama.' : 'Install Ollama from ollama.com on this system; the Lab detects it when it runs on 127.0.0.1:11434.'; }
  }
  function renderCatalog(l) {
    const body = $('catalog').querySelector('tbody'); body.textContent = '';
    const fitWord = {fits: 'fits', tight: 'tight', too_large: 'too large', unknown: 'unknown'};
    const extra = l.models.filter(m => !l.catalog.some(c => c.name === m.name || c.name + ':latest' === m.name));
    const rows = l.catalog.map(c => ({...c, kind: 'catalog'})).concat(extra.map(m => ({name: m.name, family: 'installed', params: '', size_gb: m.size_bytes ? m.size_bytes / GiB : null, min_ram_gb: null, fit: 'unknown', use: 'Already installed', installed: true, kind: 'extra'})));
    for (const c of rows) {
      const tr = document.createElement('tr'); tr.dataset.fit = c.fit; if (c.installed) tr.classList.add('installed');
      const cells = [c.name, c.size_gb != null ? '~' + Number(c.size_gb).toFixed(1) + ' GB' : '?', c.min_ram_gb != null ? c.min_ram_gb + ' GB' : '—', fitWord[c.fit] || c.fit, c.use];
      for (const text of cells) { const td = document.createElement('td'); td.textContent = text; tr.append(td); }
      const td = document.createElement('td');
      if (c.installed) { td.textContent = '✓ installed'; td.className = 'installed-mark'; }
      else { const b = document.createElement('button'); b.type = 'button'; b.className = 'quiet small'; b.textContent = 'Download'; b.dataset.model = c.name; b.dataset.size = c.size_gb || ''; b.disabled = true; if (c.disk_ok === false) { b.title = 'Not enough free disk space'; b.disabled = true; b.dataset.nodisk = '1'; } td.append(b); }
      tr.append(td); body.append(tr);
    }
    state('catalog-state', l.running ? 'Ready' : 'Waiting', l.running ? 'success' : '');
    if (l.running && $('pull-output').textContent.startsWith('Install')) $('pull-output').textContent = l.models.length ? 'Choose another model or use the ones installed.' : 'Choose a model that fits and review its download.';
  }
  function renderChat(l) {
    const select = $('chat-model'); const current = select.value; select.textContent = '';
    for (const m of l.models) { const o = document.createElement('option'); o.value = m.name; o.textContent = m.name; select.append(o); }
    if (current) select.value = current;
    state('chat-state', l.running && l.models.length ? 'Ready' : 'Waiting', l.running && l.models.length ? 'success' : '');
    if (!l.models.length) $('chat-note').textContent = 'Download a model first.'; else $('chat-note').textContent = '';
  }
  async function refresh() {
    const l = await api('/api/models'); latest = l; csrf = l.csrf;
    renderDevice(l.device); renderOllama(l); renderCatalog(l); renderChat(l); controls();
    if (l.job && l.job.running && String(l.job.action || '').startsWith('ollama')) renderJob(l.job);
    return l;
  }
  async function check() { if (busy) return; busy = true; controls(); try { await refresh(); } catch (e) { $('ollama-output').textContent = e.message; } finally { busy = false; controls(); } }
  function watch(reason) {
    clearTimeout(watchTimer); const started = Date.now();
    const tick = async () => { if (!busy) { try { await refresh(); } catch {} } const waited = Math.round((Date.now() - started) / 1000); if (latest && latest.running) { $('ollama-watch').textContent = 'Ollama detected.'; return; } if (waited > 900) { $('ollama-watch').textContent = 'Stopped waiting. Open Ollama and check again.'; return; } $('ollama-watch').textContent = `${reason} Checking again in 5 s (${waited} s)…`; watchTimer = setTimeout(tick, 5000); };
    tick();
  }
  function renderJob(job) {
    const isPull = (job.action || '').startsWith('ollama-pull:');
    const progress = $(isPull ? 'pull-progress' : 'ollama-progress'), out = $(isPull ? 'pull-output' : 'ollama-output');
    if (!job.running) { progress.hidden = true; return; }
    progress.hidden = false;
    if (Number.isFinite(job.total) && job.total > 0) { progress.max = job.total; progress.value = Math.min(job.bytes || 0, job.total); out.textContent = `${job.detail || job.phase}… ${gb(job.bytes || 0)} of ${gb(job.total)}`; }
    else { progress.removeAttribute('value'); out.textContent = `${job.detail || job.phase}…`; }
  }
  async function pollJob(action) {
    clearTimeout(pollTimer);
    try {
      const job = await api('/api/setup/job'); renderJob(job);
      if (job.running) { pollTimer = setTimeout(() => pollJob(action), 1000); return; }
      busy = false; const result = job.result || {};
      await refresh();
      const out = $(action === 'pull' ? 'pull-output' : 'ollama-output'); out.textContent = result.output || (result.ok ? 'Done.' : 'Failed.');
      if (action === 'open' && result.ok) watch('Finish the installer, then open Ollama.');
      if (action !== 'pull') state('ollama-state', result.ok ? (action === 'download' ? 'Downloaded' : 'User action needed') : 'Failed', result.ok ? '' : 'failed');
      if (action === 'pull') state('catalog-state', result.ok ? 'Ready' : 'Failed', result.ok ? 'success' : 'failed');
      controls();
    } catch (e) { busy = false; $('ollama-output').textContent = e.message; controls(); }
  }
  function review(kind, extra) {
    pending = {kind, ...extra};
    const texts = {
      download: `Download Ollama ${latest.installer.version} from ${latest.installer.url}. Uses network and disk space (about 0.5 GB). The file is verified against the pinned SHA-256, its Developer ID signature and Apple notarization before it can be opened.`,
      'open-installer': 'Verify the staged Ollama installer again and open it. Drag Ollama to Applications, then open it from Applications or with the next button. Ollama may ask to install its command-line helper; that is Ollama asking, not MainAIger.',
      'open-app': 'Open the installed Ollama app (open -a Ollama). It serves models on 127.0.0.1:11434 only.',
      pull: `Download the model "${extra && extra.model}" (about ${extra && extra.size ? Number(extra.size).toFixed(1) + ' GB' : 'unknown size'}) through your local Ollama from its registry. Uses network and disk space; you can delete models later in Ollama.`,
    };
    $('models-dialog-title').textContent = kind === 'pull' ? 'Review model download' : kind === 'download' ? 'Review Ollama download' : kind === 'open-installer' ? 'Open Ollama installer' : 'Open Ollama';
    $('models-dialog-text').textContent = texts[kind]; $('models-dialog').showModal(); $('models-cancel').focus();
  }
  $('models-cancel').addEventListener('click', () => $('models-dialog').close());
  $('models-confirm').addEventListener('click', async () => {
    $('models-dialog').close(); const p = pending; pending = null; if (!p || busy) return; busy = true; controls();
    try {
      if (p.kind === 'download') { state('ollama-state', 'Downloading'); await api('/api/models/installer/download', {confirm: true}); pollJob('download'); return; }
      if (p.kind === 'open-installer') { state('ollama-state', 'Verifying'); await api('/api/models/installer/open', {confirm: true}); pollJob('open'); return; }
      if (p.kind === 'open-app') { const r = await api('/api/models/app/open', {confirm: true}); $('ollama-output').textContent = r.output; busy = false; controls(); if (r.ok) watch('Ollama is starting.'); return; }
      if (p.kind === 'pull') { state('catalog-state', 'Downloading'); $('pull-output').textContent = 'Asking Ollama to download ' + p.model + '…'; await api('/api/models/pull', {model: p.model, confirm: true}); pollJob('pull'); return; }
    } catch (e) { busy = false; $(p.kind === 'pull' ? 'pull-output' : 'ollama-output').textContent = e.message; controls(); }
  });
  $('ollama-download').addEventListener('click', () => latest && review('download'));
  $('ollama-open-installer').addEventListener('click', () => latest && review('open-installer'));
  $('ollama-open-app').addEventListener('click', () => latest && review('open-app'));
  $('ollama-recheck').addEventListener('click', check);
  $('catalog').addEventListener('click', e => { const b = e.target.closest('button[data-model]'); if (!b || b.disabled) return; review('pull', {model: b.dataset.model, size: b.dataset.size}); });
  $('chat-send').addEventListener('click', async () => {
    const model = $('chat-model').value, content = $('chat-prompt').value.trim(); if (!model || !content) return;
    busy = true; controls(); $('chat-output').textContent = 'Waiting for the local model…'; $('chat-note').textContent = '';
    try { const r = await api('/api/models/chat', {model, messages: [{role: 'user', content}]}); $('chat-output').textContent = r.reply; $('chat-note').textContent = `${r.model} · ${r.elapsed_ms} ms` + (r.tokens && r.tokens.reply ? ` · ${r.tokens.reply} tokens` : ''); state('chat-state', 'Answered', 'success'); }
    catch (e) { $('chat-output').textContent = e.message; state('chat-state', 'Failed', 'failed'); }
    finally { busy = false; controls(); }
  });
  window.addEventListener('pagehide', () => { clearTimeout(watchTimer); clearTimeout(pollTimer); });
  (async () => { try { const l = await refresh(); $('models-main').hidden = false; if (l.job && l.job.running && String(l.job.action || '').startsWith('ollama')) { busy = true; controls(); pollJob((l.job.action || '').startsWith('ollama-pull') ? 'pull' : (l.job.action || '').endsWith('open') ? 'open' : 'download'); } } catch (e) { if ($('locked').hidden) $('ollama-output').textContent = e.message; } })();
})();
