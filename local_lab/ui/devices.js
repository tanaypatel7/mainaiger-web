(() => {
  'use strict';
  // Devices page: this device, its sessions, and the tailnet as Tailscale reports it. Read-only.
  const $ = id => document.getElementById(id);
  const GiB = 1024 ** 3; const gb = b => Number.isFinite(b) ? (b / GiB).toFixed(1) + ' GB' : 'unknown';
  const state = (id, text, kind = '') => { const el = $(id); el.textContent = text; el.dataset.state = kind; };
  async function api(path) {
    let response; try { response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', redirect: 'error'}); } catch { throw new Error('Unable to reach your local Lab.'); }
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
    $('paired-output').textContent = data.pairing_note;
  }
  async function refresh() { $('refresh-note').textContent = 'Checking…'; try { render(await api('/api/network')); $('refresh-note').textContent = ''; } catch (error) { $('refresh-note').textContent = error.message; } }
  $('refresh').addEventListener('click', refresh);
  (async () => { try { await api('/api/state'); $('devices-main').hidden = false; await refresh(); } catch (error) { if ($('locked').hidden) $('refresh-note').textContent = error.message; } })();
})();
