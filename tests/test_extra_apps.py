import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_app import localops
import desktop_apps
from schedules import fingerprint


class ExtraAppsTests(unittest.TestCase):
    def test_same_executable_name_does_not_cross_desktop_packages(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ChatGPT.exe").touch()
            (root / "AppxManifest.xml").write_text('<Package><Identity Name="OpenAI.Codex"/><Applications><Application Id="App" Executable="ChatGPT.exe"/></Applications></Package>')
            name = "OpenAI.Codex_1.0.0.0_x64__2p2nqsd0c76g0"
            self.assertIsNone(desktop_apps.read_package(name, root, "chatgpt"))
            self.assertEqual(desktop_apps.read_package(name, root, "codex")["aumid"], "OpenAI.Codex_2p2nqsd0c76g0!App")
            self.assertIsNone(desktop_apps.read_package(name.replace("2p2nqsd0c76g0", "other"), root, "codex"))

    def test_desktop_targets_only_own_package_and_children(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main = Mock(pid=10, info={"name": "ChatGPT.exe"})
            main.exe.return_value = str(root / "codex" / "ChatGPT.exe")
            child = Mock(pid=11)
            child.exe.return_value = str(root / "codex" / "resources" / "agent.exe")
            dependency = Mock(pid=12)
            dependency.exe.return_value = str(root / "other" / "python.exe")
            other = Mock(pid=20, info={"name": "ChatGPT.exe"})
            other.exe.return_value = str(root / "chatgpt" / "ChatGPT.exe")
            main.children.return_value = [child, dependency]
            packages = [{"executable": main.exe(), "root": str(root / "codex")}]
            with patch.object(desktop_apps, "packages", return_value=packages), patch.object(localops.psutil, "process_iter", return_value=[main, other]):
                self.assertEqual(desktop_apps.processes("codex", localops.psutil)[1], {10, 11})

    def test_uninstalled_desktop_has_no_controls(self):
        with patch.object(desktop_apps, "processes", return_value=([], set())):
            status = localops.desktop_status({"desktopApp": "chatgpt", "controlMode": "manage"})
            self.assertEqual(status["state"], "unconfigured")
            self.assertFalse(any(status["actions"].values()))

    def test_desktop_launch_uses_registered_windows_activation(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(desktop_apps, "packages", return_value=[{"aumid": "OpenAI.Codex_2p2nqsd0c76g0!App"}]), patch.object(localops, "LOGS", Path(folder)), patch.object(localops.subprocess, "Popen") as launch:
                localops.launch_app({"id": "codex-desktop", "desktopApp": "codex"})
                command = launch.call_args.args[0]
                self.assertEqual(Path(command[0]).name, "explorer.exe")
                self.assertEqual(command[1], "shell:AppsFolder\\OpenAI.Codex_2p2nqsd0c76g0!App")

    def test_missing_desktop_launch_never_falls_back_to_other_app(self):
        with patch.object(desktop_apps, "packages", return_value=[]), patch.object(localops.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "not installed"):
                localops.launch_app({"id": "chatgpt-desktop", "desktopApp": "chatgpt"})
            launch.assert_not_called()

    def test_desktop_access_denial_blocks_controls(self):
        with patch.object(desktop_apps, "processes", side_effect=PermissionError("denied")):
            status = localops.desktop_status({"desktopApp": "codex", "controlMode": "manage"})
            self.assertEqual(status["state"], "degraded")
            self.assertFalse(any(status["actions"].values()))

    def test_desktop_confirmation_cannot_be_disabled(self):
        entry = localops.validate_app({"name": "Codex", "desktopApp": "codex", "confirmationRequired": False})
        self.assertTrue(entry["confirmationRequired"])

    def test_web_ownership_requires_folder_and_arguments(self):
        app = {"expectedProcesses": ["node"], "processMatch": {"cwd": str(Path.cwd()), "args": ["server.js"]}}
        process = Mock()
        process.cwd.return_value = str(Path.cwd())
        process.cmdline.return_value = ["node", "server.js"]
        with patch.object(localops.psutil, "Process", return_value=process):
            self.assertTrue(localops.expected_process(app, {"id": 10, "name": "node.exe"}))
            process.cmdline.return_value = ["node", "unrelated.js"]
            self.assertFalse(localops.expected_process(app, {"id": 10, "name": "node.exe"}))
            process.cmdline.return_value = ["node", "server.js"]
            process.cwd.return_value = str(Path.cwd().parent)
            self.assertFalse(localops.expected_process(app, {"id": 10, "name": "node.exe"}))

    def test_manual_web_process_blocks_duplicate_start(self):
        app = {"id": "fixture", "controlMode": "manage", "expectedProcesses": ["node"], "start": {"path": __file__},
               "ports": [{"port": 8781, "role": "managed", "required": True}]}
        with patch.object(localops, "owned_processes", return_value={10: {"id": 10}}):
            status = localops.app_status(app, [], {})
            self.assertEqual(status["state"], "partial")
            self.assertTrue(status["actions"]["stop"])
            self.assertFalse(status["actions"]["start"])

    def test_identity_changes_invalidate_schedule_approval(self):
        base = {"id": "app"}
        self.assertNotEqual(fingerprint(base), fingerprint({**base, "desktopApp": "codex"}))
        self.assertNotEqual(fingerprint(base), fingerprint({**base, "processMatch": {"cwd": "D:/app", "args": ["server.js"]}}))
