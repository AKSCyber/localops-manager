"""Add requested apps through the authenticated local API; never replace existing entries."""
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def entries():
    restaurant = ROOT.parent / "restaurant-pos-system"
    result = []
    for app_id, name, root, port, executable, arguments, script, description in (
        ("kindergarten-learning", "Kindergarten Learning", Path(r"D:\kindergarten-touch"), 8780,
         "python", ["-m", "http.server"], "start-kindergarten.ps1", "Deployed kindergarten learning app. Dedicated port avoids llama.cpp; matching manual launches are also tracked."),
        ("restaurant-pos", "Restaurant POS", restaurant, 8781,
         "node", ["server.js"], "start-restaurant.ps1", "Existing restaurant app and live database. Uses its run-local.cmd through a dedicated-port launcher; stopping interrupts ordering terminals."),
    ):
        result.append({"id": app_id, "name": name, "description": description, "group": "Applications",
                       "url": f"http://127.0.0.1:{port}", "healthUrl": f"http://127.0.0.1:{port}/",
                       "workingDirectory": str(root), "expectedProcesses": [executable],
                       "processMatch": {"cwd": str(root), "args": arguments},
                       "ports": [{"port": port, "label": "Web app", "role": "managed", "required": True}],
                       "start": {"path": str(ROOT / "launcher" / script), "args": []},
                       "controlMode": "manage", "confirmationRequired": True, "enabled": True})
    for kind, name in (("chatgpt", "ChatGPT"), ("codex", "Codex")):
        result.append({"id": kind + "-desktop", "name": name, "group": "Windows desktop apps",
                       "description": "Windows desktop app. Stop closes the app and may interrupt active chats or agent work. Separate package identity prevents confusing ChatGPT with Codex.",
                       "desktopApp": kind, "ports": [], "expectedProcesses": [],
                       "controlMode": "manage", "confirmationRequired": True, "enabled": True})
    return result


if __name__ == "__main__":
    base = "http://127.0.0.1:8790"
    with urllib.request.urlopen(base + "/api/runtime", timeout=15) as response:
        runtime = json.load(response)
    if runtime["server"]["version"] != "1.6.0":
        raise SystemExit("Restart LocalOps into 1.6.0 before registering desktop controls.")
    with urllib.request.urlopen(base + "/api/auth/state", timeout=10) as response:
        token = json.load(response)["csrfToken"]
    with urllib.request.urlopen(base + "/api/apps/raw", timeout=15) as response:
        existing = {app["id"] for app in json.load(response)["apps"]}
    for entry in entries():
        if entry["id"] in existing:
            print(f"Preserved existing entry: {entry['name']}")
            continue
        request = urllib.request.Request(base + "/api/apps/" + entry["id"],
            data=json.dumps(entry).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-LocalOps-Token": token})
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
        print(f"Registered: {entry['name']}")
