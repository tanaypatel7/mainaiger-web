(() => {
  'use strict';
  // Browser page: scripts (fixed step lists in the Memory folder), reviewed runs, captures and their import into chats.
  // GET/POST /api/browser/* (local_lab/browser.py, browser_runner.py). One run at a time; the page polls while it goes.
  const $ = id => document.getElementById(id);
  const state = {info: null, selected: null, script: null, run: null, poll: null, pendingRun: null, pendingImport: null};
  let csrf = '';
  let toastTimer = null; const toast = t => { $('toast').textContent = t; $('toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4000); };
  async function api(path, body) {
    const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {'Content-Type': 'application/json', ...(csrf ? {'X-CSRF-Token': csrf} : {})}};
    if (body !== undefined) { options.method = 'POST'; options.body = JSON.stringify(body); }
    let response; try { response = await fetch(path, options); } catch { throw new Error('Unable to reach your local Lab.'); }
    let data = {}; try { data = await response.json(); } catch {}
    if (response.status === 401) { $('locked').hidden = false; $('browser-main').hidden = true; throw new Error('Unlock the Lab first.'); }
    if (!response.ok) { const e = new Error(typeof data.error === 'string' ? data.error : 'The local request could not be completed.'); e.status = response.status; throw e; }
    return data;
  }
  const fmtTime = t => t ? new Date(t * 1000).toLocaleString() : '';
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  // ---- status
  async function refresh(refreshRunner) {
    state.info = await api('/api/browser' + (refreshRunner ? '?refresh=1' : ''));
    const info = state.info, av = info.available;
    $('runner-state').textContent = av.ready ? `Playwright ${av.version || ''} with Chromium, for ${av.python}` : av.playwright ? `Playwright is installed for ${av.python} but its Chromium is not — run: ${av.install[1]}` : `Playwright is not installed for ${av.python} — run these two commands in a terminal, then Check again: ${av.install.join('  ·  ')}`;
    $('runner-state').classList.toggle('warn', !av.ready);
    $('browser-stats').textContent = !info.folder ? 'No Memory folder yet' : `${plural(info.scripts.length, 'script')} · ${plural(info.captures.length, 'capture')} · runner ${av.ready ? 'ready' : 'missing'}`;
    $('script-note').textContent = !info.folder ? 'Approve a Memory folder first (Memory page); scripts and captures live in it.' : info.scripts.length ? `${plural(info.scripts.length, 'script')} in ${info.folder}/browser/scripts` : 'No scripts yet. Start from a template above.';
    $('script-new').disabled = !info.folder; $('profile-clear').disabled = !info.profile; $('profile-state').textContent = info.profile ? 'A profile exists (logins may be kept).' : 'No profile yet: the first run creates one.';
    $('step-kinds').textContent = info.steps.join(', ');
    const tpl = $('script-template'); if (tpl.options.length === 1) for (const name of Object.keys(info.templates)) { const o = document.createElement('option'); o.value = name; o.textContent = `${name} — ${info.templates[name].title}`; tpl.append(o); }
    const list = $('script-list'); list.textContent = '';
    for (const s of info.scripts) {
      const li = document.createElement('li'); li.dataset.name = s.name; li.classList.toggle('selected', s.name === state.selected);
      const head = document.createElement('div'); head.className = 'row-head'; const b = document.createElement('b'); b.textContent = s.title; const kind = document.createElement('span'); kind.className = 'kind'; kind.textContent = s.name; head.append(b, kind); li.append(head);
      const p = document.createElement('p'); p.className = 'preview'; p.textContent = s.error ? 'Not valid: ' + s.error : `${plural(s.steps, 'step')}, ${plural(s.captures, 'capture')} · may visit ${s.allow.join(', ')}${s.params.length ? ' · needs ' + s.params.join(', ') : ''}${s.headed ? ' · headed' : ''} · budget ${s.budget_s} s`; li.append(p);
      list.append(li);
    }
    renderCaptures(); renderRun(info.run);
    if (info.run.state === 'running' && !state.poll) startPolling();
  }
  $('runner-refresh').addEventListener('click', () => refresh(true).catch(e => toast(e.message)));
  $('script-list').addEventListener('click', event => { const li = event.target.closest('li[data-name]'); if (li) openScript(li.dataset.name); });
  // ---- one script
  async function openScript(name) {
    try { const data = await api('/api/browser/script?name=' + encodeURIComponent(name)); state.script = data.script; } catch (error) { toast(error.message); const row = (state.info.scripts || []).find(s => s.name === name); if (!row || !row.error) return; state.script = null; }
    state.selected = name; for (const li of document.querySelectorAll('#script-list li')) li.classList.toggle('selected', li.dataset.name === name);
    $('script-editor').hidden = false; $('script-name').textContent = name; $('script-result').textContent = '';
    $('script-json').value = state.script ? JSON.stringify(state.script, null, 1) : '';
    $('script-run').disabled = !state.script || !state.info.available.ready;
  }
  $('script-new').addEventListener('click', () => {
    const name = prompt('Script name (lowercase letters, digits and dashes):', ''); if (!name) return;
    const tpl = state.info.templates[$('script-template').value] || state.info.templates['page-capture'];
    state.selected = name; state.script = JSON.parse(JSON.stringify(tpl)); $('script-editor').hidden = false; $('script-name').textContent = name; $('script-json').value = JSON.stringify(state.script, null, 1); $('script-run').disabled = true; $('script-result').textContent = 'Not saved yet.';
  });
  $('script-save').addEventListener('click', async () => {
    let script; try { script = JSON.parse($('script-json').value); } catch (error) { $('script-result').textContent = 'Not JSON: ' + error.message; return; }
    try { const row = await api('/api/browser/script', {name: state.selected, script}); await refresh(); await openScript(row.name); $('script-result').textContent = `Saved: ${plural(row.steps, 'step')}, may visit ${row.allow.join(', ')}.`; }
    catch (error) { $('script-result').textContent = error.message; }
  });
  $('script-delete').addEventListener('click', () => { $('delete-text').textContent = `${state.selected} is removed from browser/scripts. Captures it made stay.`; $('delete-dialog').showModal(); });
  $('delete-cancel').addEventListener('click', () => $('delete-dialog').close());
  $('delete-confirm').addEventListener('click', async () => { $('delete-dialog').close(); try { await api('/api/browser/script/delete', {name: state.selected}); state.selected = null; state.script = null; $('script-editor').hidden = true; await refresh(); toast('Script deleted.'); } catch (error) { toast(error.message); } });
  // ---- run (reviewed)
  $('script-run').addEventListener('click', () => {
    const s = state.script; if (!s) return;
    $('run-dialog-meta').textContent = `${s.title} (${state.selected}): ${plural(s.steps.length, 'step')}, time budget ${s.budget_s} s.`;
    $('run-dialog-allow').textContent = s.allow.join(', ');
    const ol = $('run-dialog-steps'); ol.textContent = '';
    const describe = st => {
      const words = {goto: () => `open ${st.url}`, wait: () => st.selector ? `wait for ${st.selector}` : `wait ${st.seconds} s`, click: () => `click ${st.selector}`, fill: () => `type into ${st.selector}`, press: () => `press ${st.key}`, scroll: () => `scroll ${st.times || 5}×`,
                     capture: () => `read the page as "${st.as}"${st.selector ? ' (' + st.selector + ')' : ''}`, links: () => `collect links ${st.selector} → ${st.as}`, each: () => `for each of ${st.over} (up to ${st.limit || 50}): ${st.steps.map(describe).join('; ')}`};
      return (words[st.do] ? words[st.do]() : st.do) + (st.optional ? ' (optional)' : '');
    };
    if (s.start) { const li = document.createElement('li'); li.textContent = 'open ' + s.start; ol.append(li); }
    for (const st of s.steps) { const li = document.createElement('li'); li.textContent = describe(st); ol.append(li); }
    const params = $('run-dialog-params'); params.textContent = '';
    for (const name of s.params) { const label = document.createElement('label'); label.textContent = `Parameter ${name}`; label.htmlFor = 'param-' + name; const input = document.createElement('input'); input.type = 'text'; input.id = 'param-' + name; input.dataset.param = name; input.maxLength = 4000; params.append(label, input); }
    $('run-dialog-headed').checked = !!s.headed;
    $('run-dialog').showModal();
  });
  $('run-cancel').addEventListener('click', () => $('run-dialog').close());
  $('run-confirm').addEventListener('click', async () => {
    const params = {}; for (const input of document.querySelectorAll('#run-dialog-params input')) params[input.dataset.param] = input.value;
    try { const st = await api('/api/browser/run', {name: state.selected, params, headed: $('run-dialog-headed').checked, confirm: true}); $('run-dialog').close(); renderRun(st); startPolling(); toast('Browser run started.'); }
    catch (error) { toast(error.message); }
  });
  function startPolling() {
    if (state.poll) return;
    state.poll = setInterval(async () => {
      try { const st = await api('/api/browser/run'); renderRun(st); if (st.state !== 'running') { clearInterval(state.poll); state.poll = null; await refresh(); } }
      catch (error) { clearInterval(state.poll); state.poll = null; toast(error.message); }
    }, 1000);
  }
  function renderRun(st) {
    state.run = st; const job = st.job;
    if (!job) { $('run-state').textContent = 'No run yet.'; $('run-result').hidden = true; return; }
    if (st.state === 'running') { $('run-state').textContent = `Running ${job.script}${job.headed ? ' (headed)' : ''} for ${Math.round((job.elapsed_ms || 0) / 1000)} s… may visit ${job.allow.join(', ')}`; $('run-result').hidden = true; return; }
    const r = job.result || {}; $('run-state').textContent = `${job.script}: ${r.ok ? 'finished' : 'failed'} in ${Math.round((r.elapsed_ms || 0) / 1000)} s${r.error ? ' — ' + r.error : ''}`;
    $('run-result').hidden = false; $('run-summary').textContent = `${plural((r.pages || []).length, 'page')} captured${r.folder ? ' into ' + r.folder : ''}.`;
    const pages = $('run-pages'); pages.textContent = '';
    for (const p of r.pages || []) { const li = document.createElement('li'); const b = document.createElement('b'); b.textContent = p.title; const m = document.createElement('span'); m.className = 'muted'; m.textContent = ` ${p.url} · ${p.chars} chars${p.cut ? ' (cut)' : ''}`; li.append(b, m); pages.append(li); }
    const log = $('run-log'); log.textContent = '';
    for (const l of r.log || []) { const li = document.createElement('li'); li.className = l.ok ? 'ok' : 'bad'; li.textContent = `${l.n} ${l.do}: ${l.note || (l.ok ? 'ok' : 'failed')} (${l.ms} ms)`; log.append(li); }
    $('run-blocked').hidden = !(r.blocked || []).length; $('run-blocked').textContent = (r.blocked || []).length ? `Refused ${plural(r.blocked.length, 'navigation')} outside the allowed sites: ${r.blocked.slice(0, 5).join(', ')}` : '';
    $('run-import').hidden = !(r.pages || []).length || !r.folder; $('run-import').dataset.folder = r.folder || '';
  }
  // ---- captures → chats
  function renderCaptures() {
    const caps = state.info.captures || []; $('captures-note').textContent = caps.length ? `${plural(caps.length, 'capture')} in browser/captures (newest first).` : 'Nothing captured yet.';
    const list = $('captures-list'); list.textContent = '';
    for (const c of caps) {
      const li = document.createElement('li'); const head = document.createElement('div'); head.className = 'row-head'; const b = document.createElement('b'); b.textContent = `${c.script} · ${fmtTime(c.when)}`; const s = document.createElement('span'); s.className = 'status'; s.dataset.s = c.ok ? 'stable' : 'draft'; s.textContent = c.ok ? 'ok' : 'failed'; head.append(b, s); li.append(head);
      const p = document.createElement('p'); p.className = 'preview'; p.textContent = `${plural(c.pages, 'page')}${c.imported ? ` · ${plural(c.imported.notes, 'chat note')} imported` : ''}${c.error ? ' · ' + c.error : ''}`; li.append(p);
      if (c.pages) { const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'quiet small'; btn.textContent = c.imported ? 'Import again…' : 'Import as chat notes…'; btn.dataset.folder = c.folder; btn.dataset.pages = c.pages; li.append(btn); }
      list.append(li);
    }
  }
  function askImport(folder, pages) { state.pendingImport = folder; $('import-dialog-text').textContent = `${plural(pages, 'page')} from ${folder}.`; $('import-dialog').showModal(); }
  $('captures-list').addEventListener('click', event => { const btn = event.target.closest('button[data-folder]'); if (btn) askImport(btn.dataset.folder, Number(btn.dataset.pages)); });
  $('run-import').addEventListener('click', () => askImport($('run-import').dataset.folder, ((state.run || {}).job || {}).result ? state.run.job.result.pages.length : 0));
  $('import-cancel').addEventListener('click', () => $('import-dialog').close());
  $('import-confirm').addEventListener('click', async () => {
    $('import-dialog').close();
    try { const out = await api('/api/browser/import', {folder: state.pendingImport, confirm: true}); toast(`${plural(out.imported, 'chat note')} added, ${out.skipped} already there.${out.errors.length ? ' ' + out.errors[0] : ''}`); await refresh(); }
    catch (error) { toast(error.message); }
  });
  // ---- profile
  $('profile-clear').addEventListener('click', () => $('profile-dialog').showModal());
  $('profile-cancel').addEventListener('click', () => $('profile-dialog').close());
  $('profile-confirm').addEventListener('click', async () => { $('profile-dialog').close(); try { await api('/api/browser/profile/clear', {confirm: true}); toast('Browser logins forgotten.'); await refresh(); } catch (error) { toast(error.message); } });
  // ---- boot
  (async () => {
    try { const st = await api('/api/state'); csrf = st.csrf || ''; } catch (error) { $('locked').hidden = false; return; }
    $('browser-main').hidden = false;
    try { await refresh(); } catch (error) { toast(error.message); }
  })();
})();
