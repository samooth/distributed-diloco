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
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let timer = null;
let ws = null;
let connected = false;

function headers() {
  const t = $("token").value.trim();
  const h = {};
  if (t) h["X-DiLoCo-Token"] = t;
  return h;
}

async function fetchJson(path, base) {
  const b = (base || $("coord-url").value.trim()).replace(/\/$/, "");
  const res = await fetch(b + path, { headers: headers() });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

function setPill(text, cls) {
  const p = $("status-pill");
  p.textContent = text;
  p.className = "pill " + cls;
}

async function refreshCoord() {
  try {
    const health = await fetchJson("/v1/health");
    const status = await fetchJson("/v1/status");
    connected = true;
    setPill(health.failure ? "Error" : "Conectado", health.failure ? "pill-err" : "pill-ok");

    $("k-nodes").textContent = status.registered.length;
    $("k-round").textContent = `${status.round}/${status.max_rounds}`;
    $("k-pending").textContent = status.pending_updates.length;
    $("k-quorum").textContent = status.quorum;
    $("k-state").textContent = status.finished ? "finalizado" : "en curso";
    $("topo-meta").textContent = `${status.registered.length} nodos · ronda ${status.round}`;

    // topology
    const coord = $("topo-coord");
    coord.textContent = `Coordinador (ronda ${status.round})`;
    const nodesEl = $("topo-nodes");
    nodesEl.innerHTML = "";
    status.registered.forEach((n) => {
      const div = document.createElement("div");
      div.className = "topo-node active";
      div.innerHTML = `<span class="node-name">${escapeHtml(n)}</span><span class="node-meta">shard ${status.registered.indexOf(n)}</span>`;
      nodesEl.appendChild(div);
    });

    // nodes table
    const nt = document.querySelector("#nodes-table tbody");
    nt.innerHTML = "";
    status.registered.forEach((n, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="mono">${escapeHtml(n)}</td><td><span class="pill pill-ok">activo</span></td><td>${i}</td>`;
      nt.appendChild(tr);
    });
    $("nodes-count").textContent = status.registered.length;

    // rounds table
    const rt = document.querySelector("#rounds-table tbody");
    rt.innerHTML = "";
    (status.history || []).slice().reverse().forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="mono">${r.round}</td><td>${(r.nodes || []).length}</td><td class="mono">${Number(r.train_loss).toFixed(4)}</td><td class="mono">${Number(r.sync_seconds).toFixed(2)}</td>`;
      rt.appendChild(tr);
    });
    $("rounds-count").textContent = (status.history || []).length;
  } catch (e) {
    connected = false;
    setPill("Error", "pill-err");
    log(`Error coordinador: ${e.message}`, "err");
  }
}

function connectGateway() {
  const url = $("gw-url").value.trim();
  if (!url) return;
  try {
    ws = new WebSocket(url);
    ws.onopen = () => {
      log("Gateway WebSocket conectado", "info");
      ws.send(JSON.stringify({ type: "join", room: "diloco", name: "network-monitor" }));
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "playerJoined" || msg.type === "peer-joined") {
          log(`Peer unido: ${msg.playerId || msg.name || "?"}`, "info");
        }
        if (msg.type && msg.type.startsWith("diloco-")) {
          log(`Mensaje DiLoCo: ${msg.type} (ronda ${msg.round || "?"})`, "info");
        }
        if (msg.type === "playerList") {
          $("k-peers").textContent = (msg.players || []).length;
        }
      } catch (e) {}
    };
    ws.onclose = () => { log("Gateway desconectado", "warn"); $("k-peers").textContent = "—"; };
    ws.onerror = () => { log("Error en gateway WebSocket", "err"); };
  } catch (e) {
    log(`No se pudo conectar al gateway: ${e.message}`, "err");
  }
}

function startPolling() {
  if (timer) clearInterval(timer);
  timer = setInterval(refreshCoord, 2000);
  refreshCoord();
  connectGateway();
}

function stopPolling() {
  if (timer) { clearInterval(timer); timer = null; }
  if (ws) { ws.close(); ws = null; }
}

$("mode").addEventListener("change", () => {
  const mode = $("mode").value;
  if (mode === "hyperswarm") {
    $("coord-url").value = "http://127.0.0.1:18080";
    log("Modo Hyperswarm: levanta el túnel con --p2p-topic y usa 127.0.0.1:18080", "warn");
  } else {
    $("coord-url").value = "http://127.0.0.1:8095";
  }
});

$("connect").addEventListener("click", () => {
  stopPolling();
  startPolling();
  $("connect").disabled = true;
  $("disconnect").disabled = false;
  log("Conectando...", "info");
});

$("disconnect").addEventListener("click", () => {
  stopPolling();
  $("connect").disabled = false;
  $("disconnect").disabled = true;
  setPill("Desconectado", "pill-idle");
  log("Desconectado", "warn");
});

$("clear-logs").addEventListener("click", () => { $("logs").innerHTML = ""; });

// Auto-fill from URL params
const params = new URLSearchParams(window.location.search);
if (params.get("coord")) $("coord-url").value = params.get("coord");
if (params.get("gw")) $("gw-url").value = params.get("gw");
if (params.get("token")) $("token").value = params.get("token");

log("Network monitor listo. Conecta coordinador y gateway.", "info");