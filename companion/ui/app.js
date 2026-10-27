'use strict';
const $ = id => document.getElementById(id);
let csrf = '', pending = null, generation = 0;
const status = message => { $('status').textContent = message; };
function clearSession() {
  generation += 1; csrf = ''; cancelReview(); hideReceipt();
  $('panel').hidden = true; $('login').hidden = false; $('owner-key').value = '';
  $('grants').replaceChildren(); $('grant-form').reset();
  $('resource').replaceChildren(new Option('Choose a local resource', ''));
  $('import-info').textContent = 'No directory request imported.';
}
async function api(path, body) {
  const requestGeneration = generation;
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf}, body: body === undefined ? undefined : JSON.stringify(body)});
  const value = await response.json();
  if (requestGeneration !== generation) throw new Error('The local owner session changed. Sign in again.');
  if ((response.status === 401 || response.status === 403) && path !== '/api/owner/login') clearSession();
  if (!response.ok) throw new Error(value.error || 'Local request failed');
  return value;
}
function hideReceipt() { $('receipt-data').value = ''; $('receipt').hidden = true; }
function cancelReview() { pending = null; $('review').hidden = true; }
async function refresh() {
  const state = await api('/api/owner/state'); csrf = state.csrf;
  $('login').hidden = true; $('panel').hidden = false;
  const previous = $('resource').value;
  $('resource').replaceChildren(new Option('Choose a local resource', ''));
  state.resources.forEach(r => $('resource').add(new Option(`${r.label} — ${r.root}`, r.id)));
  $('resource').value = previous;
  $('grants').replaceChildren();
  if (!state.grants.length) $('grants').textContent = 'No local grants.';
  state.grants.forEach(g => {
    const card = document.createElement('article'), text = document.createElement('p');
    text.textContent = `${g.requester} · ${g.resource_id} · ${g.operations.join(', ')} · ${g.state} · expires ${new Date(g.expires * 1000).toLocaleString()}`;
    card.append(text);
    const detail = document.createElement('small'); detail.textContent = `Space: ${g.space} · Request: ${g.request_id}`; card.append(detail);
    if (g.state === 'active') {
      const button = document.createElement('button'); button.textContent = 'Revoke'; button.className = 'secondary';
      button.onclick = async () => { try { await api('/api/owner/revoke', {id:g.id}); hideReceipt(); await refresh(); status('Capability revoked.'); } catch(e) { status(e.message); } };
      card.append(button);
    }
    $('grants').append(card);
  });
}
$('login-form').onsubmit = async event => {
  event.preventDefault();
  try { const result = await api('/api/owner/login', {key:$('owner-key').value}); csrf = result.csrf; $('owner-key').value = ''; await refresh(); status('Local owner panel unlocked.'); }
  catch(e) { $('owner-key').value = ''; status(e.message); }
};
$('logout').onclick = async () => {
  generation += 1;
  try { await api('/api/owner/logout', {}); status('Signed out.'); }
  catch(e) { status('Local display cleared. Server sign-out could not be confirmed; close this tab.'); }
  finally { clearSession(); }
};
$('grant-form').oninput = cancelReview;
$('grant-form').onsubmit = event => {
  event.preventDefault(); hideReceipt();
  const operations = [...document.querySelectorAll('input[name="operation"]:checked')].map(e => e.value);
  if (!operations.length) return status('Choose at least one read-only operation.');
  pending = {request_id:$('request-id').value.trim(), requester:$('requester').value.trim(), space:$('space').value.trim(), resource_id:$('resource').value, operations, hours:Number($('hours').value)};
  $('review-data').textContent = JSON.stringify(pending, null, 2); $('review').hidden = false; status('Review the exact recipient, local resource, operations and expiry before approving.');
};
$('approve').onclick = async () => {
  if (!pending) return; const grant = pending; cancelReview(); $('approve').disabled = true;
  try { const receipt = await api('/api/owner/approve', grant); $('receipt-data').value = JSON.stringify(receipt, null, 2); $('receipt').hidden = false; await refresh(); status('Local grant created. The private token is displayed once.'); }
  catch(e) { status(e.message); } finally { $('approve').disabled = false; }
};
$('cancel').onclick = cancelReview;
$('hide-token').onclick = hideReceipt;
$('request-file').onchange = async event => {
  cancelReview(); hideReceipt();
  try {
    const file = event.target.files[0]; if (!file || file.size > 8192) throw new Error('Choose request JSON smaller than 8 KiB.');
    const data = JSON.parse(await file.text()), fields = ['version','request_id','space_id','requester_id','display_name','requested_capabilities','status'];
    if (!data || Array.isArray(data) || Object.keys(data).some(k => !fields.includes(k)) || fields.some(k => !(k in data)) || data.version !== 1 || data.status !== 'accepted_pending_device') throw new Error('This is not a supported directory request export.');
    if (!['request_id','space_id','requester_id','display_name'].every(k => typeof data[k] === 'string' && data[k].trim().length && data[k].length <= 160 && !/[\u0000-\u001f]/.test(data[k]))) throw new Error('Request identity fields are invalid.');
    if (!Array.isArray(data.requested_capabilities) || data.requested_capabilities.length > 20 || !data.requested_capabilities.every(v => typeof v === 'string' && v.length <= 80 && !/[\u0000-\u001f]/.test(v))) throw new Error('Requested capabilities are invalid.');
    $('request-id').value = data.request_id; $('requester').value = data.requester_id; $('space').value = data.space_id;
    $('resource').value = ''; document.querySelectorAll('input[name="operation"]').forEach(e => { e.checked = false; });
    $('import-info').textContent = JSON.stringify(data, null, 2) + '\n\nUnverified request data only. No permission has been granted. Select a local resource and supported read-only operations yourself.';
    status('Request imported locally for review. Verify the requester separately before sharing any capability.');
  } catch(e) { status(e.message); } finally { event.target.value = ''; }
};
window.addEventListener('pagehide', clearSession);
window.addEventListener('pageshow', event => { if (event.persisted) refresh().catch(clearSession); });
refresh().catch(clearSession);
