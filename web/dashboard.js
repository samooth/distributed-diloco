const $ = (id) => document.getElementById(id);
const log = (msg, level = "info") => {
  const el = $("logs");
  const line = document.createElement("div");
  line.className = "log-line";
  const t = new Date().toLocaleTimeString();
  line.innerHTML = `<span class="log-time">${t}</span><span class="log-${level}">${escapeHtml(msg)}</span>`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
};

const toast = (msg, level = "info", ms = 3000) => {
  const container = $("toasts");
  const t = document.createElement("div");
  t.className = "toast " + level;
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transform = "translateX(120%)"; setTimeout(() => t.remove(), 250); }, ms);
};

// Theme
const applyTheme = (theme) => {
  document.body.classList.toggle("dark", theme === "dark");
  $("theme-toggle").textContent = theme === "dark" ? "🌙" : "☀️";
  localStorage.setItem("diloco-theme", theme);
};
$("theme-toggle").addEventListener("click", () => {
  applyTheme(document.body.classList.contains("dark") ? "light" : "dark");
});
applyTheme(localStorage.getItem("diloco-theme") || "light");
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtBytes = (v) => {
  if (v == null || !isFinite(Number(v))) return "—";
  const n = Number(v);
  if (n < 1024) return Math.round(n) + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let x = n / 1024, i = 0;
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
  return (x >= 10 ? x.toFixed(1) : x.toFixed(2)) + " " + units[i];
};

let timer = null;
let connected = false;
let lastStatus = null;

function headers() {
  const t = $("token").value.trim();
  const h = {};
  if (t) h["X-DiLoCo-Token"] = t;
  return h;
}

async function fetchJson(path) {
  const base = $("url").value.trim().replace(/\/$/, "");
  if (!base) throw new Error("URL del coordinador vacía");
  let res;
  try {
    res = await fetch(base + path, { headers: headers(), cache: "no-store" });
  } catch (e) {
    throw new Error(`No se puede conectar al coordinador (${e.message || e})`);
  }
  if (!res.ok) {
    let text = "";
    try { text = (await res.text()).slice(0, 180); } catch (_) {}
    if (res.status === 401) throw new Error("401 no autorizado: revisa el token del coordinador");
    if (res.status === 403) throw new Error(`403 prohibido: ${text || "identidad/allowlist/política"}`);
    throw new Error(`${res.status}${text ? " " + text : ""}`);
  }
  return res.json();
}

function setPill(text, cls) {
  const p = $("status-pill");
  p.textContent = text;
  p.className = "pill " + cls;
}

