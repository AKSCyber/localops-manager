const state = { node: null, nodes: [], data: null, metrics: null, system: null, token: "", filter: "all", pending: null, busy: false, auth: null, setupToken: "", pollingStarted: false, installPrompt: null };

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const formatBytes = (bytes) => !bytes ? "—" : bytes > 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${Math.round(bytes / 1024 ** 2)} MB`;
const formatRate = (bytes) => bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB/s` : bytes >= 1024 ? `${Math.round(bytes / 1024)} KB/s` : `${Math.round(bytes || 0)} B/s`;
const formatTime = (value) => value ? new Intl.DateTimeFormat(undefined, {hour:"numeric", minute:"2-digit", second:"2-digit"}).format(new Date(value)) : "—";
const relativeTime = (value) => {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
};
const iconFor = (app) => ({chart:"↗", spark:"✦", flask:"⌁", home:"⌂", model:"◉", chip:"▦", server:"◆"}[app.icon] || "◆");
const stateLabel = (value) => ({running:"Running", degraded:"Degraded", partial:"Partial", stopped:"Stopped", offline:"Offline", unconfigured:"Unconfigured"}[value] || value);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {"Content-Type":"application/json", ...(state.token ? {"X-LocalOps-Token":state.token} : {}), ...(state.node ? {"X-LocalOps-Node":state.node.id} : {}), ...(options.headers || {})}
  });
  const data = await response.json().catch(() => ({}));
  const expected = new URLSearchParams(location.search).get("node") || state.node?.id;
  if (data.node) {
    if (expected && data.node.id !== expected) {
      state.token = "";
      state.auth = null;
      state.data = null;
      $("#appShell").hidden = true;
      $("#authGate").hidden = false;
      throw new Error(`Wrong computer: reached ${data.node.computerName}. Controls are blocked. Check this computer's dedicated LocalOps address.`);
    }
    state.node = data.node;
    $$(".node-name").forEach(element => { element.textContent = data.node.computerName; });
    document.title = `LocalOps — ${data.node.computerName}`;
  }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function showAuthPanel(panelId) {
  ["authLoading", "setupPanel", "setupRemotePanel", "loginPanel", "recoveryPanel"].forEach(id => { $(`#${id}`).hidden = id !== panelId; });
  $("#authError").textContent = "";
  $("#authGate").hidden = false;
  $("#appShell").hidden = true;
}

async function beginSetup() {
  showAuthPanel("setupPanel");
  try {
    const setup = await api("/api/auth/setup/totp");
    state.setupToken = setup.setupToken;
    $("#setupSecret").textContent = setup.secret;
  } catch (error) { $("#authError").textContent = error.message; }
}

function startDashboard() {
  $("#authGate").hidden = true;
  $("#appShell").hidden = false;
  $("#logoutButton").hidden = !state.auth?.remote;
  loadNodes();
  refreshFleet();
  refresh(true);
  refreshMetrics();
  if (!state.pollingStarted) {
    state.pollingStarted = true;
    setInterval(() => refresh(), 12000);
    setInterval(refreshFleet, 12000);
    setInterval(refreshMetrics, 2500);
  }
}

async function initialize() {
  showAuthPanel("authLoading");
  try {
    state.auth = await api("/api/auth/state");
    state.token = state.auth.csrfToken || "";
    if (state.auth.setupNeeded) {
      if (state.auth.setupAllowed) await beginSetup(); else showAuthPanel("setupRemotePanel");
      return;
    }
    if (!state.auth.authenticated) { showAuthPanel("loginPanel"); return; }
    startDashboard();
  } catch (error) {
    showAuthPanel("authLoading");
    $("#authError").textContent = error.message;
  }
}

function toast(title, message = "", error = false) {
  const item = document.createElement("div");
  item.className = `toast${error ? " error" : ""}`;
  item.innerHTML = `<strong>${escapeHtml(title)}</strong>${message ? `<span>${escapeHtml(message)}</span>` : ""}`;
  $("#toasts").append(item);
  setTimeout(() => item.remove(), 5000);
}

function appVisible(app) {
  if (state.filter === "all") return true;
  if (state.filter === "running") return app.state === "running";
  if (state.filter === "attention") return ["partial", "degraded"].includes(app.state);
  return ["stopped", "offline", "unconfigured"].includes(app.state);
}

