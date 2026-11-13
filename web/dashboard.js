const $ = (id) => document.getElementById(id);
const log = (msg, level = "info") => {
  const el = $("logs");
  if (!el) return;
  const line = document.createElement("div");
  line.className = "log-line";
  const t = new Date().toLocaleTimeString();
  line.innerHTML = `<span class="log-time">${t}</span><span class="log-${level}">${escapeHtml(msg)}</span>`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
};

const toast = (msg, level = "info", ms = 3000) => {
  const container = $("toasts");
  if (!container) return;
  const t = document.createElement("div");
  t.className = "toast " + level;
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transform = "translateX(120%)"; setTimeout(() => t.remove(), 250); }, ms);
};

const applyTheme = (theme) => {
  document.body.classList.toggle("dark", theme === "dark");
  const btn = $("theme-toggle");
  if (btn) btn.textContent = theme === "dark" ? "🌙" : "☀️";
  localStorage.setItem("diloco-theme", theme);
};
$("theme-toggle")?.addEventListener("click", () => {
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
  const t = ($("token")?.value || "").trim();
  const h = {};
  if (t) h["X-DiLoCo-Token"] = t;
  return h;
}

async function fetchJson(path) {
  const base = ($("url")?.value || "").trim().replace(/\/$/, "");
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
    if (res.status === 401) throw new Error("401 no autorizado en coordinador: revisa el token");
    if (res.status === 403) throw new Error(`403 prohibido en coordinador: ${text || "identidad/allowlist"}`);
    throw new Error(`Coordinador ${res.status}${text ? " " + text : ""}`);
  }
  return res.json();
}

function setPill(text, cls) {
  const p = $("status-pill");
  if (!p) return;
  p.textContent = text;
  p.className = "pill " + cls;
}

function setAgentStatus(text, cls) {
  const p = $("agent-status");
  if (!p) return;
  p.textContent = text;
  p.className = "pill " + cls;
}

