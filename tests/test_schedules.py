import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from schedules import Scheduler


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "schedules.json"
        self.app = {"id":"fixture", "name":"Fixture", "controlMode":"manage", "enabled":True, "confirmationRequired":True}
        self.perform = Mock()
        self.audit = Mock()
        self.engine = self.build()

    def build(self):
        return Scheduler(self.path, lambda p,v:p.write_text(json.dumps(v)), lambda _:self.app, self.perform, self.audit)

    def create(self, repeat="once"):
        return self.engine.create({"appId":"fixture","action":"stop","confirmed":True,"repeat":repeat,"runAt":(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()})

    def test_execute_once_persists_and_does_not_replay(self):
        item = self.create()
        now = datetime.fromisoformat(item["nextRunAt"])
        self.engine.tick(now)
        self.engine.tick(now)
        self.build().tick(now)
        self.perform.assert_called_once_with("fixture","stop",{"confirmed":True})
        self.assertEqual(self.engine.items[0]["lastStatus"],"succeeded")

    def test_missed_daily_occurrence_skips_then_advances(self):
        item = self.create("daily")
        now = datetime.fromisoformat(item["nextRunAt"])+timedelta(hours=2)
        self.engine.tick(now)
        self.perform.assert_not_called()
        self.assertEqual(self.engine.items[0]["lastStatus"],"missed")
        self.assertGreater(datetime.fromisoformat(self.engine.items[0]["nextRunAt"]), now)

    def test_configuration_change_blocks_execution(self):
        item = self.create("weekly")
        self.app["start"] = {"path":"new-command"}
        self.engine.tick(datetime.fromisoformat(item["nextRunAt"]))
        self.perform.assert_not_called()
        self.assertFalse(self.engine.items[0]["enabled"])

    def test_interrupted_execution_disabled_after_restart(self):
        self.create("daily")
        self.engine.items[0]["inFlight"] = True
        self.engine._save()
        restored = self.build()
        self.assertFalse(restored.items[0]["enabled"])
        self.assertEqual(restored.items[0]["lastStatus"],"interrupted")

    def test_pause_delete_and_failure_history(self):
        item = self.create()
        self.engine.change(item["id"],"disable")
        self.engine.tick(datetime.fromisoformat(item["nextRunAt"]))
        self.perform.assert_not_called()
        self.engine.change(item["id"],"enable")
        self.perform.side_effect = PermissionError("Access denied")
        self.engine.tick(datetime.fromisoformat(item["nextRunAt"]))
        self.assertEqual(self.engine.items[0]["lastError"],"Access denied")
        self.engine.change(item["id"],"delete")
        self.assertEqual(self.engine.items,[])

    def test_confirmation_and_timezone_required(self):
        with self.assertRaises(ValueError):
            self.engine.create({"appId":"fixture","action":"stop","confirmed":False,"runAt":"2099-01-01T12:00:00+05:30"})
        with self.assertRaises(ValueError):
            self.engine.create({"appId":"fixture","action":"stop","confirmed":True,"runAt":"2099-01-01T12:00:00"})

    def test_fixed_offset_daily_time_preserved(self):
        item = self.engine.create({"appId":"fixture","action":"start","confirmed":True,"repeat":"daily","runAt":"2099-01-01T12:00:00+05:30"})
        self.engine.tick(datetime.fromisoformat(item["nextRunAt"]))
        self.assertEqual(self.engine.items[0]["nextRunAt"],"2099-01-02T12:00:00+05:30")
