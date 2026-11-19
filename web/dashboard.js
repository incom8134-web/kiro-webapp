"use strict";
/*
 * KIRO live dashboard. Read-only view for a big screen: it never drives.
 * Every number comes from the robot server. Small charts keep the last
 * 2 minutes, recorded in this browser since the page was opened.
 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
const ACCENT = css("--accent", "#34d07f");
const C = { ink: css("--ink", "#eef0f2"), line: css("--line-2", "#343840"), mute: css("--mute", "#858a93"), panel: css("--panel", "#111316"), stop: css("--stop", "#ff3b30") };
const HISTORY_S = 120;
const PX_PER_M = 70;

const st = { ws: null, open: false, server: null, robot: null, scan: null, rtt: null, cam: null };
const hist = {};      // key -> [{t, v}]
const trail = [];

// ------------------------------------------------------------ connection
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?name=Dashboard`);
  st.ws = ws;
  ws.onopen = () => { st.open = true; };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === "state") { st.server = m; onState(); }
    else if (m.t === "robot") { st.robot = m; onRobot(); }
    else if (m.t === "scan") { m.at = performance.now(); st.scan = m; }
    else if (m.t === "pong") st.rtt = performance.now() - m.ts;
  };
  ws.onclose = () => { st.open = false; st.server = null; st.rtt = null; render(); setTimeout(connect, 1000); };
  ws.onerror = () => ws.close();
}
const send = (o) => { if (st.open && st.ws.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify(o)); };
setInterval(() => send({ t: "ping", ts: performance.now() }), 1000);

$("estop").addEventListener("click", () => {
  // Anyone may stop the robot. Only the operator (on the control page) may release it.
  if (!(st.server && st.server.estop)) send({ t: "estop", on: true });
});
window.addEventListener("keydown", (e) => { if (e.code === "Escape") send({ t: "estop", on: true }); });
for (const b of document.querySelectorAll(".lang button")) b.addEventListener("click", () => { setLang(b.dataset.lang); buildTiles(); render(); });

// --------------------------------------------------------------- history
function record(key, v) {
  if (v == null || !Number.isFinite(v)) return;
  const now = Date.now() / 1000;
  const h = hist[key] || (hist[key] = []);
  h.push({ t: now, v });
  while (h.length && h[0].t < now - HISTORY_S) h.shift();
}

let lastSpeedSample = 0;
function onState() {
  const s = st.server;
  const now = Date.now();
  if (now - lastSpeedSample > 500) {
    lastSpeedSample = now;
    const o = s.odom;
    record("speed", o ? Math.hypot(o.vx, o.vy) : Math.hypot(s.cmd.vx, s.cmd.vy));
    const n = nearest();
    record("nearest", n ? n.d : null);
  }
  render();
}

function onRobot() {
  const j = (st.robot.health || {}).jetson || {};
  record("temp", j.temp);
  record("cpu", j.load != null && j.cores ? (100 * j.load) / j.cores : null);
  record("mem", j.mem);
  record("current", servoCurrent());
  renderRobot();
}

// ------------------------------------------------------------- numbers
function nearest() {
  const sc = st.scan && performance.now() - st.scan.at < 1000 ? st.scan : null;
  if (!sc || !sc.xy.length) return null;
  let best = null;
  for (let i = 0; i < sc.xy.length; i += 2) {
    const x = sc.xy[i] / 100, y = sc.xy[i + 1] / 100, d = Math.hypot(x, y);
    if (d > 0.05 && (!best || d < best.d)) best = { d, a: Math.atan2(y, x) };
  }
  return best;
}

function direction(a) {
  const deg = (a * 180) / Math.PI;
  return Math.abs(deg) <= 45 ? "dir.front" : Math.abs(deg) >= 135 ? "dir.back" : deg > 0 ? "dir.left" : "dir.right";
}

function servoCurrent() {
  const b = (st.robot && st.robot.health && st.robot.health.boards) || {};
  let sum = 0, any = false;
  for (const x of Object.values(b)) {
    if (!x || x.state !== "ok") continue;
    for (const s of Object.values(x.servos || {})) if (s.ma != null) { sum += s.ma; any = true; }
  }
  return any ? sum / 1000 : null;
}

function fmtUp(s) {
  if (s == null) return "-";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : m ? `${m} min` : `${s} s`;
}

// --------------------------------------------------------------- tiles
// Each tile: one real value, its unit, and a 2-minute line of the same value.
const TILES = [
  { key: "speed", label: "d.speed", unit: "m/s", fmt: (v) => v.toFixed(2) },
  { key: "dist", label: "d.dist", unit: "m", fmt: (v) => v.toFixed(1), noSpark: true },
  { key: "nearest", label: "d.nearest", unit: "m", fmt: (v) => v.toFixed(2) },
  { key: "temp", label: "d.temp", unit: "°C", fmt: (v) => v.toFixed(1) },
  { key: "cpu", label: "d.cpu", unit: "%", fmt: (v) => Math.round(v) },
  { key: "mem", label: "d.mem", unit: "%", fmt: (v) => Math.round(v) },
  { key: "current", label: "d.current", unit: "A", fmt: (v) => v.toFixed(2) },
];

function buildTiles() {
  $("tiles").innerHTML = TILES.map((tl) => `<div class="tile" data-key="${tl.key}">
    <span class="k">${esc(t(tl.label))}</span>
    <div class="num"><b id="v-${tl.key}">-</b><i>${tl.unit}</i></div>
    <span class="sub mute" id="s-${tl.key}"></span>
    ${tl.noSpark ? "" : `<svg class="spark" id="g-${tl.key}" preserveAspectRatio="none"></svg><span class="tip" id="tip-${tl.key}"></span>`}
  </div>`).join("");
  for (const tl of TILES) if (!tl.noSpark) hoverSpark(tl);
}

function current(key) {
  const h = hist[key];
  return h && h.length ? h[h.length - 1].v : null;
}

function renderTiles() {
  const s = st.server, r = st.robot;
  const vals = {
    speed: current("speed"), dist: s ? s.odo_m : null, nearest: current("nearest"),
    temp: current("temp"), cpu: current("cpu"), mem: current("mem"), current: current("current"),
  };
  const subs = {
    dist: t("d.since"),
    nearest: (() => { const n = nearest(); return n ? t(direction(n.a)) : t("d.nolidar"); })(),
    current: r && r.health && Object.values(r.health.boards || {}).some((x) => x && x.state === "ok") ? t("d.allservos") : t("d.noservos"),
    cpu: r && r.health && r.health.jetson && r.health.jetson.cores ? `${r.health.jetson.cores} ${t("d.cores")}` : "",
  };
  for (const tl of TILES) {
    const v = vals[tl.key];
    $("v-" + tl.key).textContent = v == null ? "-" : tl.fmt(v);
    $("s-" + tl.key).textContent = subs[tl.key] || (tl.noSpark ? "" : t("d.last2min"));
    if (!tl.noSpark) drawSpark(tl.key);
  }
}

function sparkPoints(key, w, h) {
  const pts = hist[key] || [];
  if (pts.length < 2) return null;
  const now = Date.now() / 1000;
  let lo = Math.min(...pts.map((p) => p.v)), hi = Math.max(...pts.map((p) => p.v));
  if (key === "speed" || key === "current" || key === "cpu" || key === "mem") lo = 0;  // magnitudes start at zero
  if (hi - lo < 1e-6) hi = lo + 1;
  const pad = 3;
  return pts.map((p) => ({ x: ((p.t - (now - HISTORY_S)) / HISTORY_S) * w, y: pad + (1 - (p.v - lo) / (hi - lo)) * (h - 2 * pad), p }));
}

function drawSpark(key) {
  const svg = $("g-" + key);
  if (!svg) return;
  const w = svg.clientWidth, h = svg.clientHeight;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const pts = sparkPoints(key, w, h);
  if (!pts) { svg.innerHTML = ""; return; }
  const d = pts.map((q, i) => `${i ? "L" : "M"}${q.x.toFixed(1)},${q.y.toFixed(1)}`).join("");
  const hov = svg.dataset.hover ? Number(svg.dataset.hover) : null;
  let cross = "";
  if (hov != null) {
    const q = pts.reduce((a, b) => (Math.abs(b.x - hov) < Math.abs(a.x - hov) ? b : a));
    cross = `<line x1="${q.x}" x2="${q.x}" y1="0" y2="${h}" class="cross"/><circle cx="${q.x}" cy="${q.y}" r="4" fill="${ACCENT}" stroke="#fff" stroke-width="2"/>`;
    const tl = TILES.find((x) => x.key === key);
    const ago = Math.max(0, Math.round(Date.now() / 1000 - q.p.t));
    $("tip-" + key).textContent = `${tl.fmt(q.p.v)} ${tl.unit} · ${t("d.ago", { s: ago })}`;
  } else {
    $("tip-" + key).textContent = "";
  }
  svg.innerHTML = `<line x1="0" x2="${w}" y1="${h - 0.5}" y2="${h - 0.5}" class="base"/>` +
    `<path d="${d}" fill="none" stroke="${ACCENT}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>` + cross;
}

function hoverSpark(tl) {
  const svg = $("g-" + tl.key);
  const tile = svg.closest(".tile");
  tile.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    svg.dataset.hover = String(Math.max(0, Math.min(r.width, e.clientX - r.left)));
    drawSpark(tl.key);
  });
  tile.addEventListener("pointerleave", () => { delete svg.dataset.hover; drawSpark(tl.key); });
}

// --------------------------------------------------------------- render
function render() {
  const s = st.server;
  applyStrings();
  for (const b of document.querySelectorAll(".lang button")) b.classList.toggle("on", b.dataset.lang === LANG);
  $("backend").textContent = s ? t(s.backend === "ros" ? "be.ros" : "be.mock") : "...";
  $("link").textContent = !st.open ? t("link.offline") : st.rtt == null ? t("link.connecting") : `${Math.round(st.rtt)} ms`;
  $("link").className = st.rtt != null && st.rtt < 150 ? "accent" : "";
  $("opstat").textContent = !s || !s.operator ? t("op.none") : s.operator.name;
  $("uptime").textContent = s ? fmtUp(s.up_s) : "-";
  $("devices").textContent = s ? s.clients : "-";
  const es = $("estop"), stopped = !!s && s.estop;
  es.classList.toggle("on", stopped);
  es.textContent = stopped ? t("estop.stopped") : t("estop");
  const banner = $("banner");
  if (!st.open) { banner.hidden = false; banner.className = "banner"; banner.textContent = t("banner.offline"); }
  else if (stopped) {
    banner.hidden = false; banner.className = "banner stop";
    banner.textContent = t("banner.estop", { by: s.estop_by ? t("banner.estop.by", { name: s.estop_by }) : "" }) + t("banner.estop.viewer");
  } else banner.hidden = true;
  $("ver").textContent = s ? `KIRO Command Center v${s.version}` : "";
  renderTiles();
  drawLidar();
}

function renderRobot() {
  const r = st.robot;
  if (!r) return;
  // Servo boards
  const names = { right_follower: ["arm.right", "arm.follower"], left_follower: ["arm.left", "arm.follower"],
    right_leader: ["arm.right", "arm.leader"], left_leader: ["arm.left", "arm.leader"], base: ["sec.base", null] };
  const b = (r.health && r.health.boards) || {};
  const rows = [`<tr><th>${t("d.board")}</th><th>${t("d.volt")}</th><th>${t("d.temp2")}</th><th>${t("d.ma")}</th></tr>`];
  if (r.mock) rows.push(`<tr><td colspan="4" class="mute">${t("d.mockservos")}</td></tr>`);
  for (const [k, [a, c]] of Object.entries(names)) {
    const x = b[k];
    if (!x) continue;
    const label = t(a) + (c ? " " + t(c) : "");
    if (x.state !== "ok") {
      const why = x.state === "missing" ? t("arm.missing") : x.state === "busy" ? t("h.inuse", { job: x.by }) : x.state === "silent" ? t("h.noread") : (x.msg || x.state);
      rows.push(`<tr><td>${esc(label)}</td><td colspan="3" class="mute">${esc(why)}</td></tr>`);
      continue;
    }
    const ma = Object.values(x.servos || {}).reduce((s, v) => s + (v.ma || 0), 0);
    rows.push(`<tr><td>${esc(label)} <span class="mute">${x.n}/${x.of}</span></td><td><b>${x.vmin ?? "-"}</b> V</td><td>${x.tmax ?? "-"} °C</td><td>${Math.round(ma)} mA${x.alarm ? " · ALARM" : ""}</td></tr>`);
  }
  $("servos").innerHTML = rows.join("");

  // Programs
  const J = r.jobs || {};
  const progs = [["base", "sec.base"], ["teleop_right", "d.tele_r"], ["teleop_left", "d.tele_l"], ["teleop_both", "d.tele_b"],
    ["arm_right", "d.pose_r"], ["arm_left", "d.pose_l"], ["skill", "sec.skills"], ["voice", "sec.voice"]];
  $("programs").innerHTML = progs.map(([k, label]) => {
    const j = J[k];
    const stTxt = !j ? `<span class="mute">${t("d.never")}</span>` : j.running
      ? `<b class="accent">${t("d.running")}</b> <span class="mute">${fmtUp(Math.round(Date.now() / 1000 - j.started))}</span>`
      : `${t("d.stopped")}${j.code ? ` <span class="mute">(${j.code})</span>` : ""}`;
    return `<tr><th>${esc(j ? j.label : t(label))}</th><td>${stTxt}</td></tr>`;
  }).join("");

  $("voice").textContent = (r.voice && r.voice.text) || "-";

  const ev = (r.events || []).slice().reverse();
  $("log").innerHTML = ev.length ? ev.map((e) => `<li><time>${new Date(e.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time><span>${esc(e.who)}: ${esc(e.text)}</span></li>`).join("")
    : `<li class="mute">${t("log.empty")}</li>`;

  renderCam();
  renderWrists();
}

// ---------------------------------------------------------------- camera
function renderCam() {
  const cams = (st.robot && st.robot.cams) || [];
  const cam = cams.find((c) => c.present && c.key === "top") || cams.find((c) => c.present);
  const img = $("cam");
  $("cam-name").textContent = cam ? cam.key : "";
  if (!cam || cam.paused_by) {
    if (st.cam) { img.removeAttribute("src"); st.cam = null; }
    img.hidden = true; $("cam-empty").hidden = false;
    $("cam-msg").textContent = !cam ? t("cam.none") : t("cam.paused", { job: cam.paused_by });
    return;
  }
  if (st.cam !== cam.key) { st.cam = cam.key; img.src = `/camera/${encodeURIComponent(cam.key)}.mjpg?t=${Date.now()}`; }
  img.hidden = false; $("cam-empty").hidden = true;
}
$("cam").addEventListener("error", () => { st.cam = null; $("cam").hidden = true; setTimeout(renderCam, 2000); });

/* The two arm (hand) cameras under the main camera. */
const wristOn = {};
function renderWrists() {
  const cams = (st.robot && st.robot.cams) || [];
  for (const view of document.querySelectorAll(".dwrists .view[data-cam]")) {
    const key = view.dataset.cam, c = cams.find((x) => x.key === key);
    const img = view.querySelector("img"), empty = view.querySelector(".empty"), msg = view.querySelector(".msg");
    const why = !c || !c.present ? t("cam.notconnected") : c.paused_by ? t("cam.inuse") : c.error ? t("cam.busy") : "";
    if (why) {
      if (wristOn[key]) { img.removeAttribute("src"); wristOn[key] = false; }
      img.hidden = true; empty.hidden = false; msg.textContent = why;
      continue;
    }
    if (!wristOn[key]) {
      wristOn[key] = true;
      img.onerror = () => { wristOn[key] = false; img.hidden = true; empty.hidden = false; setTimeout(renderWrists, 2000); };
      img.src = `/camera/${encodeURIComponent(key)}.mjpg?t=${Date.now()}`;
    }
    img.hidden = false; empty.hidden = true;
  }
}

