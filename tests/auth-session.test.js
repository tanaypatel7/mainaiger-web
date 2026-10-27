'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {MainaigerAuth}=require('../site/auth-client.js');
const cfg={enabled:true,url:'https://example.supabase.co',publishableKey:'sb_publishable_test'};
const user={id:'123',email:'owner@example.com',email_confirmed_at:'2026-01-01'};
const reply=(v,status=200)=>({ok:status<400,status,json:async()=>v});
const store=()=>{const m=new Map();return {getItem:k=>m.get(k)||null,setItem:(k,v)=>m.set(k,v),removeItem:k=>m.delete(k),values:()=>[...m.values()]};};
function fetcher(url){return Promise.resolve(reply(url.endsWith('/verify')?{access_token:'private-test-token',expires_in:600}:url.endsWith('/user')?user:{}));}
test('session is verified and restored across navigation in same tab',async()=>{
 const s=store(),a=new MainaigerAuth(cfg,{storage:s,fetch:fetcher});await a.ready;
 await a.requestCode(user.email);await a.verifyCode('12345678');assert.equal(s.values().length,1);
 let called=false;const b=new MainaigerAuth(cfg,{storage:s,fetch:async(u,o)=>{called=true;assert.equal(o.headers.Authorization,'Bearer private-test-token');return reply(user);}});
 assert.equal(b.state().signedIn,false);await b.ready;assert(called);assert.equal(b.state().signedIn,true);assert.equal(b.state().user.email,user.email);
 await b.signOut();assert.equal(s.values().length,0);
});
test('stored identity is never trusted and rejected or expired tokens are cleared',async()=>{
 const s=store(),key='mainaiger-session:https://example.supabase.co';
 s.setItem(key,JSON.stringify({accessToken:'fake',expiresAt:Date.now()+600000,user:{id:'forged'}}));
 const a=new MainaigerAuth(cfg,{storage:s,fetch:async()=>reply({},401)});await a.ready;assert.equal(a.state().signedIn,false);assert.equal(s.values().length,0);
 s.setItem(key,JSON.stringify({accessToken:'fake',expiresAt:Date.now()-1}));
 const b=new MainaigerAuth(cfg,{storage:s,fetch:async()=>{throw Error('should not call');}});await b.ready;assert.equal(b.state().signedIn,false);assert.equal(s.values().length,0);
});
test('signout while restoration is pending cannot resurrect a session',async()=>{
 const s=store(),key='mainaiger-session:https://example.supabase.co';s.setItem(key,JSON.stringify({accessToken:'test',expiresAt:Date.now()+600000}));
 let resolve;const a=new MainaigerAuth(cfg,{storage:s,fetch:()=>new Promise(r=>{resolve=r;})});
 await new Promise(r=>setImmediate(r));await a.signOut();resolve(reply(user));await a.ready;
 assert.equal(a.state().signedIn,false);assert.equal(s.values().length,0);
});
test('transient network failure retains stored session for a later retry',async()=>{
 const s=store(),key='mainaiger-session:https://example.supabase.co';s.setItem(key,JSON.stringify({accessToken:'test',expiresAt:Date.now()+600000}));
 let offline=true;const a=new MainaigerAuth(cfg,{storage:s,fetch:async()=>{if(offline)throw Error('offline');return reply(user);}});
 await a.ready;assert.equal(a.state().signedIn,false);assert.equal(s.values().length,1);
 offline=false;assert.equal(await a.restoreSession(),true);assert.equal(a.state().signedIn,true);
});