function drawChart(history) {
  const canvas = $("chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height;
  ctx.clearRect(0, 0, w, h);

  if (!history || history.length < 2) {
    ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--muted").trim() || "#888";
    ctx.font = "13px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(history && history.length === 1 ? "Se necesita más de 1 ronda para graficar tendencia" : "Sin datos suficientes", w / 2, h / 2);
    return;
  }

  const losses = history.map((r) => Number(r.train_loss));
  const deltas = history.map((r) => Number(r.delta_norm));
  const validLoss = losses.filter((v) => isFinite(v));
  const validDelta = deltas.filter((v) => isFinite(v));
  if (!validLoss.length || !validDelta.length) return;
  const minL = Math.min(...validLoss), maxL = Math.max(...validLoss);
  const minD = Math.min(...validDelta), maxD = Math.max(...validDelta);
  const rangeL = maxL - minL || 1, rangeD = maxD - minD || 1;
  const pad = 30, plotW = w - pad * 2, plotH = h - pad * 2;

  const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#1f6feb";
  const accent2 = getComputedStyle(document.body).getPropertyValue("--accent-2").trim() || "#7c3aed";

  const drawLine = (data, min, range, color) => {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    let started = false;
    data.forEach((v, i) => {
      if (!isFinite(v)) return;
      const x = pad + (i / (data.length - 1)) * plotW;
      const y = pad + plotH - ((v - min) / range) * plotH;
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };

  drawLine(losses, minL, rangeL, accent);
  drawLine(deltas, minD, rangeD, accent2);

  ctx.font = "11px sans-serif";
  ctx.fillStyle = accent;
  ctx.textAlign = "left";
  ctx.fillText("— loss", pad, 14);
  ctx.fillStyle = accent2;
  ctx.fillText("— delta_norm", pad + 50, 14);
}

function drawSparkline(canvasId, data, cssVar) {
  const canvas = $(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);
  const vals = (data || []).filter((v) => isFinite(v)).map(Number);
  if (vals.length < 2) return;
  const min = Math.min(...vals), max = Math.max(...vals), range = max - min || 1;
  const color = getComputedStyle(document.body).getPropertyValue(cssVar).trim() || "#888";
  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  vals.forEach((v, i) => {
    const x = (i / (vals.length - 1)) * rect.width;
    const y = rect.height - ((v - min) / range) * (rect.height - 4) - 2;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function renderEmptyTables(msg = "Sin conexión al coordinador") {
  const nt = document.querySelector("#nodes-table tbody");
  const rt = document.querySelector("#rounds-table tbody");
  if (nt) nt.innerHTML = `<tr><td colspan="3" class="empty">${escapeHtml(msg)}</td></tr>`;
  if (rt) rt.innerHTML = `<tr><td colspan="7" class="empty">${escapeHtml(msg)}</td></tr>`;
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
    $("k-loss").textContent = lr && isFinite(Number(lr.train_loss)) ? Number(lr.train_loss).toFixed(4) : "—";
    $("k-loss-sub").textContent = lr ? `ronda ${lr.round}` : "sin datos";
    $("k-delta").textContent = lr && isFinite(Number(lr.delta_norm)) ? Number(lr.delta_norm).toFixed(4) : "—";
    $("k-delta-sub").textContent = lr ? "agregado" : "—";
    $("k-sync").textContent = lr && isFinite(Number(lr.sync_seconds)) ? Number(lr.sync_seconds).toFixed(2) : "—";
    $("k-sync-sub").textContent = lr ? "segundos" : "—";

    const nt = document.querySelector("#nodes-table tbody");
    nt.innerHTML = "";
    if (!status.registered || status.registered.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="3" class="empty">Sin nodos registrados</td>`;
      nt.appendChild(tr);
    } else {
      status.registered.forEach((n) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td class="mono">${escapeHtml(n)}</td><td><span class="pill pill-ok">activo</span></td><td class="mono">—</td>`;
        nt.appendChild(tr);
      });
    }
    $("nodes-count").textContent = (status.registered || []).length;

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
          <td class="mono">${escapeHtml(r.round)}</td>
          <td>${(r.nodes || []).length}</td>
          <td class="mono">${isFinite(Number(r.train_loss)) ? Number(r.train_loss).toFixed(4) : "—"}</td>
          <td class="mono">${isFinite(Number(r.delta_norm)) ? Number(r.delta_norm).toFixed(4) : "—"}</td>
          <td class="mono">${isFinite(Number(r.sync_seconds)) ? Number(r.sync_seconds).toFixed(2) : "—"}</td>
          <td class="mono">${fmtBytes(r.update_bytes_total)}</td>
          <td class="mono">${fmtBytes(r.state_blob_bytes)}</td>`;
        rt.appendChild(tr);
      });
    }
    $("rounds-count").textContent = hist.length;
    drawChart(hist);
    drawSparkline("spark-loss", hist.map((r) => Number(r.train_loss)), "--accent");
    drawSparkline("spark-delta", hist.map((r) => Number(r.delta_norm)), "--accent-2");

    const short = (s, n = 10) => (s ? String(s).slice(0, n) : "—");
    const metaParts = [`${hist.length} rondas`];
    if (status.run_id) metaParts.push(`run ${status.run_id}`);
    if (status.config_hash) metaParts.push(`cfg ${short(status.config_hash, 10)}`);
    if (status.base_hash) metaParts.push(`base ${short(status.base_hash, 8)}`);
    if (status.finished) metaParts.push("finalizado");
    else if (status.failure) metaParts.push("fallo");
    $("chart-meta").textContent = metaParts.join(" · ");

    if (lr && (!window._lastRound || window._lastRound !== lr.round)) {
      window._lastRound = lr.round;
      log(`Ronda ${lr.round} completada: loss=${isFinite(Number(lr.train_loss)) ? Number(lr.train_loss).toFixed(4) : "—"} delta_norm=${isFinite(Number(lr.delta_norm)) ? Number(lr.delta_norm).toFixed(4) : "—"} sync=${isFinite(Number(lr.sync_seconds)) ? Number(lr.sync_seconds).toFixed(2) : "—"}s`, "info");
    }
  } catch (e) {
    connected = false;
    setPill("Error", "pill-err");
    const msg = e && e.message ? e.message : String(e);
    log(`Error de conexión coordinador: ${msg}`, "err");
    renderEmptyTables("Sin conexión al coordinador");
    if (window._lastConnErr !== msg) {
      window._lastConnErr = msg;
      toast(msg, "err", 4500);
    }
  }
}

function startPolling() {
  if (timer) clearInterval(timer);
  const ms = parseInt($("interval")?.value || "2000", 10) || 2000;
  timer = setInterval(refresh, ms);
  refresh();
}

function stopPolling() {
  if (timer) { clearInterval(timer); timer = null; }
}

$("mode")?.addEventListener("change", () => {
  const mode = $("mode").value;
  if (mode === "hyperswarm") {
    $("url").value = "http://127.0.0.1:18080";
    log("Modo Hyperswarm: asegúrate de tener el túnel activo en 127.0.0.1:18080", "warn");
  } else {
    $("url").value = "http://127.0.0.1:8095";
  }
});

$("connect")?.addEventListener("click", () => {
  stopPolling();
  startPolling();
  $("connect").disabled = true;
  $("disconnect").disabled = false;
  log("Conectando al coordinador " + $("url").value, "info");
});

$("disconnect")?.addEventListener("click", () => {
  stopPolling();
  $("connect").disabled = false;
  $("disconnect").disabled = true;
  setPill("Desconectado", "pill-idle");
  renderEmptyTables("Desconectado");
  log("Desconectado del coordinador", "warn");
});

$("clear-logs")?.addEventListener("click", () => { $("logs").innerHTML = ""; });

$("export-json")?.addEventListener("click", () => {
  if (!lastStatus) { log("Sin datos para exportar", "warn"); toast("Sin datos para exportar", "warn"); return; }
  const blob = new Blob([JSON.stringify(lastStatus, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `diloco-status-${Date.now()}.json`; a.click();
  URL.revokeObjectURL(url);
  toast("JSON exportado", "info");
});

$("export-csv")?.addEventListener("click", () => {
  const hist = (lastStatus && lastStatus.history) || [];
  if (!hist.length) { toast("Sin historial para exportar", "warn"); return; }
  const cols = ["round","nodes_count","train_loss","delta_norm","sync_seconds","update_bytes_total","state_blob_bytes"];
  const lines = [cols.join(",")];
  hist.forEach((r) => {
    const row = [
      r.round ?? "",
      (r.nodes || []).length,
      r.train_loss ?? "",
      r.delta_norm ?? "",
      r.sync_seconds ?? "",
      r.update_bytes_total ?? "",
      r.state_blob_bytes ?? "",
    ].map((v) => `"${String(v).replace(/"/g,'""')}"`);
    lines.push(row.join(","));
  });
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `diloco-rounds-${Date.now()}.csv`; a.click();
  URL.revokeObjectURL(url);
  toast("CSV exportado", "info");
});

