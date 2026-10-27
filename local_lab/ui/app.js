(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  let csrf='',generation=0,reviewed='',revision=null,reviewedRevision=null,running=false,configuring=false,dockerAvailable=false,notebook=null;
  const status=(id,text)=>{$(id).textContent=text;};
  function locked(){generation++;csrf='';reviewed='';notebook=null;revision=reviewedRevision=null;$('owner-key').value='';$('code').value='';$('notebook-file').value='';$('folder').value='';$('workspace').textContent='';$('command-preview').textContent='';$('output').textContent='Your output will appear here.';$('lab').hidden=true;$('logout').hidden=true;$('graph-link').hidden=true;$('login-panel').hidden=false;if($('review-dialog').open)$('review-dialog').close();window.dispatchEvent(new CustomEvent('mainaiger:locked'));}
  async function request(path,body){
    const options={credentials:'same-origin',cache:'no-store',redirect:'error',headers:{'Content-Type':'application/json',...(csrf?{'X-CSRF-Token':csrf}:{})}};
    if(body!==undefined){options.method='POST';options.body=JSON.stringify(body);}
    let response;
    try{response=await fetch(path,options);}catch{throw new Error('Unable to reach your local Lab.');}
    let result={};try{result=await response.json();}catch{}
    if(!response.ok){if(response.status===401)locked();throw new Error(typeof result.error==='string'?result.error:'The local request could not be completed.');}
    return result;
  }
  async function showState(){
    const version=generation,state=await request('/api/state');if(version!==generation)return;
    if(!Number.isSafeInteger(state.revision)||state.revision<0)throw new Error('Local settings could not be verified.');
    csrf=state.csrf;revision=state.revision;$('workspace').textContent=state.workspace||'No folder selected';$('cpu').textContent=String(state.limits.cpus)+' CPU cores';$('memory').textContent=String(state.limits.memory);
    $('folder').value=state.workspace||'';$('cpus').value=String(state.limits.cpus);$('memory-select').value=String(state.limits.memory);$('command-preview').textContent=Array.isArray(state.command)?state.command.map(x=>JSON.stringify(x)).join(' '):'Unavailable';
    dockerAvailable=!!state.docker_available;status('docker-status',dockerAvailable?'Docker command found. Starting a run checks Docker Desktop and the Python image.':'Docker command not found. Install Docker Desktop and restart the local Lab.');
    $('review').disabled=!dockerAvailable||running||configuring;$('review-hint').hidden=dockerAvailable;$('login-panel').hidden=true;$('lab').hidden=false;$('logout').hidden=false;$('graph-link').hidden=false;
    window.dispatchEvent(new CustomEvent('mainaiger:state',{detail:state}));
  }
  const inSession=()=>!!(window.MainaigerSession&&window.MainaigerSession.alive());
  function describe(result){
    const parts=[result.status==='ok'?'Finished':result.status==='error'?'Error':result.status==='interrupted'?'Interrupted':result.status==='timeout'?'Stopped at the time limit':'Session terminated','cell '+result.count,(result.elapsed_ms||0)+' ms'];
    if(result.error&&result.error.ename)parts.push(result.error.ename+(result.error.evalue?': '+result.error.evalue:''));
    if(result.truncated)parts.push('output truncated');
    if(result.note)parts.push(result.note);
    return parts.join(' · ');
  }
  $('login-form').addEventListener('submit',async event=>{
    event.preventDefault();const key=$('owner-key').value;$('owner-key').value='';status('login-status','Unlocking…');
    try{const result=await request('/api/login',{key});csrf=result.csrf;await showState();status('login-status','');}catch(error){status('login-status',error.message);}
  });
  $('logout').addEventListener('click',async()=>{try{await request('/api/logout',{});}catch{}finally{locked();status('login-status','Lab locked on this device.');}});
  $('notebook-file').addEventListener('change',async()=>{
    const file=$('notebook-file').files[0],version=generation;if(!file)return;
    try{if(file.size>1048576)throw Error('Choose a notebook smaller than 1 MB.');const data=MainaigerNotebook.loadNotebook(await file.text());if(version!==generation)return;notebook=data.notebook;$('code').value=data.source;status('notebook-status','Notebook loaded locally. Saving downloads a copy with previous outputs cleared.');}
    catch(e){if(version===generation)status('notebook-status',e.message||'Notebook could not be opened.');}
    finally{$('notebook-file').value='';}
  });
  $('save-notebook').addEventListener('click',()=>{
    try{const content=MainaigerNotebook.saveNotebook(notebook,$('code').value),url=URL.createObjectURL(new Blob([content],{type:'application/x-ipynb+json'})),link=document.createElement('a');link.href=url;link.download='mainaiger-notebook.ipynb';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);status('notebook-status','Notebook copy downloaded. Previous outputs were cleared.');}
    catch(e){status('notebook-status',e.message||'Notebook could not be saved.');}
  });
  $('settings-form').addEventListener('submit',async event=>{event.preventDefault();if(running||configuring)return;const version=generation;configuring=true;$('save-settings').disabled=true;$('review').disabled=true;status('settings-status','Saving…');try{await request('/api/settings',{workspace:$('folder').value,cpus:$('cpus').value,memory:$('memory-select').value});if(version!==generation)return;await showState();status('settings-status','Saved for subsequent runs.');}catch(error){if(version===generation)status('settings-status',error.message);}finally{configuring=false;$('save-settings').disabled=false;if(version===generation)$('review').disabled=!dockerAvailable||running;}});
  $('review').addEventListener('click',()=>{if(running||configuring||revision===null)return;reviewed=$('code').value;reviewedRevision=revision;$('review-code').textContent=reviewed;$('review-limits').textContent='Folder: '+$('workspace').textContent+' · CPU: '+$('cpu').textContent+' · Memory: '+$('memory').textContent;$('review-mode').textContent=inSession()?'The following Python code will run in your running session on this device. It can see variables from earlier cells and its results stay in memory. Network access is disabled and the folder cannot be changed by the container.':'The following Python code will run on this device in a fresh container. Network access is disabled and the folder cannot be changed by the container.';$('review-dialog').showModal();$('cancel').focus();});
  $('cancel').addEventListener('click',()=>{$('review-dialog').close();});
  $('review-dialog').addEventListener('close',()=>{reviewed='';reviewedRevision=null;$('review-code').textContent='';});
  $('confirm').addEventListener('click',async()=>{
    if(!reviewed||reviewedRevision===null||running||configuring)return;const code=reviewed,runRevision=reviewedRevision,version=generation;$('review-dialog').close();running=true;$('review').disabled=true;$('save-settings').disabled=true;status('run-status','Running in your local container…');$('output').textContent='';
    try{
      if(inSession()){
        window.dispatchEvent(new CustomEvent('mainaiger:cell-started'));status('run-status','Running in your session…');
        const result=await request('/api/kernel/execute',{code,confirm:true});if(version!==generation)return;
        const lines=[result.output||''];if(result.error&&Array.isArray(result.error.traceback)&&result.error.traceback.length)lines.push(result.error.traceback.join('\n'));
        $('output').textContent=lines.join('\n').trim()||'(No output)';status('run-status',describe(result));
        window.dispatchEvent(new CustomEvent('mainaiger:cell-finished',{detail:result.session}));
      }else{
        const result=await request('/api/run',{code,confirm:true,revision:runRevision});if(version!==generation)return;$('output').textContent=result.output||'(No output)';status('run-status',result.error||'Run finished with exit code '+String(result.exit_code)+'.');
      }
    }
    catch(error){if(version===generation){status('run-status',error.message);if(inSession())window.dispatchEvent(new CustomEvent('mainaiger:refresh-state'));}}
    finally{running=false;$('save-settings').disabled=false;if(version===generation)$('review').disabled=!dockerAvailable||configuring;}
  });
  window.addEventListener('pagehide',()=>{locked();});
  window.addEventListener('mainaiger:refresh-state',()=>{if(csrf)showState().catch(()=>{});});
  showState().catch(()=>{});
})();