function appCard(app) {
  const managed = app.ports.filter(port => port.role === "managed");
  const live = managed.filter(port => port.live).length;
  const health = app.health ? (app.health.ok ? `${app.health.latencyMs} ms` : app.health.reachable ? `HTTP ${app.health.code}` : "No response") : "No probe";
  const ports = app.ports.map(port => `<span class="port-chip ${port.live ? "live" : ""} ${port.role === "dependency" ? "dependency" : ""}" title="${escapeHtml(port.label)}"><i></i>${port.port}${port.role === "dependency" ? " dep" : ""}</span>`).join("");
  const control = app.controlMode === "manage";
  return `<article class="app-card ${escapeHtml(app.state)}" data-id="${escapeHtml(app.id)}">
    <div class="card-top"><span class="app-icon">${iconFor(app)}</span><div class="card-title"><h3>${escapeHtml(app.name)}</h3><span class="group-label">${escapeHtml(app.group)}</span></div><span class="state-pill ${escapeHtml(app.state)}">${stateLabel(app.state)}</span></div>
    <p class="card-description">${escapeHtml(app.description || "No description yet.")}</p>
    <div class="metrics"><span class="metric"><small>Health</small><strong>${escapeHtml(health)}</strong></span><span class="metric"><small>CPU</small><strong>${Number(app.cpuPercent || 0).toFixed(1)}%</strong></span><span class="metric"><small>RAM</small><strong>${formatBytes(app.memoryBytes)}</strong></span><span class="metric"><small>PIDs</small><strong>${app.pids.length || "—"}</strong></span></div>
    <div class="port-line">${ports || `<span class="port-chip">${app.desktopApp ? (app.desktopInstalled ? "Windows desktop app" : "Not installed for this Windows user") : "External endpoint"}</span>`}</div>
    <div class="card-actions">
      ${app.url ? `<button class="button ghost" data-open="${escapeHtml(app.url)}">Open</button>` : ""}
      <button class="button secondary" data-details="${escapeHtml(app.id)}">Details</button>
      ${control ? `<button class="button ${app.actions.start ? "primary" : "secondary"}" data-action="${app.actions.start ? "start" : "stop"}" data-app="${escapeHtml(app.id)}" ${app.actions.start || app.actions.stop ? "" : "disabled"}>${app.actions.start ? (app.repairStartAvailable ? "Start missing components" : "Start") : "Stop"}</button>${app.actions.start && app.actions.stop ? `<button class="button secondary" data-action="stop" data-app="${escapeHtml(app.id)}">Stop</button>` : ""}` : `<button class="button secondary" disabled>Observe only</button>`}
    </div>
  </article>`;
}

function renderRemoteControls() {
  if (!state.data) return;
  const updateRuntime = (id, buttonSelector, stateSelector) => {
    const app = state.data.apps.find(item => item.id === id);
    const button = $(buttonSelector);
    const label = $(stateSelector);
    if (!app || !button || !label) return;
    const action = app.actions.start ? "start" : app.actions.stop ? "stop" : "";
    button.dataset.quickAction = action;
    button.textContent = action ? `${action[0].toUpperCase()}${action.slice(1)}` : "Unavailable";
    button.disabled = !action;
    button.className = `button ${action === "start" ? "primary" : action === "stop" ? "danger" : "secondary"}`;
    label.textContent = `${stateLabel(app.state)}${app.pids.length ? ` · PID ${app.pids.join(", ")}` : ""}`;
  };
  updateRuntime("ollama", "#quickOllama", "#quickStateOllama");
  updateRuntime("llama-cpp", "#quickLlama", "#quickStateLlama");
  const system = state.system;
  if (system) {
    $("#systemPrivilege").textContent = system.elevated ? "Elevated application control" : "Standard Windows power control";
    $("#systemPrivilege").classList.toggle("warning", !system.powerControlsAvailable);
    $("#hibernateButton").disabled = !system.powerControlsAvailable || !system.hibernateAvailable;
    $("#hibernateStatus").textContent = system.hibernateAvailable ? "Hibernate is supported by this PC." : "Hibernate is not available in this Windows power configuration.";
    $$('[data-system-action="shutdown"]').forEach(button => { button.disabled = !system.powerControlsAvailable; });
  }
}

function render() {
  if (!state.data) return;
  const {summary, apps, discoveries, events, generatedAt} = state.data;
  $("#runningCount").textContent = summary.running;
  $("#attentionCount").textContent = summary.attention;
  $("#offlineCount").textContent = summary.offline;
  $("#listenerCount").textContent = summary.observedListeners;
  $("#lastScan").textContent = formatTime(generatedAt);
  $("#discoveryBadge").textContent = discoveries.length;
  const visible = apps.filter(appVisible);
  $("#appGrid").innerHTML = visible.length ? visible.map(appCard).join("") : `<div class="loading-card">No applications match this filter.</div>`;

  $("#discoveryList").innerHTML = discoveries.length ? discoveries.slice(0, 30).map(item => {
    const process = item.process || {};
    return `<div class="listener"><span class="listener-port">:${item.port}</span><div><strong>${escapeHtml(process.name || "Unknown process")}</strong><small>PID ${item.pid} · ${escapeHtml(item.address)}${process.path ? ` · ${escapeHtml(process.path)}` : ""}</small></div><button class="button ghost" data-import-port="${item.port}" data-import-process="${escapeHtml(process.name || "")}">Import</button></div>`;
  }).join("") : `<p class="empty">Every visible non-system listener is already assigned.</p>`;

  $("#eventList").innerHTML = events.length ? events.map(event => `<div class="event ${escapeHtml(event.kind)}"><strong>${escapeHtml(event.message)}</strong><time datetime="${escapeHtml(event.at)}" title="${escapeHtml(new Date(event.at).toLocaleString())}">${relativeTime(event.at)}</time></div>`).join("") : `<p class="empty">No activity yet.</p>`;
  renderRemoteControls();
  populateScheduleApps();
}