document.addEventListener("keydown", (e) => {
  if (e.key === "r" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); refresh(); }
});
window.addEventListener("resize", () => { if (lastStatus) drawChart(lastStatus.history || []); });

// Wizard
let wizardStep = 1;
const showWizard = (step) => {
  wizardStep = step;
  document.querySelectorAll(".wizard-pane").forEach((p) => { p.hidden = Number(p.dataset.pane) !== step; });
  document.querySelectorAll(".wizard-step-dot").forEach((d) => {
    const s = Number(d.dataset.step);
    d.classList.toggle("active", s === step);
    d.classList.toggle("done", s < step);
  });
  if (step === 2) {
    if ($("w-url") && !$("w-url").value) $("w-url").value = $("url")?.value || "http://127.0.0.1:8095";
    if ($("w-token") && !$("w-token").value) $("w-token").value = $("token")?.value || "";
  }
  if (step === 4) {
    const url = $("w-url")?.value.trim() || $("url")?.value.trim() || "";
    const token = $("w-token")?.value.trim() || $("token")?.value.trim() || "";
    $("w-summary").textContent = `Coordinador: ${url || "—"} · Token: ${token ? "configurado" : "(vacío)"}`;
  }
};
const closeWizard = () => { $("wizard")?.classList.add("hidden"); };
const openWizard = () => { $("wizard")?.classList.remove("hidden"); showWizard(1); };

document.querySelectorAll("[data-next]").forEach((b) => b.addEventListener("click", () => { if (wizardStep < 4) showWizard(wizardStep + 1); }));
document.querySelectorAll("[data-back]").forEach((b) => b.addEventListener("click", () => { if (wizardStep > 1) showWizard(wizardStep - 1); }));
document.querySelector("[data-skip]")?.addEventListener("click", () => { localStorage.setItem("diloco-wizard-done", "1"); closeWizard(); });
document.querySelector("[data-finish]")?.addEventListener("click", () => {
  const url = $("w-url")?.value.trim() || $("url")?.value.trim() || "";
  const token = $("w-token")?.value.trim() || $("token")?.value.trim() || "";
  if (url) { $("url").value = url; localStorage.setItem("diloco-url", url); }
  if (token) { $("token").value = token; localStorage.setItem("diloco-token", token); }
  localStorage.setItem("diloco-wizard-done", "1");
  closeWizard();
  $("connect")?.click();
});

