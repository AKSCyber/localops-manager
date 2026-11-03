from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import fleet
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from schedules import Scheduler
import desktop_apps

try:
    import psutil
except ImportError:  # The dashboard still runs; telemetry reports unavailable.
    psutil = None


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
APPS_FILE = DATA / "apps.json"
EVENTS_FILE = DATA / "events.json"
AUTH_FILE = DATA / "auth.json"
SESSIONS_FILE = DATA / "sessions.json"
PUBLIC_HOST = os.environ.get("LOCALOPS_PUBLIC_HOST", "").strip()
if not PUBLIC_HOST:
    try:
        PUBLIC_HOST = json.loads((DATA / "node-config.json").read_text(encoding="utf-8"))["publicHost"]
    except FileNotFoundError:
        PUBLIC_HOST = "localops.solliegiance.com"
if not isinstance(PUBLIC_HOST, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", PUBLIC_HOST):
    raise ValueError("Configure a valid publicHost in data/node-config.json or LOCALOPS_PUBLIC_HOST")
PUBLIC_HOST = PUBLIC_HOST.lower()
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
DETACHED_PROCESS = 0x00000008 if IS_WINDOWS else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if IS_WINDOWS else 0
SESSION_SECONDS = 7 * 24 * 60 * 60
APP_VERSION = "1.6.0"
SCHEDULER = None
POWER_CONFIRMATIONS = {
    "shutdown": "SHUTDOWN THIS PC",
    "hibernate": "HIBERNATE THIS PC",
}


NODES_LOCK = threading.RLock()


def node_id(name: str) -> str:
    return hashlib.sha256(name.strip().casefold().encode("utf-8")).hexdigest()[:24]


def node_identity() -> dict:
    return FLEET.identity()


def node_catalog() -> list[dict]:
    current = node_identity()
    with NODES_LOCK:
        saved = load_json(DATA / "nodes.json", [])
        return [current, *[item for item in saved if item.get("id") != current["id"]]]


def register_node(body: dict) -> dict:
    name = str(body.get("computerName", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", name):
        raise ValueError("Enter the computer's Windows name (letters, numbers, dots or hyphens).")
    parsed = urllib.parse.urlsplit(str(body.get("url", "")).strip())
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("Use this computer's independent HTTPS LocalOps address, without a path or credentials.")
    parsed.port  # Reject invalid ports before saving.
    item = {"id": node_id(name), "computerName": name, "url": parsed.geturl().rstrip("/")}
    if name.casefold() == node_identity()["computerName"].casefold():
        raise ValueError("This computer is already registered automatically.")
    with NODES_LOCK:
        nodes = [entry for entry in node_catalog()[1:] if entry["id"] != item["id"]]
        if any(entry["url"].casefold() == item["url"].casefold() for entry in node_catalog()):
            raise ValueError("Each computer needs its own LocalOps address; shared tunnel addresses cannot select a node.")
        if len(nodes) >= 19:
            raise ValueError("Up to twenty computers can be registered.")
        atomic_json(DATA / "nodes.json", [*nodes, item])
    return item


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def protect_secret(value: str) -> str:
    if not IS_WINDOWS:
        return "plain:" + b64(value.encode())
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    raw = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "LocalOps", None, None, None, 1, ctypes.byref(output)):
        raise OSError("Windows could not protect the LocalOps secret.")
    try:
        return "dpapi:" + b64(ctypes.string_at(output.pbData, output.cbData))
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def unprotect_secret(value: str) -> str:
    if value.startswith("plain:"):
        return unb64(value[6:]).decode()
    if not value.startswith("dpapi:") or not IS_WINDOWS:
        raise ValueError("Unsupported protected secret.")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    raw = unb64(value[6:])
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError("Windows could not unlock the LocalOps secret.")
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${b64(salt)}${b64(digest)}"


def password_matches(password: str, stored: str) -> bool:
    try:
        algorithm, salt, expected = stored.split("$", 2)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=unb64(salt), n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, unb64(expected))
    except (ValueError, TypeError):
        return False


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def verify_totp(secret: str, code: str, last_counter: int = -1) -> int | None:
    code = re.sub(r"\D", "", code)
    if len(code) != 6:
        return None
    current = int(time.time()) // 30
    for counter in range(current - 1, current + 2):
        if counter > last_counter and hmac.compare_digest(totp_at(secret, counter), code):
            return counter
    return None


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.apps = load_json(APPS_FILE, [])
        self.events = load_json(EVENTS_FILE, [])

    def save_apps(self) -> None:
        with self.lock:
            atomic_json(APPS_FILE, self.apps)

    def add_event(self, kind: str, message: str, app_id: str | None = None, detail: dict | None = None) -> None:
        event = {
            "id": secrets.token_hex(6),
            "at": utc_now(),
            "kind": kind,
            "appId": app_id,
            "message": message,
            "detail": detail or {},
        }
        with self.lock:
            self.events.insert(0, event)
            self.events = self.events[:500]
            atomic_json(EVENTS_FILE, self.events)

    def add_events(self, events: list[dict]) -> None:
        if not events:
            return
        prepared = [{"id": secrets.token_hex(6), "at": utc_now(), **event} for event in events]
        with self.lock:
            self.events = (prepared + self.events)[:500]
            atomic_json(EVENTS_FILE, self.events)

    def get_app(self, app_id: str) -> dict | None:
        with self.lock:
            return next((app for app in self.apps if app.get("id") == app_id), None)


class AuthStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.auth = load_json(AUTH_FILE, None)
        self.sessions = load_json(SESSIONS_FILE, {})
        self.setup_secrets: dict[str, tuple[str, float]] = {}
        self.login_attempts: dict[str, list[float]] = {}
        self._prune_sessions()

    def configured(self) -> bool:
        return isinstance(self.auth, dict) and bool(self.auth.get("passwordHash") and self.auth.get("totpSecret"))

    def verify_password(self, password: str) -> bool:
        with self.lock:
            return self.configured() and password_matches(password, str(self.auth.get("passwordHash", "")))

    def _prune_sessions(self) -> None:
        now = time.time()
        with self.lock:
            self.sessions = {key: value for key, value in self.sessions.items() if float(value.get("expiresAt", 0)) > now}
            atomic_json(SESSIONS_FILE, self.sessions)

    def begin_setup(self) -> dict:
        with self.lock:
            if self.configured():
                raise ValueError("LocalOps security is already configured.")
            secret = new_totp_secret()
            token = secrets.token_urlsafe(24)
            self.setup_secrets[token] = (secret, time.time() + 600)
            uri = "otpauth://totp/LocalOps:admin?" + urllib.parse.urlencode({"secret": secret, "issuer": "LocalOps", "algorithm": "SHA1", "digits": 6, "period": 30})
            return {"setupToken": token, "secret": secret, "uri": uri}

    def complete_setup(self, body: dict, ip: str, user_agent: str) -> tuple[dict, str, str]:
        with self.lock:
            if self.configured():
                raise ValueError("LocalOps security is already configured.")
            entry = self.setup_secrets.pop(str(body.get("setupToken", "")), None)
            password = str(body.get("password", ""))
            username = str(body.get("username", "admin")).strip()[:80] or "admin"
            if not entry or entry[1] < time.time():
                raise ValueError("The setup key expired; start setup again.")
            if len(password) < 12:
                raise ValueError("Password must contain at least 12 characters.")
            counter = verify_totp(entry[0], str(body.get("code", "")))
            if counter is None:
                raise ValueError("The Authenticator code is incorrect.")
            recovery = ["-".join((secrets.token_hex(3).upper(), secrets.token_hex(3).upper())) for _ in range(10)]
            self.auth = {
                "username": username,
                "passwordHash": password_hash(password),
                "totpSecret": protect_secret(entry[0]),
                "lastTotpCounter": counter,
                "recoveryHashes": [hashlib.sha256(item.encode()).hexdigest() for item in recovery],
                "createdAt": utc_now(),
            }
            atomic_json(AUTH_FILE, self.auth)
            token, csrf = self.create_session(ip, user_agent)
            return {"ok": True, "username": username, "recoveryCodes": recovery}, token, csrf

    def create_session(self, ip: str, user_agent: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(40)
        csrf = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            self.sessions[digest] = {"csrf": csrf, "expiresAt": time.time() + SESSION_SECONDS, "createdAt": utc_now(), "ip": ip, "userAgent": user_agent[:240]}
            atomic_json(SESSIONS_FILE, self.sessions)
        return token, csrf

    def session(self, token: str) -> dict | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            value = self.sessions.get(digest)
            if not value:
                return None
            if float(value.get("expiresAt", 0)) <= time.time():
                self.sessions.pop(digest, None)
                atomic_json(SESSIONS_FILE, self.sessions)
                return None
            return dict(value)

    def logout(self, token: str) -> None:
        if not token:
            return
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            atomic_json(SESSIONS_FILE, self.sessions)

    def login(self, body: dict, ip: str, user_agent: str) -> tuple[str, str]:
        now = time.time()
        with self.lock:
            attempts = [value for value in self.login_attempts.get(ip, []) if value > now - 900]
            self.login_attempts[ip] = attempts
            if len(attempts) >= 8:
                raise PermissionError("Too many attempts; wait fifteen minutes.")
            if not self.configured():
                raise RuntimeError("Complete security setup from this Windows computer first.")
            valid_password = password_matches(str(body.get("password", "")), self.auth["passwordHash"])
            code = re.sub(r"[^A-Fa-f0-9-]", "", str(body.get("code", ""))).upper()
            valid_second = False
            new_counter = None
            used_recovery_digest = None
            if re.fullmatch(r"\d{6}", code):
                try:
                    secret = unprotect_secret(self.auth["totpSecret"])
                except (OSError, ValueError):
                    if valid_password:
                        raise PermissionError("This Authenticator secret belongs to the old Windows account. Use a recovery code or transfer your LocalOps secret from the old machine.")
                else:
                    new_counter = verify_totp(secret, code, int(self.auth.get("lastTotpCounter", -1)))
                    valid_second = new_counter is not None
            else:
                digest = hashlib.sha256(code.encode()).hexdigest()
                hashes = list(self.auth.get("recoveryHashes", []))
                if digest in hashes:
                    valid_second = True
                    used_recovery_digest = digest
            if not (valid_password and valid_second):
                attempts.append(now)
                raise PermissionError("Invalid password or verification code.")
            if new_counter is not None:
                self.auth["lastTotpCounter"] = new_counter
            if used_recovery_digest is not None:
                hashes = list(self.auth.get("recoveryHashes", []))
                if used_recovery_digest in hashes:
                    hashes.remove(used_recovery_digest)
                    self.auth["recoveryHashes"] = hashes
            atomic_json(AUTH_FILE, self.auth)
            self.login_attempts.pop(ip, None)
        return self.create_session(ip, user_agent)


STORE = Store()
AUTH = AuthStore()
FLEET = fleet.Fleet(sys.modules[__name__])
CSRF_TOKEN = secrets.token_urlsafe(32)
STATUS_LOCK = threading.Lock()
STATUS_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
LAST_STATES: dict[str, str] = {}
SERVER_PORT = 8790
PROCESS_CPU_LOCK = threading.Lock()
PROCESS_CPU_SAMPLES: dict[int, tuple[float, float]] = {}
ACTION_LOCKS: dict[str, threading.Lock] = {}
ACTION_LOCKS_LOCK = threading.Lock()


def run_quiet(command: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def parse_gpu_row(value: str) -> dict | None:
    rows = [row.strip() for row in value.splitlines() if row.strip()]
    if not rows:
        return None
    parts = [part.strip() for part in rows[0].split(",")]
    if len(parts) < 6:
        return None

    def number(raw: str) -> float | None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return {
        "name": parts[0],
        "utilizationPercent": number(parts[1]),
        "memoryUsedMb": number(parts[2]),
        "memoryTotalMb": number(parts[3]),
        "temperatureC": number(parts[4]),
        "powerWatts": number(parts[5]),
    }


def cpu_temperature() -> dict:
    value = load_json(DATA / "cpu-temperature.json", {})
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value["sampledAt"].replace("Z", "+00:00"))).total_seconds()
        sensors = [row for row in value.get("sensors", []) if isinstance(row.get("celsius"), (int, float)) and 0 < row["celsius"] < 150]
        if -5 <= age <= 30 and sensors:
            package = [row for row in sensors if "package" in row["name"].lower() or "tctl" in row["name"].lower()]
            return {"available": True, "celsius": round(max(row["celsius"] for row in package or sensors), 1),
                    "source": "Libre Hardware Monitor", "sampledAt": value["sampledAt"], "sensors": sensors}
    except (KeyError, TypeError, ValueError):
        pass
    return {"available": False, "celsius": None, "reason": value.get("error") or "CPU sensor reader is unavailable or its reading is stale."}


class MetricsSampler:
    def __init__(self, interval: float = 2.0, history_points: int = 180) -> None:
        self.interval = interval
        self.lock = threading.RLock()
        self.history: list[dict] = []
        self.latest: dict = {"available": psutil is not None, "sampledAt": None}
        self.previous_at: float | None = None
        self.previous_network: tuple[int, int] | None = None
        self.previous_disk: tuple[int, int] | None = None
        self.history_points = history_points
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        if psutil:
            psutil.cpu_percent(interval=None)
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="localops-metrics", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.sample()
            except Exception as error:
                with self.lock:
                    self.latest = {"available": False, "sampledAt": utc_now(), "error": type(error).__name__}
            self.stop_event.wait(max(0.2, self.interval - (time.monotonic() - started)))

    def sample(self) -> None:
        if not psutil:
            return
        now_monotonic = time.monotonic()
        memory = psutil.virtual_memory()
        network = psutil.net_io_counters()
        disk = psutil.disk_io_counters()
        elapsed = max(0.001, now_monotonic - self.previous_at) if self.previous_at else None
        down_bps = up_bps = read_bps = write_bps = 0.0
        if elapsed and self.previous_network:
            down_bps = max(0.0, (network.bytes_recv - self.previous_network[0]) / elapsed)
            up_bps = max(0.0, (network.bytes_sent - self.previous_network[1]) / elapsed)
        if elapsed and disk and self.previous_disk:
            read_bps = max(0.0, (disk.read_bytes - self.previous_disk[0]) / elapsed)
            write_bps = max(0.0, (disk.write_bytes - self.previous_disk[1]) / elapsed)
        self.previous_at = now_monotonic
        self.previous_network = (network.bytes_recv, network.bytes_sent)
        self.previous_disk = (disk.read_bytes, disk.write_bytes) if disk else None

        gpu = None
        if IS_WINDOWS:
            try:
                result = run_quiet(["nvidia-smi.exe", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"], timeout=2.5)
                gpu = parse_gpu_row(result.stdout) if result.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                gpu = None
        gpu_vram_percent = 0.0
        if gpu and gpu.get("memoryTotalMb"):
            gpu_vram_percent = float(gpu.get("memoryUsedMb") or 0) / float(gpu["memoryTotalMb"]) * 100

        volumes = []
        for root in (["C:\\", "D:\\"] if IS_WINDOWS else ["/"]):
            if not Path(root).exists():
                continue
            try:
                usage = psutil.disk_usage(root)
                volumes.append({"root": root, "usedBytes": usage.used, "totalBytes": usage.total, "percent": usage.percent})
            except OSError:
                pass

        temperature = cpu_temperature()
        point = {
            "at": utc_now(),
            "cpuTemperatureC": temperature["celsius"],
            "cpuPercent": round(float(psutil.cpu_percent(interval=None)), 1),
            "memoryPercent": round(float(memory.percent), 1),
            "gpuPercent": round(float((gpu or {}).get("utilizationPercent") or 0), 1),
            "gpuVramPercent": round(gpu_vram_percent, 1),
            "networkDownBps": round(down_bps),
            "networkUpBps": round(up_bps),
            "diskReadBps": round(read_bps),
            "diskWriteBps": round(write_bps),
        }
        latest = {
            "available": True,
            "sampledAt": point["at"],
            "cpu": {"percent": point["cpuPercent"], "logicalCores": psutil.cpu_count(logical=True), "physicalCores": psutil.cpu_count(logical=False), "temperature": temperature},
            "memory": {"percent": point["memoryPercent"], "usedBytes": memory.used, "availableBytes": memory.available, "totalBytes": memory.total},
            "gpu": gpu,
            "network": {"downBps": point["networkDownBps"], "upBps": point["networkUpBps"], "receivedBytes": network.bytes_recv, "sentBytes": network.bytes_sent},
            "disk": {"readBps": point["diskReadBps"], "writeBps": point["diskWriteBps"], "volumes": volumes},
        }
        with self.lock:
            self.latest = latest
            self.history.append(point)
            self.history = self.history[-self.history_points:]

    def payload(self) -> dict:
        with self.lock:
            return {"latest": dict(self.latest), "history": list(self.history), "intervalSeconds": self.interval}


METRICS = MetricsSampler()


def parse_endpoint(value: str) -> tuple[str, int] | None:
    value = value.strip()
    if value.startswith("["):
        match = re.match(r"^\[(.+)]:(\d+)$", value)
    else:
        match = re.match(r"^(.+):(\d+)$", value)
    return (match.group(1), int(match.group(2))) if match else None


def discover_listeners() -> list[dict]:
    if not IS_WINDOWS:
        return []
    if psutil:
        try:
            connections = psutil.net_connections(kind="tcp")
            return [{"address": address, "port": port, "pid": pid}
                    for address, port, pid in sorted({(connection.laddr.ip, connection.laddr.port, connection.pid)
                        for connection in connections
                        if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.pid is not None})]
        except (psutil.Error, OSError) as exc:
            raise RuntimeError("Windows listener discovery failed; application state cannot be verified.") from exc
    try:
        result = run_quiet(["netstat", "-ano", "-p", "tcp"], timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Windows listener discovery failed; application state cannot be verified.") from exc
    if result.returncode != 0:
        raise RuntimeError("Windows listener discovery failed; application state cannot be verified.")
    listeners: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for raw in result.stdout.splitlines():
        columns = raw.split()
        if len(columns) < 5 or columns[0].upper() != "TCP" or columns[3].upper() != "LISTENING":
            continue
        endpoint = parse_endpoint(columns[1])
        if not endpoint or not columns[4].isdigit():
            continue
        address, port = endpoint
        pid = int(columns[4])
        key = (address, port, pid)
        if key not in seen:
            seen.add(key)
            listeners.append({"address": address, "port": port, "pid": pid})
    return listeners


def process_details(pids: set[int]) -> dict[int, dict]:
    if not IS_WINDOWS or not pids:
        return {}
    if psutil:
        details: dict[int, dict] = {}
        now = time.monotonic()
        cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
        with PROCESS_CPU_LOCK:
            for pid in pids:
                try:
                    process = psutil.Process(pid)
                    cpu_times = process.cpu_times()
                    total_cpu = float(cpu_times.user + cpu_times.system)
                    previous = PROCESS_CPU_SAMPLES.get(pid)
                    cpu_percent = 0.0
                    if previous and now > previous[0]:
                        cpu_percent = max(0.0, min(100.0, (total_cpu - previous[1]) / (now - previous[0]) * 100 / cpu_count))
                    PROCESS_CPU_SAMPLES[pid] = (now, total_cpu)
                    details[pid] = {"id": pid, "name": process.name().removesuffix(".exe"), "path": process.exe() or None, "started": datetime.fromtimestamp(process.create_time(), timezone.utc).isoformat(), "cpuSeconds": total_cpu, "cpuPercent": round(cpu_percent, 1), "memoryBytes": process.memory_info().rss}
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
            for stale_pid in [pid for pid, sample in PROCESS_CPU_SAMPLES.items() if now - sample[0] > 60]:
                PROCESS_CPU_SAMPLES.pop(stale_pid, None)
        return details
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        query_image = kernel32.QueryFullProcessImageNameW
        query_image.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]

        class MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        details: dict[int, dict] = {}
        for pid in pids:
            handle = open_process(0x1000 | 0x0010, False, pid)  # limited query + VM read
            if not handle:
                handle = open_process(0x1000, False, pid)
            if not handle:
                continue
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                path = buffer.value if query_image(handle, 0, buffer, ctypes.byref(size)) else ""
                counters = MemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                memory = int(counters.WorkingSetSize) if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb) else 0
                details[pid] = {"id": pid, "name": Path(path).stem if path else "", "path": path or None, "started": None, "cpuSeconds": 0, "memoryBytes": memory}
            finally:
                close_handle(handle)
        return details
    except (OSError, ValueError, AttributeError):
        return {}


def check_health(url: str, accepted: list[int] | None = None) -> dict:
    started = time.monotonic()
    accepted = accepted or [200, 204]
    parsed = urllib.parse.urlparse(url)
    if IS_WINDOWS and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        try:
            result = run_quiet([
                "curl.exe", "--silent", "--show-error", "--output", "NUL",
                "--write-out", "%{http_code} %{time_total}", "--max-time", "2",
                "--noproxy", "*", url,
            ], timeout=4)
            parts = result.stdout.strip().split()
            code = int(parts[0]) if parts and parts[0].isdigit() and parts[0] != "000" else None
            return {"ok": code in accepted, "reachable": code is not None, "code": code, "latencyMs": round((time.monotonic() - started) * 1000), **({"error": "TimeoutOrDns"} if code is None else {})}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {"ok": False, "reachable": False, "code": None, "latencyMs": round((time.monotonic() - started) * 1000), "error": "TimeoutOrDns"}
    request = urllib.request.Request(url, headers={"User-Agent": "LocalOps/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=2.2) as response:
            code = int(response.status)
        return {"ok": code in accepted, "reachable": True, "code": code, "latencyMs": round((time.monotonic() - started) * 1000)}
    except urllib.error.HTTPError as error:
        code = int(error.code)
        return {"ok": code in accepted, "reachable": True, "code": code, "latencyMs": round((time.monotonic() - started) * 1000)}
    except Exception as error:
        return {"ok": False, "reachable": False, "code": None, "latencyMs": round((time.monotonic() - started) * 1000), "error": type(error).__name__}


def expected_process(app: dict, process: dict | None) -> bool:
    expected = {str(name).lower().removesuffix(".exe") for name in app.get("expectedProcesses", [])}
    if not expected:
        return False
    actual = str((process or {}).get("name", "")).lower().removesuffix(".exe")
    if actual not in expected:
        return False
    rule = app.get("processMatch")
    if not rule:
        return True
    if not psutil or not process or not process.get("id"):
        return False
    try:
        live = psutil.Process(process["id"])
        if rule.get("cwd") and os.path.normcase(live.cwd()) != os.path.normcase(rule["cwd"]):
            return False
        arguments = live.cmdline()
        return all(argument in arguments for argument in rule.get("args", []))
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def desktop_status(app: dict) -> dict:
    error = None
    installed, pids, details = [], set(), {}
    try:
        installed, pids = desktop_apps.processes(app["desktopApp"], psutil)
        details = process_details(pids)
        if pids - details.keys():
            raise PermissionError("Desktop process identity could not be fully inspected.")
    except (OSError, RuntimeError) as exc:
        error = str(exc)
    available = bool(installed) and not error
    managed = app.get("controlMode") == "manage" and available
    return {**app, "state": "degraded" if error else "running" if pids else "stopped" if installed else "unconfigured",
            "ports": [], "health": None, "pids": sorted(pids), "desktopProcesses": list(details.values()),
            "desktopInstalled": bool(installed), "desktopError": error,
            "desktopLauncher": installed[0]["aumid"] if installed else None,
            "runtimeWorkers": [], "runtimeWorkerError": None, "repairStartAvailable": False,
            "memoryBytes": sum(item.get("memoryBytes", 0) for item in details.values()),
            "cpuSeconds": round(sum(item.get("cpuSeconds", 0) for item in details.values()), 1),
            "cpuPercent": round(sum(item.get("cpuPercent", 0) for item in details.values()), 1),
            "ownershipSafe": False if error else True if pids else None,
            "actions": {"start": managed and not pids, "stop": managed and bool(pids), "restart": managed and bool(pids)},
            "startAvailable": available}


def owned_processes(app: dict) -> dict[int, dict]:
    if not app.get("processMatch") or not psutil:
        return {}
    pids = {process.pid for process in psutil.process_iter(["pid", "name"])
            if expected_process(app, {"id": process.pid, "name": process.info.get("name")})}
    return process_details(pids)


def app_status(app: dict, listeners: list[dict], processes: dict[int, dict]) -> dict:
    if app.get("desktopApp"):
        return desktop_status(app)
    port_results = []
    managed_live = []
    required_total = 0
    required_live = 0
    ownership_safe = True
    for item in app.get("ports", []):
        port = int(item.get("port", 0))
        matches = [listener for listener in listeners if listener["port"] == port]
        live = bool(matches)
        required = bool(item.get("required", True))
        managed = item.get("role", "managed") == "managed"
        if required:
            required_total += 1
            required_live += int(live)
        details = []
        for listener in matches:
            process = processes.get(listener["pid"])
            safe = expected_process(app, process) if managed else None
            if managed:
                ownership_safe = ownership_safe and bool(safe)
                managed_live.append(listener)
            details.append({**listener, "process": process, "ownershipMatch": safe})
        port_results.append({**item, "live": live, "listeners": details})

    health = None
    health_url = app.get("healthUrl")
    if health_url:
        parsed_health = urllib.parse.urlparse(health_url)
        health_port = parsed_health.port or (443 if parsed_health.scheme == "https" else 80)
        local_health = parsed_health.hostname in {"127.0.0.1", "localhost", "::1"}
        if local_health and not any(listener["port"] == health_port for listener in listeners):
            health = {"ok": False, "reachable": False, "code": None, "latencyMs": 0, "error": "PortClosed"}
        else:
            health = check_health(health_url, app.get("acceptedHealthStatuses"))

    external_only = not app.get("ports") and bool(health_url)
    if external_only:
        state = "running" if health and health["ok"] else "offline"
    elif required_total == 0:
        state = "unconfigured"
    elif not managed_live:
        state = "stopped"
    elif required_live < required_total:
        state = "partial"
    elif health and not health["ok"]:
        state = "degraded"
    else:
        state = "running"

    owned = owned_processes(app)
    processes = {**processes, **owned}
    unique_pids = sorted({item["pid"] for item in managed_live} | owned.keys())
    if not managed_live and owned:
        state = "partial"
    companions = []
    companion_error = None
    try:
        companions = runtime_workers(app, unique_pids)
    except (OSError, PermissionError) as exc:
        companion_error = str(exc)
    if not managed_live and companions:
        state = "partial"
    memory = sum(int(processes.get(pid, {}).get("memoryBytes") or 0) for pid in unique_pids) + sum(int(worker.get("memoryBytes") or 0) for worker in companions)
    cpu = sum(float(processes.get(pid, {}).get("cpuSeconds") or 0) for pid in unique_pids)
    cpu_percent = sum(float(processes.get(pid, {}).get("cpuPercent") or 0) for pid in unique_pids)
    can_stop = bool(unique_pids or companions) and ownership_safe and app.get("controlMode", "observe") == "manage"
    start = app.get("start") or {}
    start_path = str(start.get("path", ""))
    start_exists = bool(start_path and (Path(start_path).exists() or start.get("type") == "command"))
    occupied = bool(unique_pids or companions)
    repair_start = bool(app.get("repairStart") and state == "partial" and ownership_safe and managed_live)
    can_start = app.get("controlMode", "observe") == "manage" and start_exists and (not occupied or repair_start)
    return {
        **app,
        "state": state,
        "ports": port_results,
        "health": health,
        "pids": unique_pids,
        "ownedProcesses": list(owned.values()),
        "runtimeWorkers": companions,
        "runtimeWorkerError": companion_error,
        "repairStartAvailable": repair_start,
        "memoryBytes": memory,
        "cpuSeconds": round(cpu, 1),
        "cpuPercent": round(cpu_percent, 1),
        "ownershipSafe": ownership_safe if unique_pids or companions else None,
        "actions": {"start": can_start, "stop": can_stop, "restart": can_stop and start_exists},
        "startAvailable": start_exists,
    }


def snapshot(force: bool = False) -> dict:
    now = time.monotonic()
    if not force and STATUS_CACHE["payload"] is not None and now - STATUS_CACHE["at"] < 2.0:
        return STATUS_CACHE["payload"]
    acquired = STATUS_LOCK.acquire(blocking=force)
    if not acquired:
        if STATUS_CACHE["payload"] is not None:
            return STATUS_CACHE["payload"]
        STATUS_LOCK.acquire()
    try:
        now = time.monotonic()
        if not force and STATUS_CACHE["payload"] is not None and now - STATUS_CACHE["at"] < 2.0:
            return STATUS_CACHE["payload"]
        listeners = discover_listeners()
        processes = process_details({item["pid"] for item in listeners})
        with STORE.lock:
            configured_apps = [dict(app) for app in STORE.apps if app.get("enabled", True)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(configured_apps)))) as pool:
            apps = list(pool.map(lambda app: app_status(app, listeners, processes), configured_apps))
        known_ports = {int(port["port"]) for app in apps for port in app.get("ports", [])}
        discoveries = []
        for listener in listeners:
            process = processes.get(listener["pid"])
            if listener["port"] in known_ports or listener["port"] in {SERVER_PORT} or listener["port"] < 1024 or listener["pid"] in {0, 4}:
                continue
            if listener["port"] >= 49152 and not (process or {}).get("name"):
                continue
            discoveries.append({**listener, "process": process, "known": False})
        discoveries.sort(key=lambda item: item["port"])
        transition_events = []
        for app in apps:
            old = LAST_STATES.get(app["id"])
            new = app["state"]
            if old and old != new:
                transition_events.append({"kind": "state", "message": f"{app['name']} changed from {old} to {new}.", "appId": app["id"], "detail": {"from": old, "to": new}})
            LAST_STATES[app["id"]] = new
        STORE.add_events(transition_events)
        payload = {
            "generatedAt": utc_now(),
            "apps": apps,
            "discoveries": discoveries,
            "events": STORE.events[:80],
            "summary": {
                "running": sum(app["state"] == "running" for app in apps),
                "attention": sum(app["state"] in {"partial", "degraded"} for app in apps),
                "offline": sum(app["state"] in {"stopped", "offline"} for app in apps),
                "observedListeners": len(listeners),
            },
        }
        STATUS_CACHE.update({"at": now, "payload": payload})
        return payload
    finally:
        STATUS_LOCK.release()


def launch_app(app: dict) -> dict:
    start = app.get("start") or {}
    path = str(start.get("path", ""))
    args = [str(arg) for arg in start.get("args", [])]
    # Restore Desktop when LocalOps stopped its supervisor. Desktop owns its
    # model-directory settings; launching a bare server can use different ones.
    if app.get("id") == "ollama" and load_json(DATA / "ollama-desktop-pause.json", {}).get("wasRunning"):
        desktop = ollama_desktop_path(app)
        if not desktop or not desktop.is_file():
            raise ValueError("The previously stopped Ollama Desktop executable is missing. Review its installation before starting.")
        path, args = str(desktop), []
    if app.get("desktopApp"):
        installed = desktop_apps.packages(app["desktopApp"])
        if not installed:
            raise ValueError("This Windows desktop app is not installed for the LocalOps user.")
        path = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "explorer.exe")
        args = ["shell:AppsFolder\\" + installed[0]["aumid"]]
    if not path:
        raise ValueError("No start command is configured.")
    start_type = start.get("type", "file")
    if start_type != "command" and not Path(path).is_file():
        raise ValueError(f"Start file does not exist: {path}")
    suffix = Path(path).suffix.lower()
    if suffix in {".cmd", ".bat"}:
        invocation = f'call "{path}"'
        if args:
            invocation += " " + subprocess.list2cmdline(args)
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", invocation]
    elif suffix == ".ps1":
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path, *args]
    else:
        command = [path, *args]
    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOGS / f"{app['id']}-{stamp}.log"
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        log_handle.write(f"[{utc_now()}] Launching: {subprocess.list2cmdline(command)}\n")
        log_handle.flush()
        process = subprocess.Popen(
            command,
            cwd=app.get("workingDirectory") or str(Path(path).parent),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    finally:
        log_handle.close()
    return {"pid": process.pid, "log": str(log_path), "_process": process}


def wait_for_app_state(app_id: str, running: bool, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        STATUS_CACHE["at"] = 0.0
        latest = next((item for item in snapshot(force=True)["apps"] if item["id"] == app_id), None)
        if latest:
            if running and latest["state"] == "running" and latest["pids"]:
                return latest
            if not running and not latest["pids"] and not any(port["live"] for port in latest["ports"] if port["role"] == "managed"):
                return latest
        time.sleep(0.5)
    return latest


def tail_text(path: str, lines: int = 20) -> str:
    try:
        return "\n".join(Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])[-2000:]
    except OSError:
        return ""


def ollama_desktop_path(app: dict) -> Path | None:
    server = Path((app.get("start") or {}).get("path", ""))
    if app.get("id") != "ollama" or server.name.lower() not in {"ollama.exe", "ollama app.exe"}:
        return None
    return server.with_name("ollama app.exe")


def ollama_supervised_processes(app: dict, status: dict) -> list[dict]:
    """Only the configured installation's Desktop and its Ollama children.

    Never kill arbitrary ancestors, processes by name alone, or other apps that
    happen to consume this shared runtime.
    """
    desktop = ollama_desktop_path(app)
    if not desktop or not psutil:
        return []
    server_path = os.path.normcase(str(desktop.with_name("ollama.exe")))
    desktop_path = os.path.normcase(str(desktop))
    worker_path = os.path.normcase(str(desktop.parent / "lib" / "ollama" / "llama-server.exe"))
    identities = []
    for process in psutil.process_iter(["pid", "name"]):
        if (process.info.get("name") or "").lower() != "ollama app.exe":
            continue
        try:
            if os.path.normcase(process.exe()) != desktop_path:
                continue
            children = process.children(recursive=True)
            # A listener owned by another installation must not authorize this
            # Desktop. A listener-free Desktop can be in its retry interval.
            if status["pids"] and not set(status["pids"]).intersection(child.pid for child in children):
                continue
            for target in [process, *children]:
                executable = target.exe()
                if os.path.normcase(executable) not in {desktop_path, server_path, worker_path}:
                    continue
                identities.append({"pid": target.pid, "path": executable, "name": target.name(),
                                   "started": datetime.fromtimestamp(target.create_time(), timezone.utc).isoformat(),
                                   "supervisor": target.pid == process.pid, "runtimeWorker": os.path.normcase(executable) == worker_path})
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise PermissionError("Cannot inspect Ollama Desktop safely. Run LocalOps through its elevated startup task.") from exc
    return identities


def runtime_workers(app: dict, managed_pids: list[int]) -> list[dict]:
    """Identify model workers by exact installation path plus lineage or orphan status.

    Ollama's llama-server is NOT the separately configured llama.cpp router.
    A worker with a live, unrelated parent is never taken over.
    """
    if not psutil or app.get("id") not in {"ollama", "llama-cpp"}:
        return []
    desktop = ollama_desktop_path(app)
    expected = desktop.parent / "lib" / "ollama" / "llama-server.exe" if desktop else Path((app.get("start") or {}).get("path", ""))
    if expected.name.lower() != "llama-server.exe":
        return []
    workers = []
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid in managed_pids or (process.info.get("name") or "").lower() != "llama-server.exe":
            continue
        try:
            if os.path.normcase(process.exe()) != os.path.normcase(str(expected)):
                continue
            args = process.cmdline()
            if not any(flag in args for flag in ("--model", "-m")):
                continue
            parents = process.parents()
            orphaned = not parents
            if not orphaned and not any(parent.pid in managed_pids for parent in parents):
                continue
            workers.append({"pid":process.pid, "name":process.name(), "path":process.exe(),
                            "started":datetime.fromtimestamp(process.create_time(), timezone.utc).isoformat(),
                            "runtimeWorker":True, "orphaned":orphaned, "memoryBytes":process.memory_info().rss})
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise PermissionError("Windows denied model-worker inspection; run LocalOps elevated.") from exc
    return workers


def verify_stays_stopped(app: dict, quiet_seconds: float = 8) -> None:
    ports = {int(port["port"]) for port in app.get("ports", []) if port.get("role", "managed") == "managed"}
    deadline = time.monotonic() + quiet_seconds
    while True:
        if app.get("desktopApp") and desktop_apps.processes(app["desktopApp"], psutil)[1]:
            raise RuntimeError("The desktop app restarted after stopping. No replacement process was killed.")
        if owned_processes(app):
            raise RuntimeError("An owned application process remains or restarted after stopping.")
        if any(item["port"] in ports for item in discover_listeners()):
            raise RuntimeError("The application restarted after stopping. Another launcher is still active; no replacement process was killed.")
        if runtime_workers(app, []):
            raise RuntimeError("A verified model worker remains after stop; GPU resources may still be allocated.")
        if time.monotonic() >= deadline:
            return
        time.sleep(0.5)


def stop_app(app: dict) -> dict:
    if app.get("controlMode") != "manage":
        raise ValueError("This application is observe-only and cannot be stopped.")
    status = next((item for item in snapshot(force=True)["apps"] if item["id"] == app["id"]), None)
    if not status or (status["pids"] and not status["actions"]["stop"]):
        raise ValueError("Stop was refused because the live process does not match this application's safety rules.")
    if not psutil:
        raise RuntimeError("Process controls require psutil. Install requirements.txt and restart LocalOps.")
    identities = {item["pid"]: item["process"] for port in status["ports"] if port["role"] == "managed" for item in port["listeners"]}
    identities.update({item["id"]: item for item in status.get("desktopProcesses", []) + status.get("ownedProcesses", [])})
    supervised = ollama_supervised_processes(app, status) + runtime_workers(app, status["pids"])
    if any(item.get("supervisor") for item in supervised):
        atomic_json(DATA / "ollama-desktop-pause.json", {"wasRunning": True, "at": utc_now()})
    targets = {item["pid"]: item for item in supervised}
    targets.update({pid: {**identity, "pid": pid} for pid, identity in identities.items() if pid not in targets})
    results = []
    # Desktop must exit first; otherwise it respawns the server we just killed.
    ordered = sorted(targets.values(), key=lambda item: not item.get("supervisor", False))
    for identity in ordered:
        pid = identity["pid"]
        try:
            process = psutil.Process(pid)
            current_start = datetime.fromtimestamp(process.create_time(), timezone.utc).isoformat()
            valid_name = (True if app.get("desktopApp") else process.name().lower() == "ollama app.exe" if identity.get("supervisor") else process.name().lower() == "llama-server.exe" if identity.get("runtimeWorker") else expected_process(app, {"id": pid, "name": process.name()}))
            if (not valid_name or
                    not identity.get("started") or identity["started"] != current_start or
                    os.path.normcase(process.exe()) != os.path.normcase(identity.get("path") or "")):
                raise ValueError(f"PID {pid} changed identity; stop was cancelled. Refresh and try again.")
            # On Windows terminate uses the process API, independent of taskkill's localized text.
            process.terminate()
            process.wait(timeout=15)
            results.append({"pid": pid, "ok": True, "supervisor": bool(identity.get("supervisor"))})
        except psutil.NoSuchProcess:
            results.append({"pid": pid, "ok": True, "alreadyExited": True})
        except psutil.AccessDenied:
            raise PermissionError(f"Windows denied permission to stop verified PID {pid}. Restart LocalOps through its elevated scheduled task.")
        except psutil.TimeoutExpired:
            raise RuntimeError(f"Windows has not finished stopping PID {pid}; refresh before retrying.")
    latest = wait_for_app_state(app["id"], False, 15)
    if not latest or latest.get("pids") or any(port["live"] for port in latest.get("ports", []) if port["role"] == "managed"):
        raise RuntimeError("A managed listener is still active or was restarted by another launcher. No replacement process was killed.")
    verify_stays_stopped(app)
    return {"processes": results, "verifiedStopped": True, "quietSeconds": 8, "alreadyStopped": not bool(targets)}


def control_recovery(app_id: str, action: str) -> None:
    if app_id != "tradedesk-ai":
        return
    result = run_quiet(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "launcher" / "tradedesk-recovery.ps1"), "-Action", action], timeout=30)
    if result.returncode:
        raise RuntimeError(f"TradeDesk automatic recovery could not {action}: {(result.stderr or result.stdout)[-1200:]}")


