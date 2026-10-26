'use strict';
const assert=require('node:assert/strict');
const {MainaigerAuth}=require('../site/auth-client.js');
const config={enabled:true,url:'https://example.supabase.co',publishableKey:'sb_publishable_example',codeLength:8};
const response=(data={},ok=true,status=200)=>({ok,status,json:async()=>data});
const user={id:'user-one',email:'registered@example.com',email_confirmed_at:'2026-10-26T00:00:00Z'};
const session={access_token:'opaque-test-token',expires_in:600,user:{id:'untrusted'}};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
async function test(){
  let calls=[];
  const fetch=async(url,options)=>{calls.push({url,options});return response({});};
  for(const bad of [{...config,enabled:false},{...config,url:'http://example.supabase.co'},{...config,url:'https://example.supabase.co/path'},{...config,publishableKey:'sb_secret_forbidden'},{...config,publishableKey:'eyJlegacy'}]){
    const client=new MainaigerAuth(bad,{fetch});assert.equal(client.state().configured,false);await assert.rejects(client.requestCode(user.email),e=>e.code==='not_configured');
  }
  assert.equal(calls.length,0);
  const client=new MainaigerAuth(config,{fetch});
  const sent=await client.requestCode(user.email);
  assert.match(sent.message,/If this email is registered/);assert.deepEqual(JSON.parse(calls[0].options.body),{email:user.email,create_user:false});
  assert.equal(calls[0].options.credentials,'omit');assert.equal(calls[0].options.cache,'no-store');assert.equal(calls[0].options.redirect,'error');
  await assert.rejects(client.requestCode(user.email),e=>e.code==='cooldown');
  await assert.rejects(client.verifyCode('123456'),e=>e.code==='code');
  assert.equal(client.state().signedIn,false);
  let phase=0,clock=100000;
  const held=deferred();
  const verified=new MainaigerAuth(config,{now:()=>clock,fetch:async(url,options)=>{
    phase++;
    if(url.endsWith('/otp'))return response({});
    if(url.endsWith('/verify')){assert.equal(JSON.parse(options.body).type,'email');return response(session);}
    if(url.endsWith('/user')){assert.equal(options.headers.Authorization,'Bearer '+session.access_token);return held.promise;}
    return response({});
  }});
  await verified.requestCode(user.email);const verify=verified.verifyCode('12345678');
  await new Promise(r=>setImmediate(r));assert.equal(phase,3);assert.equal(verified.state().signedIn,false,'Session not trusted before /user');
  held.resolve(response(user));await verify;assert.equal(verified.state().signedIn,true);assert.deepEqual(verified.state().user,{id:user.id,email:user.email});
  assert(!JSON.stringify(verified.state()).includes('opaque-test-token'));
  clock+=600001;assert.equal(verified.state().signedIn,false,'Expired session cleared');assert.equal(verified.state().user,null);
  let receiptClock=1000000;const delayedUser=deferred();
  const delayed=new MainaigerAuth(config,{now:()=>receiptClock,fetch:async url=>url.endsWith('/user')?delayedUser.promise:response(url.endsWith('/verify')?session:{})});
  await delayed.requestCode(user.email);const delayedVerify=delayed.verifyCode('12345678');await new Promise(r=>setImmediate(r));
  receiptClock+=600001;delayedUser.resolve(response(user));await assert.rejects(delayedVerify,e=>e.code==='session');assert.equal(delayed.state().signedIn,false,'Expired during identity verification must not resurrect');
  receiptClock=2000000;const shortenedUser=deferred();
  const shortened=new MainaigerAuth(config,{now:()=>receiptClock,fetch:async url=>url.endsWith('/user')?shortenedUser.promise:response(url.endsWith('/verify')?{...session,expires_at:2060}:{})});
  await shortened.requestCode(user.email);const shortVerify=shortened.verifyCode('12345678');await new Promise(r=>setImmediate(r));
  receiptClock+=30000;shortenedUser.resolve(response(user));await shortVerify;assert(shortened.state().signedIn);
  receiptClock=2060000;assert.equal(shortened.state().signedIn,false,'Absolute expiry bounds relative expiry without extending for /user delay');
  const invalid=new MainaigerAuth(config,{fetch:async url=>url.endsWith('/verify')?response({message:'Invalid'},false,403):response({})});
  await invalid.requestCode(user.email);await assert.rejects(invalid.verifyCode('12345678'));assert.equal(invalid.state().signedIn,false);
  const mismatch=new MainaigerAuth(config,{fetch:async url=>response(url.endsWith('/verify')?session:url.endsWith('/user')?{...user,email:'other@example.com'}:{})});
  await mismatch.requestCode(user.email);await assert.rejects(mismatch.verifyCode('12345678'));assert.equal(mismatch.state().signedIn,false);
  const anonymous=new MainaigerAuth(config,{fetch:async url=>response(url.endsWith('/verify')?session:url.endsWith('/user')?{...user,is_anonymous:true}:{})});
  await anonymous.requestCode(user.email);await assert.rejects(anonymous.verifyCode('12345678'));assert.equal(anonymous.state().signedIn,false);
  const pending=deferred();
  const race=new MainaigerAuth(config,{fetch:async url=>url.endsWith('/user')?pending.promise:response(url.endsWith('/verify')?session:{})});
  await race.requestCode(user.email);const inFlight=race.verifyCode('12345678');await new Promise(r=>setImmediate(r));await race.signOut();pending.resolve(response(user));assert.deepEqual(await inFlight,{stale:true});assert.equal(race.state().signedIn,false,'Late user response cannot restore logout');
  let logoutCalled=false;
  const logout=new MainaigerAuth(config,{fetch:async(url,opts)=>{
    if(url.includes('/logout?scope=local')){logoutCalled=true;assert.equal(opts.headers.Authorization,'Bearer '+session.access_token);return response({},false,500);}
    return response(url.endsWith('/verify')?session:url.endsWith('/user')?user:{});
  }});
  await logout.requestCode(user.email);await logout.verifyCode('12345678');assert(logout.state().signedIn);await assert.rejects(logout.signOut(),e=>e.code==='logout');assert(logoutCalled);assert.equal(logout.state().signedIn,false,'Local signout succeeds despite network error');
  const otpRace=deferred();const cancel=new MainaigerAuth(config,{fetch:()=>otpRace.promise});const requesting=cancel.requestCode(user.email);cancel.cancelPending();otpRace.resolve(response({}));assert.deepEqual(await requesting,{stale:true});
  const unknown=new MainaigerAuth(config,{fetch:async()=>response({},false,400)});assert.equal((await unknown.requestCode('unknown@example.com')).message,sent.message,'Registered and unregistered requests share UI message');
  console.log('PASS: disabled/invalid config, invitation-only payload, safe fetch, 8-digit input, verified identity, expiry, errors, anonymous rejection, logout and request races.');
}
test().catch(e=>{console.error(e);process.exitCode=1;});
