import importlib.util
import json
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock
from datetime import datetime, timezone
from subprocess import CompletedProcess


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("localops", ROOT / "app.py")
localops = importlib.util.module_from_spec(SPEC)
sys.modules["localops"] = localops
SPEC.loader.exec_module(localops)


class LocalOpsTests(unittest.TestCase):
    def setUp(self):
        verifier = patch.object(localops, "verify_stays_stopped")
        verifier.start()
        self.addCleanup(verifier.stop)

    def test_application_confirmation_is_boolean_not_name(self):
        entry = {"id":"fixture", "name":"Fixture", "controlMode":"manage", "confirmationRequired":True}
        with patch.object(localops.STORE, "get_app", return_value=entry), patch.object(localops, "snapshot", return_value={"apps":[{"id":"fixture", "state":"running", "ownershipSafe":True,"pids":[10]}]}), patch.object(localops,"control_recovery"):
            for body in ({}, {"confirmed":False}, {"confirmed":"yes"}, {"confirmName":"Fixture"}):
                with self.assertRaises(ValueError):
                    localops.perform_action("fixture", "start", body)
            self.assertTrue(localops.perform_action("fixture", "start", {"confirmed":True})["alreadyRunning"])

    def test_parse_ipv4_and_ipv6_endpoints(self):
        self.assertEqual(localops.parse_endpoint("127.0.0.1:4000"), ("127.0.0.1", 4000))
        self.assertEqual(localops.parse_endpoint("[::1]:8080"), ("::1", 8080))
        self.assertIsNone(localops.parse_endpoint("bad"))

    def test_validate_app_normalizes_ports(self):
        app = localops.validate_app({"name": "Example App", "ports": [{"port": "8080"}], "expectedProcesses": ["node"], "controlMode": "manage"})
        self.assertEqual(app["id"], "example-app")
        self.assertEqual(app["ports"][0]["port"], 8080)
        self.assertEqual(app["controlMode"], "manage")

    def test_launcher_arguments_preserved_and_wrong_type_rejected(self):
        args = ["--flag", "path with spaces", *[str(x) for x in range(25)]]
        app = localops.validate_app({"name":"Fixture", "start":{"path":"start.ps1", "args":args}})
        self.assertEqual(app["start"]["args"],args)
        with self.assertRaises(ValueError):
            localops.validate_app({"name":"Fixture", "start":{"path":"start.ps1", "args":"--flag"}})

    def test_dependency_never_becomes_managed_pid(self):
        app = {"id":"x","name":"X","ports":[{"port":4000,"role":"managed","required":True},{"port":5432,"role":"dependency","required":True}],"expectedProcesses":["node"],"controlMode":"manage","start":None}
        listeners = [{"address":"127.0.0.1","port":4000,"pid":10},{"address":"0.0.0.0","port":5432,"pid":11}]
        processes = {10:{"id":10,"name":"node","memoryBytes":100},11:{"id":11,"name":"postgres","memoryBytes":200}}
        with patch.object(localops, "check_health"):
            status = localops.app_status(app, listeners, processes)
        self.assertEqual(status["pids"], [10])
        self.assertTrue(status["actions"]["stop"])

    def test_process_mismatch_blocks_stop(self):
        app = {"id":"x","name":"X","ports":[{"port":4000,"role":"managed","required":True}],"expectedProcesses":["node"],"controlMode":"manage","start":None}
        status = localops.app_status(app, [{"address":"127.0.0.1","port":4000,"pid":12}], {12:{"id":12,"name":"python"}})
        self.assertFalse(status["ownershipSafe"])
        self.assertFalse(status["actions"]["stop"])

    def test_dependency_alive_does_not_block_start(self):
        app = {"id":"x", "ports":[{"port":4000,"role":"managed"},{"port":5432,"role":"dependency"}], "expectedProcesses":["node"], "controlMode":"manage", "start":{"path":str(ROOT / "app.py")}}
        result = localops.app_status(app, [{"port":5432,"pid":11}], {11:{"name":"postgres"}})
        self.assertEqual(result["state"], "stopped")
        self.assertTrue(result["actions"]["start"])
        self.assertEqual(result["pids"], [])

    def test_partial_start_only_for_explicit_idempotent_launcher(self):
        app = {"id":"fixture","controlMode":"manage","expectedProcesses":["node"],"start":{"path":str(ROOT / "app.py")},"ports":[{"port":4000,"role":"managed"},{"port":5173,"role":"managed"}]}
        listeners, processes = [{"port":5173,"pid":10}], {10:{"name":"node"}}
        self.assertFalse(localops.app_status(app,listeners,processes)["actions"]["start"])
        app["repairStart"] = True
        state = localops.app_status(app,listeners,processes)
        self.assertEqual(state["state"],"partial")
        self.assertTrue(state["actions"]["start"])
        self.assertTrue(state["actions"]["stop"])
        self.assertFalse(localops.app_status(app,listeners,{10:{"name":"unrelated"}})["actions"]["start"])

    def test_failed_listener_scan_is_not_reported_as_empty(self):
        with patch.object(localops,"IS_WINDOWS",True), patch.object(localops,"psutil",None), patch.object(localops,"run_quiet",side_effect=OSError("scan failed")):
            with self.assertRaisesRegex(RuntimeError,"discovery failed"):
                localops.discover_listeners()

    def test_native_listener_discovery_does_not_launch_netstat(self):
        from types import SimpleNamespace
        listener = SimpleNamespace(laddr=SimpleNamespace(ip="127.0.0.1", port=8790), pid=100, status="LISTEN")
        client = SimpleNamespace(laddr=SimpleNamespace(ip="127.0.0.1", port=45000), pid=200, status="ESTABLISHED")
        with patch.object(localops,"IS_WINDOWS",True), patch.object(localops.psutil,"net_connections",return_value=[listener, listener, client]), patch.object(localops,"run_quiet") as command:
            self.assertEqual(localops.discover_listeners(), [{"address":"127.0.0.1","port":8790,"pid":100}])
            command.assert_not_called()

    def test_native_listener_error_blocks_controls(self):
        with patch.object(localops,"IS_WINDOWS",True), patch.object(localops.psutil,"net_connections",side_effect=localops.psutil.AccessDenied()):
            with self.assertRaisesRegex(RuntimeError,"discovery failed"):
                localops.discover_listeners()

    def test_runtime_readiness_does_not_scan_applications(self):
        handler = object.__new__(localops.Handler)
        handler.path = "/api/runtime"
        handler.host_allowed = lambda: True
        handler.require_read_session = lambda: {"csrf": "test"}
        handler.send_json = MagicMock()
        with patch.object(localops,"snapshot",side_effect=AssertionError("No application scan during readiness")), patch.object(localops,"system_capabilities",side_effect=AssertionError("No external powercfg during readiness")), patch.object(localops,"is_elevated",return_value=True):
            handler.do_GET()
        handler.send_json.assert_called_once_with({"server":{"name":"LocalOps","version":localops.APP_VERSION},"system":{"elevated":True}})

    def test_stop_rechecks_pid_identity_and_excludes_dependency(self):
        started = datetime.fromtimestamp(100, timezone.utc).isoformat()
        status = {"id":"x", "actions":{"stop":True}, "pids":[10], "ports":[{"role":"managed", "listeners":[{"pid":10,"process":{"started":started,"path":"node.exe"}}]}, {"role":"dependency", "listeners":[{"pid":11}]}]}
        process = MagicMock()
        process.create_time.return_value = 100
        process.name.return_value = "node.exe"
        process.exe.return_value = "node.exe"
        with patch.object(localops,"snapshot",return_value={"apps":[status]}), patch.object(localops.psutil,"Process",return_value=process) as make, patch.object(localops,"wait_for_app_state",return_value={"pids":[],"ports":[]}):
            result = localops.stop_app({"id":"x","expectedProcesses":["node"],"controlMode":"manage"})
            self.assertTrue(result["verifiedStopped"])
            make.assert_called_once_with(10)
            process.terminate.assert_called_once()
            process.reset_mock()
            process.create_time.return_value = 200
            with self.assertRaises(ValueError):
                localops.stop_app({"id":"x","expectedProcesses":["node"],"controlMode":"manage"})
            process.terminate.assert_not_called()

    def test_stop_does_not_kill_replacement_listener(self):
        status = {"id":"x", "actions":{"stop":True}, "pids":[10], "ports":[{"role":"managed", "listeners":[{"pid":10,"process":{"started":datetime.fromtimestamp(100,timezone.utc).isoformat(),"path":"node.exe"}}]}]}
        process = MagicMock()
        process.create_time.return_value = 100
        process.name.return_value = "node.exe"
        process.exe.return_value = "node.exe"
        with patch.object(localops,"snapshot",return_value={"apps":[status]}), patch.object(localops.psutil,"Process",return_value=process) as make, patch.object(localops,"wait_for_app_state",return_value={"pids":[99],"ports":[]}):
            with self.assertRaises(RuntimeError):
                localops.stop_app({"id":"x","expectedProcesses":["node"],"controlMode":"manage"})
            make.assert_called_once_with(10)

    def test_temperature_rejects_stale_reading(self):
        with patch.object(localops,"load_json",return_value={"sampledAt":"2020-01-01T00:00:00+00:00", "sensors":[{"name":"CPU Package","celsius":70}]}):
            self.assertFalse(localops.cpu_temperature()["available"])
        with patch.object(localops,"load_json",return_value={"sampledAt":localops.utc_now(), "sensors":[{"name":"CPU Package","celsius":72}]}):
            self.assertEqual(localops.cpu_temperature()["celsius"],72)

    def test_migrated_totp_keeps_authentication_locked_with_recovery_message(self):
        store = object.__new__(localops.AuthStore)
        store.lock = localops.threading.RLock()
        store.login_attempts = {}
        store.auth = {"passwordHash": "test", "totpSecret": "dpapi:old"}
        with patch.object(localops, "password_matches", return_value=True), patch.object(localops, "unprotect_secret", side_effect=OSError("old account")):
            with self.assertRaisesRegex(PermissionError, "recovery code"):
                store.login({"password": "correct", "code": "123456"}, "fixture", "test")

    @unittest.skipUnless((ROOT / "data" / "apps.json").exists(), "Deployment-only check: private registry is not shipped with source")
    def test_initial_registry_is_valid(self):
        apps = json.loads((ROOT / "data" / "apps.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(apps), 7)
        self.assertEqual(len({app["id"] for app in apps}), len(apps))
        self.assertTrue(any(app["id"] == "tradedesk-ai" and app["workingDirectory"] == str(ROOT.parent / "tradedesk-ai") for app in apps))

    def test_gpu_csv_parser(self):
        parsed = localops.parse_gpu_row("NVIDIA GeForce GTX 1050 Ti, 17, 512, 4096, 64, 21.5\n")
        self.assertEqual(parsed["name"], "NVIDIA GeForce GTX 1050 Ti")
        self.assertEqual(parsed["utilizationPercent"], 17.0)
        self.assertEqual(parsed["memoryTotalMb"], 4096.0)
        self.assertIsNone(localops.parse_gpu_row(""))

    def test_performance_dashboard_contract(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        for chart_id in ("chartCpu", "chartMemory", "chartGpu", "chartVram", "chartNetwork", "chartDisk"):
            self.assertIn(f'id="{chart_id}"', html)
        self.assertIn('/api/metrics', javascript)
        self.assertIn('renderMetrics()', javascript)
        self.assertIn('id="authGate"', html)
        self.assertIn('/api/auth/login', javascript)
        self.assertIn('https://localops.solliegiance.com', html)
        self.assertIn('rel="manifest"', html)
        self.assertIn('id="system"', html)
        self.assertIn('data-system-action="shutdown"', html)
        self.assertIn('data-quick-app="ollama"', html)
        self.assertIn('serviceWorker', javascript)
        self.assertTrue((ROOT / "static" / "manifest.webmanifest").is_file())
        self.assertTrue((ROOT / "static" / "service-worker.js").is_file())
        self.assertTrue((ROOT / "static" / "icons" / "localops.svg").is_file())

    def test_power_commands_have_safe_shutdown_delay(self):
        self.assertEqual(localops.power_command("shutdown")[:4], ["shutdown.exe", "/s", "/t", "30"])
        self.assertEqual(localops.power_command("hibernate"), ["shutdown.exe", "/h"])
        self.assertEqual(localops.power_command("cancel-shutdown"), ["shutdown.exe", "/a"])

    def test_system_action_requires_exact_phrase_and_password(self):
        with patch.object(localops.AUTH, "verify_password", return_value=True), patch.object(localops, "is_elevated", return_value=True), patch.object(localops, "run_quiet", return_value=CompletedProcess([], 0, "", "")) as run, patch.object(localops.STORE, "add_event"):
            with self.assertRaises(ValueError):
                localops.perform_system_action("shutdown", {"confirmPhrase":"shutdown", "password":"secret"})
            result = localops.perform_system_action("shutdown", {"confirmPhrase":"SHUTDOWN THIS PC", "password":"secret"})
            self.assertTrue(result["accepted"])
            run.assert_called_once_with(localops.power_command("shutdown"), timeout=10)

        with patch.object(localops.AUTH, "verify_password", return_value=False), patch.object(localops, "is_elevated", return_value=True):
            with self.assertRaises(PermissionError):
                localops.perform_system_action("hibernate", {"confirmPhrase":"HIBERNATE THIS PC", "password":"wrong"})

    def test_javascript_id_selectors_exist_in_html(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        html_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        selected_ids = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', javascript))
        self.assertEqual(selected_ids - html_ids, set())

    def test_password_and_totp_security_primitives(self):
        stored = localops.password_hash("correct horse battery staple")
        self.assertTrue(localops.password_matches("correct horse battery staple", stored))
        self.assertFalse(localops.password_matches("wrong password", stored))
        secret = localops.new_totp_secret()
        counter = int(time.time()) // 30
        code = localops.totp_at(secret, counter)
        self.assertEqual(localops.verify_totp(secret, code, counter - 1), counter)
        self.assertIsNone(localops.verify_totp(secret, code, counter))


if __name__ == "__main__":
    unittest.main()
