"""Real local permission and HTTP boundary checks; no cloud or private device required."""
import http.client
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest

from companion.permissions import Denied, MAX_READ, PermissionStore
from companion.server import OwnerServer


class LocalPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / 'workspace'
        self.work.mkdir()
        (self.work / 'note.txt').write_text('local content')
        self.now = [1000.0]
        self.store = PermissionStore(self.root / 'state', clock=lambda: self.now[0])
        self.store.register('one', 'My selected folder', self.work)
        self.token = self.store.approve('person-one', 'space-one', 'one', ['resource.info', 'files.list', 'files.read'], 1, 'request-one')['token']

    def tearDown(self):
        self.temp.cleanup()

    def call(self, operation, path='', token=None, resource='one', **extra):
        return self.store.dispatch(token or self.token, {'resource_id': resource, 'operation': operation, 'path': path, **extra})

    def test_read_and_list(self):
        self.assertEqual(self.call('files.read', 'note.txt')['text'], 'local content')
        self.assertEqual(self.call('files.list')['entries'], [{'name':'note.txt', 'kind':'file'}])

    def test_no_cloud_status_or_jwt_authority(self):
        for token in ['accepted_pending_device', 'eyJhbGciOiJSUzI1NiJ9.payload.signature', self.store.owner_key, '']:
            with self.subTest(token=token[:8]), self.assertRaises(Denied):
                self.store.dispatch(token, {'resource_id':'one', 'operation':'resource.info'})
        with self.assertRaises(Denied):
            self.call('resource.info', status='accepted_pending_device')

    def test_expiry_and_revocation(self):
        self.now[0] = 4600.0
        with self.assertRaises(Denied): self.call('files.read', 'note.txt')
        self.now[0] = 1000.0
        gid = self.store.snapshot()['grants'][0]['id']
        self.store.revoke(gid)
        with self.assertRaises(Denied): self.call('files.read', 'note.txt')

    def test_scope_and_resource_isolation(self):
        other = self.root / 'other'; other.mkdir(); (other / 'secret').write_text('never')
        self.store.register('two', 'Second folder', other)
        with self.assertRaises(Denied): self.call('files.read', 'secret', resource='two')
        limited = self.store.approve('person-two','space-two','one',['resource.info'],1)['token']
        self.assertEqual(self.call('resource.info', token=limited)['id'], 'one')
        with self.assertRaises(Denied): self.call('files.read', 'note.txt', token=limited)
        grants = self.store.snapshot()['grants']
        self.store.revoke(next(g['id'] for g in grants if g['requester'] == 'person-two'))
        self.assertEqual(self.call('files.read', 'note.txt')['text'], 'local content')

    def test_no_execution_or_mutation_operations(self):
        for op in ['terminal.exec','notebook.run','files.write','model.infer','os.system','runs.start']:
            with self.subTest(op=op), self.assertRaises(Denied): self.call(op)
            with self.assertRaises(ValueError): self.store.approve('x','y','one',[op],1)
        with self.assertRaises(Denied): self.call('files.read','note.txt',command='id')

    def test_traversal_and_absolute_paths(self):
        for path in ['../state/owner.key','/etc/passwd','x/../../note.txt','./note.txt','x//note.txt','x\\note.txt','x\x00y']:
            with self.subTest(path=path), self.assertRaises(Denied): self.call('files.read', path)

    def test_symlink_and_hardlink_refused(self):
        secret = self.root / 'secret.txt'; secret.write_text('outside')
        (self.work / 'link').symlink_to(secret)
        (self.work / 'directory-link').symlink_to(self.root / 'state', target_is_directory=True)
        os.link(secret, self.work / 'hardlink')
        for path in ['link','directory-link/owner.key','hardlink']:
            with self.subTest(path=path), self.assertRaises(Denied): self.call('files.read',path)
        self.assertEqual(self.call('files.list')['entries'], [{'name':'note.txt','kind':'file'}])

    def test_symlink_root_and_root_change_refused(self):
        alias = self.root / 'alias'; alias.symlink_to(self.work, target_is_directory=True)
        with self.assertRaises(OSError): self.store.register('alias','Alias',alias)
        other = self.root / 'other'; other.mkdir()
        with self.assertRaises(ValueError): self.store.register('one','Changed',other)

    def test_private_state_cannot_be_shared(self):
        for root in [self.root, self.root / 'state', Path('/')]:
            with self.subTest(root=str(root)), self.assertRaises(ValueError): self.store.register('unsafe','Unsafe',root)

    def test_state_ancestor_alias_cannot_bypass_workspace_exclusion(self):
        alias = self.root / 'workspace-alias'
        alias.symlink_to(self.work, target_is_directory=True)
        aliased_store = PermissionStore(alias / 'private-state')
        self.assertEqual(aliased_store.path, (self.work / 'private-state').resolve())  # real path; macOS temp dirs live under /private/var
        with self.assertRaises(ValueError):
            aliased_store.register('unsafe', 'Workspace containing state', self.work)
        self.assertEqual(aliased_store.snapshot()['resources'], [])

    def test_state_leaf_symlink_is_rejected(self):
        alias = self.root / 'state-alias'
        alias.symlink_to(self.root / 'state', target_is_directory=True)
        with self.assertRaises(ValueError):
            PermissionStore(alias)

    def test_nonregular_binary_and_large_files_refused(self):
        os.mkfifo(self.work / 'pipe')
        (self.work / 'binary').write_bytes(b'\xff\xfe')
        (self.work / 'large').write_bytes(b'x' * (MAX_READ + 1))
        for path in ['pipe','binary','large']:
            with self.subTest(path=path), self.assertRaises(Denied): self.call('files.read',path)

    def test_hash_only_and_private_modes_and_receipt_binding(self):
        self.assertNotIn(self.token.encode(), self.store.dbpath.read_bytes())
        for path in [self.store.dbpath,self.store.owner_path]: self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o700)
        self.assertNotIn('token_hash', self.store.snapshot()['grants'][0])
        grant = self.store.approve('recipient','space-id','one',['resource.info'],1,'request-id')
        self.assertEqual((grant['request_id'],grant['space_id'],grant['requester_id']), ('request-id','space-id','recipient'))
        self.assertNotIn('root', self.call('resource.info'))

    def test_expiry_constraints(self):
        for hours in [0,-1,169,True,float('inf'),float('nan')]:
            with self.subTest(hours=hours), self.assertRaises(ValueError): self.store.approve('x','y','one',['resource.info'],hours)


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        LocalPermissionsTests.setUp(self)
        self.server = OwnerServer(('127.0.0.1',0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        LocalPermissionsTests.tearDown(self)

    def request(self, path, body=None, headers=None, method=None):
        client = http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        try:
            raw = json.dumps(body).encode() if body is not None else None
            request_headers = {'Content-Type':'application/json', **(headers or {})}
            client.request(method or ('POST' if body is not None else 'GET'),path,body=raw,headers=request_headers)
            reply = client.getresponse(); data = reply.read(); result_headers = dict(reply.getheaders())
            return reply.status,json.loads(data) if result_headers.get('Content-Type','').startswith('application/json') else data,result_headers
        finally: client.close()

    def login(self):
        status,result,headers = self.request('/api/owner/login',{'key':self.store.owner_key},{'Origin':self.origin})
        self.assertEqual(status,200)
        return {'Origin':self.origin,'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':result['csrf']}

    def test_owner_requires_origin_session_csrf(self):
        body = {'requester':'x','space':'y','resource_id':'one','operations':['resource.info'],'hours':1,'request_id':'manual'}
        self.assertEqual(self.request('/api/owner/approve',body,{'Origin':self.origin})[0],403)
        self.assertEqual(self.request('/api/owner/login',{'key':self.store.owner_key},{'Origin':'https://mainaiger.org'})[0],403)
        headers = self.login()
        without_csrf = {k:v for k,v in headers.items() if k != 'X-CSRF-Token'}
        self.assertEqual(self.request('/api/owner/approve',body,without_csrf)[0],403)
        status,receipt,_ = self.request('/api/owner/approve',body,headers)
        self.assertEqual(status,201)
        self.assertIn('token',receipt)
        self.assertEqual(self.request('/api/owner/revoke',{'id':receipt['id']},headers)[0],200)
        self.assertEqual(self.request('/api/owner/logout',{},headers)[0],200)
        self.assertEqual(self.request('/api/owner/state',headers=headers)[0],401)

    def test_browser_closed_and_bearer_local_api(self):
        body = {'resource_id':'one','operation':'files.read','path':'note.txt'}
        auth = {'Authorization':'Bearer '+self.token}
        self.assertEqual(self.request('/api/resource',body,auth)[1]['text'],'local content')
        for extra in [{'Origin':self.origin},{'Origin':'https://mainaiger.org'},{'Cookie':'anything=1'}]:
            self.assertEqual(self.request('/api/resource',body,{**auth,**extra})[0],403)
        self.assertEqual(self.request('/api/resource',body,{'Authorization':'Bearer cloud-jwt'})[0],403)
        self.assertEqual(self.request('/api/resource',method='OPTIONS')[0],403)

    def test_host_allowlist_and_no_cors(self):
        self.assertEqual(self.request('/',headers={'Host':'attacker.example'})[0],403)
        status,_,headers = self.request('/')
        self.assertEqual(status,200)
        self.assertNotIn('Access-Control-Allow-Origin',headers)
        self.assertEqual(headers['Cache-Control'],'no-store')
        with self.assertRaises(ValueError): OwnerServer(('0.0.0.0',0),self.store)


if __name__ == '__main__':
    unittest.main()
