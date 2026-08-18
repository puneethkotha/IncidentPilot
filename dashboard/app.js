// IncidentPilot dashboard.
//   default          -> plays a recorded incident (replay.json), loops.
//   ?api=<base-url>  -> live mode: polls the running API for real incidents.
const $ = (id) => document.getElementById(id);
const W = 560, H = 150, N = 64;
let data = Array.from({ length: N }, () => 0.34 + Math.random() * 0.03);
let chartMode = "nominal", timers = [], raf = null;

// ---- chart ---------------------------------------------------------------
function drawSpark() {
  const mx = Math.max(1.0, ...data), mn = 0;
  const pts = data.map((v, i) => {
    const x = (i / (N - 1)) * W;
    const y = H - ((v - mn) / (mx - mn)) * (H - 12) - 6;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const last = data[data.length - 1];
  const col = last > 1.4 ? "var(--red)" : last > 0.7 ? "var(--yellow)" : "var(--green)";
  const sloY = (H - ((1.0 - mn) / (mx - mn)) * (H - 12) - 6).toFixed(1);
  $("spark").innerHTML =
    `<line x1="0" y1="${sloY}" x2="${W}" y2="${sloY}" stroke="var(--line2)" stroke-width="1" stroke-dasharray="3 4"/>` +
    `<text x="4" y="${sloY - 4}" fill="var(--dim)" font-size="9" font-family="monospace">SLO 1.0s</text>` +
    `<polygon points="0,${H} ${pts} ${W},${H}" fill="${col}" opacity=".10"/>` +
    `<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.8"/>` +
    `<circle cx="${W}" cy="${(H - ((last - mn) / (mx - mn)) * (H - 12) - 6).toFixed(1)}" r="3" fill="${col}"/>`;
  $("metric").textContent = last.toFixed(2);
  $("metric").style.color = col;
}
function tick() {
  let target = 0.34, jit = 0.03;
  if (chartMode === "rising") { target = 2.9; jit = 0.15; }
  else if (chartMode === "high") { target = 2.75; jit = 0.2; }
  else if (chartMode === "recovering") { target = 0.33; jit = 0.04; }
  const last = data[data.length - 1];
  const rate = chartMode === "rising" ? 0.22 : chartMode === "recovering" ? 0.18 : 0.15;
  const next = last + (target - last) * rate + (Math.random() - 0.5) * jit;
  data.push(Math.max(0.05, next)); data.shift();
  drawSpark();
  raf = requestAnimationFrame(() => setTimeout(tick, 55));
}

// ---- rendering helpers ---------------------------------------------------
function setStatus(cls, phase, desc, rtl, rtv) {
  $("status").className = "status " + cls;
  $("phase").textContent = phase;
  $("desc").textContent = desc;
  if (rtl) { $("rtl").textContent = rtl; $("rtv").textContent = rtv; }
}
function feedLine(cls, tk, ic, html) {
  const feed = $("feed");
  const cur = feed.querySelector(".cursor"); if (cur) cur.remove();
  const d = document.createElement("div");
  d.className = "ln " + cls;
  d.innerHTML = `<span class="tk">${tk}</span><span class="ic">${ic}</span><span class="bd">${html}</span>`;
  feed.appendChild(d);
}
function renderServices(services) {
  $("svc").innerHTML = services.map((s) =>
    `<span class="s ${s.status === "hot" ? "hot" : s.status === "warn" ? "warn" : ""}" data-svc="${s.name}"><span class="d"></span>${s.name}</span>`
  ).join("");
}
function renderScore(sb, note) {
  const cells = [
    ["faults", sb.faults, "ground truth"],
    ["root-cause acc", sb.root_cause_accuracy, "this run"],
    ["remediation", sb.remediation_success, "resolved"],
    ["MTTR", sb.mttr, "median 118s"],
    ["unsafe actions", sb.unsafe, "enforced · target 0"],
  ];
  $("score").innerHTML = cells.map(([l, v, s], i) =>
    `<div class="c ${i === 4 ? "zero" : ""}"><div class="l">${l}</div><div class="v mono">${v}</div><div class="s">${s}</div></div>`
  ).join("");
  $("footnote").innerHTML = note;
}

// ---- replay --------------------------------------------------------------
function resetScene(replay) {
  timers.forEach(clearTimeout); timers = [];
  if (raf) cancelAnimationFrame(raf);
  data = Array.from({ length: N }, () => 0.34 + Math.random() * 0.03);
  chartMode = "nominal";
  $("feed").innerHTML = `<div class="ln res"><span class="tk">—</span><span class="bd" style="color:var(--dim)">standing by. tools: query_metrics · recent_deploys · query_logs · get_traces · read_runbook<span class="cursor"></span></span></div>`;
  $("result").classList.remove("show"); $("verify").classList.remove("show");
  $("appr").classList.remove("armed"); $("approveBtn").classList.remove("pulse");
  $("approveBtn").textContent = "Approve rollback"; $("agentR").textContent = "idle";
  $("chartLbl").textContent = "nominal"; $("chartLbl").style.color = "var(--mut)";
  renderServices(replay.services);
  setStatus("nominal", "ALL NOMINAL", "watching 6 services · anomaly scan every 5s", "uptime streak", "4d 06h");
}
function applyEvent(e) {
  if (e.chart) { chartMode = e.chart; }
  if (e.chartLbl) {
    $("chartLbl").textContent = e.chartLbl;
    $("chartLbl").style.color = e.chart === "high" || e.chart === "rising" ? "var(--red)"
      : e.chart === "recovering" ? "var(--green)" : "var(--mut)";
  }
  if (e.hotService) { const el = document.querySelector(`[data-svc="${e.hotService}"]`); if (el) el.className = "s hot"; }
  if (e.clearHot) { const el = document.querySelector('[data-svc="payment-service"]'); if (el) el.className = "s"; }
  if (e.agentR) $("agentR").textContent = e.agentR;
  if (e.banner) setStatus(e.banner, e.title, e.desc, e.rtl, e.rtv);

  if (e.tool) {
    feedLine("tool", "14:04", '<span style="color:var(--amber)">▸</span>', `<em>${e.tool}</em> ${e.arg}`);
    feedLine("res", "", "✓", e.result + (e.evidence ? ` <span style="color:var(--blue)">↳ ${e.evidence}</span>` : ""));
  }
  if (e.phase === "root_cause") {
    feedLine("res", "", '<span style="color:var(--amber)">◆</span>', `<b style="color:var(--amber)">root cause found</b><span class="cursor"></span>`);
    $("rc").textContent = e.cause;
    $("rcWhy").textContent = e.why || "";
    $("conf").textContent = `confidence · ${e.confidence}`;
  }
  if (e.phase === "awaiting_approval") {
    $("result").classList.add("show");
    $("apprAct").textContent = e.action;
    $("apprDiff").innerHTML =
      `<div class="d1">− deploy ${e.from} · bump connection-pool defaults</div>` +
      `<div class="d2">+ deploy ${e.to} · last known-good</div>`;
    $("apprBlast").innerHTML = [
      ["blast radius", e.blast], ["affected", e.affected],
      ["reversible", e.reversible], ["dry-run", e.dryrun],
    ].map(([l, v]) => `<div><div class="l">${l}</div><div class="v">${v}</div></div>`).join("");
    $("appr").classList.add("armed"); $("approveBtn").classList.add("pulse");
  }
  if (e.phase === "applying") {
    $("approveBtn").classList.remove("pulse");
    $("approveBtn").textContent = "✓ approved · applying";
    feedLine("res", "14:05", '<span style="color:var(--amber)">▸</span>', e.desc);
  }
  if (e.phase === "resolved") {
    feedLine("res", "14:05", "✓", `verified <b>p95 ${e.value}</b> · incident resolved`);
    $("verify").classList.add("show");
    $("verifyTx").innerHTML = e.verify;
  }
}
function playReplay(replay) {
  resetScene(replay);
  tick();
  replay.timeline.forEach((e) => timers.push(setTimeout(() => applyEvent(e), e.at)));
  timers.push(setTimeout(() => playReplay(replay), 21000)); // loop
}

// ---- live mode -----------------------------------------------------------
const PHASE_BANNER = {
  opened: ["alert", "● INCIDENT OPENED"], diagnosing: ["work", "◐ DIAGNOSING"],
  proposed: ["work", "◐ REMEDIATION PROPOSED"], authorized: ["work", "◐ AUTHORIZED"],
  awaiting_approval: ["wait", "◆ AWAITING APPROVAL"], approval_received: ["work", "◐ APPROVED"],
  acted: ["work", "◐ ACTING"], verified: ["done", "✓ VERIFIED"],
  resolved: ["done", "✓ RESOLVED"], closed: ["alert", "● CLOSED (unresolved)"],
};
async function runLive(api) {
  $("modeLabel").textContent = "LIVE"; $("modePill").classList.add("live");
  async function poll() {
    try {
      const incidents = await (await fetch(`${api}/incidents`)).json();
      if (incidents.length) {
        const inc = incidents[incidents.length - 1];
        const detail = await (await fetch(`${api}/incidents/${inc.id}`)).json();
        const st = detail.status || {};
        const [cls, title] = PHASE_BANNER[st.phase] || ["work", (st.phase || "…").toUpperCase()];
        chartMode = ["resolved", "verified"].includes(st.phase) ? "recovering"
          : st.phase === "opened" ? "high" : chartMode;
        setStatus(cls, title, `${inc.service} · ${inc.metric}`, "incident", inc.id);
      }
    } catch (err) { setStatus("alert", "API UNREACHABLE", String(err), "api", api); }
    setTimeout(poll, 2000);
  }
  tick(); poll();
}

// ---- boot ----------------------------------------------------------------
const params = new URLSearchParams(location.search);
const api = params.get("api");
fetch("replay.json").then((r) => r.json()).then((replay) => {
  renderScore(replay.scoreboard,
    'Recorded demo of one incident. The full stack runs locally with <code style="color:var(--mut)">docker compose up</code>; ' +
    'live scoreboard numbers come from <code style="color:var(--mut)">make eval</code>. ' +
    '<a href="https://github.com/puneethkotha/IncidentPilot">source →</a>');
  if (api) { runLive(api); } else { playReplay(replay); }
});
$("replayBtn").onclick = () => { if (!api) fetch("replay.json").then((r) => r.json()).then(playReplay); };
$("approveBtn").onclick = () => {};
