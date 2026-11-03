# LocalOps

Version 1.6.0 includes a combined dashboard for paired computers, persistent machine IDs, telemetry, and per-node app controls. See [Fleet setup](FLEET.md) for installation, pairing, permissions, and connection behavior.

LocalOps is a Windows command center for local web applications and shared AI runtimes. Its origin remains bound to `127.0.0.1`, while the authenticated Cloudflare route is designed for `https://localops.solliegiance.com`. It discovers actual TCP listeners, connects them to a persistent application registry, probes health endpoints, shows process and memory evidence, and records state/control events.

Its performance dashboard samples CPU, system RAM, NVIDIA GPU utilization and VRAM, network receive/send rates, disk read/write rates, and C:/D: volume capacity every two seconds. The graphs retain roughly six minutes of recent samples in memory and reset when LocalOps restarts.

## Start

Double-click `start-localops.cmd`. The launcher prevents duplicate starts, verifies the health endpoint, records the exact PID, and opens the dashboard. To stop only that verified background instance, double-click `stop-localops.cmd`.

The deployed installation also registers a current-user **LocalOps Manager** Windows scheduled task. It starts the same verified launcher hidden and elevated at sign-in with `-NoBrowser`, allowing it to control applications that Windows started at higher integrity. The separate named Cloudflare tunnel task remains non-elevated and duplicate-safe. Remove those tasks in Task Scheduler if sign-in startup is no longer wanted.

If Windows has not yet granted the Highest run level, double-click `enable-elevated-startup.cmd` once and approve the UAC prompt. The helper verifies the exact LocalOps listener before replacing it and records the result in `logs/startup-activation.json`.

For development, you can also run:

```powershell
python app.py
```

Then open `http://127.0.0.1:8790`.

Application Start runs the configured script/executable, arguments, and working directory. TradeDesk uses `D:\tradedesk-ai\run-local.ps1`; Training Lab uses `start-training-lab.ps1`; Studio and Smart Home use their own `launcher\start.ps1 -NoBrowser`. Studio's normal launcher also starts its AI dependencies. Direct runtime commands remain available for applications without a standalone script. Details shows the launcher, and Edit registry preserves/edit its argument array. PowerShell launchers run non-interactively with hidden-window process flags and captured logs.

The first registry includes TradeDesk AI, Olmin Studio, Olmin Training Lab, Smart Home Dashboard, Ollama, llama.cpp, and an observe-only Home Assistant endpoint. Edit `data/apps.json` or use **Add application** in the dashboard to add more.

TradeDesk AI, Olmin Studio, Olmin Training Lab, and Smart Home Dashboard include an **Open LocalOps** navigation link targeting the protected public hostname. Ollama and llama.cpp have no application dashboard to modify, but remain visible and controllable from LocalOps. Home Assistant remains observe-only unless its endpoint is explicitly configured.

## Installable PWA and remote controls

Open `https://localops.solliegiance.com` in a compatible browser and choose **Install app**. The PWA caches only its static interface; all API, authentication, application state, telemetry, and control requests always require a live protected connection and are never cached by the service worker.

The **Remote control** panel provides prominent start/stop controls for Ollama and llama.cpp, backed by the same verified-port and exact-process ownership rules as their application cards. Windows shutdown and hibernate are deliberately stricter: each request requires the current LocalOps password again and the exact phrase displayed in the confirmation dialog. Shutdown has a 30-second delay and a **Cancel shutdown** action. LocalOps tests never execute either power action.

## Online security setup

1. On this Windows computer, open `http://127.0.0.1:8790`.
2. Create the independent LocalOps administrator password and enter the displayed setup key in Microsoft Authenticator as an **Other account**.
3. Verify the current six-digit code and save the one-time recovery codes offline.
4. Use the protected PWA at `https://localops.solliegiance.com`; the existing named Cloudflare tunnel forwards only to the loopback-bound LocalOps origin.

Remote reads and controls require an HttpOnly, SameSite session cookie, password plus Authenticator login, replay-protected TOTP, an exact allowed host, and a session CSRF token. Before setup, the remote host exposes no process, application, metric, or event data.

## Metrics

- CPU and RAM come from Windows process/system counters through `psutil`.
- CPU temperature and its graph use a CPU-only Libre Hardware Monitor reader, started with LocalOps. This requires elevation and the signed PawnIO driver. Unavailable, failed, or stale readings are explicitly marked unavailable; GPU temperature is never substituted for CPU temperature.
- NVIDIA GPU load, VRAM, temperature, and power use `nvidia-smi` when available; the GPU panels show an unavailable state on machines without that command.
- Network and disk graphs show machine-wide bytes per second calculated from successive counter samples.
- Each application card shows process CPU, RAM, and verified listener PIDs separately from machine-wide performance.
- LocalOps keeps telemetry in memory only; it does not create a long-term hardware-usage database.

## Application schedules

The **Schedules** panel saves one-time, daily, and weekly application start/stop actions. Choose the app, action, date/time, and repetition, then confirm with Yes or No. Saved schedules can be paused, enabled, or deleted and show their last result/error. Schedules use the same authentication, ownership checks, and per-app action locks as manual controls.

