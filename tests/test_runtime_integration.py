"""Real process tests; use only disposable servers, never registered user apps."""
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from test_app import localops
from schedules import Scheduler


class RuntimeIntegrationTests(unittest.TestCase):
    def test_scheduled_start_stop_preserves_live_dependency(self):
        self.exercise_launcher()

    @unittest.skipUnless(localops.IS_WINDOWS, "Windows PowerShell launcher integration")
    def test_powershell_script_start_stop_preserves_live_dependency(self):
        self.exercise_launcher(script=True)

    def exercise_launcher(self, script=False):
        with tempfile.TemporaryDirectory() as folder:
            # PowerShell expands Windows 8.3 paths (e.g. RUNNER~1) before
            # starting Python. Use the same canonical directory in the fixture.
            folder = str(Path(folder).resolve())
            root = Path(folder)
            sockets = [socket.socket(), socket.socket()]
            for sock in sockets:
                sock.bind(("127.0.0.1", 0))
            managed_port, dependency_port = [sock.getsockname()[1] for sock in sockets]
            for sock in sockets:
                sock.close()
            command = [sys.executable, "-m", "http.server", str(dependency_port), "--bind", "127.0.0.1", "--directory", folder]
            dependency = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=localops.CREATE_NO_WINDOW)
            processes = [dependency]
            app = {
                "id": "localops-disposable-test", "name": "Disposable test server", "enabled": True,
                "controlMode": "manage", "confirmationRequired": True,
                "workingDirectory": folder,
                "expectedProcesses": [Path(sys.executable).stem],
                "processMatch": {"cwd": folder, "args": ["-m", "http.server", str(managed_port)]},
                "start": {"path": sys.executable, "args": ["-m", "http.server", str(managed_port), "--bind", "127.0.0.1", "--directory", folder]},
                "ports": [{"port": managed_port, "role": "managed", "required": True},
                          {"port": dependency_port, "role": "dependency", "required": True}],
            }
            if script:
                app["start"] = {"path":str(Path(__file__).parent / "fixtures" / "serve.ps1"), "args":["-Python",sys.executable,"-Port",str(managed_port)]}
            store = Mock(apps=[app], events=[], lock=threading.RLock())
            store.get_app.side_effect = lambda app_id: app if app_id == app["id"] else None
            original_launch = localops.launch_app

            def launch(value):
                result = original_launch(value)
                processes.append(result["_process"])
                return result

            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with socket.socket() as probe:
                        if probe.connect_ex(("127.0.0.1", dependency_port)) == 0:
                            break
                    time.sleep(.1)
                else:
                    self.fail("Disposable dependency did not start")
                with patch.object(localops, "STORE", store), patch.object(localops, "LOGS", root), patch.object(localops, "STATUS_CACHE", {"at": 0, "payload": None}), patch.object(localops, "LAST_STATES", {}), patch.object(localops, "launch_app", side_effect=launch):
                    engine = Scheduler(root / "schedules.json", localops.atomic_json, store.get_app, localops.perform_action, store.add_event)
                    for action in ("start", "stop"):
                        item = engine.create({"appId": app["id"], "action": action, "confirmed": True, "runAt": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()})
                        engine.tick(datetime.fromisoformat(item["nextRunAt"]))
                        result = engine.items[-1]
                        self.assertEqual(result["lastStatus"], "succeeded", result["lastError"])
                        self.assertIsNone(dependency.poll(), "A dependency must never be stopped")
                    status = localops.snapshot(force=True)["apps"][0]
                    self.assertEqual(status["state"], "stopped")
                    self.assertTrue(status["actions"]["start"])
                    self.assertEqual(status["pids"], [])
                    saved = json.loads((root / "schedules.json").read_text())
                    self.assertEqual([item["lastStatus"] for item in saved], ["succeeded", "succeeded"])
            finally:
                for process in processes:
                    if process.poll() is None:
                        try:
                            children = localops.psutil.Process(process.pid).children(recursive=True)
                            for child in reversed(children):
                                try:
                                    child.terminate()
                                except localops.psutil.NoSuchProcess:
                                    pass
                            localops.psutil.wait_procs(children, timeout=5)
                        except localops.psutil.NoSuchProcess:
                            pass
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=15)
