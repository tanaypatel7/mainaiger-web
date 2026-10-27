/* Memory-only email OTP client. Authorization belongs to the API, never this UI. */
(function(scope){
  'use strict';
  const generic='If this email is registered, a sign-in code will arrive shortly.';
  class AuthError extends Error { constructor(code,message){super(message);this.code=code;} }
  class MainaigerAuth {
    #session=null; #user=null; #generation=0; #email=''; #nextRequest=0; #dataRequests=new Set(); #apiBase='';
    constructor(config={},deps={}){
      this.config={...config};this.fetch=deps.fetch||scope.fetch?.bind(scope);this.now=deps.now||Date.now;
      this.changed=deps.onChange||(()=>{});this.codeLength=config.codeLength||8;
      let valid=false;
      try{const u=new URL(config.url);valid=u.protocol==='https:'&&!u.username&&!u.password&&u.pathname==='/'&&!u.search&&!u.hash&&/^sb_publishable_[A-Za-z0-9_-]+$/.test(config.publishableKey||'');}catch{}
      this.enabled=config.enabled===true&&valid&&typeof this.fetch==='function';
      this.base=valid?config.url.replace(/\/$/,''):'';
      this.#apiBase=this.base;
    }
    state(){
      if(this.#session&&this.#session.expiresAt<=this.now()){this.#session=null;this.#user=null;this.#generation++;this.#abortData();}
      return {configured:this.enabled,signedIn:!!this.#session,user:this.#user?{id:this.#user.id,email:this.#user.email}:null,resendSeconds:Math.max(0,Math.ceil((this.#nextRequest-this.now())/1000))};
    }
    #emit(){this.changed(this.state());}
    #require(){if(!this.enabled)throw new AuthError('not_configured','Email sign-in is being configured.');}
    #abortData(){for(const controller of this.#dataRequests)controller.abort();this.#dataRequests.clear();}
    async requestData(path,{method='GET',body,publicAccess=false}={}){
      this.#require();
      if(typeof path!=='string'||! /^(directory_spaces|directory_listings|directory_requests|rpc\/directory_public_stats)(\?[^#]*)?$/.test(path))throw new AuthError('path','This data endpoint is not available.');
      const endpoint=path.split('?')[0];
      if(!['GET','POST','PATCH','DELETE'].includes(method)||((method==='GET'||method==='DELETE')&&body!==undefined))throw new AuthError('method','This request is not supported.');
      if(publicAccess&&(method!=='GET'||!['directory_listings','rpc/directory_public_stats'].includes(endpoint)))throw new AuthError('permission','Sign in to manage your spaces and requests.');
      const state=this.state(),version=this.#generation;
      if(!publicAccess&&!state.signedIn)throw new AuthError('session','Sign in to manage your spaces and requests.');
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
      if(!publicAccess)this.#dataRequests.add(controller);
      try{
        const response=await this.fetch(this.#apiBase+'/rest/v1/'+path,{method,headers:{apikey:this.config.publishableKey,'Content-Type':'application/json',...(publicAccess?{}:{Authorization:'Bearer '+this.#session.accessToken}),...(method==='GET'?{}:{Prefer:'return=representation'})},...(body===undefined?{}:{body:JSON.stringify(body)}),credentials:'omit',cache:'no-store',redirect:'error',signal:controller.signal});
        let data=null;try{data=await response.json();}catch{}
        if(!publicAccess&&(!this.state().signedIn||version!==this.#generation))throw new AuthError('stale','Your session changed. Sign in again.');
        if(!response.ok){
          if(!publicAccess&&response.status===401){this.#session=null;this.#user=null;this.#generation++;this.#abortData();this.#emit();}
          const code=response.status===409?'conflict':response.status===429?'rate_limited':response.status===401?'session':'rejected';
          const message=code==='conflict'?'A request or space with these details already exists.':code==='rate_limited'?'Please wait before trying again.':code==='session'?'Your session expired. Sign in again.':'The data request was rejected. Check the selected fields and your permissions.';
          throw new AuthError(code,message);
        }
        return data;
      }catch(e){if(e instanceof AuthError)throw e;if(!publicAccess&&version!==this.#generation)throw new AuthError('stale','Your session changed. Sign in again.');throw new AuthError('network','Unable to reach the directory. Please try again.');}
      finally{clearTimeout(timer);this.#dataRequests.delete(controller);}
    }
    async #request(path,body,token){
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
      try{
        const response=await this.fetch(this.base+'/auth/v1/'+path,{method:body===undefined?'GET':'POST',headers:{apikey:this.config.publishableKey,'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},...(body===undefined?{}:{body:JSON.stringify(body)}),credentials:'omit',cache:'no-store',redirect:'error',signal:controller.signal});
        let data={};try{data=await response.json();}catch{}
        if(!response.ok)throw new AuthError(response.status===429?'rate_limited':'rejected',response.status===429?'Please wait before trying again.':'The request could not be completed. Please try again.');
        return data;
      }catch(e){if(e instanceof AuthError)throw e;throw new AuthError('network','Unable to reach sign-in. Please try again.');}finally{clearTimeout(timer);}
    }
    async requestCode(email){
      this.#require();email=String(email).trim();
      if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))throw new AuthError('email','Enter a valid email address.');
      if(this.state().resendSeconds)throw new AuthError('cooldown','Please wait before requesting another code.');
      const version=++this.#generation;this.#abortData();this.#session=null;this.#user=null;this.#email=email;this.#nextRequest=this.now()+60000;this.#emit();
      try{await this.#request('otp',{email,create_user:false});}
      catch(e){if(version!==this.#generation)return {stale:true};if(e.code==='network'||e.code==='rate_limited')throw e;/* Registration policy must not be exposed through the UI. */}
      if(version!==this.#generation)return {stale:true};return {message:generic};
    }
    async verifyCode(code){
      this.#require();code=String(code).trim();
      if(!this.#email)throw new AuthError('email','Request a sign-in code first.');
      if(!new RegExp('^\\d{'+this.codeLength+'}$').test(code))throw new AuthError('code','Enter the '+this.codeLength+'-digit code from your email.');
      const version=++this.#generation,email=this.#email;this.#session=null;this.#user=null;this.#emit();
      const data=await this.#request('verify',{email,token:code,type:'email'});
      if(version!==this.#generation)return {stale:true};
      if(typeof data.access_token!=='string'||!data.access_token||!Number.isFinite(Number(data.expires_in))||Number(data.expires_in)<=0)throw new AuthError('session','Sign-in could not be verified.');
      const receivedAt=this.now();
      const relativeExpiry=receivedAt+Math.min(Number(data.expires_in),86400)*1000;
      const absoluteExpiry=data.expires_at===undefined?relativeExpiry:Number(data.expires_at)*1000;
      const expiresAt=Math.min(relativeExpiry,absoluteExpiry);
      if(!Number.isFinite(expiresAt)||expiresAt<=receivedAt)throw new AuthError('session','Sign-in could not be verified.');
      const user=await this.#request('user',undefined,data.access_token);
      if(version!==this.#generation)return {stale:true};
      if(!user.id||typeof user.email!=='string'||user.email.toLowerCase()!==email.toLowerCase()||!user.email_confirmed_at||user.is_anonymous===true)throw new AuthError('session','Sign-in could not be verified.');
      if(expiresAt<=this.now())throw new AuthError('session','The sign-in session expired. Request another code.');
      this.#session={accessToken:data.access_token,expiresAt};
      this.#user={id:user.id,email:user.email};this.#email='';this.#emit();return {signedIn:true};
    }
    async signOut(){
      const token=this.#session?.accessToken;this.#generation++;this.#abortData();this.#session=null;this.#user=null;this.#email='';this.#emit();
      if(token)try{await this.#request('logout?scope=local',{},token);}catch{throw new AuthError('logout','Signed out here. Server sign-out could not be confirmed.');}
    }
    cancelPending(){if(!this.#session){this.#generation++;this.#abortData();}this.#email='';this.#emit();}
  }
  scope.MainaigerAuth=MainaigerAuth;
  if(typeof module!=='undefined'&&module.exports)module.exports={MainaigerAuth,AuthError};
})(typeof window!=='undefined'?window:globalThis);
