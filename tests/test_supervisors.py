import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from test_app import localops


class SupervisorTests(unittest.TestCase):
    def fixture(self):
        self.app = {"id":"ollama", "expectedProcesses":["ollama"], "controlMode":"manage", "start":{"path":str(Path("install") / "ollama.exe")}, "ports":[{"port":11434,"role":"managed"}]}
        self.desktop = self.process(1, "ollama app.exe")
        self.server = self.process(2, "ollama.exe")
        self.desktop.children.return_value = [self.server]
        identity = {"name":"ollama", "path":self.server.exe(), "started":datetime.fromtimestamp(100,timezone.utc).isoformat()}
        self.status = {"id":"ollama","pids":[2],"actions":{"stop":True},"ports":[{"role":"managed","listeners":[{"pid":2,"process":identity}]}]}

    def process(self, pid, name):
        process = Mock(pid=pid, info={"pid":pid,"name":name})
        process.exe.return_value = str(Path("install") / name)
        process.name.return_value = name
        process.create_time.return_value = 100
        return process

    def test_supervisor_stops_before_server_and_records_desktop_start_mode(self):
        self.fixture()
        order = []
        self.desktop.terminate.side_effect = lambda:order.append("desktop")
        self.server.terminate.side_effect = lambda:order.append("server")
        with tempfile.TemporaryDirectory() as folder, patch.object(localops,"DATA",Path(folder)), patch.object(localops.psutil,"process_iter",return_value=[self.desktop]), patch.object(localops.psutil,"Process",side_effect=lambda pid:{1:self.desktop,2:self.server}[pid]), patch.object(localops,"snapshot",return_value={"apps":[self.status]}), patch.object(localops,"wait_for_app_state",return_value={"pids":[],"ports":[]}), patch.object(localops,"verify_stays_stopped") as verify:
            self.assertTrue(localops.stop_app(self.app)["verifiedStopped"])
            self.assertEqual(order,["desktop","server"])
            self.assertTrue(localops.load_json(Path(folder)/"ollama-desktop-pause.json",{})["wasRunning"])
            verify.assert_called_once_with(self.app)

    def test_unrelated_desktop_and_other_children_are_never_targets(self):
        self.fixture()
        foreign = self.process(3,"ollama app.exe")
        foreign.exe.return_value = str(Path("elsewhere") / "ollama app.exe")
        unrelated = self.process(4,"python.exe")
        self.desktop.children.return_value = [self.server,unrelated]
        with patch.object(localops.psutil,"process_iter",return_value=[foreign,self.desktop]):
            self.assertEqual([x["pid"] for x in localops.ollama_supervised_processes(self.app,self.status)],[1,2])
        self.desktop.children.return_value = [unrelated]
        with patch.object(localops.psutil,"process_iter",return_value=[self.desktop]):
            self.assertEqual(localops.ollama_supervised_processes(self.app,self.status),[])

    def test_restarted_listener_is_reported_not_killed(self):
        self.fixture()
        with patch.object(localops,"discover_listeners",side_effect=[[],[{"port":11434,"pid":77}]]), patch.object(localops,"runtime_workers",return_value=[]), patch.object(localops.time,"sleep"), patch.object(localops.psutil,"Process") as process:
            with self.assertRaisesRegex(RuntimeError,"restarted"):
                localops.verify_stays_stopped(self.app)
            process.assert_not_called()

    def test_shared_dependency_does_not_fail_stop_verification(self):
        self.fixture()
        with patch.object(localops,"discover_listeners",return_value=[{"port":5432,"pid":90}]):
            localops.verify_stays_stopped(self.app,quiet_seconds=0)

    def test_orphaned_ollama_worker_identified_by_path_not_generic_name(self):
        self.fixture()
        worker = self.process(3,"llama-server.exe")
        worker.exe.return_value = str(Path("install") / "lib" / "ollama" / "llama-server.exe")
        worker.parents.return_value = []
        worker.cmdline.return_value = [worker.exe(),"--model","model.gguf","--port","60806"]
        worker.memory_info.return_value = Mock(rss=100)
        unrelated = self.process(4,"llama-server.exe")
        unrelated.exe.return_value = str(Path("other-install") / "llama-server.exe")
        with patch.object(localops.psutil,"process_iter",return_value=[worker,unrelated]):
            found = localops.runtime_workers(self.app,[])
            self.assertEqual([item["pid"] for item in found],[3])
            self.assertTrue(found[0]["orphaned"])
            worker.parents.return_value = [Mock(pid=99)]
            self.assertEqual(localops.runtime_workers(self.app,[]),[])
            worker.parents.return_value = [Mock(pid=2)]
            self.assertEqual(len(localops.runtime_workers(self.app,[2])),1)

    def test_worker_only_runtime_remains_stoppable(self):
        self.fixture()
        with patch.object(localops,"runtime_workers",return_value=[{"pid":3}]):
            status = localops.app_status(self.app,[],{})
            self.assertEqual(status["state"],"partial")
            self.assertTrue(status["actions"]["stop"])
