(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let csrf = '', pending = false, timer = null, status = null, lastCompleted = null;
  const selected = new Set();
  const buttonIds = ['browser-open','browser-export','browser-scan','browser-capture','browser-close','browser-check','browser-bundle'];
  function controls() {
    const busy = pending || !!(status && status.busy), running = !!(status && status.running);
    for (const id of buttonIds) $(id).disabled = busy;
    $('browser-provider').disabled = busy || running;
    $('browser-capture-files').disabled = busy;
    $('browser-open').disabled = busy || running || !!(status && status.available === false);
    for (const id of ['browser-export','browser-scan','browser-close']) $(id).disabled = busy || !running;
    $('browser-capture').disabled = busy || !running || !selected.size || selected.size > 20;
    for (const c of $('browser-chat-list').querySelectorAll('input')) c.disabled = busy;
    $('browser-selection').textContent = `${selected.size} selected · up to 20 per capture. Source records only; review before adding to memory.`;
  }
  async function api(action, body) {
    const options = {credentials:'same-origin',cache:'no-store',redirect:'error',headers:{'Content-Type':'application/json',...(csrf?{'X-CSRF-Token':csrf}:{})}};
    if (body !== undefined) { options.method='POST';options.body=JSON.stringify(body); }
    const r = await fetch('/api/memory/browser' + (action ? '/' + action : ''),options);
    const data = await r.json(); if(!r.ok) throw new Error(data.error || 'Browser action could not be completed.');
    if(data.csrf)csrf=data.csrf;
    return data;
  }
  function render(next) {
    status = next;
    const job = status.job || {}, chats = Array.isArray(status.chats)?status.chats:[];
    const progress = job.total ? ` · ${job.completed || 0}/${job.total}` : '';
    $('browser-status').textContent = status.error || (status.busy ? `${job.action || 'Working'}…${progress}` : status.available === false ? 'Browser support is not installed on this device. ZIP / JSON import remains available.' : status.running ? `${status.provider === 'claude'?'Claude':'ChatGPT'} browser open. Sign in there, then request an export or scan visible chats.` : 'Ready to open a dedicated browser. Login credentials are entered in that browser.');
    const warnings = Array.isArray(status.warnings)?status.warnings:[];
    const result = status.result || {};
    const resultText = typeof result === 'string' ? result : result.message || result.note || '';
    $('browser-warning').textContent = [resultText,...warnings,...(Array.isArray(result.warnings)?result.warnings:[])].filter(Boolean).join(' ');
    const root = $('browser-chat-list');root.textContent='';
    const urls=new Set(chats.map(c=>c.url));for(const url of selected)if(!urls.has(url))selected.delete(url);
    for(const chat of chats) {
      const row=document.createElement('label'),input=document.createElement('input'),text=document.createElement('span');input.type='checkbox';input.id='browser-chat-'+root.children.length;input.checked=selected.has(chat.url);text.textContent=chat.title||'Untitled chat';row.append(input,text);root.append(row);
      input.addEventListener('change',()=>{if(input.checked)selected.add(chat.url);else selected.delete(chat.url);controls();});
    }
    $('browser-capture-row').hidden=!chats.length && !result.bundle_available && !status.bundle_available;
    $('browser-bundle').hidden=!(result.bundle_available || status.bundle_available);
    // A completed capture only refreshes the read-only source listing. It never confirms memory.
    if(!status.busy && job.action==='capture' && job.id && lastCompleted!==job.id && job.state!=='failed') {
      lastCompleted=job.id;window.dispatchEvent(new CustomEvent('memory-refresh'));
      const captured=Number.isFinite(result.captured)?result.captured:null;
      $('browser-status').textContent=(captured === 0 ? 'Capture finished without collecting any chats. Review the warnings and try a provider export.' : captured !== null ? `Captured ${captured} chat${captured === 1 ? '' : 's'}. Open Original chats to review the source records.` : 'Capture finished. Open Original chats to inspect what was collected.') + ' Nothing was confirmed as memory.';
    }
    controls();
    clearTimeout(timer);timer=null;
    if(status.busy && $('browser-import').open)timer=setTimeout(check,1200);
  }
  async function check() {
    if(pending)return;
    try{
      if(!csrf) {
        const r=await fetch('/api/memory?offset=0&limit=1',{credentials:'same-origin',cache:'no-store',redirect:'error'});
        if(!r.ok)throw new Error('Unlock the Lab first on the Setup page.');
        csrf=(await r.json()).csrf;
      }
      render(await api(''));
    }catch(e){$('browser-status').textContent=e.message;clearTimeout(timer);timer=null;}
  }
  async function act(action, data={}) {
    if(pending)return;
    pending=true;controls();$('browser-status').textContent='Starting '+action+'…';
    try{render(await api(action,{...data,confirm:true}));}catch(e){$('browser-status').textContent=e.message;}finally{pending=false;controls();}
  }
  window.addEventListener('memory-ready',e=>{csrf=e.detail.csrf;if($('browser-import').open)check();});
  $('browser-import').addEventListener('toggle',()=>{if($('browser-import').open)check();else{clearTimeout(timer);timer=null;}});
  $('browser-check').addEventListener('click',check);
  $('browser-open').addEventListener('click',()=>act('open',{provider:$('browser-provider').value}));
  $('browser-export').addEventListener('click',()=>{if(window.confirm('Request your provider’s bulk data export using this signed-in browser? If available, MainAIger will use the export controls. A provider may email you a download link later. This does not mean an archive has been downloaded.'))act('export');});
  $('browser-scan').addEventListener('click',()=>act('scan'));
  $('browser-capture').addEventListener('click',()=>{if(!selected.size||selected.size>20)return;if(window.confirm(`Read and save text from ${selected.size} selected chats as local source records? ${$('browser-capture-files').checked ? 'Accessible attachments will also be downloaded; some files may be unavailable.' : 'Attachments will not be downloaded.'} Nothing becomes confirmed memory until you review it.`))act('capture',{urls:[...selected],capture_files:$('browser-capture-files').checked});});
  $('browser-bundle').addEventListener('click',async()=>{
    if(pending)return;pending=true;controls();
    try {
      const r=await fetch('/api/memory/browser/bundle',{credentials:'same-origin',cache:'no-store',redirect:'error'});
      if(!r.ok)throw new Error('The collected ZIP is not available yet. Check the capture result.');
      const url=URL.createObjectURL(await r.blob()),a=document.createElement('a');a.href=url;a.download='mainaiger-captured-chats.zip';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      $('browser-status').textContent='Collected ZIP downloaded. It includes private chat content and any successfully downloaded attachments.';
    } catch(e){$('browser-status').textContent=e.message;}finally{pending=false;controls();}
  });
  $('browser-close').addEventListener('click',()=>act('close'));
  window.addEventListener('pagehide',()=>{clearTimeout(timer);timer=null;});
  controls();
})();
