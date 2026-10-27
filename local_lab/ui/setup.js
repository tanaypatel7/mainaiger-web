(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let busy = false;
  async function json(path, options = {}) {
    const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...options});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Setup operation failed.');
    return data;
  }
  async function check() {
    $('setup-check-output').textContent = 'Checking local Docker Desktop…';
    $('setup-pull').disabled = true;
    const data = await json('/api/setup');
    $('setup-check-output').textContent = `Device: ${data.device}\nSystem: ${data.system} ${data.architecture}\nFree space in home filesystem: ${(data.free_bytes / 1073741824).toFixed(1)} GiB\n${data.docker_message}\nImage: ${data.image} — ${data.image_ready ? 'present' : 'not found'}\n${data.image_integrity}`;
    $('setup-pull').disabled = !data.docker_ready;
    $('setup-pull-output').textContent = data.image_ready ? 'Image is present. Download again only if needed.' : 'Image has not been downloaded.';
  }
  $('setup-check').addEventListener('click', async () => {
    if (busy) return;
    try { await check(); } catch (e) { $('setup-check-output').textContent = e.message; }
  });
  $('setup-pull').addEventListener('click', async () => {
    if (busy || !confirm('Download python:3.13.18-slim using local Docker Desktop? This uses your network and disk space. The tag is not digest-verified.')) return;
    busy = true; $('setup-pull').disabled = true; $('setup-check').disabled = true;
    $('setup-pull-output').textContent = 'Downloading… This can take several minutes.';
    try {
      const state = await json('/api/state');
      const result = await json('/api/setup/pull', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify({confirm: true})});
      $('setup-pull-output').textContent = (result.ok ? 'Completed: ' : 'Failed: ') + result.output;
      const output = $('setup-pull-output').textContent;
      await check(); $('setup-pull-output').textContent = output;
    } catch (e) { $('setup-pull-output').textContent = e.message; }
    finally { busy = false; $('setup-check').disabled = false; }
  });
})();