function drawSparkline(canvasId, values, colorVar) {
  const canvas = $(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = 24 * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = 24;
  ctx.clearRect(0, 0, W, H);
  if (!values || values.length < 2) return;
  const min = Math.min(...values), max = Math.max(...values);
  const range = (max - min) || 1;
  const color = getComputedStyle(document.body).getPropertyValue(colorVar).trim();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = (W * i) / (values.length - 1);
    const y = H - ((v - min) / range) * (H - 4) - 2;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawChart(history) {
  const canvas = $("chart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = 180 * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = 180;
  ctx.clearRect(0, 0, W, H);

  const data = (history || []).slice().sort((a, b) => a.round - b.round);
  if (data.length === 0) {
    ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--muted").trim();
    ctx.font = "13px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Sin datos de rondas", W / 2, H / 2);
    $("chart-meta").textContent = "sin datos";
    return;
  }
  $("chart-meta").textContent = `${data.length} rondas`;

  const pad = { l: 44, r: 16, t: 16, b: 28 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;

  const losses = data.map((d) => Number(d.train_loss));
  const deltas = data.map((d) => Number(d.delta_norm));
  const minL = Math.min(...losses), maxL = Math.max(...losses);
  const minD = Math.min(...deltas), maxD = Math.max(...deltas);
  const rangeL = (maxL - minL) || 1;
  const rangeD = (maxD - minD) || 1;

  // grid
  ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue("--border").trim();
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (plotH * i) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
  }

  const xAt = (i) => pad.l + (plotW * i) / Math.max(1, data.length - 1);
  const yL = (v) => pad.t + plotH - ((v - minL) / rangeL) * plotH;
  const yD = (v) => pad.t + plotH - ((v - minD) / rangeD) * plotH;

  // loss line
  ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue("--accent").trim();
  ctx.lineWidth = 2;
  ctx.beginPath();
  data.forEach((d, i) => {
    const x = xAt(i), y = yL(Number(d.train_loss));
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // delta line
  ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue("--accent-2").trim();
  ctx.lineWidth = 2;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  data.forEach((d, i) => {
    const x = xAt(i), y = yD(Number(d.delta_norm));
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);

  // legend
  ctx.font = "11px Inter, sans-serif";
  ctx.textAlign = "left";
  ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--accent").trim();
  ctx.fillText("loss", pad.l, H - 6);
  ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--accent-2").trim();
  ctx.fillText("delta_norm", pad.l + 50, H - 6);
}

async function refresh() {
  try {
    const health = await fetchJson("/v1/health");
    const status = await fetchJson("/v1/status");
    lastStatus = status;
    connected = true;
    window._lastConnErr = null;
    setPill(health.failure ? "Error" : "Conectado", health.failure ? "pill-err" : "pill-ok");

    $("k-round").textContent = `${status.round} / ${status.max_rounds}`;
    $("k-round-sub").textContent = status.finished ? "finalizado" : "en curso";
    $("k-nodes").textContent = status.registered.length;
    $("k-nodes-sub").textContent = `quórum ${status.quorum}`;
    $("k-pending").textContent = status.pending_updates.length;
    $("k-pending-sub").textContent = "deltas recibidos";

    const lr = status.last_result;
    $("k-loss").textContent = lr ? Number(lr.train_loss).toFixed(4) : "—";
    $("k-loss-sub").textContent = lr ? `ronda ${lr.round}` : "sin datos";
    $("k-delta").textContent = lr ? Number(lr.delta_norm).toFixed(4) : "—";
    $("k-delta-sub").textContent = lr ? "agregado" : "—";
    $("k-sync").textContent = lr ? Number(lr.sync_seconds).toFixed(2) : "—";
    $("k-sync-sub").textContent = lr ? "segundos" : "—";

    // nodes table
    const nt = document.querySelector("#nodes-table tbody");
    nt.innerHTML = "";
    status.registered.forEach((n) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="mono">${escapeHtml(n)}</td><td><span class="pill pill-ok">activo</span></td><td class="mono">—</td>`;
      nt.appendChild(tr);
    });
    $("nodes-count").textContent = status.registered.length;

    // rounds table
    const rt = document.querySelector("#rounds-table tbody");
    rt.innerHTML = "";
    const hist = status.history || [];
    if (hist.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="7" class="empty">Sin rondas registradas</td>`;
      rt.appendChild(tr);
    } else {
      hist.slice().reverse().forEach((r) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="mono">${r.round}</td>
          <td>${(r.nodes || []).length}</td>
          <td class="mono">${Number(r.train_loss).toFixed(4)}</td>
          <td class="mono">${Number(r.delta_norm).toFixed(4)}</td>
          <td class="mono">${Number(r.sync_seconds).toFixed(2)}</td>
          <td class="mono">${fmtBytes(r.update_bytes_total)}</td>
          <td class="mono">${fmtBytes(r.state_blob_bytes)}</td>`;
        rt.appendChild(tr);
      });
    }
    $("rounds-count").textContent = hist.length;
    drawChart(hist);
    drawSparkline("spark-loss", hist.map((r) => Number(r.train_loss)), "--accent");
    drawSparkline("spark-delta", hist.map((r) => Number(r.delta_norm)), "--accent-2");

    // run/meta info
    const short = (s, n = 8) => (s ? String(s).slice(0, n) : "—");
    const metaParts = [`${hist.length} rondas`];
    if (status.run_id) metaParts.push(`run ${status.run_id}`);
    if (status.config_hash) metaParts.push(`cfg ${short(status.config_hash, 10)}`);
    if (status.base_hash) metaParts.push(`base ${short(status.base_hash, 8)}`);
    $("chart-meta").textContent = metaParts.join(" · ");

    // nodes empty state
    if (status.registered.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="3" class="empty">Sin nodos registrados</td>`;
      nt.appendChild(tr);
    }

    if (lr && (!window._lastRound || window._lastRound !== lr.round)) {
      window._lastRound = lr.round;
      log(`Ronda ${lr.round} completada: loss=${Number(lr.train_loss).toFixed(4)} delta_norm=${Number(lr.delta_norm).toFixed(4)} sync=${Number(lr.sync_seconds).toFixed(2)}s`, "info");
    }
  } catch (e) {
    connected = false;
    setPill("Error", "pill-err");
    const msg = e && e.message ? e.message : String(e);
    log(`Error de conexión: ${msg}`, "err");
    // avoid spamming toasts every poll tick, but surface first/changed errors
    if (window._lastConnErr !== msg) {
      window._lastConnErr = msg;
      toast(msg, "err", 4500);
    }
  }
}

function startPolling() {
  if (timer) clearInterval(timer);
  const ms = parseInt($("interval").value, 10);
  timer = setInterval(refresh, ms);
  refresh();
}

function stopPolling() {
  if (timer) { clearInterval(timer); timer = null; }
}

$("mode").addEventListener("change", () => {
  const mode = $("mode").value;
  if (mode === "hyperswarm") {
    $("url").value = "http://127.0.0.1:18080";
    log("Modo Hyperswarm: asegúrate de tener el túnel activo en 127.0.0.1:18080", "warn");
  } else {
    $("url").value = "http://127.0.0.1:8095";
  }
});

$("connect").addEventListener("click", () => {
  stopPolling();
  startPolling();
  $("connect").disabled = true;
  $("disconnect").disabled = false;
  log("Conectando a " + $("url").value, "info");
});

$("disconnect").addEventListener("click", () => {
  stopPolling();
  $("connect").disabled = false;
  $("disconnect").disabled = true;
  setPill("Desconectado", "pill-idle");
  log("Desconectado", "warn");
});

$("clear-logs").addEventListener("click", () => { $("logs").innerHTML = ""; });

$("export-json").addEventListener("click", () => {
  if (!lastStatus) { log("Sin datos para exportar", "warn"); return; }
  const blob = new Blob([JSON.stringify(lastStatus, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `diloco-status-${Date.now()}.json`; a.click();
  URL.revokeObjectURL(url);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "r" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); refresh(); }
  if (e.key === "Escape") { $("logs").innerHTML = ""; }
});

window.addEventListener("resize", () => { if (lastStatus) drawChart(lastStatus.history || []); });

// Wizard
let wizardStep = 1;
const showWizard = (step) => {
  wizardStep = step;
  document.querySelectorAll(".wizard-pane").forEach((p) => {
    p.hidden = Number(p.dataset.pane) !== step;
  });
  document.querySelectorAll(".wizard-step-dot").forEach((d) => {
    const s = Number(d.dataset.step);
    d.classList.toggle("active", s === step);
    d.classList.toggle("done", s < step);
  });
  if (step === 4) {
    const url = $("w-url").value.trim() || $("url").value.trim();
    const token = $("w-token").value.trim() || $("token").value.trim();
    $("w-summary").textContent = `URL: ${url} · Token: ${token ? "••••" : "(vacío)"}`;
  }
};
const closeWizard = () => { $("wizard").classList.add("hidden"); };
const openWizard = () => { $("wizard").classList.remove("hidden"); showWizard(1); };

document.querySelectorAll("[data-next]").forEach((b) => b.addEventListener("click", () => {
  if (wizardStep < 4) showWizard(wizardStep + 1);
}));
document.querySelectorAll("[data-back]").forEach((b) => b.addEventListener("click", () => {
  if (wizardStep > 1) showWizard(wizardStep - 1);
}));
document.querySelector("[data-skip]").addEventListener("click", closeWizard);
document.querySelector("[data-finish]").addEventListener("click", () => {
  const url = $("w-url").value.trim() || $("url").value.trim();
  const token = $("w-token").value.trim() || $("token").value.trim();
  if (url) { $("url").value = url; localStorage.setItem("diloco-url", url); }
  if (token) { $("token").value = token; localStorage.setItem("diloco-token", token); }
  closeWizard();
  $("connect").click();
});

$("w-test").addEventListener("click", async () => {
  const url = $("w-url").value.trim() || $("url").value.trim();
  const token = $("w-token").value.trim() || $("token").value.trim();
  const btn = $("w-test");
  const nextBtn = document.querySelector('[data-pane="3"] [data-next]');
  btn.disabled = true;
  btn.textContent = "Probando...";
  try {
    const res = await fetch(url.replace(/\/$/, "") + "/v1/health", { headers: token ? { "X-DiLoCo-Token": token } : {} });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    $("w-test-result").textContent = `OK · ronda ${data.round} · nodos ${data.registered} · ${data.failure || "sin errores"}`;
    $("w-test-result").style.color = "var(--success)";
    nextBtn.disabled = false;
  } catch (e) {
    $("w-test-result").textContent = `Error: ${e.message}`;
    $("w-test-result").style.color = "var(--danger)";
    nextBtn.disabled = true;
  } finally {
    btn.disabled = false;
    btn.textContent = "Probar conexión";
  }
});

// Auto-open wizard on first visit
if (!localStorage.getItem("diloco-wizard-done")) {
  openWizard();
}
document.querySelector("[data-finish]").addEventListener("click", () => {
  localStorage.setItem("diloco-wizard-done", "1");
}, { once: true });
document.querySelector("[data-skip]").addEventListener("click", () => {
  localStorage.setItem("diloco-wizard-done", "1");
}, { once: true });

// Auto-fill from URL params or saved settings
const params = new URLSearchParams(window.location.search);
const paramUrl = params.get("url");
const paramToken = params.get("token");
const paramAgentUrl = params.get("agent");
const savedUrl = localStorage.getItem("diloco-url");
const savedToken = localStorage.getItem("diloco-token");
const savedAgentUrl = localStorage.getItem("diloco-agent-url");
if (paramUrl) $("url").value = paramUrl;
else if (savedUrl) $("url").value = savedUrl;
if (paramToken) $("token").value = paramToken;
else if (savedToken) $("token").value = savedToken;
if ($("agent-url")) {
  if (paramAgentUrl) $("agent-url").value = paramAgentUrl;
  else if (savedAgentUrl) $("agent-url").value = savedAgentUrl;
  else if ($("agent-url").value === "http://127.0.0.1:8765" && location.protocol.startsWith("http") && !["127.0.0.1", "localhost"].includes(location.hostname)) {
    $("agent-url").value = location.origin;
  }
  $("agent-url").addEventListener("change", () => localStorage.setItem("diloco-agent-url", $("agent-url").value.trim()));
}

// Launcher (agent)
const LAUNCHER_STORAGE_KEY = "diloco-launcher-config-v1";
const launcherFieldIds = [
  "lc-config","lc-device","lc-max-rounds","lc-init-state","lc-p2p-topic","lc-p2p-context","lc-coord-identity","lc-allow-pubkeys","lc-require-node-identity",
  "lw-config","lw-device","lw-node-id","lw-shard-id","lw-coordinator","lw-p2p-topic","lw-tunnel-port","lw-init-state","lw-state-cache-dir","lw-p2p-context","lw-node-identity","lw-coordinator-pubkey","lw-invite"
];

function loadLauncherConfig() {
  try {
    const raw = localStorage.getItem(LAUNCHER_STORAGE_KEY);
    if (!raw) return;
    const cfg = JSON.parse(raw);
    launcherFieldIds.forEach((id) => {
      const el = $(id);
      if (!el || !(id in cfg)) return;
      if (el.type === "checkbox") el.checked = !!cfg[id];
      else el.value = cfg[id] ?? "";
    });
    const st = $("launcher-config-status");
    if (st) st.textContent = "configuración cargada";
  } catch (_) { /* ignore */ }
}

function saveLauncherConfig() {
  const cfg = {};
  launcherFieldIds.forEach((id) => {
    const el = $(id);
    if (!el) return;
    cfg[id] = el.type === "checkbox" ? !!el.checked : el.value;
  });
  try {
    localStorage.setItem(LAUNCHER_STORAGE_KEY, JSON.stringify(cfg));
    const st = $("launcher-config-status");
    if (st) st.textContent = "guardado " + new Date().toLocaleTimeString();
    toast("Configuración de launcher guardada", "info");
  } catch (e) {
    log(`No se pudo guardar configuración: ${e.message}`, "warn");
  }
}

function setVal(id, v) { const el = $(id); if (el) el.value = v; }
function getVal(id) { const el = $(id); return el ? String(el.value || "").trim() : ""; }
function getNum(id, def = 0) {
  const raw = getVal(id);
  if (raw === "") return def;
  const n = Number(raw);
  return Number.isFinite(n) ? n : def;
}

async function agentFetch(path, opts = {}) {
  const base = ($("agent-url")?.value || "").trim().replace(/\/$/, "");
  if (!base) throw new Error("Agent URL vacío");
  const res = await fetch(base + path, { credentials: "same-origin", ...opts });
  if (res.status === 401) {
    throw new Error("401 no autorizado (inicia sesión con AGENT_TOKEN en el agente)");
  }
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.text()).slice(0, 120); } catch (_) {}
    throw new Error(`${res.status}${detail ? " " + detail : ""}`);
  }
  return res.json();
}

async function refreshProcs() {
  const tb = document.querySelector("#procs-table tbody");
  if (!tb) return;
  try {
    const procs = await agentFetch("/processes");
    tb.innerHTML = "";
    if (!Array.isArray(procs) || procs.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="6" class="empty">Sin procesos lanzados</td>`;
      tb.appendChild(tr);
      return;
    }
    procs.forEach((p) => {
      const tr = document.createElement("tr");
      const cmd = (p.cmd || []).join(" ");
      const logPath = p.log || "";
      tr.innerHTML = `
        <td class="mono">${escapeHtml(p.id || "")}</td>
        <td class="mono">${escapeHtml(p.pid ?? "")}</td>
        <td>${p.running ? '<span class="pill pill-ok">running</span>' : '<span class="pill pill-idle">stopped</span>'}</td>
        <td class="mono"><span class="cmd-cell" title="${escapeHtml(cmd)}">${escapeHtml(cmd)}</span></td>
        <td class="mono"><span class="cmd-cell" title="${escapeHtml(logPath)}">${escapeHtml(logPath || "—")}</span></td>
        <td>${p.running ? `<button class="btn btn-ghost btn-sm" data-stop="${escapeHtml(p.id)}">Stop</button>` : '<span class="muted">—</span>'}</td>`;
      tb.appendChild(tr);
    });
    tb.querySelectorAll("[data-stop]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await agentFetch("/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: b.dataset.stop }) });
          log(`Proceso ${b.dataset.stop} detenido`, "warn");
          toast("Proceso detenido", "warn");
          refreshProcs();
        } catch (e) {
          log(`Error al detener: ${e.message}`, "err");
          toast(`Error al detener: ${e.message}`, "err", 4500);
        }
      });
    });
  } catch (e) {
    // Keep last known rows on transient errors? replace with a clear message
    tb.innerHTML = `<tr><td colspan="6" class="empty">Agent: ${escapeHtml(e.message)}</td></tr>`;
  }
}

function buildCoordinatorPayload() {
  const payload = {
    config: getVal("lc-config") || "configs/smoke.json",
    max_rounds: Math.max(1, getNum("lc-max-rounds", 3)),
    device: getVal("lc-device") || "cpu",
  };
  const initState = getVal("lc-init-state"); if (initState) payload.init_state = initState;
  const topic = getVal("lc-p2p-topic"); if (topic) payload.p2p_topic = topic;
  const context = getVal("lc-p2p-context"); if (context) payload.p2p_context = context;
  const ident = getVal("lc-coord-identity"); if (ident) payload.coordinator_identity = ident;
  const allowRaw = ($("lc-allow-pubkeys")?.value || "").trim();
  if (allowRaw) {
    payload.allow_pubkeys = allowRaw.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
  }
  if ($("lc-require-node-identity")?.checked) payload.require_node_identity = true;
  return payload;
}

function buildWorkerPayload() {
  const payload = {
    config: getVal("lw-config") || "configs/smoke.json",
    node_id: getVal("lw-node-id") || "node-0",
    shard_id: Math.max(0, getNum("lw-shard-id", 0)),
    device: getVal("lw-device") || "cpu",
  };
  const coordinator = getVal("lw-coordinator"); if (coordinator) payload.coordinator = coordinator;
  const topic = getVal("lw-p2p-topic"); if (topic) payload.p2p_topic = topic;
  const tunnelPort = getNum("lw-tunnel-port", 0); if (tunnelPort > 0) payload.tunnel_listen_port = tunnelPort;
  const initState = getVal("lw-init-state"); if (initState) payload.init_state = initState;
  const stateCacheDir = getVal("lw-state-cache-dir"); if (stateCacheDir) payload.state_cache_dir = stateCacheDir;
  const context = getVal("lw-p2p-context"); if (context) payload.p2p_context = context;
  const ident = getVal("lw-node-identity"); if (ident) payload.node_identity = ident;
  const coordPk = getVal("lw-coordinator-pubkey"); if (coordPk) payload.coordinator_pubkey = coordPk;
  const invite = getVal("lw-invite"); if (invite) payload.invite = invite;
  return payload;
}

$("launch-coord")?.addEventListener("click", async () => {
  const payload = buildCoordinatorPayload();
  try {
    const r = await agentFetch("/launch/coordinator", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    log(`Coordinador lanzado: ${r.id}`, "info");
    toast("Coordinador lanzado", "info");
    refreshProcs();
  } catch (e) {
    log(`Error lanzando coordinador: ${e.message}`, "err");
    toast(`Error lanzando coordinador: ${e.message}`, "err", 4500);
  }
});

$("launch-worker")?.addEventListener("click", async () => {
  const payload = buildWorkerPayload();
  try {
    const r = await agentFetch("/launch/worker", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    log(`Worker lanzado: ${r.id} (${payload.node_id})`, "info");
    toast(`Worker ${payload.node_id} lanzado`, "info");
    refreshProcs();
  } catch (e) {
    log(`Error lanzando worker: ${e.message}`, "err");
    toast(`Error lanzando worker: ${e.message}`, "err", 4500);
  }
});

$("refresh-procs")?.addEventListener("click", refreshProcs);
$("toggle-launcher-config")?.addEventListener("click", () => {
  const panel = $("launcher-config");
  if (!panel) return;
  panel.hidden = !panel.hidden;
  $("toggle-launcher-config").textContent = panel.hidden ? "Mostrar configuración" : "Ocultar configuración";
});
$("save-launcher-config")?.addEventListener("click", saveLauncherConfig);

// Prefill launcher defaults from coordinator connection when useful
(() => {
  const coordUrl = ($("url")?.value || "").trim();
  const token = ($("token")?.value || "").trim();
  if (coordUrl && !$("lw-coordinator")?.value) setVal("lw-coordinator", coordUrl);
  // keep token handling separate (coordinator token is for dashboard->coordinator API)
})();

loadLauncherConfig();
setInterval(refreshProcs, 5000);
refreshProcs();

log("Dashboard listo. Conecta al coordinador y, si usas launcher, revisa Agent URL/configuración.", "info");