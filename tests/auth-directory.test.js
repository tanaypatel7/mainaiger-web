'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {MainaigerAuth}=require('../site/auth-client.js');
const config={enabled:true,url:'https://example.supabase.co',publishableKey:'sb_publishable_test'};
const user={id:'owner-id',email:'owner@example.com',email_confirmed_at:'2026-01-01'};
const reply=(data,status=200)=>({ok:status<400,status,json:async()=>data});
async function setup(handler){
  let now=1000;
  const calls=[];
  const auth=new MainaigerAuth(config,{now:()=>now,fetch:async(url,options)=>{
    calls.push({url,options});
    if(url.includes('/auth/v1/'))return reply(url.endsWith('/verify')?{access_token:'private-token',expires_in:60}:url.endsWith('/user')?user:{});
    return handler?handler(url,options):reply([{space_id:'example'}]);
  }});
  await auth.requestCode(user.email);await auth.verifyCode('12345678');
  return {auth,calls,expire:()=>{now+=61000;}};
}
test('data requests pin origin, hide tokens, and return mutation representation',async()=>{
  const {auth,calls}=await setup();
  auth.base='https://attacker.example';
  await auth.requestData('directory_spaces',{method:'POST',body:{owner_id:user.id}});
  const {url,options}=calls.at(-1);
  assert.equal(url,'https://example.supabase.co/rest/v1/directory_spaces');
  assert.equal(options.headers.Authorization,'Bearer private-token');
  assert.equal(options.headers.Prefer,'return=representation');
  assert.equal(options.credentials,'omit');assert.equal(options.cache,'no-store');assert.equal(options.redirect,'error');
  assert(!JSON.stringify(auth.state()).includes('private-token'));
  for(const path of ['https://attacker.example','//attacker.example','../auth/v1/user','directory_listings/../../x','directory_listings#x','rpc/arbitrary','directory_listings\\evil'])await assert.rejects(auth.requestData(path),{code:'path'});
});
test('public reads omit signed-in bearer and disallow private endpoints/mutations',async()=>{
  const {auth,calls}=await setup();
  await auth.requestData('directory_listings?published=eq.true',{publicAccess:true});
  assert.equal(calls.at(-1).options.headers.Authorization,undefined);
  for(const path of ['directory_spaces','directory_requests'])await assert.rejects(auth.requestData(path,{publicAccess:true}),{code:'permission'});
  await assert.rejects(auth.requestData('directory_listings',{method:'PATCH',body:{published:true},publicAccess:true}),{code:'permission'});
  await auth.signOut();
  await auth.requestData('rpc/directory_public_stats',{publicAccess:true});
  await assert.rejects(auth.requestData('directory_requests'),{code:'session'});
});
test('signout aborts private request and late responses cannot restore private data',async()=>{
  let release;let signal;
  const {auth}=await setup((url,options)=>{signal=options.signal;return new Promise(resolve=>{release=resolve;});});
  const pending=auth.requestData('directory_requests');
  const rejected=assert.rejects(pending,{code:'stale'});
  await auth.signOut();assert(signal.aborted);
  release(reply([{message:'private'}]));await rejected;
});
test('expiry during response processing discards data',async()=>{
  let release;
  const {auth,expire}=await setup(()=>new Promise(resolve=>{release=resolve;}));
  const pending=auth.requestData('directory_requests');
  const rejected=assert.rejects(pending,{code:'stale'});
  expire();release(reply([{message:'private'}]));await rejected;
  assert.equal(auth.state().signedIn,false);
});
test('API 401 clears identity; server errors cannot echo credentials',async()=>{
  const {auth}=await setup(()=>reply({message:'private-token should not appear'},401));
  await assert.rejects(auth.requestData('directory_requests'),e=>e.code==='session'&&!e.message.includes('private-token'));
  assert.equal(auth.state().signedIn,false);
});
test('closing a completed login dialog does not cancel authenticated data',async()=>{
  let release;
  const {auth}=await setup(()=>new Promise(resolve=>{release=resolve;}));
  const pending=auth.requestData('directory_spaces');
  auth.cancelPending();release(reply([{id:'own-space'}]));
  assert.deepEqual(await pending,[{id:'own-space'}]);
});