function drawChart(id, series, maxValue = null) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const width = rect.width, height = rect.height;
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = "rgba(150,180,210,.08)";
  ctx.lineWidth = 1;
  for (let row = 1; row < 4; row++) { const y = height * row / 4; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
  const allValues = series.flatMap(item => item.values).filter(Number.isFinite);
  const ceiling = maxValue || Math.max(1, ...allValues) * 1.15;
  series.forEach((item, index) => {
    const values = item.values;
    if (!values.length) return;
    ctx.beginPath();
    let connected = false;
    values.forEach((value, i) => {
      if (!Number.isFinite(value)) { connected = false; return; }
      const x = values.length === 1 ? width : i / (values.length - 1) * width;
      const y = height - Math.min(1, Math.max(0, value / ceiling)) * (height - 3) - 1;
      connected ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      connected = true;
    });
    ctx.strokeStyle = item.color;
    ctx.lineWidth = 1.7;
    ctx.stroke();
    if (index === 0 && series.length === 1 && values.every(Number.isFinite)) {
      ctx.lineTo(width, height); ctx.lineTo(0, height); ctx.closePath();
      const gradient = ctx.createLinearGradient(0, 0, 0, height);
      gradient.addColorStop(0, item.fill); gradient.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = gradient; ctx.fill();
    }
  });
}

function renderMetrics() {
  const payload = state.metrics;
  if (!payload?.latest?.available) return;
  const latest = payload.latest;
  const history = payload.history || [];
  $("#metricsSampledAt").textContent = `Updated ${formatTime(latest.sampledAt)} · ${history.length} samples`;
  $("#metricCpu").textContent = `${latest.cpu.percent.toFixed(1)}%`;
  const temperature = latest.cpu.temperature;
  $("#metricCpuTemperature").textContent = temperature?.available ? `${temperature.celsius.toFixed(1)}°C` : "Unavailable";
  $("#metricCpuTemperatureMeta").textContent = temperature?.available ? temperature.source : "No fresh CPU sensor";
  $("#cpuSensorDetail").textContent = temperature?.available ? `${temperature.sensors.length} CPU sensors · package / hottest core` : (temperature?.reason || "Waiting for sensor reader");
  drawChart("chartCpuTemperature", [{values:history.map(p => p.cpuTemperatureC ?? null), color:"#fb923c", fill:"rgba(251,146,60,.22)"}], 120);
  $("#metricCpuMeta").textContent = `${latest.cpu.physicalCores || "—"} cores · ${latest.cpu.logicalCores || "—"} logical`;
  $("#metricMemory").textContent = `${latest.memory.percent.toFixed(1)}%`;
  $("#metricMemoryMeta").textContent = `${formatBytes(latest.memory.usedBytes)} / ${formatBytes(latest.memory.totalBytes)}`;
  if (latest.gpu) {
    $("#metricGpu").textContent = `${Number(latest.gpu.utilizationPercent || 0).toFixed(1)}%`;
    $("#metricGpuMeta").textContent = `${latest.gpu.name} · ${latest.gpu.temperatureC ?? "—"}°C`;
    const vramPercent = latest.gpu.memoryTotalMb ? latest.gpu.memoryUsedMb / latest.gpu.memoryTotalMb * 100 : 0;
    $("#metricVram").textContent = `${vramPercent.toFixed(1)}%`;
    $("#metricVramMeta").textContent = `${Math.round(latest.gpu.memoryUsedMb || 0)} / ${Math.round(latest.gpu.memoryTotalMb || 0)} MB`;
  } else {
    $("#metricGpu").textContent = "N/A"; $("#metricGpuMeta").textContent = "No NVIDIA counter";
    $("#metricVram").textContent = "N/A"; $("#metricVramMeta").textContent = "Not reported";
  }
  $("#metricNetwork").textContent = `↓ ${formatRate(latest.network.downBps)}`;
  $("#metricNetworkMeta").textContent = `↑ ${formatRate(latest.network.upBps)}`;
  $("#metricDisk").textContent = `R ${formatRate(latest.disk.readBps)}`;
  $("#metricDiskMeta").textContent = `W ${formatRate(latest.disk.writeBps)}`;
  $("#volumeStrip").innerHTML = (latest.disk.volumes || []).map(volume => `<div class="volume-item"><strong>${escapeHtml(volume.root)}</strong><span class="volume-track"><i style="width:${Math.min(100, volume.percent)}%"></i></span><small>${volume.percent.toFixed(0)}% · ${formatBytes(volume.totalBytes - volume.usedBytes)} free</small></div>`).join("");
  drawChart("chartCpu", [{values:history.map(p => p.cpuPercent), color:"#60a5fa", fill:"rgba(96,165,250,.28)"}], 100);
  drawChart("chartMemory", [{values:history.map(p => p.memoryPercent), color:"#a78bfa", fill:"rgba(167,139,250,.25)"}], 100);
  drawChart("chartGpu", [{values:history.map(p => p.gpuPercent), color:"#4ade80", fill:"rgba(74,222,128,.25)"}], 100);
  drawChart("chartVram", [{values:history.map(p => p.gpuVramPercent), color:"#22d3ee", fill:"rgba(34,211,238,.25)"}], 100);
  drawChart("chartNetwork", [{values:history.map(p => p.networkDownBps), color:"#60a5fa"},{values:history.map(p => p.networkUpBps),color:"#22d3ee"}]);
  drawChart("chartDisk", [{values:history.map(p => p.diskReadBps), color:"#4ade80"},{values:history.map(p => p.diskWriteBps),color:"#fbbf24"}]);
}

