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

ASSETS=Path(__file__).parent/'ui'

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
        super().__init__(address,LabHandler)
    def kernel_status(self):
        kernel=self.kernel
        if kernel is None: return {'alive':False,'executing':False,'label':None,'count':0,'python':None,'started_at':None,'ended':None}
        return kernel.status()
    def start_kernel(self,settings):
        with self.kernel_lock:
            if self.kernel is not None and self.kernel.alive: raise KernelError('A session is already running')
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
    def start_setup_job(self, action):
        with self.lock:
            if self.setup_running:return False
            self.setup_running=True
            self.setup_job={'running':True,'action':action,'phase':'starting','bytes':0,'total':None,'result':None}
        def progress(data):
            with self.lock:
                for key in ('phase','bytes','total'):
                    if key in data:self.setup_job[key]=data[key]
        def worker():
            try:
                if action=='download': result=lab_setup.download_installer(self.state_dir,progress)
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
    def body(self):
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('Expected JSON')
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=25000: raise ValueError('Request too large')
        data=json.loads(self.rfile.read(size))
        if not isinstance(data,dict):raise ValueError('Expected JSON object')
        return data
    def do_OPTIONS(self):self.out(403,{'error':'Cross-origin requests are disabled'})
    def do_GET(self):
        if not self.host_ok():return self.out(403,{'error':'Unexpected host'})
        path=urlsplit(self.path).path
        assets={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/setup.js':('setup.js','text/javascript; charset=utf-8'),'/notebook.js':('notebook.js','text/javascript; charset=utf-8'),'/session.js':('session.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
        if path in assets:
            name,kind=assets[path];return self.out(200,raw=(ASSETS/name).read_bytes(),kind=kind)
        if path=='/api/state':
            session=self.session()
            if not session:return self.out(401,{'error':'Local owner sign-in required'})
            with self.server.lock: settings=dict(self.server.settings);revision=self.server.revision
            available=bool(lab_setup._find_docker()) if sys.platform=='darwin' else bool(shutil.which('docker'))
            return self.out(200,{'csrf':session['csrf'],'workspace':settings['workspace'],'limits':{'cpus':settings['cpus'],'memory':settings['memory']},'docker_available':available,'command':preview(settings),'session_command':session_preview(settings),'revision':revision,'session':self.server.kernel_status()})
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
            path=urlsplit(self.path).path;data=self.body()
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