// ----------------------------------------------------------------- lidar
function drawLidar() {
  const c = $("lidar");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (!w || !h) return;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const s = st.server, o = (s && s.odom) || { x: 0, y: 0, th: 0 }, S = PX_PER_M;
  const sx = (wy) => w / 2 - (wy - o.y) * S, sy = (wx) => h / 2 - (wx - o.x) * S;

  // range rings every 1 m around the robot
  g.strokeStyle = C.line; g.lineWidth = 1;
  for (let r = 1; r * S < Math.max(w, h); r++) { g.beginPath(); g.arc(w / 2, h / 2, r * S, 0, 2 * Math.PI); g.stroke(); }
  g.fillStyle = C.mute; g.font = "11px " + getComputedStyle(document.body).fontFamily;
  for (let r = 1; r <= 3; r++) g.fillText(`${r} m`, w / 2 + 4, h / 2 - r * S - 4);

  // path driven
  const last = trail[trail.length - 1];
  if (last && Math.hypot(o.x - last.x, o.y - last.y) > 1.5) trail.length = 0;
  if (!last || Math.hypot(o.x - last.x, o.y - last.y) > 0.02) { trail.push({ x: o.x, y: o.y }); if (trail.length > 2000) trail.shift(); }
  if (trail.length > 1) {
    g.strokeStyle = ACCENT; g.lineWidth = 2; g.beginPath();
    trail.forEach((p, i) => (i ? g.lineTo(sx(p.y), sy(p.x)) : g.moveTo(sx(p.y), sy(p.x))));
    g.stroke();
  }

  g.save(); g.translate(w / 2, h / 2); g.rotate(-o.th);
  const sc = st.scan && performance.now() - st.scan.at < 1000 ? st.scan : null;
  $("scan-src").textContent = sc ? (sc.src === "mock" ? t("scan.mock") : t("scan.live")) : t("d.nolidar");
  if (sc) {
    const xy = sc.xy;
    g.fillStyle = C.ink;
    for (let i = 0; i < xy.length; i += 2) g.fillRect(-xy[i + 1] / 100 * S - 1.5, -xy[i] / 100 * S - 1.5, 3, 3);
    const n = nearest();
    if (n) {
      const px = -Math.sin(n.a) * n.d * S, py = -Math.cos(n.a) * n.d * S;
      g.strokeStyle = ACCENT; g.lineWidth = 1; g.setLineDash([4, 4]);
      g.beginPath(); g.moveTo(0, 0); g.lineTo(px, py); g.stroke(); g.setLineDash([]);
    }
  }
  const L = 0.45 * S, W = 0.35 * S;
  g.fillStyle = C.panel; g.strokeStyle = s && s.estop ? C.stop : ACCENT; g.lineWidth = 2;
  g.fillRect(-W / 2, -L / 2, W, L); g.strokeRect(-W / 2, -L / 2, W, L);
  g.fillStyle = g.strokeStyle; g.beginPath(); g.moveTo(0, -L / 2 + 4); g.lineTo(-7, -L / 2 + 15); g.lineTo(7, -L / 2 + 15); g.closePath(); g.fill();
  g.restore();
}
window.addEventListener("resize", () => { drawLidar(); for (const tl of TILES) if (!tl.noSpark) drawSpark(tl.key); });

buildTiles();
render();
connect();
