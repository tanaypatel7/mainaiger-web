(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const {DirectoryClient,listingPayload,REGIONS,DEVICES,APIS,CAPABILITIES,STATUS}=MainaigerDirectory;
  let section='browse',offset=0,hasMore=false,requestOffset=0,requestsHasMore=false,publicRows=[],spaces=[],requests=[],busy=false;
  let editing=null,requesting=null,deleting=null,lastIdentity=null,publicVersion=0,privateVersion=0,lastRefresh=null,polling=null,authTimer=null;
  const auth=new MainaigerAuth(window.MAINAIGER_AUTH_CONFIG,{onChange:renderAuth});
  const directory=new DirectoryClient(auth);
  const create=(tag,text,className)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;};
  function message(id,text,error=false){$(id).textContent=text;$(id).classList.toggle('error',error);}
  function button(text,fn,className='button button-outline compact'){const node=create('button',text,className);node.type='button';node.addEventListener('click',fn);return node;}
  function empty(parent,title,body){const node=create('div',undefined,'empty-state');node.append(create('h3',title),create('p',body));parent.replaceChildren(node);}
  function date(value){const d=new Date(value);return Number.isNaN(d.getTime())?'Unknown':d.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});}
  function options(id,values){Object.entries(values).forEach(([value,label])=>{const option=create('option',label);option.value=value;$(id).append(option);});}
  function checks(id,values,name){Object.entries(values).forEach(([value,label])=>{const wrap=create('label',undefined,'check-row'),input=create('input');input.type='checkbox';input.name=name;input.value=value;wrap.append(input,create('span',label));$(id).append(wrap);});}
  const checked=name=>Array.from(document.querySelectorAll('input[name="'+name+'"]:checked'),el=>el.value);
  function setChecked(name,values=[]){document.querySelectorAll('input[name="'+name+'"]').forEach(el=>{el.checked=values.includes(el.value);});}
  function account(){if(!$('account-dialog').open)$('account-dialog').showModal();if(!auth.state().signedIn)$('email').focus();}
  function requireAccount(){if(auth.state().signedIn)return true;account();message('auth-status','Sign in with your registered email to continue.');return false;}
  function renderAuth(){
    const state=auth.state(),identity=state.user?.id||null,changed=identity!==lastIdentity;
    $('account-button').textContent=state.signedIn?'Account':'Sign in';
    $('email-form').hidden=state.signedIn;$('code-form').hidden=state.signedIn||!$('code-form').dataset.requested;
    $('account-details').hidden=!state.signedIn;$('account-email').textContent=state.user?.email||'';
    $('send-code').disabled=busy||!state.configured||state.resendSeconds>0;
    $('send-code').textContent=state.resendSeconds?'Request again in '+state.resendSeconds+'s':'Send sign-in code';
    document.querySelectorAll('.signed-out-note').forEach(el=>{el.hidden=state.signedIn;});
    if(changed){
      lastIdentity=identity;privateVersion++;spaces=[];requests=[];requestOffset=0;requestsHasMore=false;renderMine();renderRequests();renderPublic();
      if(!identity){
        ['editor-dialog','request-dialog','delete-dialog'].forEach(id=>{if($(id).open)$(id).close();});
        $('listing-form').reset();$('request-form').reset();editing=requesting=deleting=null;
        ['listing-preview','listing-preview-readable','request-space','delete-name','editor-status','request-status','delete-status'].forEach(id=>{$(id).textContent='';});
        message('page-status','Your private account view is signed out. Public summaries remain available.');
      }else refreshPrivate();
    }
  }
  async function action(fn,status='page-status'){
    if(busy)return;busy=true;renderAuth();document.body.setAttribute('aria-busy','true');
    try{await fn();}catch(e){message(status,e.message||'The request could not be completed.',true);}
    finally{busy=false;document.body.removeAttribute('aria-busy');renderAuth();}
  }
  function showSection(name){
    section=name;['browse','mine','requests'].forEach(id=>{$('section-'+id).hidden=id!==name;});
    document.querySelectorAll('[data-section]').forEach(el=>{if(el.dataset.section===name)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
    if(name!=='browse'&&auth.state().signedIn)refreshPrivate();
  }
  function baseCard(row){
    const card=create('article',undefined,'space-card'+(row.published?'':' private'));
    card.append(create('span',row.published?'Public · owner reported':'Private draft','small-label'),create('h3',row.title||'Untitled draft'));
    if(row.description)card.append(create('p',row.description,'space-description'));
    if(row.region&&REGIONS[row.region])card.append(create('p',REGIONS[row.region],'space-region'));
    const badges=create('div',undefined,'space-badges');
    (Array.isArray(row.device_types)?row.device_types:[]).forEach(x=>{if(DEVICES[x])badges.append(create('span',DEVICES[x],'space-badge'));});
    (Array.isArray(row.api_types)?row.api_types:[]).forEach(x=>{if(APIS[x])badges.append(create('span',APIS[x],'space-badge'));});
    if(badges.childNodes.length)card.append(badges);
    const metrics=create('ul',undefined,'space-metrics');
    [['device_count','devices'],['member_count','memberships'],['cpu_threads','CPU threads'],['memory_gb','GB memory'],['gpu_count','GPUs']].forEach(([key,label])=>{
      if(row[key]!==null&&row[key]!==undefined&&Number.isFinite(Number(row[key])))metrics.append(create('li',Number(row[key]).toLocaleString()+' '+label));
    });
    if(metrics.childNodes.length)card.append(metrics);
    card.append(create('p','Summary updated '+date(row.updated_at)+'. Reachability is not verified.','space-updated'));
    return card;
  }
  function renderPublic(){
    const target=$('public-list');target.replaceChildren();
    if(!publicRows.length){empty(target,'No published spaces to show','Owners choose whether to publish a summary. Private installations are not listed. Try changing your search or create your own space.');return;}
    publicRows.forEach(row=>{const card=baseCard(row),actions=create('div',undefined,'space-actions');
      if(spaces.some(s=>s.id===row.space_id))actions.append(button('Your space',()=>showSection('mine')));
      else actions.append(button('Request access',()=>openRequest(row),'button button-outline compact'));
      card.append(actions);target.append(card);
    });
  }
  async function refreshPublic(){
    if(document.hidden)return;
    if(!navigator.onLine){$('refresh-status').textContent='Offline · displayed summaries may be outdated.';return;}
    const version=++publicVersion;
    const results=await Promise.allSettled([directory.publicListings({search:$('search').value,region:$('region-filter').value,offset}),directory.publicStats()]);
    if(version!==publicVersion)return;
    if(results[0].status==='fulfilled'){
      publicRows=results[0].value.listings;hasMore=results[0].value.hasMore;lastRefresh=new Date();renderPublic();
      $('previous-page').disabled=offset===0;$('next-page').disabled=!hasMore;$('page-number').textContent='Page '+(offset/50+1);
      $('refresh-status').textContent='Refreshed '+lastRefresh.toLocaleTimeString()+'. Updates every 30 seconds while visible.';
    }else{
      $('refresh-status').textContent=lastRefresh?'Could not refresh · showing older summaries from '+lastRefresh.toLocaleTimeString()+'.':'Directory unavailable. Use Refresh to try again.';
      if(!lastRefresh)empty($('public-list'),'Directory unavailable','Published summaries could not be loaded. Your device has not been connected or scanned.');
    }
    if(results[1].status==='fulfilled'){
      const stats=results[1].value;
      [['stat-spaces','public_spaces'],['stat-devices','reported_devices'],['stat-members','reported_members']].forEach(([id,key])=>{$(id).textContent=Number.isFinite(Number(stats[key]))?Number(stats[key]).toLocaleString():'—';});
    }else ['stat-spaces','stat-devices','stat-members'].forEach(id=>{$(id).textContent='—';});
  }
  async function refreshPrivate(){
    if(document.hidden||!navigator.onLine||!auth.state().signedIn)return;
    const version=++privateVersion,identity=auth.state().user.id;
    const results=await Promise.allSettled([directory.mySpaces(),directory.requests({offset:requestOffset})]);
    if(version!==privateVersion||auth.state().user?.id!==identity)return;
    if(results[0].status==='fulfilled'){spaces=results[0].value;renderMine();renderPublic();}
    if(results[1].status==='fulfilled'){requests=results[1].value.requests;requestsHasMore=results[1].value.hasMore;renderRequests();}
    if(results.some(r=>r.status==='rejected'))message('page-status','Could not refresh your account records. Check your connection and sign in again if the session expired.',true);
  }
  function renderMine(){
    const target=$('my-list');target.replaceChildren();if(!auth.state().signedIn)return;
    if(!spaces.length){empty(target,'Your spaces are private until you publish','Add a summary of a space you control. Choosing a name does not discover devices or activate remote access.');return;}
    spaces.forEach(space=>{
      const row=space.listing||{title:'Untitled draft',published:false,updated_at:space.created_at},card=baseCard(row),actions=create('div',undefined,'space-actions');
      actions.append(button('Edit summary',()=>openEditor(space)));
      if(row.published)actions.append(button('Unpublish',()=>action(async()=>{await directory.unpublish(space.id);message('page-status','The listing is now private. Previously saved copies cannot be recalled.');await Promise.all([refreshPublic(),refreshPrivate()]);})));
      actions.append(button('Delete',()=>{deleting=space;$('delete-name').textContent=row.title;message('delete-status','');$('delete-dialog').showModal();}));
      card.append(actions);target.append(card);
    });
  }
  function renderRequests(){
    const target=$('request-list');target.replaceChildren();$('requests-pagination').hidden=!auth.state().signedIn;
    $('requests-previous').disabled=requestOffset===0;$('requests-next').disabled=!requestsHasMore;$('requests-page').textContent='Page '+(requestOffset/50+1);
    if(!auth.state().signedIn)return;
    if(!requests.length){empty(target,'No access requests','Requests you send and requests to your spaces will appear here. Your private devices do not become publicly accessible.');return;}
    requests.forEach(row=>{
      const owned=spaces.find(space=>space.id===row.space_id),incoming=row.requester_id!==auth.state().user?.id,card=create('article',undefined,'request-card');
      const title=owned?.listing?.title||publicRows.find(space=>space.space_id===row.space_id)?.title||'Space '+String(row.space_id).slice(0,8);
      card.append(create('span',incoming?'Request to your space':'Request you sent','small-label'),create('h3',title),create('p','From '+row.display_name));
      const tags=create('div',undefined,'space-badges');(row.capabilities||[]).forEach(c=>{if(CAPABILITIES[c])tags.append(create('span',CAPABILITIES[c],'space-badge'));});card.append(tags);
      if(row.message)card.append(create('p',row.message));
      card.append(create('span',STATUS[row.status]||'Unknown request status','request-state'),create('p','Updated '+date(row.updated_at),'small-print'));
      const actions=create('div',undefined,'space-actions');
      const transition=(status)=>action(async()=>{await directory.setRequestStatus(row.id,status);message('page-status',status==='accepted_pending_device'?'Request accepted for local review. No device permissions have been activated.':'Request updated. Local permissions, if any, must be changed on the owner’s server.');await refreshPrivate();});
      if(incoming){
        if(row.status==='pending')actions.append(button('Accept for local review',()=>transition('accepted_pending_device')));
        if(['pending','accepted_pending_device'].includes(row.status))actions.append(button('Decline',()=>transition('declined')));
        if(row.status==='accepted_pending_device'&&owned)actions.append(button('Download for local review',()=>exportRequest(row)));
      }else if(['pending','accepted_pending_device'].includes(row.status))actions.append(button('Cancel request',()=>transition('cancelled')));
      card.append(actions);target.append(card);
    });
  }
  function exportRequest(row){
    if(row.status!=='accepted_pending_device'||!spaces.some(s=>s.id===row.space_id))return;
    const payload={version:1,request_id:row.id,space_id:row.space_id,requester_id:row.requester_id,display_name:row.display_name,requested_capabilities:row.capabilities,status:'accepted_pending_device'};
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)+'\n'],{type:'application/json'}));
    const link=create('a');link.href=url;link.download='mainaiger-access-request.json';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    message('page-status','Downloaded a request for local owner review. The file contains no access credential and grants no permission.');
  }
  function editorInput(){return {title:$('space-title').value,description:$('space-description').value,region:$('space-region').value,device_types:checked('device-type'),device_count:$('device-count').value,member_count:$('member-count').value,cpu_threads:$('cpu-threads').value,memory_gb:$('memory-gb').value,gpu_count:$('gpu-count').value,api_types:checked('api-type')};}
  function editorConsent(){return {region:$('share-region').checked,devices:$('share-devices').checked,members:$('share-members').checked,compute:$('share-compute').checked,apis:$('share-apis').checked,publish:$('publish-consent').checked};}
  function preview(){
    const consent=editorConsent();document.querySelectorAll('[data-share-fields]').forEach(el=>{el.hidden=!consent[el.dataset.shareFields];el.querySelectorAll('input,select').forEach(input=>{input.disabled=el.hidden;});});
    try{
      const fields=listingPayload(editorInput(),{...consent,publish:true},true),list=create('dl',undefined,'summary-fields');
      const count=(value,suffix='')=>value===null?'Not shared':Number(value).toLocaleString()+suffix;
      [['Space name',fields.title],['Description',fields.description||'No description'],['Region',REGIONS[fields.region]||'Not shared'],['Device categories',fields.device_types.map(x=>DEVICES[x]).join(', ')||'Not shared'],['Advertised devices',count(fields.device_count)],['Memberships',count(fields.member_count)],['CPU threads',count(fields.cpu_threads)],['Memory',count(fields.memory_gb,' GB')],['GPUs',count(fields.gpu_count)],['API categories',fields.api_types.map(x=>APIS[x]).join(', ')||'Not shared']].forEach(([label,value])=>{const row=create('div');row.append(create('dt',label),create('dd',value));list.append(row);});
      $('listing-preview-readable').replaceChildren(list);$('listing-preview').textContent=JSON.stringify(fields,null,2);$('publish-button').disabled=!consent.publish;
    }catch(e){$('listing-preview-readable').replaceChildren(create('p',e.message,'small-print'));$('listing-preview').textContent=e.message;$('publish-button').disabled=true;}
  }
  function openEditor(space=null){
    if(!requireAccount())return;editing=space;$('listing-form').reset();
    const row=space?.listing;$('editor-title').textContent=space?'Edit your listing':'Create a listing';
    if(row){
      [['space-title','title'],['space-description','description'],['space-region','region'],['device-count','device_count'],['member-count','member_count'],['cpu-threads','cpu_threads'],['memory-gb','memory_gb'],['gpu-count','gpu_count']].forEach(([id,key])=>{$(id).value=row[key]??'';});
      setChecked('device-type',row.device_types);setChecked('api-type',row.api_types);
      $('share-region').checked=!!row.region;$('share-devices').checked=!!row.device_types?.length||row.device_count!==null;
      $('share-members').checked=row.member_count!==null;$('share-compute').checked=['cpu_threads','memory_gb','gpu_count'].some(k=>row[k]!==null);
      $('share-apis').checked=!!row.api_types?.length;
    }
    $('listing-form').querySelector('[value=private]').textContent=row?.published?'Save privately & unpublish':'Save privately';
    message('editor-status','');preview();$('editor-dialog').showModal();$('space-title').focus();
  }
  function openRequest(row){if(!requireAccount())return;requesting=row;$('request-form').reset();$('request-space').textContent='To the owner of '+row.title;message('request-status','');$('request-dialog').showModal();$('request-name').focus();}
  options('region-filter',REGIONS);options('space-region',REGIONS);checks('device-options',DEVICES,'device-type');checks('api-options',APIS,'api-type');checks('capability-options',CAPABILITIES,'capability');
  document.querySelectorAll('[data-section]').forEach(el=>el.addEventListener('click',()=>showSection(el.dataset.section)));
  document.querySelectorAll('[data-close]').forEach(el=>el.addEventListener('click',()=>$(el.dataset.close).close()));
  $('account-button').addEventListener('click',account);$('new-space').addEventListener('click',()=>openEditor());
  $('filter-form').addEventListener('submit',event=>{event.preventDefault();offset=0;refreshPublic();});
  $('refresh').addEventListener('click',()=>{refreshPublic();if(auth.state().signedIn)refreshPrivate();});
  $('previous-page').addEventListener('click',()=>{if(offset){offset-=50;refreshPublic();}});
  $('next-page').addEventListener('click',()=>{if(hasMore){offset+=50;refreshPublic();}});
  $('requests-previous').addEventListener('click',()=>{if(requestOffset){requestOffset-=50;refreshPrivate();}});
  $('requests-next').addEventListener('click',()=>{if(requestsHasMore){requestOffset+=50;refreshPrivate();}});
  $('listing-form').addEventListener('input',event=>{if(event.target.id!=='publish-consent')$('publish-consent').checked=false;preview();});
  $('listing-form').addEventListener('submit',event=>{event.preventDefault();const published=event.submitter?.value==='public';action(async()=>{
    if(!auth.state().signedIn)throw new Error('Sign in again before saving.');
    await directory.saveListing(editing?.id,!!editing?.listing,editorInput(),editorConsent(),published);
    $('editor-dialog').close();message('page-status',published?'The reviewed summary is public. It does not grant access to your devices.':'The summary is saved privately in your MainAIger account.');await Promise.all([refreshPublic(),refreshPrivate()]);
  },'editor-status');});
  $('request-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{
    if(!requesting)throw new Error('Choose a space first.');await directory.requestAccess(requesting.space_id,{display_name:$('request-name').value,capabilities:checked('capability'),message:$('request-message').value});
    $('request-dialog').close();message('page-status','Request sent. The owner will review it; no machine access has been granted.');showSection('requests');await refreshPrivate();
  },'request-status');});
  $('confirm-delete').addEventListener('click',()=>action(async()=>{if(!deleting)return;await directory.deleteSpace(deleting.id);$('delete-dialog').close();deleting=null;message('page-status','Space and directory requests deleted. Manage any existing local permissions on your server.');await Promise.all([refreshPublic(),refreshPrivate()]);},'delete-status'));
  $('email-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{const result=await auth.requestCode($('email').value);if(result.stale)return;$('code-form').dataset.requested='true';message('auth-status',result.message);renderAuth();$('code').focus();},'auth-status');});
  $('code-form').addEventListener('submit',event=>{event.preventDefault();const code=$('code').value;$('code').value='';action(async()=>{const result=await auth.verifyCode(code);if(result.stale)return;$('email').value='';delete $('code-form').dataset.requested;message('auth-status','Signed in. Your session stays in this tab.');$('account-dialog').close();message('page-status','Signed in. You can manage listings and access requests.');},'auth-status');});
  $('sign-out').addEventListener('click',()=>action(async()=>{await auth.signOut();message('auth-status','Signed out.');},'auth-status'));
  $('account-dialog').addEventListener('close',()=>{$('code').value='';$('email').value='';delete $('code-form').dataset.requested;if(!auth.state().signedIn)auth.cancelPending();renderAuth();});
  $('theme').addEventListener('change',()=>{document.documentElement.dataset.theme=$('theme').value;try{localStorage.setItem('mainaiger-theme',$('theme').value);}catch{}});
  try{const theme=localStorage.getItem('mainaiger-theme');if(['system','light','dark'].includes(theme)){$('theme').value=theme;document.documentElement.dataset.theme=theme;}}catch{}
  function refreshVisible(){if(!document.hidden){renderAuth();refreshPublic();if(auth.state().signedIn)refreshPrivate();}}
  function startTimers(){if(!polling)polling=setInterval(refreshVisible,30000);if(!authTimer)authTimer=setInterval(renderAuth,1000);}
  document.addEventListener('visibilitychange',refreshVisible);window.addEventListener('online',refreshVisible);window.addEventListener('offline',()=>{$('refresh-status').textContent='Offline · displayed summaries may be outdated.';});
  window.addEventListener('pagehide',()=>{clearInterval(polling);clearInterval(authTimer);polling=authTimer=null;publicVersion++;privateVersion++;auth.cancelPending();});
  window.addEventListener('pageshow',()=>{startTimers();refreshVisible();});
  renderAuth();message('auth-status','Use the email address registered for MainAIger.');startTimers();refreshPublic();
})();