def perform_action(app_id: str, action: str, body: dict) -> dict:
    app = STORE.get_app(app_id)
    if not app:
        raise KeyError("Application not found.")
    if not app.get("enabled", True) or app.get("controlMode") != "manage":
        raise ValueError("Application controls are disabled for this registry entry.")
    if action not in {"start", "stop", "restart"}:
        raise ValueError("Unsupported action.")
    if app.get("confirmationRequired", False) and body.get("confirmed") is not True:
        raise ValueError(f"Please confirm Yes to {action} {app.get('name')}.")
    with ACTION_LOCKS_LOCK:
        action_lock = ACTION_LOCKS.setdefault(app_id, threading.Lock())
    if not action_lock.acquire(blocking=False):
        raise ValueError(f"Another control action for {app['name']} is already running.")
    try:
        before = next((item for item in snapshot(force=True)["apps"] if item["id"] == app_id), None)
        if action == "start":
            if before and before["state"] == "running" and before["ownershipSafe"]:
                control_recovery(app_id, "resume")
                return {"alreadyRunning": True, "verifiedState": "running", "verifiedPids": before["pids"]}
            if not before or not before["actions"]["start"]:
                raise ValueError("Start is unavailable because the app is already active, a port is occupied, or no valid launcher is configured.")
            launched = launch_app(app)
            launcher_process = launched.pop("_process")
            after = wait_for_app_state(app_id, True, 120)
            if not after or after["state"] != "running" or not after.get("ownershipSafe"):
                detail = tail_text(launched["log"])
                exit_detail = f" Launcher exit code: {launcher_process.poll()}." if launcher_process.poll() is not None else " Launcher is still running."
                raise RuntimeError(f"The launcher ran, but no verified managed listener became ready.{exit_detail} {detail}".strip())
            result = {"launched": launched, "verifiedState": after["state"], "verifiedPids": after["pids"]}
            control_recovery(app_id, "resume")
        elif action == "stop":
            if before and before.get("pids") and not before["actions"]["stop"]:
                raise ValueError("Process ownership could not be verified; no controls were changed.")
            control_recovery(app_id, "pause")
            result = stop_app(app)
        else:
            if not before or not before["actions"]["restart"]:
                raise ValueError("Restart is unavailable because ownership could not be verified.")
            control_recovery(app_id, "pause")
            stopped = stop_app(app)
            launched = launch_app(app)
            launcher_process = launched.pop("_process")
            after = wait_for_app_state(app_id, True, 120)
            if not after or after["state"] != "running" or not after.get("ownershipSafe"):
                detail = tail_text(launched["log"])
                exit_detail = f" Launcher exit code: {launcher_process.poll()}." if launcher_process.poll() is not None else " Launcher is still running."
                raise RuntimeError(f"The restart launcher ran, but no verified managed listener became ready.{exit_detail} {detail}".strip())
            result = {"stopped": stopped, "launched": launched, "verifiedState": after["state"], "verifiedPids": after["pids"]}
            control_recovery(app_id, "resume")
        STATUS_CACHE["at"] = 0.0
        STORE.add_event("control", f"{action.title()} completed for {app['name']}.", app_id, result)
        return result
    except Exception as error:
        STORE.add_event("error", f"{action.title()} failed for {app['name']}.", app_id, {"message": str(error)[:1000]})
        raise
    finally:
        action_lock.release()


