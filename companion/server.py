"""Local owner UI; no website CORS, cloud JWT validation, or code execution."""
from __future__ import annotations

import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from .permissions import Denied

ASSETS = Path(__file__).parent / 'ui'


class OwnerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, container_mode=False):
        if address[0] != '127.0.0.1' and not (container_mode and address[0] == '0.0.0.0'):
            raise ValueError('Only local loopback or explicit container mode is supported')
        self.store, self.sessions, self.failures = store, {}, []
        self.lock = threading.RLock()
        super().__init__(address, OwnerHandler)


class OwnerHandler(BaseHTTPRequestHandler):
    server_version = 'MainAIgerLocal/0.1'

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def output(self, status, obj=None, raw=None, kind='application/json; charset=utf-8', cookie=None):
        payload = raw if raw is not None else json.dumps(obj, ensure_ascii=True, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionError):
            pass

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get('Host') in {f'127.0.0.1:{port}', f'localhost:{port}'}

    def same_origin(self):
        return self.headers.get('Origin') == 'http://' + self.headers.get('Host', '')

    def body(self):
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('Expected bounded JSON')
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 8192:
            raise ValueError('Request exceeds 8 KiB')
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError('Incomplete request')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        return data

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return None
        value = cookie.get('mai_local_owner')
        with self.server.lock:
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v['expires'] > time.time()}
            return self.server.sessions.get(value.value) if value else None

    def do_OPTIONS(self):
        self.output(403, {'error': 'Cross-origin browser connections are disabled'})

    def do_GET(self):
        if not self.valid_host():
            return self.output(403, {'error': 'Unexpected Host'})
        path = urlsplit(self.path).path
        assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if path in assets:
            filename, kind = assets[path]
            return self.output(200, raw=(ASSETS / filename).read_bytes(), kind=kind)
        if path == '/api/owner/state':
            session = self.session()
            if not session:
                return self.output(401, {'error': 'Local owner sign-in required'})
            return self.output(200, {**self.server.store.snapshot(), 'csrf': session['csrf']})
        self.output(404, {'error': 'Not found'})

    def do_POST(self):
        if not self.valid_host():
            return self.output(403, {'error': 'Unexpected Host'})
        path = urlsplit(self.path).path
        try:
            if path == '/api/resource':
                if self.headers.get('Origin') is not None or self.headers.get('Cookie') is not None:
                    raise Denied('Resource API accepts local non-browser clients only')
                authorization = self.headers.get('Authorization', '')
                token = authorization[7:] if authorization.startswith('Bearer ') else ''
                return self.output(200, self.server.store.dispatch(token, self.body()))
            if not self.same_origin():
                raise Denied('Same-origin owner request required')
            data = self.body()
            if path == '/api/owner/login':
                if set(data) != {'key'} or not isinstance(data['key'], str):
                    raise ValueError('Local owner key required')
                with self.server.lock:
                    self.server.failures = [t for t in self.server.failures if time.time() - t < 60]
                    if len(self.server.failures) >= 5:
                        return self.output(429, {'error': 'Retry owner sign-in in one minute'})
                    if not hmac.compare_digest(data['key'], self.server.store.owner_key):
                        self.server.failures.append(time.time())
                        raise Denied('Invalid local owner key')
                    self.server.sessions = {k: v for k, v in self.server.sessions.items() if v['expires'] > time.time()}
                    if len(self.server.sessions) >= 20:
                        raise Denied('Owner session limit reached; sign out an existing session')
                    session_id = secrets.token_urlsafe(32)
                    session = {'csrf': secrets.token_urlsafe(32), 'expires': time.time() + 3600}
                    self.server.sessions[session_id] = session
                return self.output(200, {'csrf': session['csrf']}, cookie=f'mai_local_owner={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600')
            session = self.session()
            if not session or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
                raise Denied('Owner session and CSRF token required')
            if path == '/api/owner/approve':
                expected = {'requester', 'space', 'resource_id', 'operations', 'hours', 'request_id'}
                if set(data) != expected:
                    raise ValueError('Review the exact local grant fields')
                return self.output(201, self.server.store.approve(**data))
            if path == '/api/owner/revoke':
                if set(data) != {'id'}:
                    raise ValueError('Grant ID required')
                self.server.store.revoke(data['id'])
                return self.output(200, {'revoked': True})
            if path == '/api/owner/logout':
                if data:
                    raise ValueError('Logout takes no fields')
                with self.server.lock:
                    self.server.sessions = {k: v for k, v in self.server.sessions.items() if v is not session}
                return self.output(200, {'signed_out': True}, cookie='mai_local_owner=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
            return self.output(404, {'error': 'Not found'})
        except Denied as exc:
            return self.output(403, {'error': str(exc)})
        except (ValueError, TypeError, UnicodeError, KeyError):
            return self.output(400, {'error': 'Invalid request; check the selected local permission fields'})
        except Exception:
            return self.output(500, {'error': 'Local operation failed'})
