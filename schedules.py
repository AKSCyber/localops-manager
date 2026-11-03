"""Persistent application schedules. Claims are saved before side effects (no crash retries)."""
import hashlib
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Schedule time must include its UTC offset.")
    return result


def fingerprint(app):
    fields = {key: app.get(key) for key in ("id", "name", "start", "ports", "workingDirectory", "expectedProcesses", "controlMode", "enabled", "confirmationRequired", "repairStart")}
    # Preserve existing approvals; new identity rules must invalidate old approvals.
    for key in ("desktopApp", "processMatch"):
        if app.get(key):
            fields[key] = app[key]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


class Scheduler:
    def __init__(self, path, save, get_app, perform, audit):
        self.path, self.save, self.get_app, self.perform, self.audit = path, save, get_app, perform, audit
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.pool = None
        try:
            self.items = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.items = []
        # Unknown execution outcome after a crash must never silently replay a stop.
        for item in self.items:
            if item.get("inFlight"):
                item.update(inFlight=False, enabled=False, lastStatus="interrupted", lastError="LocalOps restarted during execution. Review the app and enable or reschedule explicitly.")
        self._save()

    def _save(self):
        self.save(self.path, self.items)

    def payload(self):
        with self.lock:
            return {"schedules": json.loads(json.dumps(self.items)), "missedGraceSeconds": 300}

    def create(self, body):
        app = self.get_app(str(body.get("appId", "")))
        if not app or app.get("controlMode") != "manage" or not app.get("enabled", True):
            raise ValueError("Choose an enabled, managed application.")
        if app.get("confirmationRequired") and body.get("confirmed") is not True:
            raise ValueError(f"Please confirm Yes to schedule controls for {app['name']}.")
        action = body.get("action")
        repeat = body.get("repeat", "once")
        if action not in ("start", "stop") or repeat not in ("once", "daily", "weekly"):
            raise ValueError("Select start or stop, and once, daily, or weekly.")
        run_at = parse_time(body.get("runAt"))
        if run_at <= datetime.now(timezone.utc):
            raise ValueError("Choose a future time.")
        with self.lock:
            if len(self.items) >= 100:
                raise ValueError("Delete old schedules before adding more (limit 100).")
            if any(x["enabled"] and x["appId"] == app["id"] and parse_time(x["nextRunAt"]) == run_at for x in self.items):
                raise ValueError("This application already has a schedule at that time.")
            item = {"id": secrets.token_hex(8), "appId": app["id"], "appName": app["name"], "action": action,
                    "repeat": repeat, "nextRunAt": run_at.isoformat(), "enabled": True, "createdAt": stamp(),
                    "lastStatus": "pending", "lastRunAt": None, "lastError": None, "inFlight": False,
                    "fingerprint": fingerprint(app)}
            self.items.append(item)
            self._save()
        self.audit("schedule", f"Scheduled {action} for {app['name']}.", app["id"], {"scheduleId": item["id"], "nextRunAt": item["nextRunAt"], "repeat": repeat})
        return dict(item)

    def change(self, schedule_id, operation):
        with self.lock:
            item = next((x for x in self.items if x["id"] == schedule_id), None)
            if not item:
                raise KeyError("Schedule not found.")
            if item.get("inFlight"):
                raise ValueError("This schedule is executing; wait for its result before changing it.")
            if operation == "delete":
                self.items.remove(item)
            elif operation in ("enable", "disable"):
                if operation == "enable":
                    app = self.get_app(item["appId"])
                    if not app or fingerprint(app) != item["fingerprint"]:
                        raise ValueError("Application configuration changed. Create a new schedule to approve it.")
                    if parse_time(item["nextRunAt"]) <= datetime.now(timezone.utc):
                        if item["repeat"] == "once":
                            raise ValueError("This one-time schedule has passed. Create a new schedule.")
                        self._advance(item, datetime.now(timezone.utc))
                item["enabled"] = operation == "enable"
            else:
                raise ValueError("Unsupported schedule operation.")
            self._save()
        self.audit("schedule", f"Schedule {operation}: {item['appName']} {item['action']}.", item["appId"])

    def _advance(self, item, now):
        if item["repeat"] == "once":
            item["enabled"] = False
        else:
            # Retain the chosen UTC offset: daily is every 24h, weekly every 7 days.
            step = timedelta(days=1 if item["repeat"] == "daily" else 7)
            next_time = parse_time(item["nextRunAt"])
            while next_time <= now:
                next_time += step
            item["nextRunAt"] = next_time.isoformat()

    def tick(self, now=None):
        now = now or datetime.now(timezone.utc)
        due = []
        changed = False
        with self.lock:
            for item in self.items:
                if not item["enabled"] or item.get("inFlight") or parse_time(item["nextRunAt"]) > now:
                    continue
                late = (now - parse_time(item["nextRunAt"])).total_seconds()
                changed = True
                self._advance(item, now)
                item.update(lastRunAt=now.isoformat(), lastError=None)
                if late > 300:
                    item.update(lastStatus="missed", lastError="LocalOps was unavailable at the scheduled time. This occurrence was skipped.")
                else:
                    item.update(lastStatus="running", inFlight=True)
                    due.append(dict(item))
            if changed:
                self._save()
        for item in due:
            if self.pool:
                self.pool.submit(self._execute, item)
            else:
                self._execute(item)

    def _execute(self, item):
        status, error = "succeeded", None
        try:
            app = self.get_app(item["appId"])
            if not app or fingerprint(app) != item["fingerprint"]:
                raise ValueError("Application configuration changed since this schedule was approved. Create a new schedule.")
            self.perform(app["id"], item["action"], {"confirmed": True})
        except Exception as exc:
            status, error = "failed", str(exc)[:1200]
        with self.lock:
            current = next(x for x in self.items if x["id"] == item["id"])
            current.update(lastStatus=status, lastError=error, inFlight=False)
            if error:
                current["enabled"] = False
            self._save()
        self.audit("schedule", f"Scheduled {item['action']} {status}: {item['appName']}.", item["appId"], {"scheduleId": item["id"], "error": error})

    def start(self):
        from concurrent.futures import ThreadPoolExecutor
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="schedule-action")
        self.thread = threading.Thread(target=self._loop, daemon=True, name="localops-schedules")
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.audit("error", "Schedule scan failed.", detail={"message": str(exc)[:500]})
            self.stop_event.wait(5)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)
        if self.pool:
            self.pool.shutdown(wait=False)