function showModal(id) { const modal = $(`#${id}`); modal.hidden = false; document.body.style.overflow = "hidden"; }
function closeModal(id) { $(`#${id}`).hidden = true; if (!$(".modal-backdrop:not([hidden])")) document.body.style.overflow = ""; }

function showDetails(id) {
  const app = state.data.apps.find(item => item.id === id);
  if (!app) return;
  const components = app.ports.map(port => {
    const listener = port.listeners[0];
    const process = listener?.process;
    const right = port.live ? `${process?.name || "Unknown"} · PID ${listener.pid}` : "Offline";
    return `<div class="component"><div><strong>${escapeHtml(port.label)} · :${port.port}</strong><small>${port.role === "dependency" ? "Observed dependency — never stopped with this app" : "Managed component"}</small></div><span class="state-pill ${port.live ? "running" : "stopped"}">${escapeHtml(right)}</span></div>`;
  }).join("") || `<p class="empty">${app.desktopApp ? (app.desktopInstalled ? "Desktop app tracked by its Windows package, not network ports." : "Not installed for this Windows user. Controls become available when a supported installation is detected.") : "This is an external endpoint with no local port ownership."}</p>`;
  $("#detailContent").innerHTML = `<div class="detail-head"><span class="app-icon">${iconFor(app)}</span><div><p class="eyebrow">${escapeHtml(app.group)}</p><h2 id="detailTitle">${escapeHtml(app.name)}</h2><span class="state-pill ${escapeHtml(app.state)}">${stateLabel(app.state)}</span></div></div>
    <p class="detail-description">${escapeHtml(app.description)}</p>
    <div class="detail-box"><small>Start script / executable and arguments</small><strong>${escapeHtml(app.desktopLauncher ? `Windows AppsFolder: ${app.desktopLauncher}` : app.start?.path || "No launcher configured")}</strong><small>${escapeHtml(JSON.stringify(app.start?.args || []))}</small></div>
    <div class="detail-grid"><div class="detail-box"><small>Working directory</small><strong>${escapeHtml(app.workingDirectory || "External / not configured")}</strong></div><div class="detail-box"><small>Health check</small><strong>${escapeHtml(app.healthUrl || "No health probe")}</strong></div><div class="detail-box"><small>Ownership</small><strong>${app.ownershipSafe === true ? "Verified" : app.ownershipSafe === false ? "Mismatch — controls blocked" : "No managed process live"}</strong></div><div class="detail-box"><small>Lifetime CPU</small><strong>${app.cpuSeconds ? `${app.cpuSeconds}s` : "—"}</strong></div></div>
    <div class="component-list">${components}</div>
    ${[...(app.desktopProcesses || []), ...(app.ownedProcesses || [])].map(process => `<div class="component"><div><strong>${escapeHtml(process.name)} · PID ${process.id}</strong><small>${escapeHtml(process.path)}</small></div><span>${formatBytes(process.memoryBytes)} RAM</span></div>`).join("")}
    ${app.desktopError ? `<p class="safe-note warn">${escapeHtml(app.desktopError)}</p>` : ""}
    ${(app.runtimeWorkers || []).map(worker => `<div class="component"><div><strong>${escapeHtml(app.name)} model worker · PID ${worker.pid}</strong><small>${worker.orphaned ? "Orphaned: original parent has exited" : "Owned model worker"} · ${escapeHtml(worker.path)}</small></div><span>${formatBytes(worker.memoryBytes)} RAM</span></div>`).join("")}
    ${app.runtimeWorkerError ? `<p class="safe-note warn">${escapeHtml(app.runtimeWorkerError)}</p>` : ""}
    <div class="safe-note ${app.ownershipSafe === false ? "warn" : ""}">${app.ownershipSafe === false ? "Process ownership could not be verified. LocalOps will not stop it." : app.desktopApp ? "Stop closes this desktop app and its verified package-local children. Active work may be interrupted; shared external processes and Windows services are excluded." : "Stop targets verified managed processes, including matching manual launches. Dependency ports are excluded."}</div>
    <div class="modal-actions">${app.url ? `<button class="button ghost" data-open="${escapeHtml(app.url)}">Open app</button>` : ""}<button class="button secondary" data-edit="${escapeHtml(app.id)}">Edit registry</button>${app.actions.restart ? `<button class="button danger" data-action="restart" data-app="${escapeHtml(app.id)}">Restart</button>` : ""}</div>`;
  showModal("detailModal");
}

