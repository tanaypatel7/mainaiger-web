"""Small local capability store and POSIX descriptor-confined read operations."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import uuid

OPERATIONS = frozenset({'resource.info', 'files.list', 'files.read'})
MAX_READ = 256 * 1024
TOKEN_RE = re.compile(r'mai_cap_[A-Za-z0-9_-]{43}\Z')


class Denied(ValueError):
    pass


def label(value, maximum=160):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('Provide a nonempty label without control characters')
    return value.strip()


def private_file(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError('State files must be owner-owned regular files with mode 0600')


def open_root(root):
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise RuntimeError('Confined file access requires POSIX no-follow directory descriptors')
    path = Path(root)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Workspace must be an absolute directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def relative_parts(value):
    if not isinstance(value, str) or len(value) > 1024 or value.startswith('/') or '\\' in value or '\x00' in value:
        raise Denied('Invalid relative path')
    if value == '':
        return []
    parts = value.split('/')
    if any(p in ('', '.', '..') or any(ord(c) < 32 for c in p) for p in parts):
        raise Denied('Invalid relative path')
    return parts


def read_resource(root, operation, relative_path=''):
    parts = relative_parts(relative_path)
    fd = open_root(root)
    try:
        directories = parts[:-1] if operation == 'files.read' else parts
        for part in directories:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        if operation == 'files.list':
            entries = []
            # No symlink following, recursion, hidden automatic scan or unlimited response.
            with os.scandir(fd) as it:
                for scanned, entry in enumerate(it, 1):
                    info = entry.stat(follow_symlinks=False)
                    kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else None
                    if kind is not None:
                        entries.append({'name': entry.name, 'kind': kind})
                    if scanned == 200:
                        return {'entries': sorted(entries, key=lambda x: x['name']), 'possibly_truncated': True}
            return {'entries': sorted(entries, key=lambda x: x['name']), 'possibly_truncated': False}
        if operation == 'files.read' and parts:
            leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(leaf)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_READ:
                    raise Denied('Only single-link regular text files up to 256 KiB are readable')
                chunks, remaining = [], MAX_READ + 1
                while remaining:
                    chunk = os.read(leaf, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b''.join(chunks)
                if len(raw) > MAX_READ:
                    raise Denied('File exceeds the read limit')
                return {'path': relative_path, 'text': raw.decode('utf-8'), 'bytes': len(raw)}
            finally:
                os.close(leaf)
        raise Denied('Unsupported operation or missing file path')
    finally:
        os.close(fd)


class PermissionStore:
    def __init__(self, state_dir, clock=time.time):
        self.path = Path(state_dir).expanduser().absolute()
        self.clock = clock
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('State directory must be private, owner-owned and mode 0700')
        # Parent aliases must not conceal state beneath an offered workspace.
        # Preserve rejection of a symlink at the state directory itself above.
        self.path = self.path.resolve(strict=True)
        self.dbpath = self.path / 'permissions.sqlite3'
        if not self.dbpath.exists() and not self.dbpath.is_symlink():
            os.close(os.open(self.dbpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
        private_file(self.dbpath)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY, label TEXT NOT NULL, root TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS grants(
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, requester TEXT NOT NULL, space TEXT NOT NULL,
                    resource_id TEXT NOT NULL REFERENCES resources(id), operations TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, revoked REAL,
                    token_hash TEXT NOT NULL UNIQUE);
            ''')
        self.owner_path = self.path / 'owner.key'
        if not self.owner_path.exists() and not self.owner_path.is_symlink():
            fd = os.open(self.owner_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as out:
                out.write('mai_owner_' + secrets.token_urlsafe(32) + '\n')
        private_file(self.owner_path)
        self.owner_key = self.owner_path.read_text().strip()
        if not re.fullmatch(r'mai_owner_[A-Za-z0-9_-]{43}', self.owner_key):
            raise ValueError('Invalid owner key')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.dbpath, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, resource_id, name, root):
        resource_id, name = label(resource_id, 80), label(name)
        # The root itself must not be a symlink (an alias would hide the real
        # target from the owner), but its ancestors are resolved once: macOS keeps
        # /var, /tmp and /etc as symlinks into /private, and the stored real path
        # is walked with O_NOFOLLOW on every access so later symlink swaps are refused.
        chosen = Path(root).expanduser().absolute()
        if chosen.is_symlink():
            raise OSError('Workspace root must be a real directory, not a symlink')
        root = str(chosen.parent.resolve(strict=True) / chosen.name)
        root_path = Path(root)
        if root_path == Path('/') or root_path == self.path or root_path in self.path.parents or self.path in root_path.parents:
            raise ValueError('Workspace and private state must be separate; sharing a filesystem root is refused')
        fd = open_root(root)
        os.close(fd)
        with self.db() as db:
            old = db.execute('SELECT root FROM resources WHERE id=?', (resource_id,)).fetchone()
            if old and old['root'] != root:
                raise ValueError('Resource roots are immutable; use a new resource ID')
            taken = db.execute('SELECT id FROM resources WHERE root=? AND id<>?', (root, resource_id)).fetchone()
            if taken:
                raise ValueError('This folder is already registered as resource ' + taken['id'])
            db.execute('INSERT INTO resources VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label', (resource_id, name, root))

    def snapshot(self):
        with self.db() as db:
            resources = [dict(r) for r in db.execute('SELECT id,label,root FROM resources ORDER BY id')]
            grants = [dict(r) for r in db.execute('SELECT id,request_id,requester,space,resource_id,operations,created,expires,revoked FROM grants ORDER BY created DESC')]
        for grant in grants:
            grant['operations'] = json.loads(grant['operations'])
            grant['state'] = 'revoked' if grant['revoked'] is not None else 'expired' if grant['expires'] <= self.clock() else 'active'
        return {'resources': resources, 'grants': grants, 'supported_operations': sorted(OPERATIONS)}

    def approve(self, requester, space, resource_id, operations, hours, request_id='manual'):
        requester, space, resource_id = label(requester), label(space), label(resource_id, 80)
        request_id = label(request_id, 160)
        if not isinstance(operations, list) or not operations or any(not isinstance(v, str) or v not in OPERATIONS for v in operations):
            raise ValueError('Choose supported read-only operations')
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 168:
            raise ValueError('Expiry must be greater than zero and at most 168 hours')
        token, gid = 'mai_cap_' + secrets.token_urlsafe(32), uuid.uuid4().hex
        stamp = self.clock()
        with self.db() as db:
            if not db.execute('SELECT 1 FROM resources WHERE id=?', (resource_id,)).fetchone():
                raise ValueError('Select a locally registered resource')
            db.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,?,?)', (gid, request_id, requester, space, resource_id, json.dumps(sorted(set(operations))), stamp, stamp + hours * 3600, None, hashlib.sha256(token.encode()).hexdigest()))
        return {'id': gid, 'request_id': request_id, 'requester_id': requester, 'space_id': space, 'resource_id': resource_id, 'operations': sorted(set(operations)), 'token': token, 'expires': stamp + hours * 3600}

    def revoke(self, grant_id):
        with self.db() as db:
            row = db.execute('UPDATE grants SET revoked=COALESCE(revoked,?) WHERE id=?', (self.clock(), label(grant_id, 80)))
            if row.rowcount != 1:
                raise ValueError('Unknown grant')

    def authorize(self, token, resource_id, operation):
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token) or operation not in OPERATIONS:
            raise Denied('Local capability required')
        with self.db() as db:
            grant = db.execute('SELECT g.*,r.root,r.label FROM grants g JOIN resources r ON r.id=g.resource_id WHERE g.token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not grant or grant['revoked'] is not None or grant['expires'] <= self.clock() or grant['resource_id'] != resource_id or operation not in json.loads(grant['operations']):
            raise Denied('Capability does not allow this operation')
        return dict(grant)

    def dispatch(self, token, request):
        if not isinstance(request, dict) or set(request) - {'resource_id', 'operation', 'path'}:
            raise Denied('Only resource_id, operation and relative path are accepted')
        resource_id, operation = request.get('resource_id'), request.get('operation')
        if not isinstance(resource_id, str) or not isinstance(operation, str):
            raise Denied('Resource and operation are required')
        grant = self.authorize(token, resource_id, operation)
        if operation == 'resource.info':
            if request.get('path', '') != '':
                raise Denied('Resource info takes no path')
            return {'id': resource_id, 'label': grant['label'], 'operations': json.loads(grant['operations']), 'expires': grant['expires']}
        try:
            return read_resource(grant['root'], operation, request.get('path', ''))
        except (OSError, UnicodeError) as exc:
            raise Denied('Path is unavailable or is not permitted UTF-8 text') from exc
