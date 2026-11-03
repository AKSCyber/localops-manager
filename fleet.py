"""Server-to-server fleet pairing, identity, telemetry and scoped app control."""
from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Fleet:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.cache = None
        self.cache_at = 0

    def read(self, name, default):
        return self.app.load_json(self.app.DATA / name, default)

    def write(self, name, value):
        self.app.atomic_json(self.app.DATA / name, value)

    def identity(self):
        with self.lock:
            record = self.read('machine-identity.json', None)
            if record is None:
                record = {'id': str(uuid.uuid4())}
                self.write('machine-identity.json', record)
            uuid.UUID(record['id'])  # Corruption must fail closed, not generate a new identity.
            return {'id': record['id'], 'computerName': self.app.socket.gethostname(),
                    'url': f'https://{self.app.PUBLIC_HOST}'}

    def grant(self, label):
        label = str(label).strip()
        if not 1 <= len(label) <= 80:
            raise ValueError('Enter a name for the dashboard receiving access.')
        token = secrets.token_urlsafe(48)
        item = {'id': uuid.uuid4().hex, 'label': label, 'createdAt': time.time(),
                'expiresAt': time.time() + 90 * 86400,
                'digest': hashlib.sha256(token.encode()).hexdigest()}
        with self.lock:
            grants = [g for g in self.read('fleet-grants.json', []) if g['expiresAt'] > time.time()]
            if len(grants) >= 20:
                raise ValueError('Revoke an existing access key before creating another.')
            self.write('fleet-grants.json', [*grants, item])
        return {'id': item['id'], 'token': token, 'expiresAt': item['expiresAt'], 'node': self.identity()}

    def grants(self):
        with self.lock:
            return [{k: v for k, v in g.items() if k != 'digest'} for g in self.read('fleet-grants.json', [])]

    def revoke(self, grant_id):
        with self.lock:
            self.write('fleet-grants.json', [g for g in self.read('fleet-grants.json', []) if g['id'] != grant_id])

    def authorize_agent(self, headers):
        if headers.get('X-LocalOps-Node') != self.identity()['id']:
            raise PermissionError('The request targets a different machine.')
        value = headers.get('Authorization', '')
        if not value.startswith('Bearer ') or len(value) > 256:
            raise PermissionError('Node access key required.')
        digest = hashlib.sha256(value[7:].encode()).hexdigest()
        with self.lock:
            allowed = any(g['expiresAt'] > time.time() and hmac.compare_digest(g['digest'], digest)
                          for g in self.read('fleet-grants.json', []))
        if not allowed:
            raise PermissionError('Node access key is invalid, expired or revoked.')

    @staticmethod
    def address(value):
        p = urllib.parse.urlsplit(str(value).strip())
        if (p.scheme != 'https' or not p.hostname or p.username or p.password or
                p.query or p.fragment or p.path not in ('', '/')):
            raise ValueError('Enter a dedicated HTTPS node address without a path or credentials.')
        p.port
        return p.geturl().rstrip('/')

    def request(self, entry, path, body=None):
        # Only fixed protocol paths may receive a stored credential. Never follow redirects.
        if path not in ('/api/agent/status', '/api/agent/action'):
            raise ValueError('Unsupported node request.')
        token = self.app.unprotect_secret(entry['protectedToken'])
        request = urllib.request.Request(self.address(entry['url']) + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + token, 'X-LocalOps-Node': entry['id'],
                     'Content-Type': 'application/json'}, method='GET' if body is None else 'POST')
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=150 if body else 8) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('Node response exceeds the size limit.')
            result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Node rejected the request (HTTP {exc.code}); check access key and node configuration.') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError('Node is unreachable; check its service and dedicated HTTPS route.') from None
        if result.get('node', {}).get('id') != entry['id']:
            raise ValueError('Node identity mismatch; check the machine ID and dedicated address.')
        if result.get('error'):
            raise RuntimeError('Node could not complete the requested operation.')
        return result

    def pair(self, body):
        identifier = str(uuid.UUID(str(body.get('id', ''))))
        if identifier == self.identity()['id']:
            raise ValueError('This machine is already included in the dashboard.')
        token = str(body.get('token', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{64}', token):
            raise ValueError('Paste the complete access key generated on the node.')
        entry = {'id': identifier, 'url': self.address(body.get('url', '')),
                 'protectedToken': self.app.protect_secret(token)}
        remote = self.request(entry, '/api/agent/status')
        entry['computerName'] = str(remote['node']['computerName'])[:128]
        with self.lock:
            nodes = [n for n in self.read('fleet-peers.json', []) if n['id'] != identifier]
            if len(nodes) >= 19:
                raise ValueError('Up to twenty machines can be displayed together.')
            if any(n['url'].casefold() == entry['url'].casefold() for n in nodes) or entry['url'].casefold() == self.identity()['url'].casefold():
                raise ValueError('Each machine needs its own HTTPS address.')
            self.write('fleet-peers.json', [*nodes, entry])
            self.cache_at = 0
        return {k: v for k, v in entry.items() if k != 'protectedToken'}

    def remove(self, node_id):
        with self.lock:
            self.write('fleet-peers.json', [n for n in self.read('fleet-peers.json', []) if n['id'] != node_id])
            self.cache_at = 0

    def local_status(self):
        payload = self.app.snapshot()
        # Do not share launch commands, file paths, cookies, secrets, or registry editing access.
        apps = [{k: app.get(k) for k in ('id', 'name', 'state', 'controlMode', 'actions', 'ownershipSafe')}
                for app in payload['apps']]
        latest = self.app.METRICS.payload().get('latest', {})
        metrics = {k: latest.get(k) for k in ('cpu', 'memory', 'gpu', 'network', 'disk')}
        return {'node': self.identity(), 'version': self.app.APP_VERSION,
                'online': True, 'observedAt': self.app.utc_now(), 'apps': apps,
                'metrics': metrics,
                'summary': payload.get('summary', {})}

    def status(self):
        # Serialize polls; never return old data labelled online when a new poll fails.
        with self.poll_lock:
            if self.cache is not None and time.monotonic() - self.cache_at < 5:
                return self.cache
            with self.lock:
                peers = self.read('fleet-peers.json', [])
            def read_peer(entry):
                try:
                    return self.request(entry, '/api/agent/status')
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else 'Unable to read node status.'
                    return {'node': {k: entry[k] for k in ('id', 'computerName', 'url')},
                            'online': False, 'apps': [], 'error': message}
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                remote = list(pool.map(read_peer, peers))
            try:
                local = self.local_status()
            except Exception:
                local = {'node': self.identity(), 'online': False, 'apps': [], 'error': 'Local status scan failed.'}
            result = {'nodes': [local, *remote], 'observedAt': self.app.utc_now()}
            self.cache, self.cache_at = result, time.monotonic()
            return result

    def action(self, body):
        if body.get('confirmed') is not True:
            raise ValueError('Confirm the application action first.')
        identifier = str(body.get('nodeId', ''))
        action, app_id = body.get('action'), body.get('appId')
        if action not in ('start', 'stop', 'restart') or not isinstance(app_id, str) or not app_id:
            raise ValueError('Select an application and a supported action.')
        payload = {'nodeId': identifier, 'appId': app_id, 'action': action, 'confirmed': True}
        try:
            if identifier == self.identity()['id']:
                return {'node': self.identity(), 'ok': True,
                        'result': self.app.perform_action(app_id, action, payload)}
            with self.lock:
                peer = next((n for n in self.read('fleet-peers.json', []) if n['id'] == identifier), None)
            if not peer:
                raise ValueError('This machine is not paired.')
            # A timed-out command is never retried automatically: it may have executed.
            return self.request(peer, '/api/agent/action', payload)
        finally:
            self.cache_at = 0

    def agent_action(self, body):
        if body.get('nodeId') != self.identity()['id']:
            raise PermissionError('Command targets a different machine.')
        if body.get('confirmed') is not True or body.get('action') not in ('start', 'stop', 'restart'):
            raise ValueError('A confirmed application action is required.')
        result = self.app.perform_action(str(body.get('appId', '')), body['action'], {'confirmed': True})
        return {'node': self.identity(), 'ok': True, 'result': result}
