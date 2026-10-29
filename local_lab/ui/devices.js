(() => {
  'use strict';
  // Devices page: this device, its sessions, and the tailnet as Tailscale reports it. Read-only.
  const $ = id => document.getElementById(id);
  const GiB = 1024 ** 3; const gb = b => Number.isFinite(b) ? (b / GiB).toFixed(1) + ' GB' : 'unknown';
  const state = (id, text, kind = '') => { const el = $(id); el.textContent = text; el.dataset.state = kind; };
  let csrf = '';
  async function api(path, body) {
    const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {'Content-Type': 'application/json', ...(csrf ? {'X-CSRF-Token': csrf} : {})}};
    if (body !== undefined) { options.method = 'POST'; options.body = JSON.stringify(body); }
    let response; try { response = await fetch(path, options); } catch { throw new Error('Unable to reach your local Lab.'); }
    let data = {}; try { data = await response.json(); } catch {}
    if (response.status === 401) { $('locked').hidden = false; $('devices-main').hidden = true; throw new Error('Unlock the Lab first.'); }
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : 'The local request could not be completed.');
    return data;
  }
  function render(data) {
    const d = data.device;
    $('self-name').textContent = d.hostname; state('self-state', 'Observed', 'success');
    $('self-output').textContent = `Host: ${d.hostname}\nSystem: ${d.system} · ${d.architecture}\nChip: ${d.chip}\nMemory: ${gb(d.ram_bytes)} · free disk in home: ${gb(d.free_bytes)}\nPython ${d.python} · Docker ${d.docker_present ? 'present' : 'not found'} · Ollama ${d.ollama_running ? 'running' : 'not running'}`;
    const body = $('sessions').querySelector('tbody'); body.textContent = '';
    for (const s of data.sessions) { const tr = document.createElement('tr'); for (const v of [s.label, s.cpus, s.memory, s.image, s.alive ? (s.executing ? 'running a cell' : 'ready') : 'stopped']) { const td = document.createElement('td'); td.textContent = v; tr.append(td); } body.append(tr); }
    $('sessions').hidden = !data.sessions.length; state('sessions-state', data.sessions.length ? `${data.sessions.filter(s => s.alive).length} running` : 'None', data.sessions.length ? 'success' : '');
    $('sessions-output').textContent = data.sessions.length ? `${data.sessions.length} session(s) on this device.` : 'No sessions. Start one on the Graph page.';
    const t = data.tailnet;
    if (!t.available) { state('tailnet-state', 'Not installed', 'warn'); $('tailnet-output').textContent = t.note; $('peers').hidden = true; }
    else if (!t.logged_in) { state('tailnet-state', 'Not logged in', 'warn'); $('tailnet-output').textContent = t.note + (t.backend ? `\nBackend: ${t.backend}` : ''); $('peers').hidden = true; }
    else {
      state('tailnet-state', 'Logged in', 'success');
      $('tailnet-output').textContent = `Tailnet: ${t.tailnet || 'unknown'}\nThis device: ${t.self ? t.self.name + ' · ' + t.self.ips.join(', ') : 'unknown'}\nPeers: ${t.peers.length} (${t.peers.filter(p => p.online).length} online)`;
      const pb = $('peers').querySelector('tbody'); pb.textContent = '';
      for (const p of t.peers) { const tr = document.createElement('tr'); for (const v of [p.name, p.os, p.online ? 'online' : 'offline', p.ips.join(', '), p.exit_node ? 'exit node' : '']) { const td = document.createElement('td'); td.textContent = v; tr.append(td); } pb.append(tr); }
      $('peers').hidden = !t.peers.length;
    }
  }
  let devices = [];
  function renderDevices(data) {
    devices = data.devices; $('install-output').textContent = data.install.join('\n');
    const body = $('paired').querySelector('tbody'); body.textContent = '';
    for (const d of devices) {
      const tr = document.createElement('tr'); tr.dataset.name = d.name;
      for (const v of [d.name, d.host, String(d.port), d.tunnel === 'lab' ? 'by the Lab' : 'manual']) { const td = document.createElement('td'); td.textContent = v; tr.append(td); }
      const st = document.createElement('td'); st.textContent = d.connected ? 'connected' + (d.remote && d.remote.session && d.remote.session.alive ? ' · session running' : '') : (d.error ? 'error: ' + d.error : 'not connected'); tr.append(st);
      const act = document.createElement('td'); const mk = (label, act_, title) => { const b = document.createElement('button'); b.type = 'button'; b.className = 'quiet small'; b.dataset.act = act_; b.textContent = label; b.title = title; return b; };
      if (d.connected) { act.append(mk('New session', 'session', 'Start a session on this device'), mk('Disconnect', 'disconnect', 'End the connection')); } else act.append(mk('Connect…', 'connect', 'Review the command and connect'));
      act.append(mk('✕', 'forget', 'Forget this device')); tr.append(act); body.append(tr);
    }
    $('paired').hidden = !devices.length; state('paired-state', devices.length ? `${devices.filter(d => d.connected).length} connected of ${devices.length}` : 'None', devices.filter(d => d.connected).length ? 'success' : '');
    $('paired-output').textContent = devices.length ? devices.map(d => `${d.name}: ${d.host}:${d.port}` + (d.remote ? ` · Docker ${d.remote.docker_available ? 'yes' : 'no'} · workspace ${d.remote.workspace || 'none'}` : '')).join('\n') : 'No devices paired yet.';
  }
  async function refreshDevices() { try { renderDevices(await api('/api/devices')); } catch (error) { $('paired-note').textContent = error.message; } }
  $('pair').addEventListener('click', () => { $('pair-note').textContent = ''; $('pair-dialog').showModal(); $('pair-name').focus(); });
  $('pair-cancel').addEventListener('click', () => $('pair-dialog').close());
  $('pair-confirm').addEventListener('click', async () => {
    try { await api('/api/devices/pair', {name: $('pair-name').value.trim(), host: $('pair-host').value.trim(), port: Number($('pair-port').value), key_file: $('pair-key').value.trim(), tunnel: $('pair-tunnel').value, confirm: true}); $('pair-dialog').close(); await refreshDevices(); }
    catch (error) { $('pair-note').textContent = error.message; }
  });
  let pendingDevice = null;
  $('paired').addEventListener('click', async event => {
    const b = event.target.closest('button'); if (!b) return; const name = b.closest('tr').dataset.name; const d = devices.find(x => x.name === name); pendingDevice = name;
    try {
      if (b.dataset.act === 'connect') { $('connect-text').textContent = d.tunnel === 'lab' ? `The Lab starts this SSH forward to ${d.host}, then signs in to the Lab on port ${d.port}:` : `You run the forward yourself; the Lab signs in to 127.0.0.1:${d.port} with the copied key.`; $('connect-command').textContent = d.command ? d.command.join(' ') : '(no command; manual forward)'; $('connect-dialog').showModal(); $('connect-cancel').focus(); return; }
      if (b.dataset.act === 'session') { $('remote-session-text').textContent = `One container starts on ${name} with that device's limits and workspace; it appears in Resources as ${name}/<label>.`; $('remote-session-label').value = ''; $('remote-session-dialog').showModal(); $('remote-session-label').focus(); return; }
      if (b.dataset.act === 'disconnect') { await api('/api/devices/disconnect', {name}); }
      if (b.dataset.act === 'forget') { if (!window.confirm(`Forget device "${name}"? (Nothing on the device changes.)`)) return; await api('/api/devices/forget', {name}); }
      await refreshDevices(); await refresh();
    } catch (error) { $('paired-note').textContent = error.message; }
  });
  $('connect-cancel').addEventListener('click', () => $('connect-dialog').close());
  $('connect-confirm').addEventListener('click', async () => { $('connect-dialog').close(); $('paired-note').textContent = 'Connecting…'; try { await api('/api/devices/connect', {name: pendingDevice, confirm: true}); $('paired-note').textContent = ''; } catch (error) { $('paired-note').textContent = error.message; } await refreshDevices(); });
  $('remote-session-cancel').addEventListener('click', () => $('remote-session-dialog').close());
  $('remote-session-confirm').addEventListener('click', async () => { $('remote-session-dialog').close(); try { const row = await api('/api/devices/session/start', {name: pendingDevice, label: $('remote-session-label').value.trim() || undefined, confirm: true}); $('paired-note').textContent = `Session ${row.label} is running on ${pendingDevice}; use it from the Graph page.`; } catch (error) { $('paired-note').textContent = error.message; } await refreshDevices(); await refresh(); });
  async function refresh() { $('refresh-note').textContent = 'Checking…'; try { render(await api('/api/network')); $('refresh-note').textContent = ''; } catch (error) { $('refresh-note').textContent = error.message; } }
  $('refresh').addEventListener('click', refresh);
  (async () => { try { const st = await api('/api/state'); csrf = st.csrf; $('devices-main').hidden = false; await refresh(); await refreshDevices(); } catch (error) { if ($('locked').hidden) $('refresh-note').textContent = error.message; } })();
})();