function openAppForm(app = null) {
  const form = $("#appForm");
  form.reset();
  form.elements.id.value = app?.id || "";
  form.elements.name.value = app?.name || "";
  form.elements.group.value = app?.group || "Applications";
  form.elements.description.value = app?.description || "";
  form.elements.url.value = app?.url || "";
  form.elements.healthUrl.value = app?.healthUrl || "";
  form.elements.managedPorts.value = app?.ports.filter(p => p.role === "managed").map(p => p.port).join(", ") || "";
  form.elements.expectedProcesses.value = app?.expectedProcesses.join(", ") || "";
  form.elements.workingDirectory.value = app?.workingDirectory || "";
  form.elements.startPath.value = app?.start?.path || "";
  form.elements.startArgs.value = JSON.stringify(app?.start?.args || []);
  form.elements.manage.checked = app?.controlMode === "manage";
  form.elements.confirm.checked = app?.confirmationRequired || false;
  $("#appModalTitle").textContent = app ? `Edit ${app.name}` : "Add an application";
  showModal("appModal");
}

function askAction(id, action) {
  const app = state.data.apps.find(item => item.id === id);
  if (!app) return;
  state.pending = {id, action, app};
  $("#confirmTitle").textContent = `${action[0].toUpperCase() + action.slice(1)} ${app.name}?`;
  $("#confirmMessage").textContent = action === "start" ? `Run ${app.start?.path || "the configured launcher"} ${JSON.stringify(app.start?.args || [])}. Startup scripts may start their normal dependencies.` : app.id === "ollama" ? "Stop the Ollama Desktop supervisor and its verified server processes? Apps using Ollama will lose access until it is started again." : "Only verified managed processes will be targeted. Shared dependency ports are excluded.";
  if (app.desktopApp) $("#confirmMessage").textContent = action === "start" ? `Open ${app.name} in the signed-in Windows session using its installed package. This does not stream its window to your remote device.` : `Close ${app.name} and its package-local child processes? Active chats, agent runs, and unsaved work may be interrupted. Other installed apps and shared services are excluded.`;
  $("#confirmNameWrap").hidden = true;
  $("#confirmPasswordWrap").hidden = true;
  $("#confirmName").value = "";
  $("#confirmPassword").value = "";
  $("#confirmAction").textContent = `Yes, ${action}`;
  $("#cancelAction").textContent = "No";
  $("#confirmTitle").textContent += ` — ${state.node?.computerName || "Unknown computer"}`;
  showModal("confirmModal");
  $("#cancelAction").focus();
}

function askSystemAction(action) {
  if (action === "cancel-shutdown") {
    executeSystemAction(action);
    return;
  }
  const phrase = state.system?.powerConfirmationPhrases?.[action] || (action === "shutdown" ? "SHUTDOWN THIS PC" : "HIBERNATE THIS PC");
  state.pending = {systemAction: action, phrase};
  $("#confirmTitle").textContent = action === "shutdown" ? "Shutdown this PC?" : "Hibernate this PC?";
  $("#confirmMessage").textContent = action === "shutdown" ? "Windows will shut down after 30 seconds. You can cancel during that window." : "The PC will hibernate immediately and remote applications will become unavailable.";
  $("#confirmNameWrap").hidden = false;
  $("#confirmPasswordWrap").hidden = false;
  $("#cancelAction").textContent = "Cancel";
  $("#confirmNameLabel").textContent = `Type ${phrase}`;
  $("#confirmName").value = "";
  $("#confirmPassword").value = "";
  $("#confirmAction").textContent = action === "shutdown" ? "Schedule shutdown" : "Hibernate PC";
  $("#confirmTitle").textContent += ` — ${state.node?.computerName || "Unknown computer"}`;
  showModal("confirmModal");
  setTimeout(() => $("#confirmName").focus(), 50);
}

