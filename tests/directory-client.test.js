'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {DirectoryClient,listingPayload,requestPayload}=require('../site/directory/directory-client.js');
const SPACE='01234567-89ab-4cde-8fab-0123456789ab';
const USER='11111111-2222-4333-8444-555555555555';
const draft={title:'  A public lab  ',description:' Selected collaboration ',region:'north-america',device_types:['mac','server'],device_count:'2',member_count:3,cpu_threads:8,memory_gb:32,gpu_count:1,api_types:['ollama','mcp']};
const consent={region:true,devices:true,members:true,compute:true,apis:true,publish:true};
test('unchecked categories remove stored metadata rather than hiding it',()=>{
  const payload=listingPayload({...draft,ip:'100.1.2.3',api_key:'secret',files:['/private'],owner_id:USER,updated_at:'forged'},{},false);
  assert.deepEqual(payload,{title:'A public lab',description:'Selected collaboration',region:null,device_types:[],device_count:null,member_count:null,cpu_threads:null,memory_gb:null,gpu_count:null,api_types:[],published:false});
  assert(!JSON.stringify(payload).includes('secret'));
  assert(!JSON.stringify(payload).includes('/private'));
});
test('public publication requires deliberate consent and bounded allowlisted values',()=>{
  assert.throws(()=>listingPayload(draft,{...consent,publish:false},true),/confirm publication/);
  assert.throws(()=>listingPayload({...draft,region:'precise-location'},consent,true),/broad region/);
  assert.throws(()=>listingPayload({...draft,device_types:['__proto__']},consent,true),/valid/);
  assert.throws(()=>listingPayload({...draft,api_types:['https://private.example/api']},consent,true),/valid/);
  for(const device_count of [-1,10001,1.2,true,[],{}])assert.throws(()=>listingPayload({...draft,device_count},consent,true),/whole number/);
  assert.throws(()=>listingPayload({...draft,title:'x'.repeat(81)},consent,true),/characters/);
  const payload=listingPayload({...draft,api_types:['mcp','mcp'],cpu_threads:'0'},consent,true);
  assert.deepEqual(payload.api_types,['mcp']);assert.equal(payload.cpu_threads,0);assert.equal(payload.published,true);
});
test('request payload cannot spoof another account, status, local credential or arbitrary operation',()=>{
  const payload=requestPayload(SPACE,{display_name:'Collaborator',capabilities:['files-read'],message:'Hello',requester_id:USER,status:'approved',grant:'secret'});
  assert.deepEqual(payload,{space_id:SPACE,display_name:'Collaborator',capabilities:['files-read'],message:'Hello'});
  assert.throws(()=>requestPayload(SPACE,{display_name:'x',capabilities:[]}),/at least one/);
  assert.throws(()=>requestPayload(SPACE,{display_name:'x',capabilities:['shell.root']}),/valid/);
  assert.throws(()=>requestPayload(SPACE+'?select=*',{display_name:'x',capabilities:['files-read']}),/identifier/);
});
test('public discovery explicitly requests published rows anonymously and bounds pagination',async()=>{
  const calls=[];const auth={requestData:async(path,options)=>{calls.push({path,options});return Array.from({length:51},(_,i)=>({space_id:String(i)}));}};
  const client=new DirectoryClient(auth);
  const result=await client.publicListings({search:'hello&owner_id=eq.secret',region:'europe',offset:50});
  assert.equal(result.listings.length,50);assert.equal(result.hasMore,true);
  const query=new URL('https://example.test/'+calls[0].path).searchParams;
  assert.equal(query.get('published'),'eq.true');assert.equal(query.get('region'),'eq.europe');assert.equal(query.get('offset'),'50');assert.equal(query.get('limit'),'51');
  assert.equal(query.has('owner_id'),false);assert.equal(calls[0].options.publicAccess,true);
  await assert.rejects(client.publicListings({offset:-1}),/Invalid page/);
});
test('create persists only normalized metadata and cleans up an incomplete space on failure',async()=>{
  const calls=[];let shouldFail=false;
  const auth={state:()=>({signedIn:true,user:{id:USER}}),requestData:async(path,options)=>{calls.push({path,options});if(path==='directory_spaces')return [{id:SPACE}];if(path==='directory_listings'){if(shouldFail)throw new Error('Rejected');return [{space_id:SPACE,...options.body}];}return [{id:SPACE}];}};
  const client=new DirectoryClient(auth);
  await client.saveListing(null,false,{...draft,owner_id:SPACE},consent,true);
  assert.deepEqual(calls[0].options.body,{owner_id:USER});assert.equal(calls[1].options.body.space_id,SPACE);assert.equal(calls[1].options.body.published,true);
  shouldFail=true;calls.length=0;
  await assert.rejects(client.saveListing(null,false,draft,consent,false),/Rejected/);
  assert.equal(calls.length,3);assert.equal(calls[2].options.method,'DELETE');
  assert.equal(new URL('https://example.test/'+calls[2].path).searchParams.get('id'),'eq.'+SPACE);
});
test('new-space owner comes only from a current valid signed-in identity',async()=>{
  let called=false;
  for(const state of [{signedIn:false,user:null},{signedIn:true,user:{id:'invalid'}}]){
    const client=new DirectoryClient({state:()=>state,requestData:async()=>{called=true;return [];}});
    await assert.rejects(client.saveListing(null,false,draft,consent,false));
  }
  assert.equal(called,false);
});
test('updates never rewrite ownership, and empty mutation results are not treated as success',async()=>{
  const calls=[];const auth={requestData:async(path,options)=>{calls.push({path,options});return [];}};
  const client=new DirectoryClient(auth);
  await assert.rejects(client.saveListing(SPACE,true,{...draft,owner_id:USER},consent,false),/could not be changed/);
  assert.equal(calls[0].options.method,'PATCH');assert.equal('owner_id' in calls[0].options.body,false);
  await assert.rejects(client.setRequestStatus(SPACE,'approved'),/Invalid request action/);
  await assert.rejects(client.setRequestStatus(SPACE,'accepted_pending_device'),/could not be changed/);
  assert.deepEqual(calls[1].options.body,{status:'accepted_pending_device'});
});
test('private listing queries use only owned IDs returned by the protected endpoint',async()=>{
  const calls=[];const auth={requestData:async(path,options)=>{calls.push({path,options});if(path.startsWith('directory_spaces'))return [{id:SPACE,created_at:'now'}];return [{space_id:SPACE,title:'Mine'}];}};
  const result=await new DirectoryClient(auth).mySpaces();
  assert.equal(result[0].listing.title,'Mine');assert.equal(calls[1].options,undefined);
  assert.equal(new URL('https://example.test/'+calls[1].path).searchParams.get('space_id'),'in.('+SPACE+')');
});
test('request pagination stays authenticated and does not silently hide older requests',async()=>{
  const calls=[];const auth={requestData:async(path,options)=>{calls.push({path,options});return Array.from({length:51},(_,i)=>({id:String(i)}));}};
  const client=new DirectoryClient(auth),result=await client.requests({offset:100});
  assert.equal(result.requests.length,50);assert.equal(result.hasMore,true);assert.equal(calls[0].options,undefined);
  assert.equal(new URL('https://example.test/'+calls[0].path).searchParams.get('offset'),'100');
  await assert.rejects(client.requests({offset:-1}),/Invalid request page/);
});
