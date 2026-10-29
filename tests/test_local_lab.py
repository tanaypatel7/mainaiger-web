import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from local_lab import setup
from local_lab.runner import command, validate_settings, execute
from local_lab.server import LabServer, initialize_state

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.workspace=self.root/'files';self.workspace.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def test_default_isolated_invocation(self):
        settings=validate_settings({'workspace':'','cpus':'0.5','memory':'256m'},self.state)
        args=command(settings,'test')
        for flag in ['--network=none','--read-only','--cap-drop=ALL','--pull=never','--user=65534:65534','--memory=256m','--cpus=0.5']:
            self.assertIn(flag,args)
        self.assertNotIn('--mount',args)
        self.assertEqual(args[-4:],['python:3.13.18-slim','python','-I','-'])
    def test_only_explicit_folder_read_only(self):
        settings=validate_settings({'workspace':str(self.workspace),'cpus':'1','memory':'512m'},self.state)
        args=command(settings,'test')
        self.assertIn('type=bind,src='+str(self.workspace.resolve())+',dst=/workspace,readonly',args)  # mounts use the real path
        self.assertNotIn('/var/run/docker.sock',' '.join(args))
    def test_reject_injection_state_and_unsafe_limits(self):
        base={'workspace':str(self.workspace),'cpus':'1','memory':'512m'}
        for change in ({'workspace':'/'},{'workspace':str(self.root)},{'workspace':str(self.state)},{'workspace':str(self.workspace)+',rw'},{'cpus':'--privileged'},{'memory':'8g'},{'extra':'x'}):
            with self.subTest(change=change),self.assertRaises(ValueError):validate_settings({**base,**change},self.state)
    def test_missing_docker_does_not_run_host_python(self):
        # On a Mac execute() consults Docker Desktop instead of PATH; patch both so the test is platform-neutral.
        with patch.object(setup,'_local_docker',return_value=(None,'Docker was not found. Install and start Docker Desktop first.')):
            result=execute('print(1)',{'workspace':None,'cpus':'1','memory':'512m'},find=lambda _:None,popen=lambda *a,**k:self.fail('host process launched'))
        self.assertIn('Docker was not found',result['error'])
    def test_code_length_and_empty(self):
        for text in ['', 'x'*20001]:
            with self.assertRaises(ValueError):execute(text,{},find=lambda _:None)
    def test_stalled_docker_stdin_is_bounded_by_timeout(self):
        import local_lab.runner as runner
        timeout=runner.TIMEOUT;runner.TIMEOUT=0.2
        try:
            start=time.monotonic()
            def stalled(_args,**kwargs):return subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],**kwargs)
            with patch.object(setup,'_local_docker',return_value=('docker','verified')):
                result=execute('x'*19000,{'workspace':None,'cpus':'1','memory':'512m'},popen=stalled,run=lambda *a,**k:None,find=lambda _:True)
            self.assertLess(time.monotonic()-start,4)
            self.assertIn('Cleanup was attempted',result['output'])
        finally:runner.TIMEOUT=timeout

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();state,key=initialize_state(Path(self.tmp.name)/'private')
        self.key=key;self.calls=[]
        self.server=LabServer(('127.0.0.1',0),state,key,executor=lambda c,s:self.calls.append((c,s)) or {'output':'ok','exit_code':0,'error':None})
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_port;self.origin=f'http://127.0.0.1:{self.port}'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def req(self,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        h={'Host':f'127.0.0.1:{self.port}'};h.update(headers or {})
        if body is not None:h['Content-Type']='application/json'
        c.request('POST' if body is not None else 'GET',path,body=json.dumps(body) if body is not None else None,headers=h)
        r=c.getresponse();payload=r.read();head=dict(r.getheaders());c.close();return r.status,json.loads(payload),head
    def login(self):
        status,data,head=self.req('/api/login',{'key':self.key},{'Origin':self.origin})
        self.assertEqual(status,200);return {'Origin':self.origin,'Cookie':head['Set-Cookie'].split(';')[0],'X-CSRF-Token':data['csrf']}
    def test_no_cloud_or_browser_cross_origin_authority(self):
        self.assertEqual(self.req('/api/state')[0],401)
        self.assertEqual(self.req('/api/login',{'key':self.key},{'Origin':'https://mainaiger.org'})[0],403)
        self.assertEqual(self.req('/api/login',{'key':'cloud-token'},{'Origin':self.origin})[0],403)
        self.assertEqual(self.req('/api/run',{'code':'print(1)','confirm':True},{'Origin':'https://mainaiger.org'})[0],403)
        self.assertFalse(self.calls)
    def test_run_needs_session_csrf_and_explicit_confirmation(self):
        auth=self.login();self.assertEqual(self.req('/api/run',{'code':'print(1)','confirm':False,'revision':0},auth)[0],400)
        self.assertEqual(self.req('/api/run',{'code':'print(1)','confirm':True,'revision':0},{'Origin':self.origin,'Cookie':auth['Cookie']})[0],403)
        self.assertEqual(self.req('/api/run',{'code':'print(1)','confirm':True,'revision':0},auth)[0],200)
        self.assertEqual(self.calls[0][1]['workspace'],None)
        self.assertEqual(self.req('/api/settings',{'workspace':'','cpus':'1','memory':'512m'},auth)[0],200)
        self.assertEqual(self.req('/api/run',{'code':'print(1)','confirm':True,'revision':0},auth)[0],409)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.req('/api/logout',{},auth)[0],200)
        self.assertEqual(self.req('/api/run',{'code':'print(1)','confirm':True,'revision':1},auth)[0],403)
    def test_no_token_in_url_or_global_data(self):
        auth=self.login();status,data,_=self.req('/api/state',headers={'Cookie':auth['Cookie']})
        self.assertEqual(status,200);self.assertNotIn(self.key,json.dumps(data))
        self.assertEqual(data['limits'],{'cpus':'0.5','memory':'256m'})

if __name__=='__main__':unittest.main()
