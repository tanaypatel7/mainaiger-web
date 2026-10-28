(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {data: {ideas: [], entries: [], conversations: []}, tab: 'confirmed', idea: '', entry: null, conversation: null, selected: new Set(), search: null, dirty: false, busy: false, ai: null};
  let csrf = '';
  const el = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
  const unwrap = r => r.entry || r;
  const statusOf = e => e.status || e.state || 'draft';
  const list = value => Array.isArray(value) ? value : [];
  function notice(message, error = false) { $('notice').textContent = message; $('notice').dataset.error = String(error); }
  function date(value) { if (!value) return ''; const d = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value); return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString(); }
  function selectedMessages() { return list(state.conversation && state.conversation.messages).filter(m => state.selected.has(m.id)); }
  async function api(path, body) {
    const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {'Content-Type': 'application/json', ...(csrf ? {'X-CSRF-Token': csrf} : {})}};
    if (body !== undefined) { options.method = 'POST'; options.body = JSON.stringify(body); }
    let response; try { response = await fetch(path, options); } catch { throw new Error('Unable to reach your local Lab.'); }
    let data = {}; try { data = await response.json(); } catch {}
    if (response.status === 401) { $('locked').hidden = false; $('memory-main').hidden = true; throw new Error('Unlock the Lab first on the Setup page.'); }
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : 'The local request could not be completed.');
    return data;
  }
  async function task(fn) {
    if (state.busy) return;
    state.busy = true; controls();
    try { await fn(); } catch (e) { notice(e.message, true); }
    finally { state.busy = false; controls(); }
  }
  function controls() {
    for (const b of document.querySelectorAll('button')) if (!b.id.startsWith('browser-')) b.disabled = state.busy;
    for (const field of document.querySelectorAll('input,textarea,select')) if (!field.id.startsWith('browser-')) field.disabled = state.busy;
    const hasSelected = state.selected.size > 0;
    $('draft-selection').disabled = state.busy || !hasSelected;
    $('ai-selection').disabled = state.busy || !hasSelected;
    const e = state.entry;
    $('entry-confirm').disabled = state.busy || !e || !e.id || statusOf(e) !== 'draft' || state.dirty;
    $('entry-ai').disabled = state.busy || !e || !e.id || state.dirty;
    $('ai-submit').disabled = state.busy || !$('ai-model').options.length;
    for (const id of ['entry-reject', 'entry-archive', 'entry-reopen', 'entry-mainbook-open', 'entry-mainbook-download']) $(id).disabled = state.busy || state.dirty;
  }
  function canLeave() { return !state.dirty || window.confirm('Discard your unsaved memory edits?'); }
  function ideaOptions(select, empty, selected) {
    select.textContent = ''; const option = el('option', empty); option.value = ''; select.append(option);
    for (const idea of state.data.ideas) { const o = el('option', idea.title); o.value = idea.id; select.append(o); }
    select.value = selected || '';
  }
  async function refresh(offset = 0) {
    const data = await api('/api/memory?offset=' + offset + '&limit=200'); csrf = data.csrf || csrf;
    const combine = key => offset ? [...new Map([...state.data[key], ...list(data[key])].map(row => [row.id, row])).values()] : list(data[key]);
    state.data = {...data, ideas: combine('ideas'), entries: combine('entries'), conversations: combine('conversations')};
    window.dispatchEvent(new CustomEvent('memory-ready', {detail:{csrf}}));
    $('load-more').hidden = data.next_offset == null;
    $('load-more').textContent = 'Load more';
    ideaOptions($('idea-filter'), 'All ideas', state.idea);
    const currentIdea = state.entry && $('entry-idea').value;
    ideaOptions($('entry-idea'), 'Unassigned', currentIdea || state.idea);
    const entries = state.data.entries;
    const counts = state.data.counts || {};
    const count = {chats: counts.conversations ?? state.data.conversations.filter(c => !c.deleted).length, draft: counts.draft ?? entries.filter(e => statusOf(e) === 'draft').length, confirmed: counts.confirmed ?? entries.filter(e => statusOf(e) === 'confirmed').length};
    $('count-chats').textContent = count.chats; $('count-drafts').textContent = count.draft; $('count-confirmed').textContent = count.confirmed;
    for (const key of ['chats', 'draft', 'confirmed']) $('badge-' + key).textContent = count[key];
    renderList();
  }
  function selectTab(tab) {
    state.tab = tab; state.search = null; $('search-query').value = ''; $('search-cost').hidden = true;
    for (const b of document.querySelectorAll('[data-tab]')) { b.setAttribute('aria-selected', String(b.dataset.tab === tab)); b.classList.toggle('selected', b.dataset.tab === tab); b.tabIndex = b.dataset.tab === tab ? 0 : -1; }
    $('collection').setAttribute('aria-labelledby', 'tab-' + tab);
    const labels = {confirmed: ['Confirmed memory', 'Reviewed by you. Search and reuse without a model call.'], draft: ['Review queue', 'Edit, confirm, or reject. Drafts are excluded from memory search.'], chats: ['Original chats', 'Select the messages worth keeping. Sources remain separate from memory.'], history: ['Archived / rejected', 'Kept for traceability; excluded from memory search.']};
    $('collection-title').textContent = labels[tab][0]; $('collection-note').textContent = labels[tab][1]; $('search-form').hidden = tab !== 'confirmed';
    renderList();
  }
  function renderList() {
    const root = $('memory-list'); root.textContent = '';
    let rows = state.tab === 'chats' ? state.data.conversations.filter(c => !c.deleted) : (state.search !== null && state.tab === 'confirmed' ? state.search : state.data.entries.filter(e => state.tab === 'history' ? ['archived', 'rejected'].includes(statusOf(e)) : statusOf(e) === state.tab));
    if (state.tab !== 'chats' && state.idea) rows = rows.filter(e => e.idea_id === state.idea);
    if (!rows.length) { root.append(el('p', state.search !== null ? 'No confirmed memories matched. Create a new draft or change the search.' : state.tab === 'chats' ? 'Import a ChatGPT or Claude ZIP / JSON export to begin.' : state.tab === 'draft' ? 'No drafts to review. Write a note or select content from an imported chat.' : 'Nothing here yet. Your ideas can grow one reviewed entry at a time.', 'empty-list')); return; }
    for (const row of rows) {
      const b = el('button', undefined, 'memory-row'); b.type = 'button';
      const isChat = state.tab === 'chats'; b.dataset.id = row.id; b.classList.toggle('selected', isChat ? state.conversation && state.conversation.id === row.id : state.entry && state.entry.id === row.id);
      b.append(el('span', row.title || 'Untitled', 'row-title'));
      if (!isChat && (row.preview || row.body)) b.append(el('span', row.preview || row.body, 'row-preview'));
      b.append(el('span', isChat ? `${row.provider || 'Chat'} · ${row.message_count || 0} messages` : `${row.kind || 'note'} · revision ${row.revision || 1}` + (row.idea_id ? ' · ' + (state.data.ideas.find(i => i.id === row.idea_id) || {}).title : ''), 'row-meta'));
      b.addEventListener('click', () => { if (!canLeave()) return; task(() => isChat ? openConversation(row.id) : openEntry(row.id)); }); root.append(b);
    }
  }
  function panel(name) { for (const id of ['detail-empty', 'conversation-panel', 'entry-panel']) $(id).hidden = id !== name; }
  async function openConversation(id) {
    const response = await api('/api/memory/conversation?id=' + encodeURIComponent(id)); state.conversation = response.conversation || response;
    state.entry = null; state.selected.clear(); state.dirty = false; panel('conversation-panel');
    $('conversation-title').textContent = state.conversation.title || 'Untitled conversation';
    $('conversation-note').textContent = `${state.conversation.provider || 'Imported'} · Original messages are read-only. Branches, when present, remain source records.`;
    const root = $('messages'); root.textContent = '';
    for (const message of list(state.conversation.messages)) {
      const card = el('article', undefined, 'message-card'), label = el('label'), input = el('input'); input.type = 'checkbox'; input.value = message.id;
      const heading = el('span', message.role || 'message', 'role'); label.append(input, heading); card.append(label);
      if (message.parent_id) card.append(el('p', 'Parent: ' + message.parent_id, 'small muted'));
      card.append(el('pre', message.text || '[No supported text]'));
      input.setAttribute('aria-label', 'Select ' + (message.role || 'message') + ': ' + (message.text || '').slice(0,80));
      input.addEventListener('change', () => { if (input.checked) state.selected.add(message.id); else state.selected.delete(message.id); selectionNote(); controls(); }); root.append(card);
    }
    selectionNote(); renderList();
  }
  function selectionNote() { $('selection-note').textContent = state.selected.size ? `${state.selected.size} message${state.selected.size === 1 ? '' : 's'} selected. Only selected text is used for a draft.` : 'Select messages to keep their source links.'; }
  async function openEntry(id) { const response = await api('/api/memory/entry?id=' + encodeURIComponent(id)); showEntry(unwrap(response)); }
  function showEntry(entry) {
    state.entry = entry; state.conversation = null; state.selected.clear(); state.dirty = false; panel('entry-panel');
    $('entry-title').value = entry.title || ''; $('entry-body').value = entry.body || ''; $('entry-kind').value = entry.kind || 'note'; $('entry-tags').value = list(entry.tags).join(', '); ideaOptions($('entry-idea'), 'Unassigned', entry.idea_id);
    const status = statusOf(entry); $('entry-status').textContent = entry.id ? status : 'Unsaved draft'; $('entry-status').dataset.status = status;
    $('entry-version').textContent = entry.id ? 'Revision ' + entry.revision + (entry.updated_at ? ' · ' + date(entry.updated_at) : '') : '';
    $('entry-rule').textContent = status === 'confirmed' ? 'Editing creates a new draft revision. Confirm it again before it is reused as memory.' : 'Save as a draft, then confirm when it is ready to reuse.';
    $('entry-confirm').hidden = status !== 'draft'; $('entry-reject').hidden = !entry.id || status !== 'draft'; $('entry-archive').hidden = !entry.id || !['draft', 'confirmed'].includes(status); $('entry-reopen').hidden = !entry.id || !['archived','rejected'].includes(status);
    $('entry-mainbook-download').hidden = !entry.id; $('entry-mainbook-download').textContent = entry.mainbook || entry.has_mainbook ? 'Download Mainbook' : 'Download as Mainbook';
    $('entry-mainbook-open').hidden = !entry.id || !(entry.mainbook || entry.has_mainbook);
    renderProvenance(entry); renderList(); controls();
  }
  function renderProvenance(entry) {
    const sources = $('sources-list'); sources.textContent = '';
    const values = list(entry.sources); $('sources-summary').textContent = 'Sources (' + (values.length || list(entry.source_message_ids).length) + ')';
    if (!values.length) sources.append(el('p', list(entry.source_message_ids).length ? 'Selected source links will be shown after saving.' : 'Manually written content. No imported messages linked.', 'muted'));
    for (const source of values) { const card = el('div', undefined, 'source-record'); card.append(el('strong', (source.conversation_title || 'Conversation') + ' · ' + (source.role || 'message'))); card.append(el('pre', source.deleted ? '[Source content deleted]' : source.text || '')); sources.append(card); }
    const history = $('history-list'); history.textContent = '';
    if (!list(entry.history).length) history.append(el('p', 'Revision history begins when you save.', 'muted'));
    for (const version of list(entry.history).slice().reverse()) { const row = el('div', undefined, 'history-record'), details = el('details'); details.append(el('summary', `Revision ${version.revision} · ${version.action || ''} · ${date(version.at)}`)); const snap = version.snapshot || {}; details.append(el('pre', [snap.title, snap.body].filter(Boolean).join('\n\n'))); row.append(details); history.append(row); }
  }
  function formPayload() {
    const e = state.entry || {}; return {...(e.id ? {id:e.id, expected_revision:e.revision} : {}), title:$('entry-title').value.trim(), body:$('entry-body').value, kind:$('entry-kind').value, idea_id:$('entry-idea').value || null, tags:$('entry-tags').value.split(',').map(s=>s.trim()).filter(Boolean), source_message_ids:list(e.source_message_ids), ...(e.mainbook ? {mainbook:e.mainbook} : {})};
  }
  function newEntry(source) { showEntry({title:'',body:'',kind:'note',status:'draft',tags:[],idea_id:state.idea || null,source_message_ids:[],...source}); state.dirty = !!source; controls(); $('entry-title').focus(); }
  async function saveEntry() {
    const entry = unwrap(await api('/api/memory/save', formPayload())); state.search = null; await refresh(); showEntry(entry);
    notice('Draft saved. Review it, then confirm to include it in persistent memory.');
  }
  async function review(action) {
    const e = state.entry; if (!e || !e.id || state.dirty) return;
    const entry = unwrap(await api('/api/memory/review', {id:e.id,action,expected_revision:e.revision})); await refresh(); showEntry(entry);
    notice(action === 'confirm' ? 'Memory confirmed. It is now available to memory search.' : action === 'reopen' ? 'Reopened as a draft for review.' : 'Memory ' + (action === 'archive' ? 'archived' : 'rejected') + '. It is excluded from memory search.');
  }
  async function aiDialog(kind) {
    if (kind === 'entry' && (!state.entry || !state.entry.id || state.dirty)) return;
    state.ai = kind === 'entry' ? {entry_id:state.entry.id,expected_revision:state.entry.revision,idea_id:state.entry.idea_id || null,message_ids:[]} : {message_ids:[...state.selected],idea_id:state.idea || null};
    $('ai-scope').textContent = kind === 'entry' ? `Use the saved content of “${state.entry.title}” to propose a revised draft. Save any manual changes first.` : `Use ${state.selected.size} selected message${state.selected.size === 1 ? '' : 's'} from “${state.conversation.title}” to propose one organized memory.`;
    $('ai-instruction').value = kind === 'entry' ? 'Improve this memory while preserving its meaning, sources, and uncertainty. Keep it concise.' : 'Extract one useful memory from these messages. Include only relevant facts or methods; preserve uncertainty and distinguish user statements from assistant suggestions.';
    const result = await api('/api/memory/models'); const select = $('ai-model'); select.textContent = '';
    for (const model of list(result.models)) { const name = typeof model === 'string' ? model : model.name; const o = el('option',name); o.value=name; select.append(o); }
    $('ai-model-note').textContent = select.options.length ? 'Runs on this machine through Ollama. No automatic remote fallback.' : 'No local models available. Install and start one on the Models page; manual memory editing works without a model.';
    $('ai-dialog').showModal(); controls();
  }
  function download(data, name, type='application/json') { const url = URL.createObjectURL(new Blob([typeof data === 'string' ? data : JSON.stringify(data,null,2)],{type})); const a = el('a'); a.href=url; a.download=name; document.body.append(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000); }
  document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>selectTab(b.dataset.tab)));
  document.querySelector('.memory-tabs').addEventListener('keydown',e=>{ if(!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End'].includes(e.key))return; e.preventDefault();const tabs=[...document.querySelectorAll('[data-tab]')];let i=tabs.indexOf(document.activeElement); if(i<0)return;i=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(['ArrowUp','ArrowLeft'].includes(e.key)?-1:1)+tabs.length)%tabs.length;tabs[i].focus();selectTab(tabs[i].dataset.tab); });
  $('idea-filter').addEventListener('change',()=>{state.idea=$('idea-filter').value;state.search=null;$('search-query').value='';$('search-cost').hidden=true;renderList();});
  $('refresh').addEventListener('click',()=>task(refresh));
  $('load-more').addEventListener('click',()=>task(()=>refresh(state.data.next_offset || 0)));
  window.addEventListener('memory-refresh',()=>task(refresh));
  $('new-entry').addEventListener('click',()=>{if(canLeave())newEntry();});
  $('entry-form').addEventListener('input',()=>{state.dirty=true;controls();});
  $('entry-form').addEventListener('submit',e=>{e.preventDefault();task(saveEntry);});
  for(const action of ['confirm','reject','archive','reopen']) $('entry-'+action).addEventListener('click',()=>task(()=>review(action)));
  $('entry-ai').addEventListener('click',()=>task(()=>aiDialog('entry')));
  $('ai-selection').addEventListener('click',()=>task(()=>aiDialog('selection')));
  $('clear-selection').addEventListener('click',()=>{state.selected.clear();for(const input of $('messages').querySelectorAll('input'))input.checked=false;selectionNote();controls();});
  $('draft-selection').addEventListener('click',()=>{const messages=selectedMessages();if(!messages.length)return;const title=state.conversation.title;newEntry({title,body:messages.map(m=>(m.role||'message')+':\n'+m.text).join('\n\n'),source_message_ids:messages.map(m=>m.id)});notice('Edit the selected content into a useful memory, then save and confirm it.');});
  $('new-idea').addEventListener('click',()=>{$('idea-title').value='';$('idea-dialog').showModal();$('idea-title').focus();});
  $('idea-form').addEventListener('submit',e=>{e.preventDefault();task(async()=>{const result=await api('/api/memory/ideas',{title:$('idea-title').value.trim()});state.idea=(result.idea||result).id;await refresh();$('idea-dialog').close();notice('Idea space created. New memories can be assigned to it.');});});
  document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
  $('ai-form').addEventListener('submit',e=>{e.preventDefault();task(async()=>{notice('The local model is preparing a draft…');const result=await api('/api/memory/propose',{...state.ai,model:$('ai-model').value,instruction:$('ai-instruction').value.trim(),confirm:true});$('ai-dialog').close();state.search=null;await refresh();selectTab('draft');showEntry(unwrap(result));notice('AI draft ready. Check its content and sources before confirming.');});});
  $('search-form').addEventListener('submit',e=>{e.preventDefault();task(async()=>{const q=$('search-query').value.trim();if(!q){state.search=null;$('search-cost').hidden=true;renderList();return;}const result=await api('/api/memory/search?q='+encodeURIComponent(q)+(state.idea?'&idea_id='+encodeURIComponent(state.idea):''));state.search=list(result.entries||result.results||result);renderList();$('search-cost').hidden=false;$('search-cost').textContent='Saved memory only · 0 model calls · $0 API cost. Open a match to reuse it or request an AI revision.';});});
  $('import-open').addEventListener('click',()=>{if(canLeave())$('import-file').click();});
  $('import-file').addEventListener('change',()=>task(async()=>{const file=$('import-file').files[0];if(!file)return;try{if(file.size>32*1024*1024)throw new Error('Choose a ZIP or JSON export no larger than 32 MiB.');notice('Reading '+file.name+' locally…');const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let offset=0;offset<bytes.length;offset+=16384)binary+=String.fromCharCode(...bytes.subarray(offset,offset+16384));const result=await api('/api/memory/import',{filename:file.name,data:btoa(binary)});await refresh();selectTab('chats');panel('detail-empty');state.entry=null;state.conversation=null;state.dirty=false;const warnings=list(result.warnings);notice('Import complete. Select an original chat and choose the messages to keep.'+(warnings.length?' '+warnings.join(' '):''));}finally{$('import-file').value='';}}));
  $('save-mainbook').addEventListener('click',()=>{if(!canLeave())return;$('mainbook-title').value='';$('mainbook-dialog').showModal();});
  $('mainbook-form').addEventListener('submit',e=>{e.preventDefault();task(async()=>{const entry=unwrap(await api('/api/memory/mainbook',{title:$('mainbook-title').value.trim(),idea_id:state.idea||null}));await refresh();$('mainbook-dialog').close();selectTab('draft');showEntry(entry);notice('Mainbook snapshot saved as draft memory. Nothing was executed.');});});
  $('entry-mainbook-download').addEventListener('click',()=>task(async()=>{const result=await api('/api/memory/graph?id='+encodeURIComponent(state.entry.id));download(result.document||result,'memory-mainbook.ipymb');}));
  $('entry-mainbook-open').addEventListener('click',()=>task(async()=>{const current=await api('/api/graph');if(!window.confirm('Replace the currently open graph with this saved Mainbook? Save unsaved graph work first. This only opens the snapshot; no code will run.'))return;await api('/api/memory/open-mainbook',{id:state.entry.id,expected_revision:state.entry.revision,graph_revision:current.revision,confirm:true});window.location.assign('/graph');}));
  $('export-memory').addEventListener('click',()=>task(async()=>{download(await api('/api/memory/export'),'mainaiger-memory-export.json');notice('Memory and source export downloaded. It includes private conversation content.');}));
  window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue='';}});
  task(async()=>{await refresh();$('locked').hidden=true;$('memory-main').hidden=false;selectTab('confirmed');});
})();
