import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from test_app import localops

class NodeTests(unittest.TestCase):
    def test_machine_name_is_current_and_stable_across_case(self):
        with patch.object(localops.socket,'gethostname',return_value='Maximus'):
            self.assertEqual(localops.node_identity()['computerName'],'Maximus')
            self.assertEqual(localops.node_id('Maximus'),localops.node_id('MAXIMUS'))

    def test_nodes_are_persisted_and_each_address_is_independent(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(localops,'DATA',Path(folder)),patch.object(localops.socket,'gethostname',return_value='Maximus'):
            other=localops.register_node({'computerName':'Old-PC','url':'https://old-localops.example.com/'})
            self.assertEqual(localops.node_catalog()[0]['computerName'],'Maximus')
            self.assertEqual(localops.node_catalog()[1],other)
            self.assertEqual(json.loads((Path(folder)/'nodes.json').read_text())[0]['computerName'],'Old-PC')
            with self.assertRaisesRegex(ValueError,'own LocalOps address'):
                localops.register_node({'computerName':'Third-PC','url':'https://old-localops.example.com'})
            with self.assertRaisesRegex(ValueError,'shared tunnel'):
                localops.register_node({'computerName':'Another-PC','url':localops.node_identity()['url']})

    def test_node_registration_rejects_unsafe_or_credentialled_addresses(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(localops,'DATA',Path(folder)):
            for url in ['http://remote.example.com','javascript:alert(1)','https://user:secret@example.com','https://example.com/?token=secret','https://example.com/path']:
                with self.subTest(url=url),self.assertRaises(ValueError):
                    localops.register_node({'computerName':'Other-PC','url':url})

    def test_wrong_target_is_rejected_before_any_action_or_login(self):
        handler=Mock()
        handler.headers={'X-LocalOps-Node':'wrong-computer'}
        localops.Handler.do_POST(handler)
        self.assertEqual(handler.send_json.call_args.args[1],409)
        handler.authorized_write.assert_not_called()
        handler.read_json.assert_not_called()

    def test_selector_and_client_identity_guard_are_present(self):
        html=(localops.ROOT/'static/index.html').read_text(encoding='utf-8')
        javascript=(localops.ROOT/'static/app.js').read_text(encoding='utf-8')
        self.assertIn('id="nodeSelect"',html)
        self.assertIn('X-LocalOps-Node',javascript)
        self.assertIn('data.node.id !== expected',javascript)
        self.assertIn('location.assign(target.href)',javascript)
