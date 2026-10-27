(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let busy = false, latest = null, selected = null, pollTimer = null, watchTimer = null, watchStarted = 0, checkedOnce = false, pullOffered = false;
  async function json(path, options = {}) {
    const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', redirect: 'error', ...options});
    let data = {};
    try { data = await response.json(); } catch {}
    if (!response.ok) throw new Error(data.error || 'The local setup request could not be completed.');
    return data;
  }
  function state(id, value, kind = '') {
    const el = $(id); el.textContent = value; el.dataset.state = kind;
  }
  function controls() {
    $('setup-check').disabled = busy;
    $('setup-recheck').disabled = busy;
    $('setup-download').disabled = busy || !latest || latest.system !== 'Darwin' || !latest.installer_url || !!latest.installer_installed;
    $('setup-open').disabled = busy || !latest || !latest.installer_ready || !!latest.installer_installed;
    $('setup-pull').disabled = busy || !latest || !latest.docker_ready;
    $('setup-open-docker').disabled = busy || !latest || !latest.installer_installed || !!latest.docker_ready;
  }
  // Keep checking Docker's engine after the installer or the app was opened, then move on by itself.
  function watch(reason) {
    clearTimeout(watchTimer); watchStarted = Date.now();
    const tick = async () => {
      if (busy) { watchTimer = setTimeout(tick, 3000); return; }
      try { await check(); } catch { }
      const waited = Math.round((Date.now() - watchStarted) / 1000);
      if (latest && latest.docker_ready) { $('setup-watch-note').textContent = 'Docker engine ready.'; advance(); return; }
      if (waited > 900) { $('setup-watch-note').textContent = 'Stopped waiting after 15 minutes. Start Docker Desktop and select Check Docker again.'; return; }
      $('setup-watch-note').textContent = `${reason} Checking again in 5 s (${waited} s so far)…`;
      watchTimer = setTimeout(tick, 5000);
    };
    tick();
  }
  function advance() {
    if (!latest || !latest.docker_ready) return;
    if (!latest.image_ready) { if (!pullOffered) { pullOffered = true; $('setup-pull').scrollIntoView({behavior: 'smooth', block: 'center'}); review('pull'); } return; }
    const start = document.getElementById('session-start');
    if (start) { start.scrollIntoView({behavior: 'smooth', block: 'center'}); start.focus(); }
  }
  async function check() {
    state('setup-check-state', 'Checking');
    $('setup-check-output').textContent = 'Checking this Mac and Docker Desktop…';
    const data = await json('/api/setup');
    latest = data;
    const free = Number.isFinite(data.free_bytes) ? `${(data.free_bytes / 1073741824).toFixed(1)} GiB` : 'unavailable';
    $('setup-check-output').textContent = `Device: ${data.device}\nSystem: ${data.system} · ${data.architecture}\nFree space in home filesystem: ${free}\n${data.docker_message}`;
    $('setup-installer-source').textContent = data.installer_url ? `Docker Desktop ${data.installer_version}: ${data.installer_url}` : 'Installer source is unavailable on this device.';
    if (data.installer_installed) {
      state('setup-install-state', 'Installed', 'success');
      $('setup-install-output').textContent = 'Docker Desktop is installed. Start it and complete any first-run prompts, then check again.';
    } else if (data.installer_ready) {
      state('setup-install-state', 'Downloaded');
      $('setup-install-output').textContent = 'An installer is present. It will be verified again before opening. ' + (data.installer_verification || '');
    } else {
      state('setup-install-state', 'Needs action');
      $('setup-install-output').textContent = data.system === 'Darwin' ? 'Review the download to get the Docker installer.' : 'Automatic Docker installation is available only on supported Macs.';
    }
    state('setup-check-state', 'Complete', 'success');
    state('setup-ready-state', data.docker_ready ? 'Ready' : 'Waiting', data.docker_ready ? 'success' : '');
    $('setup-ready-output').textContent = data.docker_message;
    state('setup-pull-state', data.image_ready ? 'Image present' : 'Waiting', data.image_ready ? 'success' : '');
    $('setup-pull-output').textContent = data.image_ready ? 'Python image is present. Download again only if needed.' : 'The image has not been found locally.';
    controls();
    window.dispatchEvent(new CustomEvent('mainaiger:refresh-state'));
  }
  async function post(path) {
    const state = await json('/api/state');
    return json(path, {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf}, body:JSON.stringify({confirm:true})});
  }
  async function doCheck() {
    if (busy) return;
    busy = true; controls();
    try { await check(); }
    catch (error) { state('setup-check-state', 'Failed', 'failed'); $('setup-check-output').textContent = error.message; }
    finally { busy = false; controls(); }
  }
  function review(type) {
    selected = type;
    const source = latest && latest.installer_url ? latest.installer_url : 'Official Docker Desktop installer';
    const descriptions = {
      download: `Download Docker Desktop ${latest && latest.installer_version || ''} from ${source}. This uses your network and disk space. The installer will be verified before it can be opened.`,
      open: 'Verify the downloaded Docker installer again and open it on this Mac. Follow Docker and macOS prompts, review any requested permissions and accept Docker terms yourself.',
      pull: 'Download python:3.13.18-slim using your local Docker Desktop. This uses network and disk space. The image tag is not digest-verified.',
      'open-docker': 'Open the installed Docker Desktop app on this Mac (the command is: open -a Docker). Docker may ask you to accept its terms or for your password; MainAIger does not answer for you. The Lab then keeps checking until the engine is ready.'
    };
    $('setup-review-title').textContent = type === 'download' ? 'Review Docker download' : type === 'open' ? 'Open Docker installer' : type === 'open-docker' ? 'Open Docker Desktop' : 'Review Python image download';
    $('setup-review-description').textContent = descriptions[type];
    $('setup-review-dialog').showModal(); $('setup-review-cancel').focus();
  }
  function renderJob(job) {
    const progress = $('setup-progress');
    if (!job || !job.running) { progress.hidden = true; return; }
    if (job.action === 'open') {
      state('setup-install-state', 'Verifying');
      progress.hidden = false; progress.removeAttribute('value');
      $('setup-install-output').textContent = 'Rechecking the installer and opening it in macOS…';
      return;
    }
    state('setup-install-state', job.phase === 'verifying' ? 'Verifying' : 'Downloading');
    progress.hidden = false;
    if (Number.isFinite(job.total) && job.total > 0 && Number.isFinite(job.bytes)) {
      progress.max = job.total; progress.value = Math.min(job.bytes, job.total);
      $('setup-install-output').textContent = `${job.phase === 'verifying' ? 'Verifying installer' : 'Downloading'}… ${(job.bytes / 1048576).toFixed(1)} of ${(job.total / 1048576).toFixed(1)} MiB.`;
    } else {
      progress.removeAttribute('value');
      $('setup-install-output').textContent = job.phase === 'verifying' ? 'Verifying installer…' : `Downloading installer…${Number.isFinite(job.bytes) ? ` ${(job.bytes / 1048576).toFixed(1)} MiB received.` : ''}`;
    }
  }
  async function poll() {
    try {
      const job = await json('/api/setup/job');
      renderJob(job);
      if (job.running) { pollTimer = setTimeout(poll, 800); return; }
      busy = false;
      const result = job.result;
      if (result) {
        await check();
        $('setup-install-output').textContent = result.output || (result.ok ? 'Installer is ready.' : 'Installer download failed.');
        if (!result.ok) state('setup-install-state', 'Failed', 'failed');
        else if (job.action === 'open') { state('setup-install-state', 'User action needed'); watch('Finish Docker\'s installer, then open Docker Desktop (button above) or let it start.'); }
      }
      controls();
    } catch (error) {
      busy = false; $('setup-progress').hidden = true;
      state('setup-install-state', 'Check failed', 'failed'); $('setup-install-output').textContent = error.message;
      controls();
    }
  }
  $('setup-check').addEventListener('click', doCheck);
  $('setup-recheck').addEventListener('click', doCheck);
  $('setup-download').addEventListener('click', () => { if (!busy && latest) review('download'); });
  $('setup-open').addEventListener('click', () => { if (!busy && latest) review('open'); });
  $('setup-pull').addEventListener('click', () => { if (!busy && latest) review('pull'); });
  $('setup-open-docker').addEventListener('click', () => { if (!busy && latest) review('open-docker'); });
  // Run the device check as soon as the Lab is unlocked, so the Mainbook fills in without a click.
  window.addEventListener('mainaiger:state', () => { if (!checkedOnce) { checkedOnce = true; doCheck().then(() => { if (latest && latest.docker_ready) advance(); }); } });
  window.addEventListener('mainaiger:locked', () => { checkedOnce = false; pullOffered = false; clearTimeout(watchTimer); });
  $('setup-review-cancel').addEventListener('click', () => $('setup-review-dialog').close());
  $('setup-review-dialog').addEventListener('close', () => { selected = null; });
  $('setup-review-confirm').addEventListener('click', async () => {
    if (busy || !selected) return;
    const action = selected;
    $('setup-review-dialog').close();
    busy = true; controls();
    try {
      if (action === 'download') {
        state('setup-install-state', 'Starting');
        $('setup-install-output').textContent = 'Starting the installer download…';
        await post('/api/setup/installer/download');
        await poll();
        return;
      }
      if (action === 'open') {
        state('setup-install-state', 'Opening');
        $('setup-install-output').textContent = 'Verifying the installer before opening…';
        await post('/api/setup/installer/open');
        await poll();
        return;
      } else if (action === 'open-docker') {
        state('setup-ready-state', 'Starting');
        const result = await post('/api/setup/docker/open');
        $('setup-ready-output').textContent = result.output || '';
        if (!result.ok) state('setup-ready-state', 'Failed', 'failed'); else watch('Docker Desktop is starting.');
      } else {
        state('setup-pull-state', 'Downloading');
        $('setup-pull-output').textContent = 'Downloading the image… Docker does not provide progress in this preview.';
        const result = await post('/api/setup/pull');
        await check();
        $('setup-pull-output').textContent = result.output || (result.ok ? 'Download completed.' : 'Download failed.');
        if (!result.ok) state('setup-pull-state', 'Failed', 'failed'); else { busy = false; controls(); advance(); return; }
      }
    } catch (error) {
      if (action === 'pull') { state('setup-pull-state', 'Failed', 'failed'); $('setup-pull-output').textContent = error.message; }
      else { state('setup-install-state', 'Failed', 'failed'); $('setup-install-output').textContent = error.message; }
    }
    busy = false; controls();
  });
  window.addEventListener('pagehide', () => { if (pollTimer) clearTimeout(pollTimer); if (watchTimer) clearTimeout(watchTimer); });
})();
