// IncidentPilot console — "Tape".
//   default          → plays a recorded incident (replay.json) on a calm loop.
//   ?api=<base-url>  → live mode: follows the real workflow status from the API.
const $ = (id) => document.getElementById(id);
const W = 560, H = 96, N = 60, YMAX = 3.2;
let data = Array.from({ length: N }, () => 0.34 + Math.random() * 0.015);
let mode = "nominal", timers = [], raf = null, R = null;

// ── chart: fixed scale so SLO/baseline annotations hold still ──────────────
function y(v) { return H - (v / YMAX) * (H - 8) - 4; }
function drawSpark() {
  const last = data[data.length - 1];
  const col = last > 1.0 ? "#E5484D" : "#46A758";
  const pts = data.map((v, i) => `${((i / (N - 1)) * W).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const deploy = mode === "nominal" ? "" :
    `<line x1="196" y1="0" x2="196" y2="${H}" stroke="#E8952B" stroke-width="1" stroke-dasharray="2 3"/>
     <text x="200" y="11" fill="#E8952B" font-size="9" font-family="'JetBrains Mono',monospace">▲ v412 14:00:08Z</text>`;
  $("spark").innerHTML =
    `<rect x="0" y="${y(0.34) - 4}" width="${W}" height="8" fill="#46A758" opacity=".10"/>
     <line x1="0" y1="${y(1.0)}" x2="${W}" y2="${y(1.0)}" stroke="#3a3f47" stroke-dasharray="4 4"/>
     <text x="4" y="${y(1.0) - 4}" fill="#8A8A92" font-size="9" font-family="'JetBrains Mono',monospace">SLO 1.0s</text>
     ${deploy}
     <polygon points="0,${H} ${pts} ${W},${H}" fill="${col}" opacity=".08"/>
     <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.6"/>`;
  const m = $("metric"); m.textContent = last.toFixed(2); m.className = "n tnum" + (last <= 1.0 ? " ok" : "");
  $("delta").innerHTML = last > 1.0 ? "&nbsp;▲ " + (last / 0.35).toFixed(1) + "× over SLO" : "&nbsp;";
}
function tick() {
  let target = 0.34, jit = 0.015;
  if (mode === "rising" || mode === "high") { target = 2.94; jit = 0.12; }
  else if (mode === "recovering") { target = 0.33; jit = 0.02; }
  const last = data[data.length - 1];
  const rate = mode === "rising" ? 0.24 : mode === "recovering" ? 0.16 : 0.14;
  data.push(Math.max(0.05, last + (target - last) * rate + (Math.random() - 0.5) * jit));
  data.shift();
  drawSpark();
  raf = requestAnimationFrame(() => setTimeout(tick, 60));
}

// ── render helpers ─────────────────────────────────────────────────────────
function setRibbon(cls, phase, say, elapsed) {
  $("ribbon").className = "ribbon " + cls;
  $("phase").textContent = phase; $("say").textContent = say;
  if (elapsed) $("elapsed").textContent = elapsed;
}
function buildTape() {
  $("spine").innerHTML = R.tape.map((e, i) =>
    `<a class="ev ${e.cls}" data-i="${i}"><span class="tc tnum">${e.tc}</span><span class="lab">${e.lab}</span></a>`).join("");
}
function revealTape(upto) {
  R.tape.forEach((_, i) => { if (i <= upto) $("spine").querySelector(`[data-i="${i}"]`).classList.add("in"); });
}
function renderSignals() {
  const rows = R.signals.map((s) => {
    const spk = s.spark.map((v, i) => `${(i / (s.spark.length - 1) * 52).toFixed(0)},${v}`).join(" ");
    const col = s.hot ? "#E5484D" : "#8A8A92";
    return `<tr class="${s.hot ? "hot" : ""}"><td>${s.name}</td><td><span class="now tnum">${s.now}</span></td>` +
      `<td class="tnum">${s.base}</td><td class="tnum">${s.limit}</td>` +
      `<td class="spk"><svg viewBox="0 0 52 14" width="52" height="14"><polyline points="${spk}" fill="none" stroke="${col}" stroke-width="1.2"/></svg></td></tr>`;
  }).join("");
  $("sigwall").innerHTML = `<tr><th>signal</th><th>now</th><th>baseline</th><th>limit</th><th>5m</th></tr>` + rows;
}
function streamStep(i) {
  const s = R.steps[i];
  const el = document.createElement("div");
  el.className = "ln";
  el.innerHTML = `<span class="t">${s.t}</span><span class="tool">${s.tool}</span><span class="dur">${s.dur}</span>`;
  $("stream").appendChild(el);
  requestAnimationFrame(() => el.classList.add("in"));
}
function after(ms, fn) { timers.push(setTimeout(fn, ms)); }

// ── recorded incident, on a loop ────────────────────────────────────────────
function playReplay() {
  timers.forEach(clearTimeout); timers = []; if (raf) cancelAnimationFrame(raf);
  data = Array.from({ length: N }, () => 0.34 + Math.random() * 0.015); mode = "nominal";
  $("nominal").classList.add("in"); $("incident").style.display = "none";
  $("stream").innerHTML = ""; ["verdict", "decision", "resolved"].forEach((id) => $(id).classList.remove("in"));
  $("sev").textContent = "SEV1"; $("sev").className = "sev nom"; $("sev").textContent = "NOMINAL";
  $("incid").textContent = "6 services · us-east-1"; $("viol").className = "viol ok"; $("viol").textContent = "all SLOs met";
  $("mmeta").innerHTML = "on-call <b>payments-oncall</b>";
  buildTape();
  setRibbon("", "All nominal", "watching 6 services · anomaly scan every 5s", "uptime 4d 06h");
  tick();

  after(2200, () => { mode = "rising"; });
  after(3400, () => {
    mode = "high";
    $("nominal").classList.remove("in"); $("incident").style.display = "block";
    $("sev").className = "sev"; $("sev").textContent = "SEV1";
    $("incid").textContent = "INC-4471 · payment-service · prod";
    $("viol").className = "viol"; $("viol").innerHTML = "SLO p95 &lt; 1.0s — VIOLATED";
    $("mmeta").innerHTML = "owner <b>payments-oncall</b> · ack <b>K. Rao 14:01:40Z</b>";
    setRibbon("alert", "Incident detected", "payment-service p95 2.9s vs 0.35s — robust z-score 8.4", "elapsed 00:04");
    renderSignals(); revealTape(1);
  });
  after(4400, () => { setRibbon("work", "Investigating", "correlating the 14:00 deploy against the latency break…", "elapsed 00:12"); revealTape(2); streamStep(0); });
  after(5200, () => { streamStep(1); revealTape(3); });
  after(6000, () => { streamStep(2); revealTape(4); });
  after(6800, () => { streamStep(3); revealTape(5); });
  after(7600, () => { streamStep(4); revealTape(6); });
  after(9000, () => {
    setRibbon("work", "Root cause found", "connection pool exhausted after deploy v412", "elapsed 00:24");
    $("rc").textContent = R.verdict.rc; $("rcwhy").innerHTML = R.verdict.why; $("rcdx").innerHTML = R.verdict.dx;
    $("verdict").classList.add("in"); revealTape(7);
  });
  after(10600, () => {
    setRibbon("wait", "Awaiting your approval", "won’t touch prod without you — review the fix below", "elapsed 00:26");
    $("dact").textContent = R.decision.act; $("dvoice").textContent = R.decision.voice;
    $("ddiff").innerHTML = R.decision.diff; $("dimpact").innerHTML = R.decision.impact;
    $("decision").classList.add("in"); revealTape(8);
  });
  after(13600, () => {
    $("typed").innerHTML = "armed · <b>payment-service</b> ✓"; $("approve").disabled = false;
    $("approve").textContent = "✓ approved · applying"; $("approve").style.opacity = "1";
    setRibbon("work", "Applying fix", "rolling back v412 → v411 · monitoring recovery", "elapsed 00:29");
    mode = "recovering";
  });
  after(16200, () => {
    setRibbon("done", "Resolved", "self-verified — p95 back under SLO", "MTTR 142s");
    $("restx").innerHTML = R.resolved.tx; $("resolved").classList.add("in");
    $("viol").className = "viol ok"; $("viol").innerHTML = "SLO recovered";
  });
  after(20500, playReplay);
}

// ── live mode ────────────────────────────────────────────────────────────────
const PHASE = {
  opened: ["alert", "Incident opened"], diagnosing: ["work", "Investigating"],
  proposed: ["work", "Remediation proposed"], authorized: ["work", "Authorized"],
  awaiting_approval: ["wait", "Awaiting your approval"], approval_received: ["work", "Approved"],
  acted: ["work", "Applying fix"], verified: ["done", "Verified"], resolved: ["done", "Resolved"],
  closed: ["alert", "Closed — unresolved"],
};
async function runLive(api) {
  $("modenote").textContent = "live · " + api;
  tick();
  async function poll() {
    try {
      const list = await (await fetch(`${api}/incidents`)).json();
      if (list.length) {
        const inc = list[list.length - 1];
        const st = (await (await fetch(`${api}/incidents/${inc.id}`)).json()).status || {};
        const [cls, phase] = PHASE[st.phase] || ["work", (st.phase || "…")];
        mode = ["resolved", "verified"].includes(st.phase) ? "recovering" : (st.phase === "opened" ? "high" : mode);
        $("nominal").classList.remove("in"); $("incident").style.display = "block";
        $("incid").textContent = `${inc.id} · ${inc.service}`;
        setRibbon(cls, phase, `${inc.metric}`, "");
      }
    } catch (e) { setRibbon("alert", "API unreachable", String(e), ""); }
    setTimeout(poll, 2000);
  }
  poll();
}

// ── boot ──────────────────────────────────────────────────────────────────────
const api = new URLSearchParams(location.search).get("api");
fetch("replay.json").then((r) => r.json()).then((data) => {
  R = data; buildTape();
  if (api) runLive(api); else playReplay();
});
$("replay").onclick = () => { if (!api) playReplay(); };
$("approve").onclick = () => {};
