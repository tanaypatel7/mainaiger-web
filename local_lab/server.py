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
from .runner import execute, preview, validate_settings, session_preview, session_command, session_control, resolve_docker, stage_kernel
from .kernel_session import KernelSession, KernelError, KernelBusy
from . import setup as lab_setup
from . import graph as graph_module
from . import models as lab_models
from . import mainbooks as lab_mainbooks
from . import library as lab_library
from . import environment as lab_environment
from .graph_run import GraphRun

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
        self.kernel=None;self.kernel_factory=kernel_factory or docker_kernel;self.kernel_lock=threading.Lock()
        # One graph document in memory (the browser opens/downloads .ipymb files) and at most one run.
        self.graph_doc=graph_module.new_document();self.graph_revision=0;self.graph_run=None;self.graph_lock=threading.RLock()
        self.env_build=None;self.image_ready=lab_environment.image_ready
        super().__init__(address,LabHandler)
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
            return self.graph_view()
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
            check['mode']=mode;check['session_alive']=bool(kernel is not None and kernel.alive)
            return plan,check
    def start_graph_run(self,mode,nodes,include_prerequisites):
        kernel=self.kernel
        if kernel is None or not kernel.alive:raise KernelError('Start a session before running the graph')
        with self.graph_lock:
            if self.graph_running():raise KernelError('A graph run is already in progress')
            if kernel.executing:raise KernelError('A cell is already running; interrupt it or wait')
            plan,check=self.graph_plan(mode,nodes,include_prerequisites)
            if not plan:raise graph_module.GraphError('Nothing to run: no code nodes are reachable in this plan')
            doc=self.graph_doc
            def record(ident,result):
                with self.graph_lock:
                    graph_module.record_result(doc,ident,result)
            self.graph_run=GraphRun(doc,plan,kernel,mode=mode,on_result=record).start()
            return self.graph_run.snapshot()
    def kernel_status(self):
        kernel=self.kernel
        if kernel is None: return {'alive':False,'executing':False,'label':None,'count':0,'python':None,'started_at':None,'ended':None}
        return kernel.status()
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
    def start_kernel(self,settings):
        with self.kernel_lock:
            if self.kernel is not None and self.kernel.alive: raise KernelError('A session is already running')
            env=self.environment_status()
            if env['derived']:
                if not env['ready']:raise KernelError('This Mainbook declares packages (%s) whose image %s is not built yet. Build the environment first.'%(', '.join(env['packages']),env['image']))
                settings=dict(settings,image=env['image'])
            kernel=self.kernel_factory(settings,self.state_dir)
            kernel.start()
            self.kernel=kernel
            return kernel.status()
    def stop_kernel(self):
        with self.kernel_lock:
            kernel=self.kernel
            if kernel is None: return self.kernel_status()
            kernel.stop()
            return kernel.status()
    def server_close(self):
        try: self.stop_kernel()
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
        assets={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/setup.js':('setup.js','text/javascript; charset=utf-8'),'/notebook.js':('notebook.js','text/javascript; charset=utf-8'),'/session.js':('session.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8'),'/graph':('graph.html','text/html; charset=utf-8'),'/graph.js':('graph.js','text/javascript; charset=utf-8'),'/graph.css':('graph.css','text/css; charset=utf-8'),'/models':('models.html','text/html; charset=utf-8'),'/models.js':('models.js','text/javascript; charset=utf-8')}
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
            path=urlsplit(self.path).path;data=self.body(limit=4000000 if path in ('/api/graph','/api/graph/import') else 600000 if path=='/api/models/chat' else 25000)
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
                if self.server.kernel_status()['alive']:return self.out(409,{'error':'Stop the running session before changing the folder or limits; a session keeps the settings it started with.'})
                with self.server.lock:self.server.settings=settings;self.server.revision+=1
                return self.out(200,{'ok':True})
            if path=='/api/kernel/start':
                if set(data)!={'confirm','revision'} or data['confirm'] is not True or type(data['revision']) is not int:raise ValueError('Explicit confirmation is required')
                with self.server.lock:
                    if data['revision']!=self.server.revision:return self.out(409,{'error':'Settings changed after review. Review the current settings again.'})
                    settings=dict(self.server.settings)
                try:return self.out(200,self.server.start_kernel(settings))
                except KernelError as e:return self.out(409,{'error':str(e)})
            if path=='/api/kernel/stop':
                return self.out(200,self.server.stop_kernel())
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
