import unittest
from unittest.mock import Mock, patch
from test_app import localops


class FleetRouteTests(unittest.TestCase):
    def handler(self, path):
        handler = Mock()
        handler.path = path
        handler.headers = {}
        handler.host_allowed.return_value = True
        handler.require_read_session.return_value = None
        return handler

    def test_remote_fleet_read_requires_dashboard_login(self):
        handler = self.handler('/api/fleet')
        with patch.object(localops.FLEET, 'status') as status:
            localops.Handler.do_GET(handler)
        status.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[1], 401)

    def test_agent_read_requires_key_even_on_loopback(self):
        handler = self.handler('/api/agent/status')
        with patch.object(localops.FLEET, 'authorize_agent', side_effect=PermissionError('key required')), patch.object(localops.FLEET, 'local_status') as status:
            localops.Handler.do_GET(handler)
        status.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[1], 401)

    def test_agent_command_requires_key_before_body_is_read(self):
        handler = self.handler('/api/agent/action')
        with patch.object(localops.FLEET, 'authorize_agent', side_effect=PermissionError('key required')), patch.object(localops.FLEET, 'agent_action') as action:
            localops.Handler.do_POST(handler)
        action.assert_not_called()
        handler.read_json.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[1], 403)

    def test_fleet_action_requires_dashboard_csrf(self):
        handler = self.handler('/api/fleet/action')
        handler.authorized_write.return_value = False
        with patch.object(localops.FLEET, 'action') as action:
            localops.Handler.do_POST(handler)
        action.assert_not_called()
        handler.read_json.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[1], 403)

    def test_paired_result_does_not_change_dashboard_identity(self):
        handler = self.handler('/api/fleet/action')
        handler.authorized_write.return_value = True
        handler.read_json.return_value = {'nodeId':'remote', 'appId':'fixture','action':'stop','confirmed':True}
        with patch.object(localops.FLEET, 'action', return_value={'node':{'id':'remote'},'ok':True}):
            localops.Handler.do_POST(handler)
        response = handler.send_json.call_args.args[0]
        self.assertNotIn('node',response)
        self.assertEqual(response['targetResult']['node']['id'],'remote')
