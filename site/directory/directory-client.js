/* Public metadata only. Device access is enforced by each owner's installation. */
(function(scope){
  'use strict';
  const REGIONS=Object.freeze({'africa':'Africa','asia':'Asia','europe':'Europe','north-america':'North America','south-america':'South America','oceania':'Oceania','antarctica':'Antarctica'});
  const DEVICES=Object.freeze({'mac':'Mac','windows':'Windows','linux':'Linux','server':'Server','mobile':'Mobile','other':'Other'});
  const APIS=Object.freeze({'ollama':'Ollama','openai-compatible':'OpenAI-compatible','jupyter':'Jupyter','mcp':'MCP','custom':'Custom API'});
  const CAPABILITIES=Object.freeze({'files-read':'Read selected files','files-write':'Edit selected files','notebooks-run':'Run notebooks','terminal-run':'Run terminal commands','models-use':'Use models','api-use':'Use selected APIs'});
  const STATUS=Object.freeze({'pending':'Awaiting owner review','accepted_pending_device':'Accepted · local permission still required','declined':'Declined','cancelled':'Cancelled'});
  const FIELDS='space_id,title,description,region,device_types,device_count,member_count,cpu_threads,memory_gb,gpu_count,api_types,published,updated_at';
  function text(value,max,name,required=false){
    if(typeof value!=='string')value='';
    value=value.trim();
    if(value.length>max||(required&&!value))throw new Error(name+' must contain '+(required?'1–':'at most ')+max+' characters.');
    return value;
  }
  function picks(value,allowed,name){
    if(!Array.isArray(value)||value.some(x=>typeof x!=='string'||!Object.hasOwn(allowed,x)))throw new Error('Choose valid '+name+'.');
    return [...new Set(value)];
  }
  function number(value,max,name){
    if(value===''||value===null||value===undefined)return null;
    const n=typeof value==='number'?value:Number(value);
    if(!['number','string'].includes(typeof value)||!Number.isSafeInteger(n)||n<0||n>max)throw new Error(name+' must be a whole number from 0 to '+max+'.');
    return n;
  }
  function uuid(value){
    if(typeof value!=='string'||!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value))throw new Error('Invalid space or request identifier.');
    return value;
  }
  function listingPayload(input,consent={},published=false){
    if(published&&consent.publish!==true)throw new Error('Review the preview and confirm publication first.');
    const region=consent.region===true?input.region:null;
    if(region!==null&&!Object.hasOwn(REGIONS,region))throw new Error('Choose a broad region.');
    return {
      title:text(input.title,80,'Space name',true),description:text(input.description,400,'Description'),region,
      device_types:consent.devices===true?picks(input.device_types,DEVICES,'device categories'):[],
      device_count:consent.devices===true?number(input.device_count,10000,'Device count'):null,
      member_count:consent.members===true?number(input.member_count,10000,'Reported membership count'):null,
      cpu_threads:consent.compute===true?number(input.cpu_threads,1000000,'CPU threads'):null,
      memory_gb:consent.compute===true?number(input.memory_gb,100000000,'Memory'):null,
      gpu_count:consent.compute===true?number(input.gpu_count,100000,'GPU count'):null,
      api_types:consent.apis===true?picks(input.api_types,APIS,'API categories'):[],published:published===true
    };
  }
  function requestPayload(spaceId,input){
    const capabilities=picks(input.capabilities,CAPABILITIES,'requested permissions');
    if(!capabilities.length)throw new Error('Select at least one requested permission.');
    return {space_id:uuid(spaceId),display_name:text(input.display_name,60,'Display name',true),capabilities,message:text(input.message,500,'Message')};
  }
  function query(table,params){return table+'?'+new URLSearchParams(params).toString();}
  function rows(value){if(!Array.isArray(value))throw new Error('The directory returned an unexpected response.');return value;}
  function one(value){const result=rows(value);if(result.length!==1)throw new Error('The record could not be changed. Refresh and check your access.');return result[0];}
  class DirectoryClient{
    constructor(auth){this.auth=auth;}
    async publicListings({search='',region='',offset=0}={}){
      if(!Number.isSafeInteger(offset)||offset<0)throw new Error('Invalid page.');
      const params={select:FIELDS,published:'eq.true',order:'updated_at.desc,space_id.asc',limit:'51',offset:String(offset)};
      if(region){if(!Object.hasOwn(REGIONS,region))throw new Error('Invalid region filter.');params.region='eq.'+region;}
      const term=text(search,80,'Search').replace(/[\\%_*]/g,' ');
      if(term.trim())params.title='ilike.%'+term+'%';
      const data=rows(await this.auth.requestData(query('directory_listings',params),{publicAccess:true}));
      return {listings:data.slice(0,50),hasMore:data.length>50};
    }
    async publicStats(){return one(await this.auth.requestData('rpc/directory_public_stats',{publicAccess:true}));}
    async mySpaces(){
      const spaces=rows(await this.auth.requestData('directory_spaces?select=id,created_at&order=created_at.desc&limit=20'));
      if(!spaces.length)return [];
      const ids=spaces.map(s=>uuid(s.id));
      const listings=rows(await this.auth.requestData(query('directory_listings',{select:FIELDS,space_id:'in.('+ids.join(',')+')'})));
      return spaces.map(s=>({id:s.id,created_at:s.created_at,listing:listings.find(l=>l.space_id===s.id)||null}));
    }
    async saveListing(spaceId,hasListing,input,consent,published){
      const payload=listingPayload(input,consent,published);
      let created=false;
      if(!spaceId){
        const state=this.auth.state();
        if(!state.signedIn)throw new Error('Sign in before creating a space.');
        const ownerId=uuid(state.user?.id);
        const space=one(await this.auth.requestData('directory_spaces',{method:'POST',body:{owner_id:ownerId}}));
        spaceId=uuid(space.id);created=true;
      }else uuid(spaceId);
      try{
        if(hasListing)return one(await this.auth.requestData(query('directory_listings',{space_id:'eq.'+spaceId}),{method:'PATCH',body:payload}));
        return one(await this.auth.requestData('directory_listings',{method:'POST',body:{space_id:spaceId,...payload}}));
      }catch(error){
        if(created){try{await this.deleteSpace(spaceId);}catch{/* An incomplete draft remains visible under My spaces. */}}
        throw error;
      }
    }
    async unpublish(spaceId){return one(await this.auth.requestData(query('directory_listings',{space_id:'eq.'+uuid(spaceId)}),{method:'PATCH',body:{published:false}}));}
    async deleteSpace(spaceId){return one(await this.auth.requestData(query('directory_spaces',{id:'eq.'+uuid(spaceId)}),{method:'DELETE'}));}
    async requests({offset=0}={}){
      if(!Number.isSafeInteger(offset)||offset<0)throw new Error('Invalid request page.');
      const data=rows(await this.auth.requestData(query('directory_requests',{select:'id,space_id,requester_id,display_name,capabilities,message,status,created_at,updated_at',order:'created_at.desc,id.asc',limit:'51',offset:String(offset)})));
      return {requests:data.slice(0,50),hasMore:data.length>50};
    }
    async requestAccess(spaceId,input){return one(await this.auth.requestData('directory_requests',{method:'POST',body:requestPayload(spaceId,input)}));}
    async setRequestStatus(requestId,status){
      if(!['cancelled','accepted_pending_device','declined'].includes(status))throw new Error('Invalid request action.');
      return one(await this.auth.requestData(query('directory_requests',{id:'eq.'+uuid(requestId)}),{method:'PATCH',body:{status}}));
    }
  }
  const exports={DirectoryClient,listingPayload,requestPayload,REGIONS,DEVICES,APIS,CAPABILITIES,STATUS};
  scope.MainaigerDirectory=Object.freeze(exports);
  if(typeof module!=='undefined'&&module.exports)module.exports=exports;
})(typeof window!=='undefined'?window:globalThis);
