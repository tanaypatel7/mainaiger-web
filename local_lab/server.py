"""Loopback-only owner panel with no remote access or browser Docker socket."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hmac
import json
import os
import secrets
import shutil
import stat
import sys
import threading
import time
from urllib.parse import urlsplit
from .runner import execute, preview, validate_settings, session_preview, session_command, session_control, resolve_docker, stage_kernel, CPUS, MEMORY
from .kernel_session import KernelSession, KernelError, KernelBusy
from . import setup as lab_setup
from . import graph as graph_module
from . import models as lab_models
from . import mainbooks as lab_mainbooks
from . import library as lab_library
from . import environment as lab_environment
from . import files as lab_files
from . import decide as lab_decide
from . import network as lab_network
from . import codemap as lab_codemap
from . import events as lab_events
from .journal import Journal
from .sessions import SessionRegistry
from .devices import DeviceManager, DeviceError
from . import remote as lab_remote
from .graph_run import GraphRun, MAX_STEPS

ASSETS=Path(__file__).parent/'ui'
EXAMPLES=Path(__file__).resolve().parent.parent/'examples'
EXAMPLE_NAME=__import__('re').compile(r'^[a-z0-9][a-z0-9-]{0,60}$')

def initialize_state(path):
    path=Path(path).expanduser()
    if path.is_symlink(): raise ValueError('State directory cannot be a symlink')
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    state=path.resolve(strict=True)
    info=state.stat()
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('State directory must be private and owned by this account')
    file=state/'owner.key'
    try:
        fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as out: out.write(secrets.token_urlsafe(32))
    except FileExistsError: pass
    fd=os.open(file,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077 or info.st_nlink!=1:
            raise ValueError('Owner key file must be private and owned by this account')
        with os.fdopen(fd,'r') as inp:
            fd=None
            key=inp.read(256).strip()
    finally:
        if fd is not None: os.close(fd)
    if len(key)<32 or len(key)>128: raise ValueError('Invalid owner key file')
    return state,key

def docker_kernel(settings,state_dir):
    """Build a session whose kernel runs inside a limited Docker container."""
    docker,env,error=resolve_docker()
    if error: raise KernelError(error)
    try: kernel_dir=stage_kernel(state_dir)
    except (OSError,ValueError) as e: raise KernelError('The kernel could not be staged: %s'%e)
    name='mainaiger-lab-session-'+secrets.token_hex(8)
    args=session_command(settings,name,kernel_dir);args[0]=docker
    control=session_control(name)
    for cmd in control.values(): cmd[0]=docker
    return KernelSession(args,env=env,interrupt_args=control['interrupt'],kill_args=control['kill'],label=name)

class LabServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,state,key,executor=execute,kernel_factory=None):
        if address[0]!='127.0.0.1': raise ValueError('Lab listens on loopback only')
        self.state_dir=Path(state);self.key=key;self.executor=executor
        self.settings={'workspace':None,'cpus':'0.5','memory':'256m'}
        self.sessions={};self.failures=[];self.running=False;self.setup_running=False;self.revision=0;self.lock=threading.RLock();self.slots=threading.BoundedSemaphore(16)
        self.setup_job={'running':False,'action':None,'phase':'idle','bytes':0,'total':None,'result':None}
        # One persistent kernel at a time. The factory builds a KernelSession for the
        # given settings; the default runs the kernel inside a Docker container.
        self.kernel_factory=kernel_factory or docker_kernel;self.kernel_lock=threading.Lock()
        self.journal=Journal(state)
        self.kernels=SessionRegistry(self.kernel_factory,state,self.journal)
        # One graph document in memory (the browser opens/downloads .ipymb files) and at most one run.
        self.graph_doc=graph_module.new_document();self.graph_revision=0;self.graph_run=None;self.graph_lock=threading.RLock()
        self.env_build=None;self.image_ready=lab_environment.image_ready
        self.cell_inbox=[]  # cells made elsewhere (the Map page) waiting for the Graph page to add them to the open Mainbook
        self.write_roots=set()  # map roots the owner allowed this Lab to write files under, until it stops (never the running app's own code)
        super().__init__(address,LabHandler)
        self.devices=DeviceManager(state,local_port=self.server_port,journal=self.journal)
        # Event triggers: a timer, a changed file, a chat prompt or "Fire now" may start a run once the owner arms the Mainbook.
        self.events=lab_events.EventRegistry(start_run=lambda bindings,reason:self.start_graph_run('all',[],False,bindings=bindings,reason=reason),journal=self.journal,
                                             running=self.graph_running,workspace=lambda:self.settings.get('workspace'))
    @property
    def kernel(self):
        """The active session's kernel (or None). Routes that predate multiple sessions use this."""
        return self.kernels.active_kernel()
    def graph_view(self):
        with self.graph_lock:
            doc=graph_module.validate(self.graph_doc)
            return {'document':doc,'revision':self.graph_revision,'analysis':graph_module.analysis(doc),'data_edges':graph_module.data_edges(doc),
                    'order':graph_module.order(doc),'disconnected':graph_module.disconnected(doc),'run':self.graph_run.snapshot() if self.graph_run else None,
                    'library':self.library_links(doc)}
    def library_links(self,doc):
        # node id -> link info for cells that came from (or were saved to) the library: {name, version, state}
        # where state is current | behind | modified | missing (see library.link_state).
        try:by_name={r['name']:r for r in lab_library.listing(self.state_dir)}
        except OSError:by_name={}
        out={}
        for node in doc['graph']['nodes']:
            if node['kind'] not in ('code','markdown'):continue
            cell=doc['notebook']['cells'][node['cell_index']];link=lab_library.link_of(cell)
            if link:out[node['id']]={'name':link['name'],'version':link['version'],'state':lab_library.link_state(cell,by_name),'latest':by_name[link['name']]['version'] if link['name'] in by_name else None}
        return out
    def graph_running(self):
        return self.graph_run is not None and self.graph_run.state in ('pending','running')
    def set_graph(self,document):
        doc=graph_module.validate(document)
        with self.graph_lock:
            if self.graph_running():raise graph_module.GraphError('A graph run is in progress; cancel it before editing')
            self.graph_doc=doc;self.graph_revision+=1
            view=self.graph_view()
        self.events.graph_changed(graph_module.fingerprint(doc))  # an edit to cells, arrows or sources disarms
        return view
    def graph_plan(self,mode,nodes,include_prerequisites=False):
        with self.graph_lock:
            doc=self.graph_doc
            plan=graph_module.plan(doc,mode,nodes)
            present=[]
            kernel=self.kernel
            if kernel is not None and kernel.alive and not kernel.executing:
                try:present=[v['name'] for v in kernel.variables()]
                except KernelError:present=[]
            check=graph_module.preflight(doc,plan,present)
            if include_prerequisites and check['prerequisites']:
                plan=[i for i in graph_module.order(doc) if i in set(plan)|set(check['prerequisites'])]
                check=graph_module.preflight(doc,plan,present)
            check['mode']=mode
            # Cells may name the session they run on; data cannot cross sessions, and a named session must be running.
            check['sessions']={i:graph_module.session_of(doc,i) for i in plan if graph_module.session_of(doc,i)}
            check['cross_session']=graph_module.cross_session(doc,plan)
            check['missing_sessions']=sorted({l for l in check['sessions'].values() if not (self.kernels.by_label(l) and self.kernels.by_label(l)['kernel'].alive)})
            needs_active=any(graph_module.node_by_id(doc,i)['kind']=='code' and i not in check['sessions'] for i in plan)
            check['needs_active_session']=needs_active
            # Arrows the run will decide: loops (with budgets) and conditions, so the review names them.
            chosen=set(plan);start=next(n['id'] for n in doc['graph']['nodes'] if n['kind']=='start')
            title=lambda i:'Start' if i==start else (doc['notebook']['cells'][graph_module.node_by_id(doc,i)['cell_index']].get('metadata',{}).get('mainaiger',{}).get('title') or i) if graph_module.node_by_id(doc,i)['kind']=='code' else i
            check['loops']=[{'id':e['id'],'source':e['source'],'target':e['target'],'source_title':title(e['source']),'target_title':title(e['target']),'max_fires':e['max_fires'],'when':e.get('when'),'inside':e['target'] in chosen} for e in graph_module.loop_edges(doc) if e['source'] in chosen]
            check['conditions']=[{'id':e['id'],'source':e['source'],'target':e['target'],'source_title':title(e['source']),'target_title':title(e['target']),'when':e['when']} for e in graph_module.forward_edges(doc) if e.get('when') and (e['source'] in chosen or (mode=='all' and e['source']==start))]
            check['session_alive']=bool(kernel is not None and kernel.alive) if needs_active else not check['missing_sessions']
            return plan,check
    def start_graph_run(self,mode,nodes,include_prerequisites,bindings=None,reason=None):
        kernel=self.kernel
        with self.graph_lock:
            if self.graph_running():raise KernelError('A graph run is already in progress')
            plan,check=self.graph_plan(mode,nodes,include_prerequisites)
            if check['needs_active_session'] and (kernel is None or not kernel.alive):raise KernelError('Start a session before running the graph (cells that name no session use the active one)')
            if kernel is not None and kernel.alive and kernel.executing:raise KernelError('A cell is already running; interrupt it or wait')
            if not plan:raise graph_module.GraphError('Nothing to run: no code nodes are reachable in this plan')
            doc=self.graph_doc
            if check['missing_sessions']:raise KernelError('These cells name sessions that are not running: %s'%', '.join(check['missing_sessions']))
            def record(ident,result):
                with self.graph_lock:
                    graph_module.record_result(doc,ident,result)
                node=graph_module.node_by_id(doc,ident);title=(doc['notebook']['cells'][node['cell_index']].get('metadata',{}).get('mainaiger',{}).get('title') or ident) if node else ident
                self.journal.write('cell.'+result['status'],'%s: %s%s%s'%(title,result['status'],(' in %d ms'%result['elapsed_ms']) if result.get('elapsed_ms') is not None else '',(' · model %s'%result['model']) if result.get('model') else ''),
                                   {'node':ident,'status':result['status'],'elapsed_ms':result.get('elapsed_ms'),'session':result.get('session'),'model':result.get('model'),'error':(result.get('error') or {}).get('evalue')})
            def finished(run):
                ok=sum(1 for n in run.nodes.values() if n.get('status')=='ok');idle=sum(1 for n in run.nodes.values() if n.get('status')=='idle')
                self.journal.write('run.'+run.state,'Run %s %s: %d/%d ok%s%s%s'%(run.mode,run.state,ok,len(run.plan),(' · %d idle'%idle) if idle else '',(' · %d steps'%run.steps) if run.steps>len(run.plan) else '',(' · '+run.error) if run.error else ''),{'run':run.id,'mode':run.mode,'state':run.state,'ok':ok,'idle':idle,'steps':run.steps,'total':len(run.plan)})
            def arrow(edge,info):
                # Only arrows with something to decide are journaled: conditions (with the answer) and loops (with the count).
                state=info.get('state');who='%s → %s'%(info.get('source_title',edge['source']),info.get('target_title',edge['target']))
                if state=='error':text='Condition on %s failed: %s'%(who,info.get('error'))
                elif edge.get('loop'):text='Loop %s %s (%d/%d)%s'%(who,{'fired':'fired: running the body again','not_fired':'not taken','exhausted':'budget spent','blocked':'leads outside this run'}.get(state,state),info.get('fires',0),info.get('max_fires',0),(' · when %s → %s'%(edge['when'],info.get('value'))) if edge.get('when') else '')
                else:text='Arrow %s %s · when %s → %s'%(who,'fired' if state=='fired' else 'not fired',edge.get('when'),info.get('value'))
                self.journal.write('arrow.'+(state or 'decided'),text,{'edge':edge['id'],'source':edge['source'],'target':edge['target'],'state':state,'fires':info.get('fires'),'max_fires':info.get('max_fires'),'when':edge.get('when'),'value':info.get('value')})
            self.graph_run=GraphRun(doc,plan,kernel,mode=mode,on_result=record,kernel_for=self.kernels.kernel_for,on_finish=finished,ask_model=self.ask_model,on_edge=arrow,bindings=bindings,reason=reason).start()
            self.journal.write('run.start','Run %s %s: %d cell%s'%(mode,reason or 'approved',len(plan),'' if len(plan)==1 else 's'),{'run':self.graph_run.id,'mode':mode,'plan':plan,'reason':reason})
            return self.graph_run.snapshot()
    def ask_model(self,cfg,prompt):
        """How a model cell asks the local model: schema -> one checked decision (decide.ask); no schema -> plain chat."""
        if cfg.get('schema'):
            r=lab_decide.ask(cfg['model'],prompt,'',cfg['schema'],attempts=cfg.get('attempts',2))
            return {'ok':bool(r.get('ok')),'value':r.get('decision'),'raw':r.get('raw'),'error':r.get('error'),'elapsed_ms':r.get('elapsed_ms'),'attempts':r.get('attempts'),'model':r.get('model',cfg['model'])}
        r=lab_models.chat(cfg['model'],[{'role':'user','content':prompt[:50000]}])
        return {'ok':bool(r.get('ok')),'value':r.get('reply'),'raw':r.get('reply'),'error':None if r.get('ok') else r.get('output'),'elapsed_ms':r.get('elapsed_ms'),'attempts':1,'model':r.get('model',cfg['model'])}
    def kernel_status(self):
        row=self.kernels.active()
        if row is None: return {'alive':False,'executing':False,'label':None,'count':0,'python':None,'started_at':None,'ended':None,'session_label':None}
        return dict(row['kernel'].status(),session_label=row['label'])
    def environment_status(self):
        """The environment the current Mainbook declares, whether its image exists, and the last build."""
        with self.graph_lock:
            try:packages=lab_environment.packages_of(self.graph_doc)
            except lab_environment.EnvironmentSpecError:packages=[]
        info=lab_environment.describe(packages)
        if not packages:info['ready']=True
        else:
            # docker image inspect is a child process; remember its answer for a few seconds because this is polled.
            cache=getattr(self,'_image_cache',None)
            if not cache or cache[0]!=info['image'] or time.time()-cache[2]>5:
                cache=(info['image'],self.image_ready(info['image']),time.time());self._image_cache=cache
            info['ready']=cache[1]
        build=self.env_build
        info['build']=build.snapshot() if build else None
        if build and build.state=='done' and build.image==info['image']:info['ready']=True
        return info
    def start_kernel(self,settings,label=None):
        """Start one more session. The first session (or the first alive one) becomes the active session."""
        with self.kernel_lock:
            env=self.environment_status()
            image=None
            if env['derived']:
                if not env['ready']:raise KernelError('This Mainbook declares packages (%s) whose image %s is not built yet. Build the environment first.'%(', '.join(env['packages']),env['image']))
                image=env['image']
            return self.kernels.start(settings,label=label,image=image)
    def stop_kernel(self,ident=None):
        with self.kernel_lock:
            status=self.kernels.stop(ident)
            return status if status is not None else self.kernel_status()
    def server_close(self):
        try:
            self.events.close()
            self.kernels.stop_all()
            self.devices.close()
        finally: super().server_close()
    def start_setup_job(self, action, runner=None):
        """Run one long setup operation in the background; `runner(progress)` returns {'ok','output'}."""
        with self.lock:
            if self.setup_running:return False
            self.setup_running=True
            self.setup_job={'running':True,'action':action,'phase':'starting','bytes':0,'total':None,'result':None,'detail':None}
        def progress(data):
            with self.lock:
                for key in ('phase','bytes','total','detail'):
                    if key in data:self.setup_job[key]=data[key]
        def worker():
            try:
                if runner is not None: result=runner(progress)
                elif action=='download': result=lab_setup.download_installer(self.state_dir,progress)
                else: result=lab_setup.open_installer(self.state_dir)
            except Exception:
                result={'ok':False,'output':'The setup action failed. Check the installer and try again.'}
            with self.lock:
                self.setup_job.update(running=False,phase='done' if result.get('ok') else 'failed',result=result)
                self.setup_running=False
        threading.Thread(target=worker,daemon=True).start()
        return True
    def process_request(self,request,client_address):
        if not self.slots.acquire(blocking=False):return self.shutdown_request(request)
        try:super().process_request(request,client_address)
        except Exception:
            self.slots.release()
            raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()