$("w-test")?.addEventListener("click", async () => {
  const url = $("w-url")?.value.trim() || $("url")?.value.trim() || "";
  const token = $("w-token")?.value.trim() || $("token")?.value.trim() || "";
  const btn = $("w-test");
  const nextBtn = document.querySelector('[data-pane="3"] [data-next]');
  if (!url) { $("w-test-result").textContent = "Ingresa una URL del coordinador."; $("w-test-result").style.color = "var(--danger)"; return; }
  btn.disabled = true;
  btn.textContent = "Probando...";
  try {
    const res = await fetch(url.replace(/\/$/, "") + "/v1/health", { headers: token ? { "X-DiLoCo-Token": token } : {}, cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    $("w-test-result").textContent = `OK · ronda ${data.round} · nodos ${data.registered} · ${data.failure || "sin errores"}`;
    $("w-test-result").style.color = "var(--success)";
    if (nextBtn) nextBtn.disabled = false;
  } catch (e) {
    $("w-test-result").textContent = `Error: ${e.message}`;
    $("w-test-result").style.color = "var(--danger)";
    if (nextBtn) nextBtn.disabled = true;
  } finally {
    btn.disabled = false;
    btn.textContent = "Probar conexión";
  }
});

// Auto-fill from URL params or saved settings
const params = new URLSearchParams(window.location.search);
const paramUrl = params.get("url");
const paramToken = params.get("token");
const paramAgentUrl = params.get("agent");
const savedUrl = localStorage.getItem("diloco-url");
const savedToken = localStorage.getItem("diloco-token");
const savedAgentUrl = localStorage.getItem("diloco-agent-url");
if (paramUrl) $("url").value = paramUrl; else if (savedUrl) $("url").value = savedUrl;
if (paramToken) $("token").value = paramToken; else if (savedToken) $("token").value = savedToken;
if ($("agent-url")) {
  if (paramAgentUrl) $("agent-url").value = paramAgentUrl;
  else if (savedAgentUrl) $("agent-url").value = savedAgentUrl;
  else if ($("agent-url").value === "http://127.0.0.1:8765" && location.protocol.startsWith("http") && !["127.0.0.1", "localhost"].includes(location.hostname)) {
    $("agent-url").value = location.origin;
  }
  $("agent-url").addEventListener("change", () => {
    localStorage.setItem("diloco-agent-url", $("agent-url").value.trim());
    refreshProcs();
  });
}

if (!localStorage.getItem("diloco-wizard-done")) openWizard();

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
  } catch (_) {}
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
  let res;
  try {
    res = await fetch(base + path, { credentials: "same-origin", cache: "no-store", ...opts });
  } catch (e) {
    throw new Error(`Agente no accesible (${e.message || e})`);
  }
  if (res.status === 401) {
    throw new Error("401 no autorizado (inicia sesión en el agente: contraseña = AGENT_TOKEN)");
  }
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.text()).slice(0, 160); } catch (_) {}
    throw new Error(`Agente ${res.status}${detail ? " " + detail : ""}`);
  }
  return res.json();
}

let selectedProcLogId = null;
let procLogTimer = null;

async function loadProcLog(procId, opts = {}) {
  if (!procId) return;
  selectedProcLogId = procId;
  const panel = $("proc-log-panel");
  const title = $("proc-log-title");
  const content = $("proc-log-content");
  if (panel) panel.hidden = false;
  if (title) title.textContent = `Log: ${procId}`;
  if (content && !opts.silent) content.textContent = "Cargando…";
  try {
    const data = await agentFetch(`/process/log?id=${encodeURIComponent(procId)}&tail=220`);
    if (selectedProcLogId !== procId) return;
    const state = data.running ? "running" : `stopped (${data.returncode ?? "?"})`;
    if (title) title.textContent = `Log: ${procId} · ${state}`;
    if (content) content.textContent = (data.tail || "(log vacío)");
  } catch (e) {
    if (selectedProcLogId !== procId) return;
    if (content) content.textContent = `Error: ${e.message}`;
  }
}

function closeProcLog() {
  selectedProcLogId = null;
  if (procLogTimer) { clearInterval(procLogTimer); procLogTimer = null; }
  const panel = $("proc-log-panel");
  if (panel) panel.hidden = true;
}

$("proc-log-close")?.addEventListener("click", closeProcLog);
$("proc-log-refresh")?.addEventListener("click", () => { if (selectedProcLogId) loadProcLog(selectedProcLogId); });

