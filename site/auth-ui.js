(() => {
  'use strict';
  const $=id=>document.getElementById(id),emailForm=$('auth-email-form'),codeForm=$('auth-code-form'),status=$('auth-status'),account=$('auth-account');
  let busy=false,timer,lastSignedIn=false;
  const client=new MainaigerAuth(window.MAINAIGER_AUTH_CONFIG,{onChange:render});
  function message(text){status.textContent=text;}
  function render(){
    const state=client.state();
    if(lastSignedIn&&!state.signedIn&&!busy){message('Session expired. Sign in again.');document.dispatchEvent(new Event('mainaiger:session-ended'));}
    lastSignedIn=state.signedIn;
    emailForm.hidden=state.signedIn;codeForm.hidden=state.signedIn||!codeForm.dataset.requested;
    account.hidden=!state.signedIn;$('auth-account-email').textContent=state.user?.email||'';
    $('auth-email').disabled=busy||!state.configured;$('auth-send').disabled=busy||!state.configured||state.resendSeconds>0;
    $('auth-send').textContent=state.resendSeconds?'Request another code in '+state.resendSeconds+'s':'Send sign-in code';
    $('auth-verify').disabled=busy||!state.configured;$('auth-code').disabled=busy||!state.configured;
    $('auth-signout').disabled=busy;
  }
  async function run(fn){busy=true;render();try{await fn();}catch(e){message(e.message||'Sign-in could not be completed.');}finally{busy=false;render();}}
  emailForm.addEventListener('submit',e=>{e.preventDefault();run(async()=>{
    const result=await client.requestCode($('auth-email').value);
    if(result.stale)return;codeForm.dataset.requested='true';message(result.message);render();$('auth-code').focus();
  });});
  codeForm.addEventListener('submit',e=>{e.preventDefault();const code=$('auth-code').value;$('auth-code').value='';run(async()=>{
    const result=await client.verifyCode(code);if(result.stale)return;
    $('auth-email').value='';delete codeForm.dataset.requested;message('Signed in. Device pairing and the macOS installer are not available yet.');
    document.dispatchEvent(new Event('mainaiger:signin'));
  });});
  $('auth-signout').addEventListener('click',()=>run(async()=>{await client.signOut();message('Signed out.');}));
  $('entry-dialog').addEventListener('close',()=>{
    $('auth-code').value='';$('auth-email').value='';delete codeForm.dataset.requested;client.cancelPending();render();
  });
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)render();});
  timer=setInterval(render,1000);
  window.addEventListener('pagehide',()=>{clearInterval(timer);timer=null;client.cancelPending();});
  window.addEventListener('pageshow',()=>{if(!timer)timer=setInterval(render,1000);render();});
  if(!client.enabled)message('Email sign-in is being configured. No email is sent and no account is created.');
  else message('Invite-only access. Enter the email address registered for MainAIger. This tab does not save your session.');
  render();
})();