def is_elevated() -> bool:
    if not IS_WINDOWS:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def hibernate_available() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        completed = run_quiet(["powercfg.exe", "/a"], timeout=10)
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        return completed.returncode == 0 and bool(re.search(r"(?im)^\s*Hibernate\s*$", output))
    except (OSError, subprocess.SubprocessError):
        return False


def system_capabilities() -> dict:
    return {
        "elevated": is_elevated(),
        "powerControlsAvailable": IS_WINDOWS,
        "hibernateAvailable": hibernate_available(),
        "shutdownDelaySeconds": 30,
        "powerConfirmationPhrases": dict(POWER_CONFIRMATIONS),
    }


def power_command(action: str) -> list[str]:
    if action == "shutdown":
        return ["shutdown.exe", "/s", "/t", "30", "/d", "p:0:0", "/c", "Requested through LocalOps"]
    if action == "hibernate":
        return ["shutdown.exe", "/h"]
    if action == "cancel-shutdown":
        return ["shutdown.exe", "/a"]
    raise ValueError("Unsupported system action.")


def perform_system_action(action: str, body: dict) -> dict:
    if not IS_WINDOWS:
        raise RuntimeError("PC power controls are available only on Windows.")
    if action == "cancel-shutdown":
        completed = run_quiet(power_command(action), timeout=10)
        detail = (completed.stdout or completed.stderr).strip()[-500:]
        if completed.returncode != 0:
            raise RuntimeError(detail or "Windows did not find a pending shutdown to cancel.")
        STORE.add_event("control", "Pending Windows shutdown was cancelled.")
        return {"action": action, "cancelled": True}
    expected = POWER_CONFIRMATIONS.get(action)
    if not expected:
        raise ValueError("Unsupported system action.")
    if str(body.get("confirmPhrase", "")) != expected:
        raise ValueError(f"Type the exact confirmation phrase: {expected}")
    if not AUTH.verify_password(str(body.get("password", ""))):
        raise PermissionError("The LocalOps password is incorrect.")
    if action == "hibernate" and not hibernate_available():
        raise RuntimeError("Hibernate is not available in this Windows power configuration.")
    completed = run_quiet(power_command(action), timeout=10)
    detail = (completed.stdout or completed.stderr).strip()[-500:]
    if completed.returncode != 0:
        raise RuntimeError(detail or f"Windows refused the {action} request.")
    message = "Windows shutdown scheduled with a 30-second cancellation window." if action == "shutdown" else "Windows hibernation requested."
    STORE.add_event("control", message, detail={"action": action})
    return {"action": action, "accepted": True, "delaySeconds": 30 if action == "shutdown" else 0}


