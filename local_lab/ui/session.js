(() => {
  'use strict';
  // Persistent-session panel: start/stop/interrupt one kernel and inspect its variables.
  // app.js owns the editor and decides whether a run goes to the session or a fresh container.
  const $ = id => document.getElementById(id);
  let session = {alive: false, executing: false, count: 0, python: null, started_at: null, ended: null};
  let csrf = '', busy = false, limitsText = '', dockerAvailable = false;
  const note = text => { $('session-note').textContent = text; };
  async function json(path, body) {
    const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {'Content-Type': 'application/json', ...(csrf ? {'X-CSRF-Token': csrf} : {})}};
    if (body !== undefined) { options.method = 'POST'; options.body = JSON.stringify(body); }
    let response;
    try { response = await fetch(path, options); } catch { throw new Error('Unable to reach your local Lab.'); }
    let data = {}; try { data = await response.json(); } catch {}
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : 'The local request could not be completed.');
    return data;
  }
  function render() {
    const alive = !!session.alive;
    let state = 'Not started';
    if (alive) {
      const since = session.started_at ? new Date(session.started_at * 1000).toLocaleTimeString() : '';
      state = (session.executing ? 'Running a cell' : 'Ready') + (since ? ' · since ' + since : '');
    } else if (session.ended) state = 'Stopped · ' + session.ended;
    $('session-state').textContent = state;
    $('session-count').textContent = String(session.count || 0);
    $('session-python').textContent = alive && session.python ? session.python : '—';
    $('session-start').disabled = busy || alive || !dockerAvailable;
    $('session-interrupt').disabled = busy || !alive || !session.executing;
    $('session-stop').disabled = busy || !alive;
    $('variables-refresh').disabled = busy || !alive || !!session.executing;
    $('editor-mode').textContent = alive
      ? 'A session is running: each approved cell runs in the same kernel and variables persist until you stop the session. Code cannot save changes to your selected folder.'
      : 'No session is running: each run starts a fresh container and variables are lost afterwards. Code cannot save changes to your selected folder. Packages are limited to what is already in the container image.';
    if (!alive) { $('variables').hidden = true; $('variables-note').textContent = 'Start a session to inspect variables. Summaries never call your objects\' own code.'; }
    window.dispatchEvent(new CustomEvent('mainaiger:session-rendered', {detail: {alive}}));
  }
  function escapeCell(text) { const cell = document.createElement('td'); cell.textContent = text; return cell; }
  async function refreshVariables() {
    if (!session.alive || session.executing) return;
    try {
      const data = await json('/api/kernel/variables');
      const body = $('variables').querySelector('tbody'); body.textContent = '';
      for (const item of data.variables || []) {
        const row = document.createElement('tr'); row.append(escapeCell(item.name), escapeCell(item.type), escapeCell(item.summary)); body.append(row);
      }
      $('variables').hidden = false;
      $('variables-note').textContent = (data.variables || []).length ? 'Names defined in this session. Summaries are limited descriptions, not full values.' : 'No variables defined yet.';
    } catch (error) { $('variables-note').textContent = error.message; }
  }
  function update(next) { if (next && typeof next === 'object') session = next; render(); }
  $('session-start').addEventListener('click', () => { if (busy || session.alive) return; $('session-dialog-limits').textContent = limitsText; $('session-dialog').showModal(); $('session-cancel').focus(); });
  $('session-cancel').addEventListener('click', () => $('session-dialog').close());
  $('session-confirm').addEventListener('click', async () => {
    $('session-dialog').close(); if (busy) return;
    busy = true; render(); note('Starting the session container…');
    try {
      const state = await json('/api/state'); csrf = state.csrf;
      update(await json('/api/kernel/start', {confirm: true, revision: state.revision}));
      note('Session started. Next: open the Graph tab to build connected cells.'); busy = false; render(); await refreshVariables();
    } catch (error) { note(error.message); busy = false; render(); }
    window.dispatchEvent(new CustomEvent('mainaiger:refresh-state'));
  });
  $('session-stop').addEventListener('click', async () => {
    if (busy || !session.alive) return; busy = true; render(); note('Stopping…');
    try { update(await json('/api/kernel/stop', {})); note('Session stopped; its variables are gone.'); }
    catch (error) { note(error.message); }
    busy = false; render(); window.dispatchEvent(new CustomEvent('mainaiger:refresh-state'));
  });
  $('session-interrupt').addEventListener('click', async () => {
    if (!session.alive) return;
    try { const data = await json('/api/kernel/interrupt', {}); note(data.ok ? 'Interrupt sent.' : 'Interrupt could not be delivered.'); }
    catch (error) { note(error.message); }
  });
  $('variables-refresh').addEventListener('click', refreshVariables);
  // app.js reports state snapshots and run results here.
  window.addEventListener('mainaiger:state', event => {
    const state = event.detail || {}; csrf = state.csrf || csrf;
    limitsText = 'Folder: ' + (state.workspace || 'No folder selected') + ' · CPU: ' + (state.limits ? state.limits.cpus : '?') + ' cores · Memory: ' + (state.limits ? state.limits.memory : '?');
    $('session-command').textContent = Array.isArray(state.session_command) ? state.session_command.map(x => JSON.stringify(x)).join(' ') : 'Unavailable';
    dockerAvailable = !!state.docker_available;
    update(state.session);
  });
  window.addEventListener('mainaiger:cell-finished', event => { update(event.detail); refreshVariables(); });
  window.addEventListener('mainaiger:cell-started', () => { session.executing = true; render(); });
  window.addEventListener('mainaiger:locked', () => { csrf = ''; session = {alive: false, executing: false, count: 0, python: null, started_at: null, ended: null}; note(''); render(); });
  window.MainaigerSession = {alive: () => !!session.alive};
  render();
})();