async function executeSystemAction(action, payload = {}) {
  if (state.busy) return;
  state.busy = true;
  try {
    const response = await api(`/api/system/actions/${action}`, {method:"POST", body:JSON.stringify(payload)});
    closeModal("confirmModal");
    if (action === "shutdown") toast("Shutdown scheduled", "Windows will shut down in 30 seconds. Use Cancel shutdown to abort.");
    else if (action === "hibernate") toast("Hibernation requested", "The PC will become unavailable while it hibernates.");
    else toast("Shutdown cancelled", "The pending Windows shutdown was cancelled.");
    return response;
  } catch (error) { toast("PC action refused", error.message, true); }
  finally { state.busy = false; }
}

async function executeAction() {
  if (!state.pending || state.busy) return;
  if (state.pending.schedule) {
    const payload = state.pending.schedule;
    state.busy = true;
    $("#confirmAction").disabled = true;
    try {
      await api("/api/schedules", {method:"POST", body:JSON.stringify({...payload, confirmed:true})});
      closeModal("confirmModal");
      $("#scheduleError").textContent = "Schedule saved.";
      await refreshSchedules();
    } catch (error) { toast("Schedule not saved", error.message, true); }
    finally { state.busy = false; $("#confirmAction").disabled = false; }
    return;
  }
  if (state.pending.systemAction) {
    const {systemAction, phrase} = state.pending;
    const button = $("#confirmAction");
    button.disabled = true;
    button.textContent = "Working…";
    await executeSystemAction(systemAction, {confirmPhrase:$("#confirmName").value, password:$("#confirmPassword").value});
    button.disabled = false;
    button.textContent = systemAction === "shutdown" ? "Schedule shutdown" : "Hibernate PC";
    return;
  }
  const {id, action, app} = state.pending;
  state.busy = true;
  const button = $("#confirmAction");
  button.disabled = true;
  button.textContent = "Working…";
  try {
    // Compatibility for an old backend while an elevated restart is pending.
    // The user confirms Yes; no typed name is needed in either version.
    await api(`/api/apps/${encodeURIComponent(id)}/actions/${action}`, {method:"POST", body:JSON.stringify({confirmed:true, confirmName:app.name})});
    closeModal("confirmModal");
    closeModal("detailModal");
    toast(`${app.name}: ${action} completed`, action === "stop" ? "Verified that its managed ports stayed closed." : "The managed application passed its readiness checks.");
    await refresh(true);
  } catch (error) { toast("Action refused", error.message, true); }
  finally { state.busy = false; button.disabled = false; button.textContent = `Yes, ${action}`; }
}

async function refresh(force = false) {
  if (!state.auth?.authenticated) return;
  if (state.busy && !force) return;
  $("#scanPulse").style.background = "#fbbf24";
  try {
    const data = await api(state.data ? "/api/status" : "/api/bootstrap");
    if (data.csrfToken) state.token = data.csrfToken;
    if (data.system) state.system = data.system;
    if (data.server) {
      $("#runtimeUpdateNotice").hidden = data.server.version === "1.6.0";
      $("#runtimeUpdateNotice").textContent = `The backend is still LocalOps ${data.server.version}. Restart LocalOps using restart-localops.cmd and approve Windows elevation to activate the pending process-control fixes. Reload this page after the restart.`;
    }
    state.data = data;
    render();
    await refreshSchedules();
  } catch (error) {
    if (error.status === 401 && state.auth?.remote) { state.data = null; state.token = ""; showAuthPanel("loginPanel"); }
    else toast("Refresh failed", error.message, true);
  }
  finally { $("#scanPulse").style.background = ""; }
}

async function refreshMetrics() {
  if (!state.auth?.authenticated) return;
  try { state.metrics = await api("/api/metrics"); renderMetrics(); }
  catch (error) { $("#metricsSampledAt").textContent = "Telemetry unavailable"; }
}

document.addEventListener("click", async event => {
  const target = event.target.closest("button,a");
  if (!target) return;
  if (target.dataset.close) closeModal(target.dataset.close);
  if (target.dataset.open) window.open(target.dataset.open, "_blank", "noopener");
  if (target.dataset.details) showDetails(target.dataset.details);
  if (target.dataset.action) askAction(target.dataset.app, target.dataset.action);
  if (target.dataset.quickApp && target.dataset.quickAction) askAction(target.dataset.quickApp, target.dataset.quickAction);
  if (target.dataset.systemAction) askSystemAction(target.dataset.systemAction);
  if (target.dataset.edit) {
    const app = state.data.apps.find(item => item.id === target.dataset.edit);
    closeModal("detailModal"); openAppForm(app);
  }
  if (target.dataset.importPort) {
    const name = target.dataset.importProcess ? `${target.dataset.importProcess} on ${target.dataset.importPort}` : `Service on ${target.dataset.importPort}`;
    try {
      await api("/api/discoveries/import", {method:"POST", body:JSON.stringify({port:Number(target.dataset.importPort), processName:target.dataset.importProcess, name})});
      toast("Listener imported", `${name} is observe-only until you review it.`); await refresh(true);
    } catch (error) { toast("Import failed", error.message, true); }
  }
  if (target.classList.contains("filter")) {
    $$(".filter").forEach(item => item.classList.remove("active")); target.classList.add("active"); state.filter = target.dataset.filter; render();
  }
});