LocalOps must be running and the PC awake. Schedules do not wake or power on the PC. Occurrences more than five minutes overdue are skipped; crashes never silently replay an uncertain action. Failed schedules pause for review. Repeating schedules retain the selected UTC offset (every 24 hours or seven days), including across daylight-saving changes. Changing an app's launcher or ownership configuration requires a newly approved schedule.

TradeDesk's separate API recovery task is paused before an intentional stop, so it cannot immediately relaunch the API. Its previous enabled/running state is restored after a successful start. Shared PostgreSQL and AI dependencies are not stopped with TradeDesk.

## Safety model

- The server refuses non-loopback binding.
- Local browser writes require a per-run token and loopback origin; remote writes require an authenticated session, the exact HTTPS origin, and a session CSRF token.
- Stop/restart is enabled only when every managed listener is owned by an expected executable.
- Dependency ports are observed but never stopped with an application.
- Application controls use Yes/No confirmation, without typing names. Shared-runtime API requests require `confirmed: true`; authentication and CSRF protection remain unchanged.
- Ollama Stop first terminates the verified Desktop supervisor from the configured installation, then its verified Ollama server/children. Start restores Desktop when it was previously stopped, preserving Desktop's model settings. Windows sign-in startup preferences are unchanged; a future sign-in or another external launcher can still start an app.
- Stops are verified over an eight-second quiet interval. If another launcher reopens a managed port, the operation reports failure rather than claiming success or chasing new PIDs.
- Ollama's bundled `lib\ollama\llama-server.exe` workers are attributed to Ollama, not the standalone llama.cpp router. Workers from the exact configured installation with verified ancestry or no surviving parent are included in Stop, even when the API listener is gone. Worker-only runtimes remain visible/stoppable; unrelated live parents and other installation paths are excluded.
- TradeDesk's idempotent launcher is explicitly allowed to start missing components while a verified frontend/backend is already running. Generic launch commands remain blocked when occupied, to avoid duplicate instances. Listener-scan failures now fail closed instead of being treated as proof that all applications stopped.
- PC shutdown and hibernate additionally require password re-entry, an exact confirmation phrase, and an authenticated CSRF-protected session. They use the signed-in Windows user's normal power rights; shutdown remains cancellable for 30 seconds.
- Managed-application stop targets only listener PIDs whose executable name, path, and creation time are freshly verified. On Windows it uses process termination and waits for managed ports to close. It never chases and kills replacement PIDs. This is a process stop, not an application-specific graceful shutdown; save in-app work first.
- Discovered listeners are imported as observe-only entries.
- The LocalOps password is stored as a salted scrypt hash, its TOTP secret is protected with Windows DPAPI, recovery codes are stored only as hashes, and session tokens are stored only as hashes.

## Data

- `data/apps.json` — persistent service registry.
- `data/schedules.json` — persisted schedules, execution claims, and results.
- `data/cpu-temperature.json` — latest sensor reading (unavailable after 30 seconds without an update).
- `data/events.json` — latest 500 state and control events.
- `data/auth.json` — DPAPI-protected administrator verifier created by local setup; it never contains the plaintext password or recovery codes.
- `data/sessions.json` — hashed, expiring session records; plaintext session tokens remain only in HttpOnly cookies.
- `logs/` — output from launchers started by LocalOps.

This app intentionally tracks the web runtime separately from application-level jobs. For example, an online Training Lab server does not imply that a training worker is active; the lab remains the authority for its worker PID, checkpoint, and progress.
# Additional application controls (1.5.1)

`python register-extra-apps.py` adds the following entries to the running 1.5.1
manager without overwriting existing registry entries:

- **Kindergarten Learning**: `D:\kindergarten-touch`, served on loopback port
  **8780** by `launcher/start-kindergarten.ps1`. This avoids llama.cpp on 8080.
- **Restaurant POS**: the sibling `restaurant-pos-system` folder and its existing
  `data/restaurant-live.sqlite`. `launcher/start-restaurant.ps1` calls the existing
  `run-local.cmd` with LAN port **8781**, avoiding Smart Home on 3000. Strict-port
  mode refuses collisions instead of silently moving to an untracked port.
- **ChatGPT** and **Codex**: distinct Windows Store package identities. Their
  executable names can both be `ChatGPT.exe`; name-only termination is never used.
  Missing installations show as unconfigured with controls disabled. Detection
  refreshes automatically after installation or an update.

All four entries use Yes/No confirmation and the existing application scheduler.
Desktop launch uses Windows AppsFolder activation in the signed-in user's session;
it does not provide remote desktop viewing. Stop terminates the selected package's
main processes and package-local descendants, not external shared dependencies or
Windows services. Save work first: stopping Codex can interrupt running agents.

The web entries additionally require a matching working directory and command
arguments. Matching manual launches on other ports are shown as partial and can
be stopped; a duplicate start is blocked. Details shows launcher and process paths.
Editing these safety rules invalidates any previously approved schedules.

The LocalOps website/PWA and existing authenticated tunnel share this registry.
These additions do not publish the underlying apps or change their public routes.