def validate_app(payload: dict, current_id: str | None = None) -> dict:
    name = str(payload.get("name", "")).strip()[:80]
    if not name:
        raise ValueError("Application name is required.")
    app_id = current_id or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    if not app_id:
        raise ValueError("Application id is invalid.")
    desktop = payload.get("desktopApp") or None
    if desktop and desktop not in desktop_apps.PACKAGE_NAMES:
        raise ValueError("Unsupported desktop app identity.")
    rule = payload.get("processMatch") or None
    if rule:
        if (not isinstance(rule, dict) or not isinstance(rule.get("cwd"), str) or
                not Path(rule["cwd"]).is_absolute() or not isinstance(rule.get("args"), list) or
                not rule["args"] or len(rule["args"]) > 64 or not all(isinstance(arg, str) and arg for arg in rule["args"])):
            raise ValueError("Process ownership requires an absolute working directory and exact command arguments.")
        rule = {"cwd": rule["cwd"], "args": rule["args"]}
    ports = []
    for raw in payload.get("ports", []):
        port = int(raw.get("port", 0))
        if not 1 <= port <= 65535:
            raise ValueError("Ports must be between 1 and 65535.")
        ports.append({"port": port, "label": str(raw.get("label") or f"Port {port}")[:60], "role": "dependency" if raw.get("role") == "dependency" else "managed", "required": bool(raw.get("required", True))})
    start = payload.get("start") or None
    if start:
        arguments = start.get("args", [])
        if not isinstance(arguments, list) or len(arguments) > 64 or not all(isinstance(item, str) for item in arguments):
            raise ValueError("Launcher arguments must be a JSON array of at most 64 strings.")
        start = {"type": "file", "path": str(start.get("path", "")).strip(), "args": arguments}
    return {
        "id": app_id,
        "name": name,
        "description": str(payload.get("description", "")).strip()[:280],
        "group": str(payload.get("group", "Applications")).strip()[:40] or "Applications",
        "icon": str(payload.get("icon", "server")).strip()[:20],
        "url": str(payload.get("url", "")).strip(),
        "healthUrl": str(payload.get("healthUrl", "")).strip(),
        "acceptedHealthStatuses": [int(code) for code in payload.get("acceptedHealthStatuses", [200]) if str(code).isdigit()][:10] or [200],
        "workingDirectory": str(payload.get("workingDirectory", "")).strip(),
        "ports": ports,
        "expectedProcesses": [str(item).strip()[:80] for item in payload.get("expectedProcesses", []) if str(item).strip()][:20],
        "desktopApp": desktop,
        "processMatch": rule,
        "start": start,
        "repairStart": bool(payload.get("repairStart", False)),
        "controlMode": "manage" if payload.get("controlMode") == "manage" else "observe",
        "confirmationRequired": bool(desktop or payload.get("confirmationRequired", False)),
        "enabled": bool(payload.get("enabled", True)),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalOps/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, value: Any, status: int = 200, cookie: str | None = None) -> None:
        if isinstance(value, dict):
            value = {**value, "node": node_identity()}
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def host_name(self) -> str:
        host = self.headers.get("Host", "").strip().lower()
        if host.startswith("["):
            return host.split("]", 1)[0].strip("[]")
        return host.rsplit(":", 1)[0] if ":" in host else host

    def host_allowed(self) -> bool:
        return self.host_name() in {"127.0.0.1", "localhost", "::1", PUBLIC_HOST}

    def is_local_request(self) -> bool:
        return self.host_name() in {"127.0.0.1", "localhost", "::1"} and not self.headers.get("CF-Connecting-IP")

    def client_ip(self) -> str:
        return self.headers.get("CF-Connecting-IP", "").strip() or self.client_address[0]

    def cookie_token(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie["localops_session"].value if "localops_session" in cookie else ""

    def remote_session(self) -> dict | None:
        return AUTH.session(self.cookie_token())

    def session_cookie(self, token: str, clear: bool = False) -> str:
        secure = bool(self.headers.get("CF-Ray")) or self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        value = f"localops_session={'' if clear else token}; Path=/; HttpOnly; SameSite=Strict"
        value += "; Max-Age=0" if clear else f"; Max-Age={SESSION_SECONDS}"
        if secure:
            value += "; Secure"
        return value

    def authorized_write(self) -> bool:
        origin = self.headers.get("Origin")
        supplied = self.headers.get("X-LocalOps-Token", "")
        if self.is_local_request():
            if origin and not (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")):
                return False
            return secrets.compare_digest(supplied, CSRF_TOKEN)
        session = self.remote_session()
        if not session or origin != f"https://{PUBLIC_HOST}":
            return False
        return secrets.compare_digest(supplied, str(session.get("csrf", "")))

    def require_read_session(self) -> dict | None:
        if self.is_local_request():
            return {"csrf": CSRF_TOKEN, "local": True}
        return self.remote_session()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if not self.host_allowed():
            self.send_json({"error": "Host is not allowed."}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/agent/status":
            try:
                FLEET.authorize_agent(self.headers)
                self.send_json(FLEET.local_status())
            except PermissionError as error:
                self.send_json({"error": str(error)}, HTTPStatus.UNAUTHORIZED)
            except Exception:
                self.send_json({"error": "Node status scan failed."}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path == "/api/auth/state":
            session = self.require_read_session()
            self.send_json({"configured": AUTH.configured(), "setupNeeded": not AUTH.configured(), "setupAllowed": self.is_local_request(), "authenticated": bool(session), "csrfToken": session.get("csrf") if session else None, "publicUrl": f"https://{PUBLIC_HOST}", "remote": not self.is_local_request()})
            return
        if path == "/api/auth/setup/totp":
            if not self.is_local_request():
                self.send_json({"error": "Initial setup is available only from this Windows computer."}, HTTPStatus.FORBIDDEN)
                return
            try:
                self.send_json(AUTH.begin_setup())
            except ValueError as error:
                self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)
            return
        if path == "/health":
            self.send_json({"status": "ok", "app": "LocalOps", "authentication": "configured" if AUTH.configured() else "setup-required"})
            return
        session = self.require_read_session()
        if not session and path.startswith("/api/"):
            self.send_json({"error": "Authentication required."}, HTTPStatus.UNAUTHORIZED)
            return
        if path == "/api/fleet":
            self.send_json(FLEET.status())
            return
        if path == "/api/fleet/grants":
            self.send_json({"grants": FLEET.grants()})
            return
        if path == "/api/runtime":
            # Deployment readiness must not wait for listener/health/sensor scans.
            self.send_json({"server": {"name": "LocalOps", "version": APP_VERSION}, "system": {"elevated": is_elevated()}})
            return
        if path == "/api/nodes":
            self.send_json({"nodes": node_catalog()})
            return
        if path == "/api/bootstrap":
            self.send_json({**snapshot(), "csrfToken": session.get("csrf"), "server": {"name": "LocalOps", "version": APP_VERSION, "localOnly": False, "publicUrl": f"https://{PUBLIC_HOST}"}, "system": system_capabilities()})
            return
        if path == "/api/status":
            self.send_json(snapshot())
            return
        if path == "/api/metrics":
            self.send_json(METRICS.payload())
            return
        if path == "/api/schedules":
            self.send_json(SCHEDULER.payload())
            return
        if path == "/api/apps/raw":
            with STORE.lock:
                self.send_json({"apps": STORE.apps})
            return
        if path == "/api/diagnostics":
            self.send_json({"scanBusy": STATUS_LOCK.locked(), "cacheReady": STATUS_CACHE["payload"] is not None, "cacheAgeSeconds": round(max(0.0, time.monotonic() - STATUS_CACHE["at"]), 1) if STATUS_CACHE["at"] else None, "requestThreads": threading.active_count(), "system": system_capabilities()})
            return
        self.serve_static(path)

    def do_POST(self) -> None:
        target = self.headers.get("X-LocalOps-Node")
        if target and target != node_identity()["id"]:
            self.send_json({"error": "Selected computer does not match this node. Reload and select the correct computer."}, HTTPStatus.CONFLICT)
            return
        if not self.host_allowed():
            self.send_json({"error": "Host is not allowed."}, HTTPStatus.BAD_REQUEST)
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/agent/action":
            try:
                FLEET.authorize_agent(self.headers)
                self.send_json(FLEET.agent_action(self.read_json()))
            except PermissionError as error:
                self.send_json({"error": str(error)}, HTTPStatus.FORBIDDEN)
            except (ValueError, KeyError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception:
                self.send_json({"error": "Application action failed; inspect this node before retrying."}, HTTPStatus.CONFLICT)
            return
        if path == "/api/auth/setup":
            if not self.is_local_request():
                self.send_json({"error": "Initial setup is available only from this Windows computer."}, HTTPStatus.FORBIDDEN)
                return
            try:
                value, token, csrf = AUTH.complete_setup(self.read_json(), self.client_ip(), self.headers.get("User-Agent", ""))
                value["csrfToken"] = csrf
                STORE.add_event("security", "Online access security was configured.")
                self.send_json(value, HTTPStatus.CREATED, self.session_cookie(token))
            except ValueError as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/auth/login":
            try:
                token, csrf = AUTH.login(self.read_json(), self.client_ip(), self.headers.get("User-Agent", ""))
                STORE.add_event("security", "Remote administrator signed in.", detail={"ip": self.client_ip()})
                self.send_json({"ok": True, "csrfToken": csrf}, cookie=self.session_cookie(token))
            except PermissionError as error:
                self.send_json({"error": str(error)}, HTTPStatus.UNAUTHORIZED)
            except RuntimeError as error:
                self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)
            return
        if path == "/api/auth/logout":
            session = self.remote_session()
            if not session or not secrets.compare_digest(self.headers.get("X-LocalOps-Token", ""), str(session.get("csrf", ""))):
                self.send_json({"error": "Authentication required."}, HTTPStatus.UNAUTHORIZED)
                return
            AUTH.logout(self.cookie_token())
            self.send_json({"ok": True}, cookie=self.session_cookie("", clear=True))
            return
        if not self.authorized_write():
            self.send_json({"error": "Write request was rejected."}, HTTPStatus.FORBIDDEN)
            return
        try:
            body = self.read_json()
            if path == "/api/fleet/grant":
                self.send_json(FLEET.grant(body.get("label", "")), HTTPStatus.CREATED)
                return
            if path == "/api/fleet/revoke":
                FLEET.revoke(str(body.get("id", "")))
                self.send_json({"ok": True})
                return
            if path == "/api/fleet/pair":
                self.send_json({"ok": True, "peer": FLEET.pair(body)}, HTTPStatus.CREATED)
                return
            if path == "/api/fleet/remove":
                FLEET.remove(str(body.get("nodeId", "")))
                self.send_json({"ok": True})
                return
            if path == "/api/fleet/action":
                # Keep the response envelope bound to this dashboard; the result names the target.
                self.send_json({"ok": True, "targetResult": FLEET.action(body)})
                return
            if path == "/api/nodes":
                self.send_json({"ok": True, "nodeEntry": register_node(body)}, HTTPStatus.CREATED)
                return
            if path == "/api/schedules":
                self.send_json({"ok": True, "schedule": SCHEDULER.create(body)}, HTTPStatus.CREATED)
                return
            schedule_match = re.fullmatch(r"/api/schedules/([a-f0-9]+)/actions/(enable|disable|delete)", path)
            if schedule_match:
                SCHEDULER.change(*schedule_match.groups())
                self.send_json({"ok": True})
                return
            action_match = re.fullmatch(r"/api/apps/([^/]+)/actions/(start|stop|restart)", path)
            system_action_match = re.fullmatch(r"/api/system/actions/(shutdown|hibernate|cancel-shutdown)", path)
            app_match = re.fullmatch(r"/api/apps(?:/([^/]+))?", path)
            import_match = re.fullmatch(r"/api/discoveries/import", path)
            if action_match:
                result = perform_action(action_match.group(1), action_match.group(2), body)
                self.send_json({"ok": True, "result": result})
                return
            if system_action_match:
                result = perform_system_action(system_action_match.group(1), body)
                self.send_json({"ok": True, "result": result})
                return
            if import_match:
                port = int(body.get("port", 0))
                name = str(body.get("name") or f"Service on port {port}")
                app = validate_app({"name": name, "description": "Imported from a live local listener. Review ownership before enabling controls.", "group": "Discovered", "ports": [{"port": port, "label": "Detected listener"}], "expectedProcesses": [body.get("processName")] if body.get("processName") else [], "controlMode": "observe", "url": f"http://127.0.0.1:{port}"})
                with STORE.lock:
                    if any(item["id"] == app["id"] for item in STORE.apps):
                        raise ValueError("An application with this name already exists.")
                    STORE.apps.append(app)
                    STORE.save_apps()
                STATUS_CACHE["at"] = 0
                STORE.add_event("registry", f"Imported {app['name']} as observe-only.", app["id"])
                self.send_json({"ok": True, "app": app}, HTTPStatus.CREATED)
                return
            if app_match:
                current_id = app_match.group(1)
                app = validate_app(body, current_id)
                with STORE.lock:
                    index = next((i for i, item in enumerate(STORE.apps) if item["id"] == app["id"]), None)
                    if index is None:
                        STORE.apps.append(app)
                    else:
                        STORE.apps[index] = app
                    STORE.save_apps()
                STATUS_CACHE["at"] = 0
                STORE.add_event("registry", f"Saved configuration for {app['name']}.", app["id"])
                self.send_json({"ok": True, "app": app}, HTTPStatus.CREATED if index is None else HTTPStatus.OK)
                return
            self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
        except KeyError as error:
            self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except PermissionError as error:
            self.send_json({"error": str(error)}, HTTPStatus.FORBIDDEN)
        except Exception as error:
            STORE.add_event("error", f"Request failed: {type(error).__name__}", detail={"message": str(error)[:500]})
            self.send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (STATIC / relative).resolve()
        try:
            target.relative_to(STATIC.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            target = STATIC / "index.html"
        payload = target.read_bytes()
        content_type = "application/manifest+json" if target.suffix == ".webmanifest" else (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)


class LocalOpsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self) -> None:
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    global SERVER_PORT, SCHEDULER
    parser = argparse.ArgumentParser(description="LocalOps local application manager")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("LocalOps intentionally binds only to a loopback address.")
    SERVER_PORT = args.port
    DATA.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    server = LocalOpsServer((args.host, args.port), Handler)
    SCHEDULER = Scheduler(DATA / "schedules.json", atomic_json, STORE.get_app, perform_action, STORE.add_event)
    STORE.add_event("system", f"LocalOps started on http://{args.host}:{args.port}.")
    METRICS.start()
    SCHEDULER.start()
    sensor_process = None
    if IS_WINDOWS and (ROOT / "vendor" / "LibreHardwareMonitor" / "LibreHardwareMonitorLib.dll").exists():
        sensor_process = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "launcher" / "cpu-sensors.ps1"), "-OwnerPid", str(os.getpid())], creationflags=CREATE_NO_WINDOW)
    print(f"LocalOps is ready at http://{args.host}:{args.port}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        SCHEDULER.stop()
        if sensor_process and sensor_process.poll() is None:
            sensor_process.terminate()
        METRICS.stop()
        STORE.add_event("system", "LocalOps stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