async function refreshProcs() {
  const tb = document.querySelector("#procs-table tbody");
  if (!tb) return;
  try {
    const procs = await agentFetch("/processes");
    setAgentStatus(`Agente: OK (${Array.isArray(procs) ? procs.length : 0})`, "pill-ok");
    tb.innerHTML = "";
    if (!Array.isArray(procs) || procs.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="6" class="empty">Sin procesos lanzados en el agente</td>`;
      tb.appendChild(tr);
      return;
    }
    procs.forEach((p) => {
      const tr = document.createElement("tr");
      const cmd = (p.cmd || []).join(" ");
      const logPath = p.log || "";
      const status = p.running ? '<span class="pill pill-ok">running</span>' : `<span class="pill pill-idle">stopped${p.returncode != null ? ` (${p.returncode})` : ""}</span>`;
      const actions = [
        `<button class="btn btn-ghost btn-sm" data-log="${escapeHtml(p.id)}">Ver log</button>`,
        p.running ? `<button class="btn btn-ghost btn-sm" data-stop="${escapeHtml(p.id)}">Stop</button>` : ""
      ].filter(Boolean).join(" ");
      tr.innerHTML = `
        <td class="mono">${escapeHtml(p.id || "")}</td>
        <td class="mono">${escapeHtml(p.pid ?? "")}</td>
        <td>${status}</td>
        <td class="mono"><span class="cmd-cell" title="${escapeHtml(cmd)}">${escapeHtml(cmd)}</span></td>
        <td class="mono"><span class="cmd-cell" title="${escapeHtml(logPath)}">${escapeHtml(logPath || "—")}</span></td>
        <td class="actions-cell">${actions || '<span class="muted">—</span>'}</td>`;
      tb.appendChild(tr);
    });
    tb.querySelectorAll("[data-stop]").forEach((b) => {
      b.addEventListener("click", async () => {
        const id = b.dataset.stop;
        if (!id) return;
        if (!window.confirm(`¿Detener proceso ${id}?`)) return;
        try {
          await agentFetch("/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
          log(`Proceso ${id} detenido`, "warn");
          toast("Proceso detenido", "warn");
          refreshProcs();
          if (selectedProcLogId === id) loadProcLog(id);
        } catch (e) {
          log(`Error al detener ${id}: ${e.message}`, "err");
          toast(`Error al detener: ${e.message}`, "err", 4500);
        }
      });
    });
    tb.querySelectorAll("[data-log]").forEach((b) => {
      b.addEventListener("click", () => {
        const id = b.dataset.log;
        loadProcLog(id);
        if (procLogTimer) clearInterval(procLogTimer);
        procLogTimer = setInterval(() => { if (selectedProcLogId === id) loadProcLog(id, { silent: true }); }, 3000);
      });
    });
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    if (/401/.test(msg)) setAgentStatus("Agente: no autorizado", "pill-err");
    else setAgentStatus("Agente: sin conexión", "pill-idle");
    tb.innerHTML = `<tr><td colspan="6" class="empty">Agent: ${escapeHtml(msg)}</td></tr>`;
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

function validateCoordinatorPayload(p) {
  if (!p.config) return "Config de coordinador requerida";
  if (!Number.isFinite(Number(p.max_rounds)) || Number(p.max_rounds) < 1) return "max_rounds debe ser >= 1";
  return null;
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

function validateWorkerPayload(p) {
  if (!p.config) return "Config de worker requerida";
  if (!p.node_id) return "node_id requerido";
  if (!/^[A-Za-z0-9._:-]+$/.test(p.node_id)) return "node_id contiene caracteres no recomendados";
  if (!Number.isFinite(Number(p.shard_id)) || Number(p.shard_id) < 0) return "shard_id debe ser >= 0";
  if (!p.coordinator && !p.p2p_topic) return "Indica Coordinator URL o P2P topic";
  return null;
}

$("launch-coord")?.addEventListener("click", async () => {
  const payload = buildCoordinatorPayload();
  const err = validateCoordinatorPayload(payload);
  if (err) { toast(err, "err", 4000); log(`Validación launcher: ${err}`, "err"); return; }
  if (!window.confirm(`¿Lanzar coordinador con config ${payload.config} (max_rounds=${payload.max_rounds}, device=${payload.device})?`)) return;
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
  const err = validateWorkerPayload(payload);
  if (err) { toast(err, "err", 4000); log(`Validación launcher: ${err}`, "err"); return; }
  if (!window.confirm(`¿Lanzar worker ${payload.node_id} (shard=${payload.shard_id}, device=${payload.device})?`)) return;
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
  if (coordUrl && !$("lw-coordinator")?.value) setVal("lw-coordinator", coordUrl);
})();

loadLauncherConfig();
renderEmptyTables("Desconectado");
setAgentStatus("Agente: comprobando…", "pill-idle");
setInterval(refreshProcs, 5000);
refreshProcs();
log("Dashboard listo. Conecta al coordinador y, si usas launcher, revisa Agent URL/configuración.", "info");