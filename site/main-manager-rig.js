/* Main Manager v5 — local path deformation; no body transform or external dependency. */
(function (scope) {
  'use strict';
  const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
  const smooth = v => { const n = clamp(v); return n * n * (3 - 2 * n); };
  const numberPattern = /[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:e[-+]?\d+)?/gi;
  function parsePath(d) {
    const numbers = [];
    const raw = String(d).replace(numberPattern, n => { numbers.push(Number(n)); return '@'; });
    if (/[^MLCQZ\s@,]/.test(raw)) throw new Error('Rig paths must use absolute M/L/C/Q/Z coordinates.');
    const template=raw.replace(/[\s,]/g,'').split('').join(' ');
    if (numbers.length % 2) throw new Error('Rig path coordinate pairs are incomplete.');
    return {template, numbers};
  }
  function subdividePath(parsed, parts = 4) {
    const input=parsed.template.split(' '),numbers=parsed.numbers,output=[],values=[];
    let ni=0,i=0,current=[0,0],start=[0,0];
    const push=(command,points)=>{output.push(command);for(const p of points){output.push('@','@');values.push(p[0],p[1]);}};
    const take=()=>[numbers[ni++],numbers[ni++]];
    while(i<input.length){
      const command=input[i++];
      if(command==='Z'){output.push('Z');current=start.slice();continue;}
      if(command==='M'||command==='L'){const end=take();i+=2;push(command,[end]);current=end;if(command==='M')start=end.slice();continue;}
      if(command==='C'){
        const p0=current,p1=take(),p2=take(),p3=take();i+=6;
        const at=t=>p0.map((v,k)=>(1-t)**3*v+3*(1-t)**2*t*p1[k]+3*(1-t)*t*t*p2[k]+t**3*p3[k]);
        const slope=t=>p0.map((v,k)=>3*(1-t)**2*(p1[k]-v)+6*(1-t)*t*(p2[k]-p1[k])+3*t*t*(p3[k]-p2[k]));
        for(let j=0;j<parts;j++){const a=j/parts,b=(j+1)/parts,u=at(a),v=at(b),du=slope(a),dv=slope(b),h=(b-a)/3;push('C',[u.map((n,k)=>n+h*du[k]),v.map((n,k)=>n-h*dv[k]),v]);}
        current=p3;continue;
      }
      if(command==='Q'){
        const p0=current,p1=take(),p2=take();i+=4;
        const at=t=>p0.map((v,k)=>(1-t)**2*v+2*(1-t)*t*p1[k]+t*t*p2[k]);
        const slope=t=>p0.map((v,k)=>2*(1-t)*(p1[k]-v)+2*t*(p2[k]-p1[k]));
        for(let j=0;j<parts;j++){const a=j/parts,b=(j+1)/parts,u=at(a),v=at(b),du=slope(a),h=(b-a)/2;push('Q',[u.map((n,k)=>n+h*du[k]),v]);}
        current=p2;continue;
      }
      throw new Error('Unsupported or implicit path command.');
    }
    return {template:output.join(' '),numbers:values};
  }
  function recordPath(baseD, centerD, sideD) {
    const base = subdividePath(parsePath(baseD)), center = subdividePath(parsePath(centerD || baseD)), side = subdividePath(parsePath(sideD || baseD));
    if (base.template !== center.template || base.template !== side.template) throw new Error('Eye morph path topology differs from base.');
    return {template:base.template, base:base.numbers, center:center.numbers, side:side.numbers};
  }
  function mixPath(record, center = 0, side = 0) {
    return record.base.map((v, i) => v + (record.center[i] - v) * center + (record.side[i] - v) * side);
  }
  function warpPoint(x, y, state = {}) {
    const side = x < 164 ? 'left' : x > 436 ? 'right' : null;
    if (!side || y <= 165 || y >= 470) return [x, y];
    const edgeWeight = smooth(side === 'left' ? (164 - x) / 72 : (x - 436) / 72);
    const verticalWeight = Math.sin(Math.PI * (y - 165) / 305) ** 2;
    const pulse = clamp(Number(state[side]) || 0);
    const pointer = state.pointer || {};
    const distance = Math.abs(y - (Number(pointer.y) || 315));
    const localWeight = distance < 94 ? Math.cos(distance / 94 * Math.PI / 2) ** 2 : 0;
    const localPulse = pointer.side === side ? clamp(Number(pointer.strength) || 0) * localWeight : 0;
    const power = Math.min(1, pulse + localPulse);
    if (!power || !edgeWeight) return [x, y];
    const clock = Number(state.waveTime ?? state.time) || 0;
    const strength = clamp(Number(state.waveStrength ?? 1), .25, 1);
    const phase = (y - 165) / 118 * Math.PI * 2 - clock * 8.2;
    const dx = Math.sin(phase) * 8.2 * edgeWeight * verticalWeight * power * strength * (side === 'left' ? -1 : 1);
    return [x + dx, y];
  }
  function pathAt(record, state = {}) {
    const values = mixPath(record, eyePresentation(state.center || 0).panelProgress, eyePresentation(state.side || 0).panelProgress);
    for (let i = 0; i < values.length; i += 2) {
      [values[i], values[i+1]] = warpPoint(values[i], values[i+1], state);
    }
    let index = 0;
    return record.template.replace(/@/g, () => Number(values[index++].toFixed(4)).toString());
  }
  // A panel opens first. Pupil movement follows inside that panel; reverse is the same timeline backwards.
  function eyePresentation(progress = 0, kind = 'center', drop) {
    const p = clamp(Number(progress) || 0);
    const panelProgress = smooth(p / .6);
    const descent = clamp((p - .46) / .54);
    const depth = Number.isFinite(Number(drop)) && drop != null ? Number(drop) : kind === 'side' ? 22 : 18;
    const eased = smooth(descent);
    // A small, bounded settling motion belongs only to the pupils, never the M or eye panel.
    const settle = descent > .7 && descent < 1 ? Math.sin((descent - .7) / .3 * Math.PI) * .7 : 0;
    return {
      panelProgress,
      pupilOpacity: smooth((p - .46) / .19),
      pupilY: -depth * (1 - eased) + settle,
      glowOpacity: smooth((p - .55) / .3)
    };
  }
  const eyeProgress = progress => eyePresentation(progress).pupilOpacity;
  function flowAt(time = 0, direction = 1, layer = 'sheen') {
    const sign = direction === 'ccw' || direction === -1 ? -1 : 1;
    // Every track has pathLength=1000. A broad material highlight, brighter head and thin core travel together.
    const config = {
      sheen: {opacity:.52,dasharray:'180 320',phase:0},
      lead: {opacity:.70,dasharray:'52 448',phase:0},
      core: {opacity:.38,dasharray:'16 484',phase:0}
    }[layer] || {opacity:.44,dasharray:'180 820',phase:0};
    return {opacity:config.opacity,dasharray:config.dasharray,dashoffset:(Number(time)||0)*210*sign+config.phase};
  }
  const Geometry = Object.freeze({clamp,smooth,parsePath,subdividePath,recordPath,mixPath,warpPoint,pathAt,eyeProgress,eyePresentation,flowAt});

  class MainManagerRig {
    constructor(svg, options = {}) {
      if (!svg || !svg.querySelectorAll) throw new TypeError('An inline SVG element is required.');
      this.svg = svg;
      this.options = options;
      this.records = [...svg.querySelectorAll('path[data-rig="surface"]')].map(el => ({el,record:recordPath(el.getAttribute('data-rest-d')||el.getAttribute('d'),el.getAttribute('data-center-d'),el.getAttribute('data-side-d'))}));
      this.eyes = {center:svg.querySelector('#eyes-center'),side:svg.querySelector('#eyes-side')};
      this.flows = [...svg.querySelectorAll('[data-flow]')];
      this.reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
      this.state = {appearance:'open-loops',center:0,side:0,time:0,waveTime:0,flowTime:0,waveSpeed:1,flowSpeed:1,waveStrength:1,left:0,right:0,pointer:{side:null,y:315,strength:0},surfaceFlow:false,flowDirection:1,pointerEnabled:true,paused:false};
      this.morph = null;
      this.waveClip = null;
      this.pointerTarget = {side:null,y:315,strength:0};
      this.visible = true;
      this.frame = 0;
      this.previousTime = 0;
      this.disposed = false;
      this.boundTick = t => this.tick(t);
      this.handlers = [];
      this.edgeSamples = this.sampleEdges();
      this.listen(svg,'pointermove',e => this.pointerMove(e));
      this.listen(svg,'pointerleave',() => { this.pointerTarget.strength = 0; this.queue(); });
      this.listen(document,'visibilitychange',() => { this.cancel(); if (!document.hidden) this.queue(); });
      this.reducedHandler = () => {
        this.cancel(); this.waveClip = null; this.state.left = this.state.right = 0;
        this.state.pointer.strength = this.pointerTarget.strength = 0;
        this.state.surfaceFlow = false;
        this.morph = null;
        this.state.center = this.state.appearance === 'center-eyes' ? 1 : 0;
        this.state.side = this.state.appearance === 'side-pupils' ? 1 : 0;
        this.draw(); this.emit('Motion preference updated.');
      };
      this.reduced.addEventListener('change',this.reducedHandler);
      if (typeof IntersectionObserver !== 'undefined') {
        this.observer = new IntersectionObserver(entries => { this.visible=entries[0].isIntersecting; this.cancel(); if(this.visible) this.queue(); },{threshold:.05});
        this.observer.observe(svg);
      }
      this.updateTitle();this.draw();
      this.emit('At rest.');
    }
    listen(target,type,fn) { target.addEventListener(type,fn); this.handlers.push([target,type,fn]); }
    sampleEdges() {
      const outline = this.svg.querySelector('[data-edge-outline]');
      if (!outline || typeof outline.getTotalLength !== 'function') return [];
      const points=[],length=outline.getTotalLength();
      for(let i=0;i<=240;i++) { const p=outline.getPointAtLength(length*i/240); if(p.y>165&&p.y<470&&(p.x<130||p.x>470)) points.push({x:p.x,y:p.y,side:p.x<300?'left':'right'}); }
      return points;
    }
    pointerMove(event) {
      if (this.reduced.matches || !this.state.pointerEnabled || this.state.paused || event.pointerType==='touch') return;
      const matrix=this.svg.getScreenCTM(); if(!matrix) return;
      const p=new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
      let closest=null,distance=Infinity;
      for(const sample of this.edgeSamples) { const d=Math.hypot(p.x-sample.x,p.y-sample.y); if(d<distance){distance=d;closest=sample;} }
      if(closest&&distance<38) this.pointerTarget={side:closest.side,y:closest.y,strength:smooth(1-distance/38)};
      else this.pointerTarget.strength=0;
      this.queue();
    }
    emit(message) { if(typeof this.options.onChange==='function') this.options.onChange({...this.getState(),message}); }
    updateTitle(){const title=this.svg.querySelector('title');if(title)title.textContent=`Main Manager — ${{'open-loops':'open loops','center-eyes':'center-bottom eyes','side-pupils':'loop-side pupils'}[this.state.appearance]}`;}
    getState() { return {...this.state,pointer:{...this.state.pointer},running:!this.state.paused&&!!(this.morph||this.waveClip||this.state.surfaceFlow||this.state.pointer.strength),reducedMotion:this.reduced.matches}; }
    resume(){this.state.paused=false;this.queue();}
    togglePause(){if(this.state.paused){this.resume();this.emit('Motion resumed.');}else this.stop();}
    setAppearance(value) {
      if(!['open-loops','center-eyes','side-pupils'].includes(value)) throw new Error('Unknown Main Manager appearance.');
      this.state.appearance=value; this.updateTitle();this.resume();
      const end={center:value==='center-eyes'?1:0,side:value==='side-pupils'?1:0};
      if(this.reduced.matches){Object.assign(this.state,end);this.morph=null;this.draw();}
      else {this.morph={start:{center:this.state.center,side:this.state.side},end,elapsed:0,duration:1.65};this.queue();}
      this.emit(value==='open-loops'?'Retracting the eye recess.':'Opening the dark eye panel; pupils descend into place. Meaning is unassigned.');
    }
    wave(side) {
      if(!['left','right'].includes(side))throw new Error('Choose left or right.');
      if(this.reduced.matches){this.emit('Reduced motion is on; Main Manager stays still.');return;}
      this.resume();this.waveClip={side,elapsed:0,duration:2.8};this.queue();this.emit(`${side==='left'?'Left':'Right'} outer edge wave.`);
    }
    setSurfaceFlow(value,direction='cw') {
      this.state.surfaceFlow=!!value&&!this.reduced.matches;this.state.flowDirection=direction==='ccw'?-1:1;this.resume();
      this.draw();this.queue();this.emit(this.state.surfaceFlow?'Surface flow on. The M remains in place.':'Surface flow off.');
    }
    setWaveSpeed(value){this.state.waveSpeed=clamp(Number(value)||1,.5,2);this.emit('Wave speed updated.');}
    setFlowSpeed(value){this.state.flowSpeed=clamp(Number(value)||1,.5,2);this.emit('Flow speed updated.');}
    setWaveStrength(value){this.state.waveStrength=clamp(Number(value)||1,.25,1);this.draw();this.emit('Wave strength updated.');}
    setPointerEnabled(value){this.state.pointerEnabled=!!value; if(!value)this.pointerTarget.strength=0;this.queue();this.emit(value?'Edge tickling enabled.':'Edge tickling disabled.');}
    stop(){this.cancel();this.state.paused=true;this.pointerTarget.strength=0;this.emit('Motion paused. Resume continues from this frame.');}
    rest(){this.cancel();this.morph=null;this.waveClip=null;Object.assign(this.state,{appearance:'open-loops',center:0,side:0,time:0,waveTime:0,flowTime:0,left:0,right:0,surfaceFlow:false,paused:false,pointer:{side:null,y:315,strength:0}});this.pointerTarget={side:null,y:315,strength:0};this.updateTitle();this.draw();this.emit('At rest. Eyes retracted.');}
    cancel(){if(this.frame)cancelAnimationFrame(this.frame);this.frame=0;this.previousTime=0;}
    needsFrame(){return !!(this.morph||this.waveClip||this.state.surfaceFlow||this.pointerTarget.strength||this.state.pointer.strength);}
    queue(){if(!this.frame&&!this.disposed&&!this.state.paused&&!this.reduced.matches&&this.visible&&!document.hidden&&this.needsFrame())this.frame=requestAnimationFrame(this.boundTick);}
    tick(timestamp){
      this.frame=0;if(this.disposed||this.state.paused||this.reduced.matches||!this.visible||document.hidden){this.previousTime=0;return;}
      const dt=this.previousTime?Math.min(.04,(timestamp-this.previousTime)/1000):0;this.previousTime=timestamp;this.state.time+=dt;this.state.waveTime+=dt*this.state.waveSpeed;this.state.flowTime+=dt*this.state.flowSpeed;
      if(this.morph){const m=this.morph;m.elapsed+=dt;const p=clamp(m.elapsed/m.duration);this.state.center=m.start.center+(m.end.center-m.start.center)*p;this.state.side=m.start.side+(m.end.side-m.start.side)*p;if(m.elapsed>=m.duration){Object.assign(this.state,m.end);this.morph=null;this.emit('Eye appearance set. Meaning is unassigned.');}}
      if(this.waveClip){const w=this.waveClip;w.elapsed+=dt*this.state.waveSpeed;this.state.left=this.state.right=0;this.state[w.side]=Math.sin(Math.PI*clamp(w.elapsed/w.duration))**2;if(w.elapsed>=w.duration){this.state.left=this.state.right=0;this.waveClip=null;this.emit('Outer edge at rest.');}}
      const p=this.state.pointer,t=this.pointerTarget;
      if(t.strength>0){p.side=t.side;p.y=t.y;}
      p.strength+=(t.strength-p.strength)*Math.min(1,dt*12);
      if(!t.strength&&p.strength<.002){p.strength=0;p.side=null;}
      this.draw();if(this.needsFrame())this.queue();else this.previousTime=0;
    }
    draw(){
      this.records.forEach(({el,record})=>el.setAttribute('d',pathAt(record,this.state)));
      for(const key of ['center','side']){
        const eye=this.eyes[key];if(!eye)continue;
        const progress=this.state[key];eye.style.display=progress>0?'inline':'none';eye.style.opacity='1';
        eye.querySelectorAll('[data-pupil-kind]').forEach(pupil=>{
          const look=eyePresentation(progress,pupil.getAttribute('data-pupil-kind'),pupil.getAttribute('data-pupil-drop'));
          pupil.style.opacity=String(look.pupilOpacity);
          pupil.setAttribute('transform',`translate(0 ${look.pupilY.toFixed(4)})`);
        });
      }
      this.flows.forEach(el=>{const flow=flowAt(this.state.flowTime,this.state.flowDirection,el.getAttribute('data-flow-layer')||'sheen');el.style.opacity=this.state.surfaceFlow?String(flow.opacity):'0';el.style.strokeDasharray=flow.dasharray;el.style.strokeDashoffset=String(flow.dashoffset);});
      this.svg.dataset.appearance=this.state.appearance;
      this.svg.dataset.localMotion=this.waveClip?this.waveClip.side:(this.state.pointer.strength?'tickle':'rest');
    }
    destroy(){this.disposed=true;this.cancel();this.handlers.forEach(([t,k,fn])=>t.removeEventListener(k,fn));this.reduced.removeEventListener('change',this.reducedHandler);this.observer?.disconnect();}
  }
  scope.MainManagerGeometry=Geometry;
  scope.MainManagerRig=MainManagerRig;
  if(typeof module!=='undefined'&&module.exports)module.exports={MainManagerGeometry:Geometry,MainManagerRig};
})(typeof window!=='undefined'?window:globalThis);
