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
  const res = await fetch(base + path, { headers: headers() });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text.slice(0, 200)}`);
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
      tr.innerHTML = `<td colspan="5" class="empty">Sin rondas registradas</td>`;
      rt.appendChild(tr);
    } else {
      hist.slice().reverse().forEach((r) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="mono">${r.round}</td>
          <td>${(r.nodes || []).length}</td>
          <td class="mono">${Number(r.train_loss).toFixed(4)}</td>
          <td class="mono">${Number(r.delta_norm).toFixed(4)}</td>
          <td class="mono">${Number(r.sync_seconds).toFixed(2)}</td>`;
        rt.appendChild(tr);
      });
    }
    $("rounds-count").textContent = hist.length;
    drawChart(hist);
    drawSparkline("spark-loss", hist.map((r) => Number(r.train_loss)), "--accent");
    drawSparkline("spark-delta", hist.map((r) => Number(r.delta_norm)), "--accent-2");

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
    log(`Error de conexión: ${e.message}`, "err");
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

// Restore saved settings
const savedUrl = localStorage.getItem("diloco-url");
const savedToken = localStorage.getItem("diloco-token");
if (savedUrl) $("url").value = savedUrl;
if (savedToken) $("token").value = savedToken;

log("Dashboard listo. Introduce URL y token, luego pulsa Conectar.", "info");