class LabHandler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def setup(self): super().setup();self.connection.settimeout(10)
    def out(self,status,data=None,raw=None,kind='application/json; charset=utf-8',cookie=None):
        payload=raw if raw is not None else json.dumps(data,ensure_ascii=True).encode()
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(payload)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers()
        try:self.wfile.write(payload)
        except (BrokenPipeError,ConnectionError):pass
    def host_ok(self):return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
    def origin_ok(self):return self.headers.get('Origin')=='http://'+self.headers.get('Host','')
    def session(self):
        c=SimpleCookie()
        try:c.load(self.headers.get('Cookie',''))
        except Exception:return None
        value=c.get('mainaiger_lab_owner')
        with self.server.lock:
            self.server.sessions={k:v for k,v in self.server.sessions.items() if v['expires']>time.time()}
            return self.server.sessions.get(value.value) if value else None
    def body(self,limit=25000):
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('Expected JSON')
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=limit: raise ValueError('Request too large')
        data=json.loads(self.rfile.read(size))
        if not isinstance(data,dict):raise ValueError('Expected JSON object')
        return data
    def do_OPTIONS(self):self.out(403,{'error':'Cross-origin requests are disabled'})
    def do_GET(self):
        if not self.host_ok():return self.out(403,{'error':'Unexpected host'})
        path=urlsplit(self.path).path
        assets={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/setup.js':('setup.js','text/javascript; charset=utf-8'),'/notebook.js':('notebook.js','text/javascript; charset=utf-8'),'/session.js':('session.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8'),'/graph':('graph.html','text/html; charset=utf-8'),'/graph.js':('graph.js','text/javascript; charset=utf-8'),'/graph.css':('graph.css','text/css; charset=utf-8'),'/models':('models.html','text/html; charset=utf-8'),'/models.js':('models.js','text/javascript; charset=utf-8'),'/devices':('devices.html','text/html; charset=utf-8'),'/devices.js':('devices.js','text/javascript; charset=utf-8'),'/map':('map.html','text/html; charset=utf-8'),'/map.js':('map.js','text/javascript; charset=utf-8'),'/map.css':('map.css','text/css; charset=utf-8')}
        if path in assets:
            name,kind=assets[path];return self.out(200,raw=(ASSETS/name).read_bytes(),kind=kind)
        if path=='/api/state':
            session=self.session()
            if not session:return self.out(401,{'error':'Local owner sign-in required'})
            with self.server.lock: settings=dict(self.server.settings);revision=self.server.revision
            env=self.server.environment_status()
            available=bool(lab_setup._find_docker()) if sys.platform=='darwin' else bool(shutil.which('docker'))
            return self.out(200,{'csrf':session['csrf'],'workspace':settings['workspace'],'limits':{'cpus':settings['cpus'],'memory':settings['memory']},'docker_available':available,'command':preview(settings),'session_command':session_preview(settings,env['image'] if env['derived'] else None),'environment':{'image':env['image'],'derived':env['derived'],'ready':env['ready'],'packages':env['packages']},'revision':revision,'session':self.server.kernel_status()})
        if path=='/api/kernel':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            return self.out(200,self.server.kernel_status())
        if path=='/api/kernel/variables':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            kernel=self.server.kernel
            if kernel is None or not kernel.alive:return self.out(409,{'error':'No running session'})
            if kernel.executing:return self.out(409,{'error':'A cell is still running'})
            try:return self.out(200,{'variables':kernel.variables()})
            except KernelError as e:return self.out(409,{'error':str(e)})
        if path=='/api/graph':
            session=self.session()
            if not session:return self.out(401,{'error':'Local owner sign-in required'})
            view=self.server.graph_view();view['csrf']=session['csrf'];view['session']=self.server.kernel_status()
            return self.out(200,view)
        if path=='/api/mainbooks':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            data=lab_mainbooks.status(self.server.state_dir);data['mainbooks']=lab_mainbooks.listing(self.server.state_dir)
            return self.out(200,data)
        if path.startswith('/api/mainbooks/'):
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            name=__import__('urllib.parse').parse.unquote(path[len('/api/mainbooks/'):])
            try:return self.out(200,{'document':lab_mainbooks.load(self.server.state_dir,name),'name':name})
            except lab_mainbooks.MainbookError as e:return self.out(404,{'error':str(e)})
        if path=='/api/sessions':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            self.server.kernels.sync_remote(self.server.devices)
            with self.server.lock: settings=dict(self.server.settings);revision=self.server.revision
            env=self.server.environment_status()
            return self.out(200,{'sessions':self.server.kernels.list(),'active':self.server.kernels.active_id,'max':__import__('local_lab.sessions',fromlist=['MAX_SESSIONS']).MAX_SESSIONS,
                                 'defaults':{'cpus':settings['cpus'],'memory':settings['memory'],'workspace':settings['workspace']},'revision':revision,
                                 'environment':{'image':env['image'],'derived':env['derived'],'ready':env['ready']},'options':{'cpus':sorted(CPUS,key=float),'memory':sorted(MEMORY,key=lambda m:int(m[:-1])*(1024 if m.endswith('g') else 1))}})
        if path=='/api/graph/inbox':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            with self.server.graph_lock:return self.out(200,{'cells':list(self.server.cell_inbox)})
        if path=='/api/events':
            # What may fire this Mainbook, whether it is armed, and the recent firings.
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            with self.server.graph_lock:doc=self.server.graph_doc;sources=graph_module.start_events(doc);title=doc['notebook'].get('metadata',{}).get('mainaiger',{}).get('title') or doc['notebook'].get('metadata',{}).get('mainaiger',{}).get('name') or ''
            status=self.server.events.status();ws=self.server.settings.get('workspace')
            status['document']={'sources':[dict(s,description=lab_events.describe(s,ws)) for s in sources],'title':title,'fingerprint':graph_module.fingerprint(doc),'workspace':ws,'revision':self.server.graph_revision}
            status['limits']={'queue':lab_events.QUEUE_MAX,'timer_min':lab_events.TIMER_MIN,'timer_max':lab_events.TIMER_MAX,'poll':lab_events.FILE_POLL,'max_steps':MAX_STEPS}
            return self.out(200,status)
        if path=='/api/journal':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            q=__import__('urllib.parse').parse.parse_qs(urlsplit(self.path).query)
            try:after=int(q.get('after',['0'])[0]);limit=int(q.get('limit',['200'])[0])
            except ValueError:return self.out(400,{'error':'after and limit must be integers'})
            return self.out(200,{'entries':self.server.journal.tail(after,limit),'seq':self.server.journal.seq})
        if path in ('/api/files','/api/files/read'):
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            q=__import__('urllib.parse').parse.parse_qs(urlsplit(self.path).query)
            with self.server.lock: settings=dict(self.server.settings)
            roots=lab_files.roots(settings,self.server.state_dir)
            root=q.get('root',[''])[0];rel=q.get('path',[''])[0]
            if path=='/api/files' and not root:return self.out(200,{'roots':roots})
            if root not in roots:return self.out(404,{'error':'Unknown folder'})
            try:
                if path=='/api/files':data=lab_files.listing(roots[root],rel)
                else:data=lab_files.read(roots[root],rel)
            except lab_files.FilesError as e:return self.out(400,{'error':str(e)})
            data['root']=root;data['roots']=roots
            return self.out(200,data)
        if path=='/api/environment':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            return self.out(200,self.server.environment_status())
        if path=='/api/library':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            query=__import__('urllib.parse').parse.parse_qs(urlsplit(self.path).query).get('q',[''])[0][:200]
            data=lab_library.status(self.server.state_dir);data['cells']=lab_library.listing(self.server.state_dir,query);data['query']=query
            return self.out(200,data)
        if path.startswith('/api/library/'):
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            name=__import__('urllib.parse').parse.unquote(path[len('/api/library/'):])
            try:return self.out(200,{'cell':lab_library.load(self.server.state_dir,name)})
            except lab_library.LibraryError as e:return self.out(404,{'error':str(e)})
        if path=='/api/examples':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            items=[]
            for file in sorted(EXAMPLES.glob('*.ipymb')) if EXAMPLES.is_dir() else []:
                if EXAMPLE_NAME.match(file.stem):
                    try:
                        doc=json.loads(file.read_text(encoding='utf-8'));cells=doc.get('notebook',{}).get('cells',[])
                        intro=next((graph_module.source_text(c).strip().splitlines()[0].lstrip('# ') for c in cells if c.get('cell_type')=='markdown' and graph_module.source_text(c).strip()),file.stem)
                        items.append({'name':file.stem,'title':intro[:80],'cells':len(cells)})
                    except (OSError,ValueError,IndexError):continue
            return self.out(200,{'examples':items})
        if path.startswith('/examples/'):
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            name=path[len('/examples/'):]
            if not EXAMPLE_NAME.match(name) or not (EXAMPLES/(name+'.ipymb')).is_file():return self.out(404,{'error':'No such example'})
            try:return self.out(200,{'document':graph_module.validate(json.loads((EXAMPLES/(name+'.ipymb')).read_text(encoding='utf-8')))})
            except (OSError,ValueError) as e:return self.out(500,{'error':'Example could not be read: %s'%e})
        if path=='/api/graph/export':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            with self.server.graph_lock:notebook,info=graph_module.to_notebook(self.server.graph_doc)
            return self.out(200,{'notebook':notebook,'lossy':info['lossy'],'note':'Cells are in trigger order; a branching graph is flattened to one sequence.' if info['lossy'] else 'The graph is one chain; this notebook runs identically in Jupyter.'})
        if path=='/api/graph/run':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            run=self.server.graph_run
            return self.out(200,{'run':run.snapshot() if run else None,'session':self.server.kernel_status(),'revision':self.server.graph_revision})
        if path in ('/api/codemap','/api/codemap/source'):
            # Code map: the Lab's own package ("self") or one of the Files roots, read with ast, never executed.
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            q=__import__('urllib.parse').parse.parse_qs(urlsplit(self.path).query);root=q.get('root',['self'])[0]
            with self.server.lock: settings=dict(self.server.settings)
            roots={'self':str(Path(__file__).resolve().parent),**lab_files.roots(settings,self.server.state_dir)}
            if root not in roots:return self.out(404,{'error':'Unknown folder'})
            try:
                if path=='/api/codemap':
                    cache=getattr(self.server,'_codemap_cache',{});entry=cache.get(root)
                    if not entry or time.time()-entry[0]>10:entry=(time.time(),lab_codemap.scan(roots[root]));cache[root]=entry;self.server._codemap_cache=cache
                    data=dict(entry[1]);data['roots']=sorted(roots);data['root_name']=root;data['root']=roots[root] if root!='self' else 'this Lab (local_lab)'
                    data['writable']=root in self.server.write_roots;data['can_write']=root!='self';data['folder']=roots[root]
                    # The same folder as the session sees it (the workspace is mounted at /workspace), or None.
                    inside=None
                    if settings.get('workspace') and root!='self':
                        ws=Path(settings['workspace']).resolve();hr=Path(roots[root]).resolve()
                        if hr==ws:inside='/workspace'
                        elif ws in hr.parents:inside='/workspace/'+hr.relative_to(ws).as_posix()
                    data['inside']=inside
                    return self.out(200,data)
                data=lab_codemap.source(roots[root],q.get('path',[''])[0],int(q.get('line',['1'])[0]),int(q.get('end_line',['1'])[0]))
                return self.out(200,data)
            except (lab_codemap.MapError,ValueError,OSError) as e:return self.out(400,{'error':str(e)})
        if path=='/api/devices':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            return self.out(200,{'devices':self.server.devices.list(),'local_port':self.server.server_port,
                                 'install':['git clone https://github.com/tanaypatel7/mainaiger-web ~/mainaiger-web','cd ~/mainaiger-web && git checkout feature/persistent-kernel','python3 -m local_lab --port 8795','# the Lab prints the owner key path; copy that file to this Mac with scp and chmod 600 it']})
        if path in ('/api/devices/files','/api/devices/files/read','/api/devices/journal'):
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            q=__import__('urllib.parse').parse.parse_qs(urlsplit(self.path).query);name=q.get('name',[''])[0]
            try:
                lab=self.server.devices.lab(name)
                if path=='/api/devices/journal':data=lab.journal(int(q.get('after',['0'])[0]),int(q.get('limit',['100'])[0]))
                elif path=='/api/devices/files':data=lab.files(q.get('root',[''])[0] or None,q.get('path',[''])[0])
                else:data=lab.read_file(q.get('root',[''])[0],q.get('path',[''])[0])
            except (DeviceError,lab_remote.RemoteError,ValueError) as e:return self.out(409,{'error':str(e)})
            data['device']=name
            return self.out(200,data)
        if path=='/api/network':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            data=lab_network.overview();data['sessions']=self.server.kernels.list();data['journal_seq']=self.server.journal.seq
            return self.out(200,data)
        if path=='/api/models':
            session=self.session()
            if not session:return self.out(401,{'error':'Local owner sign-in required'})
            data=lab_models.status(self.server.state_dir);data['csrf']=session['csrf']
            with self.server.lock: data['job']=dict(self.server.setup_job)
            return self.out(200,data)
        if path=='/api/setup':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            return self.out(200,lab_setup.status(self.server.state_dir))
        if path=='/api/setup/job':
            if not self.session():return self.out(401,{'error':'Local owner sign-in required'})
            with self.server.lock: job=dict(self.server.setup_job)
            return self.out(200,job)
        self.out(404,{'error':'Not found'})
    def do_POST(self):
        if not self.host_ok() or not self.origin_ok():return self.out(403,{'error':'Local same-origin request required'})
        try:
            path=urlsplit(self.path).path;data=self.body(limit=4000000 if path in ('/api/graph','/api/graph/import') else 600000 if path in ('/api/models/chat','/api/models/decide') else 25000)
            if path=='/api/login':
                if set(data)!={'key'} or not isinstance(data['key'],str):raise ValueError('Key required')
                with self.server.lock:
                    self.server.failures=[x for x in self.server.failures if time.time()-x<60]
                    if len(self.server.failures)>=5:return self.out(429,{'error':'Try again in one minute'})
                    if not hmac.compare_digest(data['key'],self.server.key):
                        self.server.failures.append(time.time());return self.out(403,{'error':'Invalid local key'})
                    sid=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
                    self.server.sessions[sid]={'csrf':csrf,'expires':time.time()+3600}
                return self.out(200,{'csrf':csrf},cookie=f'mainaiger_lab_owner={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600')
            session=self.session()
            if not session or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']):return self.out(403,{'error':'Local owner sign-in required'})
            if path=='/api/logout':
                with self.server.lock:self.server.sessions={k:v for k,v in self.server.sessions.items() if v is not session}
                return self.out(200,{'ok':True},cookie='mainaiger_lab_owner=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
            if path in ('/api/models/installer/download','/api/models/installer/open','/api/models/app/open'):
                if data != {'confirm': True} or type(data.get('confirm')) is not bool:raise ValueError('Explicit confirmation is required')
                if path.endswith('/app/open'):return self.out(200,lab_models.open_app())
                state_dir=self.server.state_dir
                runner=(lambda progress: lab_models.download_installer(state_dir,progress)) if path.endswith('/download') else (lambda progress: lab_models.open_installer(state_dir))
                if not self.server.start_setup_job('ollama-download' if path.endswith('/download') else 'ollama-open',runner):return self.out(409,{'error':'A setup action is already running'})
                return self.out(202,{'ok':True})
            if path=='/api/models/pull':
                if set(data)!={'model','confirm'} or data['confirm'] is not True or not isinstance(data['model'],str) or not lab_models.MODEL_NAME.match(data['model']):raise ValueError('Choose a model and confirm the download')
                model=data['model']
                if not self.server.start_setup_job('ollama-pull:'+model,lambda progress: lab_models.pull(model,progress)):return self.out(409,{'error':'A setup action is already running'})
                return self.out(202,{'ok':True,'model':model})
            if path=='/api/models/chat':
                if set(data)!={'model','messages'}:raise ValueError('Send {model, messages}')
                result=lab_models.chat(data['model'],data['messages'])
                # An armed Mainbook with a chat source is fired by the prompt too (the chat still answers as usual).
                if self.server.events.has_source('chat') and isinstance(data['messages'],list) and data['messages'] and isinstance(data['messages'][-1],dict) and isinstance(data['messages'][-1].get('content'),str):
                    result['event']=self.server.events.fire('chat',{'prompt':data['messages'][-1]['content'],'model':data['model']},origin='chat')
                return self.out(200 if result.get('ok') else 409,result)
            if path=='/api/models/decide':
                # Thin slice: one question, a JSON answer constrained and checked against a small schema (local_lab/decide.py).
                if set(data)-{'model','question','context','schema','attempts'} or not {'model','question','schema'}<=set(data):raise ValueError('Send {model, question, context, schema, attempts}')
                if not isinstance(data['model'],str):raise ValueError('model must be text')
                result=lab_decide.ask(data['model'],data['question'],data.get('context',''),data['schema'],attempts=data.get('attempts',2) if isinstance(data.get('attempts',2),int) else 2)
                self.server.journal.write('model.decide','Decision %s via %s (%d attempt%s)'%('ok' if result.get('ok') else 'failed',result.get('model',data['model']),result.get('attempts',0),'' if result.get('attempts')==1 else 's'),{'model':data['model'],'ok':bool(result.get('ok')),'attempts':result.get('attempts'),'error':result.get('error')})
                return self.out(200 if result.get('ok') else 409,result)
            if path=='/api/setup/docker/open':
                if data != {'confirm': True} or type(data.get('confirm')) is not bool:raise ValueError('Explicit confirmation is required')
                return self.out(200,lab_setup.open_docker())
            if path=='/api/setup/pull':
                if data != {'confirm': True} or type(data.get('confirm')) is not bool:raise ValueError('Explicit confirmation is required')
                with self.server.lock:
                    if self.server.setup_running:return self.out(409,{'error':'A setup action is already running'})
                    self.server.setup_running=True
                try:result=lab_setup.pull()
                finally:
                    with self.server.lock:self.server.setup_running=False
                return self.out(200,result)
            if path in ('/api/setup/installer/download','/api/setup/installer/open'):
                if data != {'confirm': True} or type(data.get('confirm')) is not bool:raise ValueError('Explicit confirmation is required')
                action='download' if path.endswith('/download') else 'open'
                if not self.server.start_setup_job(action):return self.out(409,{'error':'A setup action is already running'})
                return self.out(202,{'ok':True})
            if path=='/api/settings':
                settings=validate_settings(data,self.server.state_dir)
                if self.server.kernels.any_alive():return self.out(409,{'error':'Stop the running sessions before changing the folder or limits; a session keeps the settings it started with.'})
                with self.server.lock:self.server.settings=settings;self.server.revision+=1
                return self.out(200,{'ok':True})
            if path=='/api/kernel/start':
                if set(data)!={'confirm','revision'} or data['confirm'] is not True or type(data['revision']) is not int:raise ValueError('Explicit confirmation is required')
                with self.server.lock:
                    if data['revision']!=self.server.revision:return self.out(409,{'error':'Settings changed after review. Review the current settings again.'})
                    settings=dict(self.server.settings)
                if self.server.kernel_status()['alive']:return self.out(409,{'error':'A session is already running; start additional sessions from Resources on the graph page'})
                try:return self.out(200,self.server.start_kernel(settings))
                except KernelError as e:return self.out(409,{'error':str(e)})
            if path=='/api/kernel/stop':
                return self.out(200,self.server.stop_kernel())
            if path=='/api/sessions/start':
                if set(data)-{'confirm','revision','label','cpus','memory'} or data.get('confirm') is not True or type(data.get('revision')) is not int:raise ValueError('Explicit confirmation is required')
                with self.server.lock:
                    if data['revision']!=self.server.revision:return self.out(409,{'error':'Settings changed after review. Review the current settings again.'})
                    settings=dict(self.server.settings)
                if 'cpus' in data:
                    if data['cpus'] not in CPUS:raise ValueError('Choose one of the supported CPU limits')
                    settings['cpus']=data['cpus']
                if 'memory' in data:
                    if data['memory'] not in MEMORY:raise ValueError('Choose one of the supported memory limits')
                    settings['memory']=data['memory']
                label=data.get('label')
                if label is not None and not isinstance(label,str):raise ValueError('label must be text')
                try:return self.out(200,self.server.start_kernel(settings,label=label or None))
                except KernelError as e:return self.out(409,{'error':str(e)})
            if path=='/api/sessions/execute':
                # Run code in one specific session (local or on a device). Used by other Labs that reach this one, and by tests.
                if set(data)-{'id','code','confirm'} or data.get('confirm') is not True or not isinstance(data.get('id'),str) or not isinstance(data.get('code'),str):raise ValueError('Send {id, code, confirm}')
                row=self.server.kernels.get(data['id'])
                if row is None or not row['kernel'].alive:return self.out(409,{'error':'No running session with that id'})
                try:result=row['kernel'].execute(data['code'])
                except KernelBusy as e:return self.out(409,{'error':str(e)})
                except (KernelError,ValueError) as e:return self.out(409,{'error':str(e)})
                self.server.journal.write('session.execute','Code run in "%s" through the API: %s'%(row['label'],result.get('status')),{'id':row['id'],'label':row['label'],'status':result.get('status'),'bytes':len(data['code'])})
                return self.out(200,{'session':self.server.kernels.row_status(row),'result':result})
            if path=='/api/sessions/evaluate':
                # One expression answered by a session (a condition on a trigger arrow); no cell runs, no count advances.
                if set(data)-{'id','expression','confirm'} or data.get('confirm') is not True or not isinstance(data.get('id'),str) or not isinstance(data.get('expression'),str):raise ValueError('Send {id, expression, confirm}')
                row=self.server.kernels.get(data['id'])
                if row is None or not row['kernel'].alive:return self.out(409,{'error':'No running session with that id'})
                try:result=row['kernel'].evaluate(data['expression'])
                except KernelBusy as e:return self.out(409,{'error':str(e)})
                except (KernelError,ValueError) as e:return self.out(409,{'error':str(e)})
                return self.out(200,{'session':self.server.kernels.row_status(row),'result':result})
            if path=='/api/sessions/variables':
                if set(data)!={'id'} or not isinstance(data['id'],str):raise ValueError('Send {id}')
                row=self.server.kernels.get(data['id'])
                if row is None or not row['kernel'].alive:return self.out(409,{'error':'No running session with that id'})
                if row['kernel'].executing:return self.out(409,{'error':'A cell is still running'})
                try:return self.out(200,{'variables':row['kernel'].variables()})
                except KernelError as e:return self.out(409,{'error':str(e)})
            if path=='/api/devices/pair':
                if set(data)-{'name','host','port','key_file','tunnel','confirm'} or data.get('confirm') is not True or not {'name','host','port','key_file'}<=set(data):raise ValueError('Send {name, host, port, key_file, tunnel, confirm}')
                try:return self.out(200,self.server.devices.pair(data['name'],data['host'],data['port'],data['key_file'],data.get('tunnel','lab')))
                except (DeviceError,OSError) as e:return self.out(400,{'error':str(e)})
            if path=='/api/devices/connect':
                if set(data)!={'name','confirm'} or data['confirm'] is not True or not isinstance(data['name'],str):raise ValueError('Confirm the connection')
                try:return self.out(200,self.server.devices.connect(data['name']))
                except DeviceError as e:return self.out(409,{'error':str(e)})
            if path in ('/api/devices/disconnect','/api/devices/forget'):
                if set(data)!={'name'} or not isinstance(data['name'],str):raise ValueError('Send {name}')
                try:
                    if path.endswith('/forget'):self.server.devices.forget(data['name'])
                    else:self.server.devices.disconnect(data['name'])
                    self.server.kernels.sync_remote(self.server.devices)
                    return self.out(200,{'ok':True})
                except DeviceError as e:return self.out(409,{'error':str(e)})
            if path=='/api/devices/session/start':
                if set(data)-{'name','label','cpus','memory','confirm'} or data.get('confirm') is not True or not isinstance(data.get('name'),str):raise ValueError('Confirm the session on the device')
                try:return self.out(200,self.server.kernels.start_remote(self.server.devices,data['name'],data.get('label') or None,data.get('cpus'),data.get('memory')))
                except (DeviceError,KernelError) as e:return self.out(409,{'error':str(e)})
            if path=='/api/sessions/command':
                # A terminal line, run inside the chosen session's container (no network, read-only workspace, limits), never on this device.
                if set(data)-{'id','command','confirm'} or data.get('confirm') is not True or not isinstance(data.get('command'),str):raise ValueError('Send {command, confirm, id?}')
                if 'id' in data and not isinstance(data['id'],str):raise ValueError('id must be text')
                try:row,result=self.server.kernels.command(data['command'],data.get('id'))
                except (KernelError,ValueError) as e:return self.out(409,{'error':str(e)})
                return self.out(200,{'session':row,'result':result})
            if path in ('/api/sessions/stop','/api/sessions/interrupt','/api/sessions/select','/api/sessions/forget'):
                if set(data)!={'id'} or not isinstance(data['id'],str):raise ValueError('Send {id}')
                try:
                    if path.endswith('/stop'):status=self.server.stop_kernel(data['id']);return self.out(200,{'session':status})
                    if path.endswith('/interrupt'):ok,status=self.server.kernels.interrupt(data['id']);return self.out(200,{'ok':ok,'session':status})
                    if path.endswith('/select'):return self.out(200,{'session':self.server.kernels.select(data['id'])})
                    self.server.kernels.forget(data['id']);return self.out(200,{'ok':True})
                except KernelError as e:return self.out(409,{'error':str(e)})
            if path=='/api/kernel/interrupt':
                kernel=self.server.kernel
                if kernel is None or not kernel.alive:return self.out(409,{'error':'No running session'})
                return self.out(200,{'ok':kernel.interrupt(),'session':kernel.status()})
            if path=='/api/kernel/execute':
                if set(data)!={'code','confirm'} or data['confirm'] is not True:raise ValueError('Explicit confirmation is required')
                kernel=self.server.kernel
                if kernel is None or not kernel.alive:return self.out(409,{'error':'Start a session first'})
                try:result=kernel.execute(data['code'])
                except KernelBusy as e:return self.out(409,{'error':str(e)})
                except KernelError as e:return self.out(409,{'error':str(e)})
                result['session']=kernel.status()
                return self.out(200,result)
            if path=='/api/graph':
                if set(data)!={'document'}:raise ValueError('Send {document}')
                try:return self.out(200,self.server.set_graph(data['document']))
                except graph_module.GraphError as e:return self.out(409 if 'in progress' in str(e) else 400,{'error':str(e)})
            if path=='/api/mainbooks/folder':
                if set(data)!={'folder','confirm'} or data['confirm'] is not True:raise ValueError('Confirm the folder choice')
                try:return self.out(200,lab_mainbooks.configure(self.server.state_dir,data['folder']))
                except lab_mainbooks.MainbookError as e:return self.out(400,{'error':str(e)})
                except OSError as e:return self.out(400,{'error':'The folder could not be created: %s'%e})
            if path=='/api/mainbooks/save':
                if set(data)-{'name','overwrite'} or 'name' not in data or not isinstance(data.get('overwrite',False),bool):raise ValueError('Send {name, overwrite}')
                with self.server.graph_lock:
                    if self.server.graph_running():return self.out(409,{'error':'Wait for the run to finish before saving'})
                    try:result=lab_mainbooks.save(self.server.state_dir,data['name'],self.server.graph_doc,bool(data.get('overwrite',False)))
                    except lab_mainbooks.MainbookError as e:return self.out(409 if 'already exists' in str(e) else 400,{'error':str(e)})
                    except OSError as e:return self.out(400,{'error':'The Mainbook could not be written: %s'%e})
                    self.server.graph_doc=result['document']
                self.server.journal.write('mainbook.save','Saved Mainbook "%s"'%result['name'],{'name':result['name'],'bytes':result['bytes']})
                return self.out(200,{'name':result['name'],'path':result['path'],'bytes':result['bytes']})
            if path=='/api/environment/build':
                # Fixed, reviewed step: the packages come from the validated document, never from the request.
                if set(data)!={'confirm','image'} or data['confirm'] is not True or not isinstance(data['image'],str):raise ValueError('Confirm the build of a named image')
                env=self.server.environment_status()
                if not env['derived']:return self.out(400,{'error':'This Mainbook declares no packages; the base image needs no build'})
                if data['image']!=env['image']:return self.out(409,{'error':'The package list changed after review; review the environment again'})
                with self.server.graph_lock:
                    current=self.server.env_build
                    if current and current.state=='running':return self.out(409,{'error':'A build is already running'})
                    build=lab_environment.Build(env['packages'])
                    try:build.start()
                    except lab_environment.EnvironmentSpecError as e:return self.out(400,{'error':str(e)})
                    self.server.env_build=build
                self.server.journal.write('env.build.start','Environment build approved: %s'%env['image'],{'image':env['image'],'packages':env['packages']})
                return self.out(200,self.server.environment_status())
            if path=='/api/library/folder':
                if set(data)!={'folder','confirm'} or data['confirm'] is not True:raise ValueError('Confirm the folder choice')
                try:return self.out(200,lab_library.configure(self.server.state_dir,data['folder']))
                except lab_library.LibraryError as e:return self.out(400,{'error':str(e)})
                except OSError as e:return self.out(400,{'error':'The folder could not be created: %s'%e})
            if path=='/api/library/seed':
                if set(data)!={'confirm'} or data['confirm'] is not True:raise ValueError('Confirm adding the starter cells')
                try:return self.out(200,lab_library.seed(self.server.state_dir))
                except lab_library.LibraryError as e:return self.out(400,{'error':str(e)})
                except OSError as e:return self.out(400,{'error':'The starter cells could not be written: %s'%e})
            if path=='/api/library/save':
                # Save one code cell of the current graph as a library cell (new name: version 1; existing name + overwrite: next version).
                if set(data)-{'node','name','tags','description','overwrite'} or not {'node','name'}<=set(data) or not isinstance(data.get('overwrite',False),bool):raise ValueError('Send {node, name, tags, description, overwrite}')
                with self.server.graph_lock:
                    try:
                        record=lab_library.record_from_cell(self.server.graph_doc,data['node'],data['name'],data.get('tags'),data.get('description',''))
                        result=lab_library.save(self.server.state_dir,record,bool(data.get('overwrite',False)))
                        # The saved cell now carries a link to the version it became.
                        node=graph_module.node_by_id(self.server.graph_doc,data['node']);cell=self.server.graph_doc['notebook']['cells'][node['cell_index']]
                        cell.setdefault('metadata',{}).setdefault('mainaiger',{})['library']={'name':result['record']['name'],'version':result['record']['version'],'source_hash':result['record']['source_hash']}
                        self.server.graph_revision+=1
                    except lab_library.LibraryError as e:return self.out(409 if 'already exists' in str(e) else 400,{'error':str(e)})
                    except OSError as e:return self.out(400,{'error':'The library cell could not be written: %s'%e})
                    view=self.server.graph_view()
                self.server.journal.write('library.save','Saved library cell %s v%d'%(result['record']['name'],result['record']['version']),{'name':result['record']['name'],'version':result['record']['version']})
                return self.out(200,{'name':result['record']['name'],'version':result['record']['version'],'path':result['path'],'view':view})
            if path=='/api/library/insert':
                if set(data)-{'name','position','after'} or not {'name','position'}<=set(data):raise ValueError('Send {name, position, after}')
                with self.server.graph_lock:
                    if self.server.graph_running():return self.out(409,{'error':'A graph run is in progress; cancel it before editing'})
                    try:
                        record=lab_library.load(self.server.state_dir,data['name'])
                        doc,node_id=lab_library.insert(self.server.graph_doc,record,data['position'],data.get('after'))
                    except (lab_library.LibraryError,graph_module.GraphError) as e:return self.out(400,{'error':str(e)})
                    self.server.graph_doc=doc;self.server.graph_revision+=1
                    view=self.server.graph_view()
                self.server.journal.write('library.insert','Inserted %s v%d from the Library as %s'%(record['name'],record['version'],node_id),{'name':record['name'],'version':record['version'],'node':node_id})
                return self.out(200,{'node':node_id,'view':view})
            if path=='/api/library/pull':
                if set(data)!={'node'}:raise ValueError('Send {node}')
                with self.server.graph_lock:
                    if self.server.graph_running():return self.out(409,{'error':'A graph run is in progress; cancel it before editing'})
                    try:
                        node=graph_module.node_by_id(self.server.graph_doc,data['node']);link=node and lab_library.link_of(self.server.graph_doc['notebook']['cells'][node['cell_index']])
                        if not link:return self.out(400,{'error':'That cell is not linked to a library cell'})
                        doc=lab_library.pull(self.server.graph_doc,data['node'],lab_library.load(self.server.state_dir,link['name']))
                    except (lab_library.LibraryError,graph_module.GraphError) as e:return self.out(400,{'error':str(e)})
                    self.server.graph_doc=doc;self.server.graph_revision+=1
                    view=self.server.graph_view()
                return self.out(200,{'view':view})
            if path=='/api/graph/import':
                if set(data)!={'notebook'}:raise ValueError('Send {notebook}')
                try:document,report=graph_module.from_notebook(data['notebook'])
                except graph_module.GraphError as e:return self.out(400,{'error':str(e)})
                return self.out(200,{'document':document,'report':report})
            if path=='/api/graph/preflight':
                mode=data.get('mode');nodes=data.get('nodes',[])
                if mode not in ('all','from','one','selected') or not isinstance(nodes,list) or not all(isinstance(n,str) for n in nodes):raise ValueError('Send a run mode and node ids')
                try:plan,check=self.server.graph_plan(mode,nodes,bool(data.get('include_prerequisites')))
                except graph_module.GraphError as e:return self.out(400,{'error':str(e)})
                check['plan']=plan;return self.out(200,check)
            if path=='/api/graph/run':
                mode=data.get('mode');nodes=data.get('nodes',[])
                if data.get('confirm') is not True or mode not in ('all','from','one','selected') or not isinstance(nodes,list) or not all(isinstance(n,str) for n in nodes):raise ValueError('Explicit confirmation is required')
                try:return self.out(202,{'run':self.server.start_graph_run(mode,nodes,bool(data.get('include_prerequisites')))})
                except graph_module.GraphError as e:return self.out(400,{'error':str(e)})
                except KernelError as e:return self.out(409,{'error':str(e)})
            if path=='/api/graph/cancel':
                run=self.server.graph_run
                if run is None or not self.server.graph_running():return self.out(409,{'error':'No graph run is in progress'})
                run.cancel();return self.out(200,{'run':run.snapshot()})
            if path=='/api/codemap/cell':
                # A function on the Map becomes a cell: a form for its parameters in front of a call. Queued for the Graph page.
                if set(data)!={'root','id'} or not isinstance(data['root'],str) or not isinstance(data['id'],str):raise ValueError('Send {root, id}')
                with self.server.lock: settings=dict(self.server.settings)
                roots={'self':str(Path(__file__).resolve().parent),**lab_files.roots(settings,self.server.state_dir)}
                if data['root'] not in roots:return self.out(404,{'error':'Unknown folder'})
                try:
                    scanned=lab_codemap.scan(roots[data['root']]);node=next((n for n in scanned['nodes'] if n['id']==data['id']),None)
                    if node is None:return self.out(404,{'error':'No such node on the map (reload the map)'})
                    host_root=str(Path(roots[data['root']]).resolve().parent) if data['root']=='self' else roots[data['root']]
                    import_roots=[]
                    if settings.get('workspace'):
                        ws=Path(settings['workspace']).resolve();hr=Path(host_root).resolve()
                        if hr==ws:import_roots.append('/workspace')
                        elif ws in hr.parents:import_roots.append('/workspace/'+hr.relative_to(ws).as_posix())
                    import_roots.append(host_root)
                    cell=lab_codemap.cell_for(node,import_roots)
                except lab_codemap.MapError as e:return self.out(400,{'error':str(e)})
                cell['id']='inbox-'+secrets.token_hex(3);cell['root']=data['root'];cell['queued_at']=time.time()
                cell['warning']=None if len(import_roots)>1 else 'The session cannot see this folder: set the workspace (Setup page) to %s or a parent of it so the container can import the package'%host_root
                with self.server.graph_lock:
                    self.server.cell_inbox=[c for c in self.server.cell_inbox if c['from']!=cell['from']][-19:]+[cell]
                self.server.journal.write('map.cell','Cell made from %s (%s:%d), waiting for the Graph page'%(node['name'],node['path'],node['line']),{'id':cell['id'],'node':node['id'],'root':data['root']})
                return self.out(200,{'cell':cell,'queued':len(self.server.cell_inbox)})
            if path in ('/api/codemap/approve-write','/api/codemap/diff','/api/codemap/write'):
                # Editing from the map: a diff first, then an atomic write into a folder the owner allowed this Lab to write, this session.
                if not isinstance(data.get('root'),str):raise ValueError('Send the map root')
                with self.server.lock: settings=dict(self.server.settings)
                roots={'self':str(Path(__file__).resolve().parent),**lab_files.roots(settings,self.server.state_dir)}
                if data['root'] not in roots:return self.out(404,{'error':'Unknown folder'})
                if path.endswith('/approve-write'):
                    if set(data)!={'root','confirm'} or data['confirm'] is not True:raise ValueError('Confirm the folder')
                    if data['root']=='self':return self.out(409,{'error':'The running app\'s own code is not edited in place; set your workspace to a checkout of the repository and map that'})
                    self.server.write_roots.add(data['root']);self.server.journal.write('map.write.allow','Writes allowed under %s until the Lab stops'%roots[data['root']],{'root':data['root'],'folder':roots[data['root']]})
                    return self.out(200,{'ok':True,'root':data['root'],'folder':roots[data['root']]})
                need={'root','path','line','end_line','text','expected_hash'}|({'confirm'} if path.endswith('/write') else set())
                if set(data)!=need or not isinstance(data['path'],str) or type(data['line']) is not int or type(data['end_line']) is not int or not isinstance(data['text'],str) or not isinstance(data['expected_hash'],str):raise ValueError('Send {root, path, line, end_line, text, expected_hash%s}'%(', confirm' if path.endswith('/write') else ''))
                writing=path.endswith('/write')
                if writing and data['confirm'] is not True:raise ValueError('Review the diff, then confirm')
                if writing and data['root'] not in self.server.write_roots:return self.out(409,{'error':'Allow writes under this folder first (the Map asks once per Lab session)'})
                try:result=lab_codemap.replace_lines(roots[data['root']],data['path'],data['line'],data['end_line'],data['text'],data['expected_hash'],write=writing)
                except lab_codemap.MapError as e:return self.out(409 if 'changed since' in str(e) else 400,{'error':str(e)})
                except OSError as e:return self.out(500,{'error':'The file could not be written: %s'%e})
                if result['written']:
                    self.server._codemap_cache={}
                    self.server.journal.write('map.write','Wrote %s lines %d-%d (%d line%s before, %d after)'%(data['path'],data['line'],data['end_line'],result['lines']['before'],'' if result['lines']['before']==1 else 's',result['lines']['after']),{'root':data['root'],'path':data['path'],'line':data['line'],'end_line':data['end_line'],'bytes':len(data['text'].encode('utf-8')),'hash':result['new_hash']})
                return self.out(200,result)
            if path=='/api/graph/inbox/take':
                if set(data)!={'ids'} or not isinstance(data['ids'],list) or not all(isinstance(i,str) for i in data['ids']):raise ValueError('Send {ids}')
                with self.server.graph_lock:
                    taken=[c for c in self.server.cell_inbox if c['id'] in set(data['ids'])];self.server.cell_inbox=[c for c in self.server.cell_inbox if c['id'] not in set(data['ids'])]
                return self.out(200,{'taken':len(taken),'left':len(self.server.cell_inbox)})
            if path=='/api/events/arm':
                # The reviewed step: the owner saw exactly what may fire this Mainbook (the dialog lists /api/events descriptions).
                if set(data)!={'confirm','revision'} or data['confirm'] is not True or type(data['revision']) is not int:raise ValueError('Review what may fire the Mainbook, then confirm')
                with self.server.graph_lock:
                    if data['revision']!=self.server.graph_revision:return self.out(409,{'error':'The Mainbook changed after review; look again and arm it then'})
                    doc=self.server.graph_doc;sources=graph_module.start_events(doc);fp=graph_module.fingerprint(doc)
                    title=doc['notebook'].get('metadata',{}).get('mainaiger',{}).get('title') or doc['notebook'].get('metadata',{}).get('mainaiger',{}).get('name') or 'the Mainbook'
                try:return self.out(200,self.server.events.arm(sources,fp,title))
                except lab_events.EventError as e:return self.out(409,{'error':str(e)})
            if path=='/api/events/disarm':
                return self.out(200,self.server.events.disarm())
            if path=='/api/events/fire':
                # "Fire now": the owner's own click (or a script of theirs) fires an armed Mainbook with a small payload.
                if set(data)-{'kind','payload','confirm'} or data.get('confirm') is not True:raise ValueError('Confirm the firing')
                kind=data.get('kind','manual');payload=data.get('payload',{})
                if kind not in lab_events.EVENT_KINDS or not isinstance(payload,dict) or len(json.dumps(payload))>20000:raise ValueError('Send {kind, payload (a small object), confirm}')
                outcome=self.server.events.fire(kind,payload,origin='owner')
                return self.out(200 if outcome in ('queued','coalesced') else 409,{'outcome':outcome,'events':self.server.events.status()})
            if path=='/api/run':
                if set(data)!={'code','confirm','revision'} or data['confirm'] is not True or type(data['revision']) is not int:raise ValueError('Explicit confirmation is required')
                with self.server.lock:
                    if data['revision']!=self.server.revision:return self.out(409,{'error':'Settings changed after review. Review the current settings and code again.'})
                    if self.server.running:return self.out(409,{'error':'A run is already in progress'})
                    self.server.running=True;settings=dict(self.server.settings)
                try:result=self.server.executor(data['code'],settings)
                finally:
                    with self.server.lock:self.server.running=False
                return self.out(200,result)
            self.out(404,{'error':'Not found'})
        except (ValueError,TypeError,json.JSONDecodeError) as e:self.out(400,{'error':str(e)})
        except (OSError,TimeoutError):self.out(503,{'error':'Local runtime could not complete the request'})
