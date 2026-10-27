'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto').webcrypto;
const {MainaigerAuth}=require('../site/auth-client.js');
const base={enabled:true,googleEnabled:true,url:'https://example.supabase.co',publishableKey:'sb_publishable_test'};
const user={id:'google-user',email:'owner@example.com',email_confirmed_at:'2026-01-01'};
const reply=(v,status=200)=>({ok:status<400,status,json:async()=>v});
const store=()=>{const map=new Map();return {getItem:k=>map.get(k)||null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};};
const fake=(url)=>{let navigated='',cleaned='';return {location:{href:url,assign:x=>{navigated=x;}},history:{replaceState:(a,b,x)=>{cleaned=x;}},nav:()=>navigated,clean:()=>cleaned};};
test('Google button fails closed when provider disabled or on unsafe page',async()=>{
 const a=new MainaigerAuth({...base,googleEnabled:false},{storage:store(),crypto,...fake('https://mainaiger.org/'),fetch:async()=>reply({})});await a.ready;
 assert.equal(a.googleAvailable,false);await assert.rejects(a.startGoogle(),{code:'provider'});
 const b=new MainaigerAuth(base,{storage:store(),crypto,...fake('https://example.com/unexpected'),fetch:async()=>reply({})});await b.ready;
 await assert.rejects(b.startGoogle(),{code:'redirect'});
});
test('Google flow creates S256 challenge, exchanges once, verifies user and cleans callback URL',async()=>{
 const s=store(),start=fake('https://mainaiger.org/directory/?view=mine'),a=new MainaigerAuth(base,{storage:s,crypto,...start,fetch:async()=>reply({})});await a.ready;await a.startGoogle();
 const u=new URL(start.nav()),stored=JSON.parse(s.getItem('mainaiger-google-pkce:'+base.url));
 assert.equal(u.origin,base.url);assert.equal(u.pathname,'/auth/v1/authorize');assert.equal(u.searchParams.get('provider'),'google');assert.equal(u.searchParams.get('redirect_to'),'https://mainaiger.org/directory/');
 const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(stored.verifier));
 assert.equal(u.searchParams.get('code_challenge'),Buffer.from(digest).toString('base64url'));assert.equal(u.searchParams.get('code_challenge_method'),'s256');
 const calls=[],callback=fake('https://mainaiger.org/directory/?code=one-time-code');
 const b=new MainaigerAuth(base,{storage:s,crypto,...callback,fetch:async(url,opt)=>{calls.push({url,opt});return reply(url.includes('/token?')?{access_token:'google-access',expires_in:600}:user);}});
 assert.equal(await b.completeGoogle(),true);await b.ready;
 assert.equal(callback.clean(),'/directory/');assert.equal(b.state().user.id,'google-user');
 assert.deepEqual(JSON.parse(calls[0].opt.body),{auth_code:'one-time-code',code_verifier:stored.verifier});assert.equal(calls[1].opt.headers.Authorization,'Bearer google-access');
 assert.equal(s.getItem('mainaiger-google-pkce:'+base.url),null);
 await assert.rejects(b.completeGoogle(),{code:'provider'});
});
test('missing or expired verifier cannot exchange and signout removes pending verifier',async()=>{
 const s=store(),cb=fake('https://mainaiger.org/?code=untrusted');let calls=0;
 const a=new MainaigerAuth(base,{storage:s,crypto,...cb,fetch:async()=>{calls++;return reply(user);}});await a.ready;await assert.rejects(a.completeGoogle(),{code:'provider'});assert.equal(calls,0);assert.equal(cb.clean(),'/');
 s.setItem('mainaiger-google-pkce:'+base.url,JSON.stringify({verifier:'x'.repeat(43),createdAt:Date.now()-301000,path:'/'}));
 await assert.rejects(a.completeGoogle(),{code:'provider'});assert.equal(calls,0);
 s.setItem('mainaiger-google-pkce:'+base.url,'pending');await a.signOut();assert.equal(s.getItem('mainaiger-google-pkce:'+base.url),null);
});
test('old stored session cannot race a Google callback to overwrite new identity',async()=>{
 const s=store(),key='mainaiger-session:'+base.url;s.setItem(key,JSON.stringify({accessToken:'old-token',expiresAt:Date.now()+600000}));
 s.setItem('mainaiger-google-pkce:'+base.url,JSON.stringify({verifier:'x'.repeat(43),createdAt:Date.now(),path:'/'}));
 const cb=fake('https://mainaiger.org/?code=new-code'),seen=[];
 const a=new MainaigerAuth(base,{storage:s,crypto,...cb,fetch:async(url,opt)=>{seen.push(opt.headers.Authorization);return reply(url.includes('/token?')?{access_token:'new-token',expires_in:600}:user);}});
 assert.equal(await a.completeGoogle(),true);await a.ready;assert.deepEqual(seen,[undefined,'Bearer new-token']);assert.equal(a.state().user.id,'google-user');
});
test('realistic callback cleanup and online restore cannot race pending exchange',async()=>{
 const s=store();s.setItem('mainaiger-session:'+base.url,JSON.stringify({accessToken:'old-token',expiresAt:Date.now()+600000}));
 s.setItem('mainaiger-google-pkce:'+base.url,JSON.stringify({verifier:'x'.repeat(43),createdAt:Date.now(),path:'/'}));
 const location={href:'https://mainaiger.org/?code=new-code',assign(){}},history={replaceState:(a,b,path)=>{location.href=new URL(path,location.href).href;}};
 let finish;const seen=[];
 const a=new MainaigerAuth(base,{storage:s,crypto,location,history,fetch:async(url,opt)=>{seen.push(opt.headers.Authorization);if(url.includes('/token?'))return new Promise(r=>{finish=r;});return reply(user);}});
 const pending=a.completeGoogle();await a.ready;assert.equal(location.href,'https://mainaiger.org/');assert.equal(await a.restoreSession(),false);
 assert.deepEqual(seen,[undefined]);finish(reply({access_token:'new-token',expires_in:600}));assert.equal(await pending,true);
 assert.deepEqual(seen,[undefined,'Bearer new-token']);assert.equal(a.state().user.id,'google-user');
});
