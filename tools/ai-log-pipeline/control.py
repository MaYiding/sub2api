"""Administrator UI and source-scoped heartbeat API, separate from log queries."""
from collections import defaultdict, deque
from contextlib import closing
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from common import load_config
from registry import Conflict, Registry, digest


def create_app(config=None):
    config = config or load_config(os.environ['AI_LOG_CONTROL_CONFIG'])
    registry = Registry(config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry
    networks = [ipaddress.ip_network(value) for value in config['admin_networks']]
    proxy_ips = set(config.get('trusted_proxy_ips', []))
    attempts = defaultdict(deque)
    assets = Path(__file__).parent / 'control_assets'

    def address(request):
        host = request.client.host
        if host in proxy_ips:
            host = request.headers.get('x-real-ip', '')
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            raise HTTPException(403, 'Access denied') from None

    def internal(request):
        ip = address(request)
        if not any(ip in network for network in networks):
            raise HTTPException(403, 'Administrator access requires the private network')

    def csrf_for(token):
        return hmac.new(config['session_secret'].encode(), token.encode(), hashlib.sha256).hexdigest()

    def admin(request, mutation=False):
        internal(request)
        authorization = request.headers.get('authorization', '')
        if authorization:
            if authorization.startswith('Bearer ') and len(authorization) < 4096 and hmac.compare_digest(digest(authorization[7:]), config['admin_token_hash']):
                return 'api-admin'
            raise HTTPException(401, 'Administrator authentication required')
        token = request.cookies.get('__Host-ai-log-admin', '')
        if not token or len(token) > 512:
            raise HTTPException(401, 'Administrator authentication required')
        with closing(registry.connect()) as db:
            session = db.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?', (digest(token), int(time.time()))).fetchone()
        if not session:
            raise HTTPException(401, 'Administrator authentication required')
        if mutation:
            if request.headers.get('origin') != config['public_origin'] or not hmac.compare_digest(digest(request.headers.get('x-csrf-token', '')), session['csrf_hash']):
                raise HTTPException(403, 'CSRF validation failed')
        return 'browser-admin'

    async def body(request):
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise HTTPException(415, 'JSON required')
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 16384:
                raise HTTPException(413, 'Request too large')
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'Invalid JSON object') from None

    @app.middleware('http')
    async def secure_headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(Conflict)
    async def conflict(_request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_request, _error):
        return JSONResponse({'detail': 'Invalid source configuration'}, status_code=400)

    @app.get('/control/healthz')
    def health(request: Request):
        internal(request)
        with closing(registry.connect()) as db:
            db.execute('SELECT 1 FROM sources LIMIT 1').fetchall()
        return {'ok': True}

    @app.get('/admin/sources')
    def page(request: Request):
        internal(request)
        return FileResponse(assets / 'index.html')

    @app.get('/admin/assets/{name}')
    def asset(name: str, request: Request):
        internal(request)
        if name not in ('app.js', 'app.css'):
            raise HTTPException(404)
        return FileResponse(assets / name)

    @app.post('/control/v1/login')
    async def login(request: Request):
        internal(request)
        if request.headers.get('origin') != config['public_origin']:
            raise HTTPException(403, 'Origin validation failed')
        now = time.monotonic()
        for bucket, limit in [('global', 30), (str(address(request)), 8)]:
            times = attempts[bucket]
            while times and times[0] < now-60:
                times.popleft()
            if len(times) >= limit:
                raise HTTPException(429, 'Try again later')
            times.append(now)
        if len(attempts) > 4096:
            for key in list(attempts):
                if key != 'global' and (not attempts[key] or attempts[key][-1] < now-60):
                    del attempts[key]
        value = await body(request)
        password = value.get('password', '')
        if not isinstance(password, str) or len(password) > 512:
            raise HTTPException(401, 'Invalid credentials')
        computed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(config['admin_password_salt']), n=16384, r=8, p=1).hex()
        if value.get('username') != config.get('admin_username', 'admin') or not hmac.compare_digest(computed, config['admin_password_hash']):
            raise HTTPException(401, 'Invalid credentials')
        token = secrets.token_urlsafe(36)
        csrf = csrf_for(token)
        with registry.transaction() as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), digest(csrf), int(time.time())+28800))
            registry.audit(db, 'browser-admin', 'login', 'session')
        response = JSONResponse({'ok': True, 'csrf': csrf})
        response.set_cookie('__Host-ai-log-admin', token, max_age=28800, secure=True, httponly=True, samesite='strict', path='/')
        return response

    @app.get('/control/v1/session')
    def session(request: Request):
        admin(request)
        return {'ok': True, 'csrf': csrf_for(request.cookies.get('__Host-ai-log-admin', ''))}

    @app.post('/control/v1/logout')
    def logout(request: Request):
        admin(request, True)
        with registry.transaction() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(request.cookies.get('__Host-ai-log-admin', '')),))
        response = JSONResponse({'ok': True})
        response.delete_cookie('__Host-ai-log-admin', path='/', secure=True, httponly=True, samesite='strict')
        return response

    @app.get('/control/v1/sources')
    def sources(request: Request):
        admin(request)
        return {'sources': registry.sources(), 'raw_budget_gib': config.get('raw_budget_gib', 384), 'server_time': int(time.time())}

    @app.post('/control/v1/sources')
    async def create(request: Request):
        actor = admin(request, True)
        result = registry.mutate('create', await body(request), request.headers.get('idempotency-key'), actor=actor)
        return JSONResponse(result, status_code=202)

    @app.post('/control/v1/sources/{source_id}/bundle')
    def bundle(source_id: str, request: Request, credential_id: str | None = None):
        # Browser downloads use POST so Origin and CSRF are both checked.
        admin(request, True)
        value = registry.bundle(source_id, credential_id)
        return JSONResponse(value, headers={'Content-Disposition': 'attachment; filename="'+value['source_id']+'-shipper.json"'})

    @app.post('/control/v1/sources/{source_id}/{action}')
    async def action(source_id: str, action: str, request: Request):
        actor = admin(request, True)
        mapping = {'rotations': 'rotate', 'cancel-rotation': 'cancel_rotation', 'confirm-rotation': 'confirm', 'disable': 'disable', 'enable': 'enable', 'rename': 'rename'}
        if action not in mapping:
            raise HTTPException(404)
        result = registry.mutate(mapping[action], await body(request), request.headers.get('idempotency-key'), source_id, actor)
        return JSONResponse(result, status_code=202)

    @app.get('/control/v1/audit')
    def audit(request: Request):
        admin(request)
        with closing(registry.connect()) as db:
            return {'events': [dict(row) for row in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 200')]}

    @app.post('/agent/v1/heartbeat')
    @app.post('/agent/v1/config-acks')
    async def heartbeat(request: Request):
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or len(authorization) > 4096:
            raise HTTPException(401, 'Source authentication required')
        try:
            return registry.heartbeat(authorization[7:], await body(request))
        except PermissionError:
            raise HTTPException(401, 'Source authentication required') from None

    return app