function populateScheduleApps() {
  const select = $("#scheduleApp");
  const previous = select.value;
  const options = (state.data?.apps || []).filter(app => app.controlMode === "manage" && app.enabled);
  const signature = JSON.stringify(options.map(app => [app.id, app.name]));
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  select.innerHTML = options.map(app => `<option value="${escapeHtml(app.id)}">${escapeHtml(app.name)}</option>`).join("");
  if (options.some(app => app.id === previous)) select.value = previous;
}

async function refreshSchedules() {
  const result = await api("/api/schedules");
  $("#scheduleList").innerHTML = result.schedules.length ? result.schedules.map(item => `<article class="schedule-row"><div><strong>${escapeHtml(item.appName)} · ${escapeHtml(item.action)}</strong><p>${escapeHtml(new Date(item.nextRunAt).toLocaleString())} · ${escapeHtml(item.repeat)} · ${item.enabled ? "Enabled" : "Disabled"}</p><small>Last result: ${escapeHtml(item.lastStatus)}${item.lastRunAt ? ` · ${escapeHtml(new Date(item.lastRunAt).toLocaleString())}` : ""}${item.lastError ? ` — ${escapeHtml(item.lastError)}` : ""}</small></div><div class="power-actions"><button class="button secondary" data-schedule="${item.id}" data-schedule-action="${item.enabled ? "disable" : "enable"}" ${item.inFlight ? "disabled" : ""}>${item.enabled ? "Pause" : "Enable"}</button><button class="button ghost" data-schedule="${item.id}" data-schedule-action="delete" ${item.inFlight ? "disabled" : ""}>Delete</button></div></article>`).join("") : '<p class="empty">No schedules yet.</p>';
}

$("#scheduleTimezone").textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
$("#scheduleForm").addEventListener("submit", async event => {
  event.preventDefault();
  const fields = event.currentTarget.elements;
  const submit = event.currentTarget.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    const date = new Date(fields.runAt.value);
    const offset = -date.getTimezoneOffset();
    const suffix = `${offset >= 0 ? "+" : "-"}${String(Math.floor(Math.abs(offset) / 60)).padStart(2,"0")}:${String(Math.abs(offset) % 60).padStart(2,"0")}`;
    const app = state.data.apps.find(item => item.id === fields.appId.value);
    state.pending = {schedule:{appId:app.id, confirmName:app.name, action:fields.action.value, repeat:fields.repeat.value, runAt:fields.runAt.value + suffix}};
    $("#confirmTitle").textContent = `Schedule ${fields.action.value} for ${app.name}?`;
    $("#confirmMessage").textContent = `${date.toLocaleString()} · ${fields.repeat.value}. This authorizes the selected future controls. The PC must be awake and LocalOps running.`;
    $("#confirmNameWrap").hidden = true;
    $("#confirmPasswordWrap").hidden = true;
    $("#confirmAction").textContent = "Yes, add schedule";
    $("#cancelAction").textContent = "No";
    $("#confirmTitle").textContent += ` — ${state.node?.computerName || "Unknown computer"}`;
  showModal("confirmModal");
    $("#cancelAction").focus();
  } catch (error) { $("#scheduleError").textContent = error.message; }
  finally { submit.disabled = false; }
});
$("#scheduleList").addEventListener("click", async event => {
  const button = event.target.closest("button[data-schedule]");
  if (!button) return;
  button.disabled = true;
  try {
    await api(`/api/schedules/${button.dataset.schedule}/actions/${button.dataset.scheduleAction}`, {method:"POST",body:"{}"});
    await refreshSchedules();
  } catch (error) { toast("Schedule unchanged", error.message, true); button.disabled = false; }
});

