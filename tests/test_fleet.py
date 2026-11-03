import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.error
import fleet


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        def write(path, value):
            path.write_text(json.dumps(value))
        self.app = SimpleNamespace(DATA=root, PUBLIC_HOST='maximus.example.com',
            socket=SimpleNamespace(gethostname=lambda: 'Maximus'),
            load_json=lambda path, default: json.loads(path.read_text()) if path.exists() else default,
            atomic_json=write, protect_secret=lambda s: 'protected:'+s,
            unprotect_secret=lambda s: s.removeprefix('protected:'),
            APP_VERSION='1.6.0', utc_now=lambda: '2026-11-03T00:00:00Z',
            METRICS=Mock(payload=lambda: {'latest': {'cpu': {'percent': 20}}}),
            snapshot=Mock(return_value={'apps':[{'id':'same-app','name':'Studio','state':'running',
                'actions':{'stop':True},'launcher':'private command'}]}),
            perform_action=Mock(return_value={'verifiedStopped':True}))
        self.fleet = fleet.Fleet(self.app)

    def test_identity_survives_rename_and_restart(self):
        identity = self.fleet.identity()
        self.app.socket.gethostname = lambda: 'Renamed'
        other = fleet.Fleet(self.app).identity()
        self.assertEqual(identity['id'], other['id'])
        self.assertEqual(other['computerName'], 'Renamed')

    def test_different_installations_have_different_ids(self):
        first = self.fleet.identity()['id']
        with tempfile.TemporaryDirectory() as folder:
            self.app.DATA = Path(folder)
            self.assertNotEqual(first, self.fleet.identity()['id'])

    def test_corrupt_identity_is_not_replaced(self):
        self.fleet.write('machine-identity.json', {'id':'broken'})
        with self.assertRaises(ValueError): self.fleet.identity()

    def test_grant_is_hashed_revocable_and_target_bound(self):
        grant = self.fleet.grant('Hub')
        stored = (self.app.DATA/'fleet-grants.json').read_text()
        self.assertNotIn(grant['token'], stored)
        headers = {'X-LocalOps-Node':grant['node']['id'], 'Authorization':'Bearer '+grant['token']}
        self.fleet.authorize_agent(headers)
        with self.assertRaises(PermissionError): self.fleet.authorize_agent({**headers,'X-LocalOps-Node':'wrong'})
        self.assertNotIn('digest', self.fleet.grants()[0])
        self.fleet.revoke(grant['id'])
        with self.assertRaises(PermissionError): self.fleet.authorize_agent(headers)

    def test_expired_or_missing_token_is_rejected(self):
        grant = self.fleet.grant('Hub')
        headers = {'X-LocalOps-Node':grant['node']['id'], 'Authorization':'Bearer '+grant['token']}
        with patch.object(fleet.time,'time',return_value=grant['expiresAt']+1):
            with self.assertRaises(PermissionError): self.fleet.authorize_agent(headers)
        with self.assertRaises(PermissionError): self.fleet.authorize_agent({'X-LocalOps-Node':grant['node']['id']})

    def peer(self):
        return {'id':'c454b593-11ed-42d3-a5a5-d5cdfa3ea949','computerName':'Helios',
                'url':'https://helios.example.com','protectedToken':'protected:'+'a'*64}

    def test_pair_verifies_identity_and_stores_encrypted_key(self):
        peer = self.peer()
        with patch.object(self.fleet,'request',return_value={'node':peer}):
            result = self.fleet.pair({'id':peer['id'],'url':peer['url'],'token':'a'*64})
        self.assertNotIn('protectedToken',result)
        self.assertEqual(self.fleet.read('fleet-peers.json',[])[0]['protectedToken'],'protected:'+'a'*64)

    def test_bad_pair_does_not_change_registry(self):
        peer = self.peer()
        with patch.object(self.fleet,'request',side_effect=ValueError('identity mismatch')):
            with self.assertRaises(ValueError): self.fleet.pair({'id':peer['id'],'url':peer['url'],'token':'a'*64})
        self.assertFalse((self.app.DATA/'fleet-peers.json').exists())

    def test_unsafe_urls_rejected(self):
        for url in ('http://example.com','https://u:p@example.com','https://example.com/path',
                    'https://example.com/?secret=x','https://example.com/#x'):
            with self.subTest(url=url), self.assertRaises(ValueError): self.fleet.address(url)

    def test_request_checks_response_identity(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"node":{"id":"other"}}'
        with patch.object(fleet.urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError,'identity mismatch'): self.fleet.request(self.peer(),'/api/agent/status')

    def test_redirects_do_not_forward_credentials(self):
        self.assertIsNone(fleet.NoRedirect().redirect_request(None,None,302,'',{},'https://other.example.com'))
        with self.assertRaises(ValueError): self.fleet.request(self.peer(),'/api/auth/login')

    def test_offline_nodes_have_no_stale_apps(self):
        self.fleet.write('fleet-peers.json',[self.peer()])
        with patch.object(self.fleet,'request',side_effect=RuntimeError('unreachable')):
            result = self.fleet.status()
        self.assertTrue(result['nodes'][0]['online'])
        self.assertFalse(result['nodes'][1]['online'])
        self.assertEqual(result['nodes'][1]['apps'],[])

    def test_actions_route_same_app_id_to_correct_machine(self):
        peer = self.peer()
        self.fleet.write('fleet-peers.json',[peer])
        with patch.object(self.fleet,'request',return_value={'node':peer,'ok':True}) as request:
            self.fleet.action({'nodeId':peer['id'],'appId':'same-app','action':'stop','confirmed':True})
            self.assertEqual(request.call_args.args[0]['id'],peer['id'])
            self.app.perform_action.assert_not_called()
        self.fleet.action({'nodeId':self.fleet.identity()['id'],'appId':'same-app','action':'stop','confirmed':True})
        self.app.perform_action.assert_called_once()

    def test_no_unpaired_unconfirmed_or_power_actions(self):
        for body in ({'nodeId':'unknown','appId':'same-app','action':'stop','confirmed':True},
                     {'nodeId':self.fleet.identity()['id'],'appId':'same-app','action':'shutdown','confirmed':True},
                     {'nodeId':self.fleet.identity()['id'],'appId':'same-app','action':'stop'}):
            with self.assertRaises(ValueError): self.fleet.action(body)
        self.app.perform_action.assert_not_called()

    def test_agent_command_wrong_body_target_rejected(self):
        with self.assertRaises(PermissionError):
            self.fleet.agent_action({'nodeId':'wrong','appId':'same-app','action':'stop','confirmed':True})
        self.app.perform_action.assert_not_called()

    def test_status_does_not_disclose_launcher(self):
        status = self.fleet.local_status()
        self.assertNotIn('launcher',status['apps'][0])
        self.assertEqual(status['metrics']['cpu']['percent'],20)

    def test_timeout_does_not_retry_command(self):
        peer = self.peer()
        self.fleet.write('fleet-peers.json',[peer])
        with patch.object(self.fleet,'request',side_effect=RuntimeError('timeout')) as request:
            with self.assertRaises(RuntimeError):
                self.fleet.action({'nodeId':peer['id'],'appId':'same-app','action':'start','confirmed':True})
            self.assertEqual(request.call_count,1)