$("#addButton").addEventListener("click", () => openAppForm());
$("#refreshButton").addEventListener("click", () => refresh(true));
$("#installButton").addEventListener("click", async () => {
  if (!state.installPrompt) return;
  state.installPrompt.prompt();
  await state.installPrompt.userChoice;
  state.installPrompt = null;
  $("#installButton").hidden = true;
});
$("#confirmAction").addEventListener("click", executeAction);
$("#appForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const fields = form.elements;
  const current = state.data?.apps.find(item => item.id === fields.id.value);
  let startArgs;
  try {
    startArgs = JSON.parse(fields.startArgs.value || "[]");
    if (!Array.isArray(startArgs) || !startArgs.every(item => typeof item === "string")) throw new Error("Use a JSON array of strings for launcher arguments.");
  } catch (error) { toast("Could not save", error.message, true); return; }
  const ports = fields.managedPorts.value.split(",").map(value => Number(value.trim())).filter(Boolean).map(port => ({port, label:`Port ${port}`, role:"managed", required:true}));
  const payload = {
    icon:current?.icon || "server", acceptedHealthStatuses:current?.acceptedHealthStatuses || [200], repairStart:current?.repairStart || false,
    desktopApp:current?.desktopApp || null, processMatch:current?.processMatch || null,
    name:fields.name.value, group:fields.group.value, description:fields.description.value, url:fields.url.value, healthUrl:fields.healthUrl.value,
    ports:[...ports, ...(current?.ports || []).filter(port => port.role === "dependency")], expectedProcesses:fields.expectedProcesses.value.split(",").map(value => value.trim()).filter(Boolean), workingDirectory:fields.workingDirectory.value,
    start:fields.startPath.value ? {path:fields.startPath.value, args:startArgs} : null, controlMode:fields.manage.checked ? "manage" : "observe", confirmationRequired:fields.confirm.checked, enabled:true
  };
  try {
    const path = fields.id.value ? `/api/apps/${encodeURIComponent(fields.id.value)}` : "/api/apps";
    await api(path, {method:"POST", body:JSON.stringify(payload)}); closeModal("appModal"); toast("Registry saved", `${payload.name} is now being tracked.`); await refresh(true);
  } catch (error) { toast("Could not save", error.message, true); }
});

$("#setupForm").addEventListener("submit", async event => {
  event.preventDefault();
  const fields = event.currentTarget.elements;
  try {
    const result = await api("/api/auth/setup", {method:"POST", body:JSON.stringify({setupToken:state.setupToken, username:fields.username.value, password:fields.password.value, code:fields.code.value})});
    state.token = result.csrfToken || state.token;
    $("#recoveryCodes").textContent = (result.recoveryCodes || []).join("\n");
    showAuthPanel("recoveryPanel");
  } catch (error) { $("#authError").textContent = error.message; }
});

$("#loginForm").addEventListener("submit", async event => {
  event.preventDefault();
  const fields = event.currentTarget.elements;
  try {
    const result = await api("/api/auth/login", {method:"POST", body:JSON.stringify({password:fields.password.value, code:fields.code.value})});
    state.token = result.csrfToken;
    state.auth = await api("/api/auth/state");
    startDashboard();
  } catch (error) { $("#authError").textContent = error.message; }
});

$("#continueDashboard").addEventListener("click", async () => {
  state.auth = await api("/api/auth/state");
  state.token = state.auth.csrfToken || state.token;
  startDashboard();
});

$("#logoutButton").addEventListener("click", async () => {
  try { await api("/api/auth/logout", {method:"POST", body:"{}"}); } catch (_) {}
  state.data = null; state.metrics = null; state.token = ""; state.auth = null;
  showAuthPanel("loginPanel");
});

$$('.modal-backdrop').forEach(modal => modal.addEventListener("mousedown", event => { if (event.target === modal) closeModal(modal.id); }));
document.addEventListener("keydown", event => { if (event.key === "Escape") $$(".modal-backdrop:not([hidden])").forEach(modal => closeModal(modal.id)); });


async function loadNodes() {
  try {
    const result = await api("/api/nodes");
    state.nodes = result.nodes;
    $("#nodeSelect").innerHTML = state.nodes.map(node => `<option value="${escapeHtml(node.id)}">${escapeHtml(node.computerName)}${node.id === state.node.id ? " · connected" : ""}</option>`).join("");
    $("#nodeSelect").value = state.node.id;
  } catch (error) { toast("Computer list unavailable", error.message, true); }
}
$("#nodeSelect").addEventListener("change", event => {
  const node = state.nodes.find(item => item.id === event.target.value);
  if (!node || node.id === state.node?.id) return;
  if (state.busy || $(".modal-backdrop:not([hidden])")) {
    event.target.value = state.node.id;
    toast("Finish the current action", "Close the open dialog before switching computers.", true);
    return;
  }
  const target = new URL(node.url);
  target.searchParams.set("node", node.id);
  location.assign(target.href);
});
$("#addNodeButton").addEventListener("click", () => openFleetDialog("pair"));

document.addEventListener("DOMContentLoaded", initialize);
window.addEventListener("resize", () => { if (state.metrics) renderMetrics(); });
window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  state.installPrompt = event;
  $("#installButton").hidden = false;
});
window.addEventListener("appinstalled", () => { state.installPrompt = null; $("#installButton").hidden = true; toast("LocalOps installed", "It is now available from this device's app launcher."); });
if ("serviceWorker" in navigator) window.addEventListener("load", () => navigator.serviceWorker.register("/service-worker.js").catch(error => console.warn("LocalOps service worker registration failed", error)));
