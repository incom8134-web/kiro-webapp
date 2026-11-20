"use strict";
/*
 * HAEBOT Robot live dashboard. Read-only view for a big screen: it never drives (E-STOP still works).
 * Every number comes from the robot server. Line charts keep the last 5 minutes, recorded in
 * this browser since the page was opened. A chart with no data says so; nothing is invented.
 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
const WINDOW_S = 300;

const st = { ws: null, open: false, server: null, robot: null, scan: null, rtt: null, cam: null };
const hist = {};      // key -> [{x: epoch seconds, y}]
const trail = [];
let theme = {};

function readTheme() {
  theme = {
    ink: css("--ink", "#e8eef8"), ink2: css("--ink-2", "#b7c3d7"), mute: css("--mute", "#7c8aa5"),
    line: css("--line", "#1f2b47"), line2: css("--line-2", "#2c3b5c"), panel: css("--panel", "#0f172a"),
    raise: css("--raise", "#141e35"), accent: css("--accent", "#38bdf8"), stop: css("--stop", "#ff3b30"),
    font: css("--font", "system-ui"),
    series: css("--series", "#3987e5,#d95926,#199e70").split(",").map((x) => x.trim()),
  };
}
readTheme();

// ------------------------------------------------------------ connection
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?name=Dashboard`);
  st.ws = ws;
  ws.onopen = () => { st.open = true; send({ t: "logs" }); termLive(); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === "state") { st.server = m; onState(); }
    else if (m.t === "robot") { st.robot = m; onRobot(); }
    else if (m.t === "scan") { m.at = performance.now(); st.scan = m; }
    else if (m.t === "pong") st.rtt = performance.now() - m.ts;
    else if (m.t === "log") onLog(m);
  };
  ws.onclose = () => { st.open = false; st.server = null; st.rtt = null; render(); termLive(); renderCheck(); setTimeout(connect, 1000); };
  ws.onerror = () => ws.close();
}
const send = (o) => { if (st.open && st.ws.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify(o)); };
setInterval(() => send({ t: "ping", ts: performance.now() }), 1000);

$("estop").addEventListener("click", () => {   // anyone may stop the robot; only the operator releases it
  if (!(st.server && st.server.estop)) send({ t: "estop", on: true });
});
window.addEventListener("keydown", (e) => { if (e.code === "Escape") send({ t: "estop", on: true }); });
for (const b of document.querySelectorAll(".lang button")) b.addEventListener("click", () => { setLang(b.dataset.lang); buildTiles(); buildCharts(); render(); onRobot(); termLive(); });
$("theme-btn").addEventListener("click", () => toggleTheme());
window.addEventListener("kiro-theme", () => { readTheme(); themeIcon($("theme-btn")); buildCharts(); drawLidar(); });
themeIcon($("theme-btn"));

// --------------------------------------------------------------- history
const now = () => Date.now() / 1000;
function record(key, v) {
  if (v == null || !Number.isFinite(v)) return;
  const h = hist[key] || (hist[key] = []);
  h.push({ x: now(), y: v });
  while (h.length && h[0].x < now() - WINDOW_S) h.shift();
}
const last = (key) => { const h = hist[key]; return h && h.length ? h[h.length - 1].y : null; };

let lastSpeedSample = 0;
function onState() {
  const s = st.server;
  if (Date.now() - lastSpeedSample > 500) {
    lastSpeedSample = Date.now();
    const o = s.odom;
    record("speed", o ? Math.hypot(o.vx, o.vy) : Math.hypot(s.cmd.vx, s.cmd.vy));
    const n = nearest();
    record("nearest", n ? n.d : null);
  }
  render();
}

function onRobot() {
  const r = st.robot;
  if (!r) return;
  const j = (r.health || {}).jetson || {};
  record("cpu", j.cpu);
  record("gpu", j.gpu);
  record("mem", j.mem);
  const tz = j.temps || {};
  record("t_cpu", tz.cpu);
  record("t_gpu", tz.gpu);
  const soc = Object.entries(tz).filter(([k]) => k.startsWith("soc")).map(([, v]) => v);
  record("t_soc", soc.length ? soc.reduce((a, b) => a + b, 0) / soc.length : null);
  const pw = j.power || {};
  record("p_in", pw.VDD_IN);
  record("p_cg", pw.VDD_CPU_GPU_CV);
  record("p_soc", pw.VDD_SOC);
  record("rx", j.net ? j.net.rx : null);
  record("tx", j.net ? j.net.tx : null);
  record("rtt", st.rtt);
  renderTiles();
  updateCharts();
  renderLists();
  renderCams();
  renderCheck();
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

/* Nearest lidar point on each side of the robot (front, left, right, back), metres. */
function sides() {
  const sc = st.scan && performance.now() - st.scan.at < 1000 ? st.scan : null;
  if (!sc || !sc.xy.length) return null;
  const out = { front: null, left: null, right: null, back: null };
  for (let i = 0; i < sc.xy.length; i += 2) {
    const x = sc.xy[i] / 100, y = sc.xy[i + 1] / 100, d = Math.hypot(x, y);
    if (d < 0.05) continue;
    const deg = Math.atan2(y, x) * 180 / Math.PI;
    const k = Math.abs(deg) <= 45 ? "front" : Math.abs(deg) >= 135 ? "back" : deg > 0 ? "left" : "right";
    if (out[k] == null || d < out[k]) out[k] = d;
  }
  return out;
}

function direction(a) {
  const deg = (a * 180) / Math.PI;
  return Math.abs(deg) <= 45 ? "dir.front" : Math.abs(deg) >= 135 ? "dir.back" : deg > 0 ? "dir.left" : "dir.right";
}

function fmtUp(s) {
  if (s == null) return "-";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : m ? `${m} min` : `${s} s`;
}

// --------------------------------------------------------------- tiles
const TILES = [
  { key: "speed", label: "d.speed", unit: "m/s", fmt: (v) => v.toFixed(2) },
  { key: "dist", label: "d.dist", unit: "m", fmt: (v) => v.toFixed(1) },
  { key: "nearest", label: "d.nearest", unit: "m", fmt: (v) => v.toFixed(2) },
  { key: "p_in", label: "d.power", unit: "W", fmt: (v) => v.toFixed(1) },
  { key: "cpu", label: "d.cpu", unit: "%", fmt: (v) => Math.round(v) },
  { key: "gpu", label: "d.gpu", unit: "%", fmt: (v) => Math.round(v) },
  { key: "t_cpu", label: "d.temp", unit: "°C", fmt: (v) => v.toFixed(1) },
  { key: "mem", label: "d.mem", unit: "%", fmt: (v) => Math.round(v) },
  { key: "net", label: "d.net", unit: "KB/s", fmt: (v) => v.toFixed(1) },
  { key: "rtt", label: "d.link", unit: "ms", fmt: (v) => Math.round(v) },
];

function buildTiles() {
  $("tiles").innerHTML = TILES.map((tl) => `<div class="tile">
    <span class="k">${esc(t(tl.label))}</span>
    <div class="num"><b id="v-${tl.key}">-</b><i>${tl.unit}</i></div>
    <span class="sub" id="s-${tl.key}"></span></div>`).join("");
}

function renderTiles() {
  const s = st.server, r = st.robot, j = (r && r.health && r.health.jetson) || {};
  const n = nearest();
  const vals = {
    speed: last("speed"), dist: s ? s.odo_m : null, nearest: n ? n.d : null, p_in: last("p_in"), cpu: last("cpu"),
    gpu: last("gpu"), t_cpu: last("t_cpu"), mem: last("mem"),
    net: j.net ? j.net.rx + j.net.tx : null, rtt: st.rtt,
  };
  const subs = {
    dist: t("d.since"), nearest: n ? t(direction(n.a)) : t("d.nolidar"),
    p_in: j.power ? `CPU+GPU ${j.power.VDD_CPU_GPU_CV} W · SoC ${j.power.VDD_SOC} W` : "",
    cpu: j.cores ? `${j.cores} ${t("d.cores")}` : "", gpu: "Orin Nano",
    t_cpu: j.temps && j.temps.gpu != null ? `GPU ${j.temps.gpu}°C` : "",
    mem: j.mem_mb ? `${(j.mem_mb[0] / 1024).toFixed(1)} / ${(j.mem_mb[1] / 1024).toFixed(1)} GB` : "",
    net: j.net ? `↓ ${j.net.rx} · ↑ ${j.net.tx}` : "", speed: t("d.last5"), rtt: t("d.thisdevice"),
  };
  for (const tl of TILES) {
    const v = vals[tl.key];
    const el = $("v-" + tl.key);
    if (!el) continue;
    el.textContent = v == null ? "-" : tl.fmt(v);
    $("s-" + tl.key).textContent = subs[tl.key] || "";
  }
}

// --------------------------------------------------------------- charts
const charts = {};
const timeTick = (v) => new Date(v * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function baseOptions(unit, opts = {}) {
  const T = theme;
  const tooltip = {
    backgroundColor: T.panel, titleColor: T.ink, bodyColor: T.ink2, borderColor: T.line2, borderWidth: 1,
    padding: 10, boxPadding: 4, usePointStyle: true,
    callbacks: { label: (c) => ` ${c.dataset.label ? c.dataset.label + ": " : ""}${c.parsed.y != null && !opts.horizontal ? c.parsed.y : c.parsed.x} ${unit}`,
                 title: (items) => (opts.time && items[0] ? timeTick(items[0].parsed.x) : items[0] ? items[0].label : "") },
  };
  const grid = { color: T.line, drawTicks: false };
  const ticks = { color: T.mute, padding: 6, font: { size: 11 } };
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: opts.time ? { mode: "index", intersect: false } : { mode: "nearest", intersect: true },
    plugins: {
      legend: { display: !!opts.legend, position: "top", align: "end", labels: { color: T.ink2, boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "rectRounded", font: { size: 11 } } },
      tooltip,
    },
    indexAxis: opts.horizontal ? "y" : "x",
    scales: {
      x: opts.time
        ? { type: "linear", min: now() - WINDOW_S, max: now(), grid, border: { display: false }, ticks: { ...ticks, maxTicksLimit: 5, callback: timeTick } }
        : { grid: { ...grid, display: !!opts.horizontal }, border: { display: false }, ticks, beginAtZero: true, suggestedMax: opts.max },
      y: opts.horizontal
        ? { grid: { display: false }, border: { display: false }, ticks: { ...ticks, color: T.ink2 } }
        : { grid, border: { display: false }, ticks, beginAtZero: true, suggestedMax: opts.max },
    },
  };
}

function lineChart(id, series, unit, opts = {}) {
  const ds = series.map((s, i) => ({
    label: t(s.label), key: s.key, data: [], borderColor: theme.series[i], backgroundColor: theme.series[i] + "22",
    borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25, fill: series.length === 1,
  }));
  return new Chart($(id), { type: "line", data: { datasets: ds }, options: baseOptions(unit, { time: true, legend: series.length > 1, ...opts }) });
}

function barChart(id, unit, opts = {}) {
  return new Chart($(id), {
    type: "bar",
    data: { labels: [], datasets: [{ data: [], backgroundColor: theme.series[opts.slot || 0], borderRadius: 5, borderSkipped: "start", maxBarThickness: 34, categoryPercentage: 0.7, barPercentage: 0.9 }] },
    options: baseOptions(unit, opts),
  });
}

function buildCharts() {
  for (const c of Object.values(charts)) c.destroy();
  if (typeof Chart === "undefined") return;
  Chart.defaults.font.family = theme.font;
  Chart.defaults.color = theme.mute;
  charts.cpugpu = lineChart("ch-cpugpu", [{ key: "cpu", label: "s.cpu" }, { key: "gpu", label: "s.gpu" }], "%", { max: 100 });
  charts.cores = barChart("ch-cores", "%", { max: 100 });
  charts.temps = lineChart("ch-temps", [{ key: "t_cpu", label: "s.cpu" }, { key: "t_gpu", label: "s.gpu" }, { key: "t_soc", label: "s.soc" }], "°C");
  charts.power = lineChart("ch-power", [{ key: "p_in", label: "s.total" }, { key: "p_cg", label: "s.cpugpu" }, { key: "p_soc", label: "s.soc" }], "W");
  charts.speed = lineChart("ch-speed", [{ key: "speed", label: "c.speed" }], "m/s", { max: 0.3 });
  charts.net = lineChart("ch-net", [{ key: "rx", label: "s.rx" }, { key: "tx", label: "s.tx" }], "KB/s");
  charts.sides = barChart("ch-sides", "m", { horizontal: true, slot: 2 });
  charts.svolt = barChart("ch-svolt", "V", { slot: 0 });
  charts.stemp = barChart("ch-stemp", "°C", { slot: 1 });
  updateCharts();
}

function empty(chart, text) {   // show "no data" over a chart instead of drawing anything made up
  const box = chart.canvas.parentElement, e = box.querySelector(".empty");
  e.hidden = !text; e.textContent = text || "";
  chart.canvas.style.visibility = text ? "hidden" : "visible";
}

function updateCharts() {
  if (!charts.cpugpu) return;
  const r = st.robot, j = (r && r.health && r.health.jetson) || {};
  for (const name of ["cpugpu", "temps", "power", "speed", "net"]) {
    const c = charts[name];
    let any = false;
    for (const d of c.data.datasets) { d.data = (hist[d.key] || []).slice(); any = any || d.data.length > 0; }
    // the time axis grows from the first reading up to 5 minutes, so short histories fill the chart
    const first = Math.min(...c.data.datasets.map((d) => (d.data[0] ? d.data[0].x : Infinity)));
    c.options.scales.x.min = Math.max(now() - WINDOW_S, Number.isFinite(first) ? first : now() - WINDOW_S);
    c.options.scales.x.max = now();
    empty(c, any ? "" : t("c.nodata"));
    c.update("none");
  }
  const cores = j.cpu_cores || [];
  charts.cores.data.labels = cores.map((_, i) => `${t("s.core")} ${i}`);
  charts.cores.data.datasets[0].data = cores;
  empty(charts.cores, cores.length ? "" : t("c.nodata"));
  charts.cores.update("none");

  const sd = sides();
  const order = ["front", "left", "right", "back"].filter((k) => sd && sd[k] != null);
  charts.sides.data.labels = order.map((k) => t("dir." + k));
  charts.sides.data.datasets[0].data = order.map((k) => +sd[k].toFixed(2));
  empty(charts.sides, order.length ? "" : t("d.nolidar"));
  charts.sides.update("none");

  const boards = (r && r.health && r.health.boards) || {};
  const names = { right_follower: "arm.right", left_follower: "arm.left", right_leader: "arm.right", left_leader: "arm.left", base: "sec.base" };
  const ok = Object.entries(boards).filter(([, x]) => x && x.state === "ok");
  charts.svolt.data.labels = ok.map(([k]) => t(names[k] || k) + (k.includes("leader") ? ` ${t("arm.leader")}` : ""));
  charts.svolt.data.datasets[0].data = ok.map(([, x]) => x.vmin);
  empty(charts.svolt, ok.length ? "" : t("c.noservo"));
  charts.svolt.update("none");

  const joints = [];
  for (const [k, x] of ok) for (const [id, sv] of Object.entries(x.servos || {})) if (sv.t != null) joints.push([`${t(names[k] || k)[0]}${k.includes("leader") ? "L" : ""}${id}`, sv.t]);
  charts.stemp.data.labels = joints.map((x) => x[0]);
  charts.stemp.data.datasets[0].data = joints.map((x) => x[1]);
  empty(charts.stemp, joints.length ? "" : t("c.noservo"));
  charts.stemp.update("none");
}
setInterval(() => { if (st.robot) updateCharts(); }, 2000);   // keep the time axis moving

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
  const stopped = !!s && s.estop;
  $("estop").classList.toggle("on", stopped);
  $("estop-label").textContent = stopped ? t("estop.stopped") : t("estop");
  const banner = $("banner");
  if (!st.open) { banner.hidden = false; banner.className = "banner"; banner.textContent = t("banner.offline"); }
  else if (stopped) {
    banner.hidden = false; banner.className = "banner stop";
    banner.textContent = t("banner.estop", { by: s.estop_by ? t("banner.estop.by", { name: s.estop_by }) : "" }) + t("banner.estop.viewer");
  } else banner.hidden = true;
  $("ver").textContent = s ? `HAEBOT Robot v${s.version}` : "";
  renderTiles();
  drawLidar();
}

function renderLists() {
  const r = st.robot;
  if (!r) return;
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
      const why = x.state === "missing" ? t("arm.missing") : x.state === "busy" ? t("h.inuse", { job: x.by }) : x.state === "silent" ? t("arm.nopower") : (x.msg || x.state);
      rows.push(`<tr><td>${esc(label)}</td><td colspan="3" class="mute">${esc(why)}</td></tr>`);
      continue;
    }
    const ma = Object.values(x.servos || {}).reduce((s, v) => s + (v.ma || 0), 0);
    rows.push(`<tr><td>${esc(label)} <span class="mute">${x.n}/${x.of}</span></td><td><b>${x.vmin ?? "-"}</b> V</td><td>${x.tmax ?? "-"} °C</td><td>${Math.round(ma)} mA${x.alarm ? " · ALARM" : ""}</td></tr>`);
  }
  $("servos").innerHTML = rows.join("");

  const J = r.jobs || {};
  const progs = [["base", "sec.base"], ["teleop_right", "d.tele_r"], ["teleop_left", "d.tele_l"], ["arm_right", "d.pose_r"],
    ["arm_left", "d.pose_l"], ["bridge", "leader.title"], ["skill", "sec.skills"], ["voice", "sec.voice"], ["wizard", "eng.wizard"], ["record", "d.record"]];
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
}

// ---------------------------------------------------------------- cameras
const liveOpen = () => $("d-live").open;
let camsOff = false;   // the pause button: no camera streams, so no Wi-Fi used for video
$("cam-pause").addEventListener("click", () => { camsOff = !camsOff; renderCams(); });
/* The three camera boxes of the live view (main camera, left hand, right hand). */
function renderCams() {
  const cams = (st.robot && st.robot.cams) || [];
  const boxes = document.querySelectorAll(".quad .qbox[data-cam]");
  if (!liveOpen()) {   // closed: stop every stream, so the cameras use no Wi-Fi
    for (const box of boxes) if (box.dataset.on) { box.querySelector("img").removeAttribute("src"); delete box.dataset.on; }
    $("live-hint").textContent = t("d.livehint", { n: cams.filter((c) => c.present).length });
    return;
  }
  $("live-hint").textContent = "";
  const pb = $("cam-pause");
  pb.querySelector("i").className = camsOff ? "ri-play-fill" : "ri-pause-fill";
  pb.querySelector("span").textContent = t(camsOff ? "cam.resume" : "cam.pause");
  pb.title = t(camsOff ? "b.cams.play" : "b.cams.pause");
  pb.setAttribute("aria-pressed", String(camsOff));
  for (const box of boxes) {
    const key = box.dataset.cam, c = cams.find((x) => x.key === key);
    const im = box.querySelector("img"), em = box.querySelector(".empty"), msg = box.querySelector(".msg");
    box.querySelector(".qhead").title = c && c.auto ? `${t("cam.auto")} ${c.auto}` : "";
    const why = camsOff ? t("b.cams.off") : !c || !c.present ? t("cam.notconnected") : c.paused_by ? t("cam.inuse") : c.error ? t("cam.busy") : "";
    if (why) {
      if (box.dataset.on) { im.removeAttribute("src"); delete box.dataset.on; }
      im.hidden = true; em.hidden = false; msg.textContent = why;
      continue;
    }
    if (!box.dataset.on) {
      box.dataset.on = "1";
      im.onerror = () => { delete box.dataset.on; im.hidden = true; em.hidden = false; setTimeout(renderCams, 2000); };
      im.src = `/camera/${encodeURIComponent(key)}.mjpg?t=${Date.now()}`;
    }
    im.hidden = false; em.hidden = true;
  }
}

// ----------------------------------------------------------- system check
/* Every part of the robot on one line: green = fine, amber = needs attention (with what to do),
   grey = not connected and not needed right now. Built from what the robot server measures. */
const ARM_MIN_V = 4.5, WHEEL_MIN_V = 11.0, HOT_C = 55;

function boardCheck(b, kind) {   // kind: "arm", "wheels" or "leader"
  const power = kind === "wheels" ? "chk.fix.12v" : "chk.fix.5v";
  if (!b || b.state === "missing") return kind === "leader" ? ["off", t("chk.off"), t("chk.lead.opt")] : ["warn", t("chk.off"), t("chk.fix.usb")];
  if (b.state === "busy") return ["ok", t("chk.inuse"), ""];
  if (b.state === "silent") return ["warn", t("chk.silent"), t(power)];
  if (b.state !== "ok") return ["warn", t("chk.noanswer"), t("chk.fix.usb")];
  const d = t("chk.motors", { n: b.n, of: b.of, v: b.vmin ?? "-", c: b.tmax ?? "-" });
  if (b.n < b.of) return ["warn", d, t("chk.fix.motors", { n: b.n, of: b.of })];
  if (b.vmin != null && b.vmin < (kind === "wheels" ? WHEEL_MIN_V : ARM_MIN_V)) return ["warn", d, t(power)];
  if (b.alarm) return ["warn", d, t("chk.fix.alarm")];
  if (b.tmax != null && b.tmax >= HOT_C) return ["warn", d, t("chk.fix.hot")];
  return ["ok", d, ""];
}

function wheelsCheck(r, boards) {
  const base = r.jobs && r.jobs.base;
  if (base && base.running) {
    if (r.wheels === "off") return ["warn", t("chk.base.run"), t("chk.fix.12v")];
    return ["ok", t(r.wheels === "ok" ? "chk.base.ready" : "chk.base.starting"), ""];
  }
  const [s, d, f] = boardCheck(boards.base, "wheels");
  return [s, s === "ok" ? `${d} · ${t("chk.base.idle")}` : d, f];
}

function camCheck(r, key) {
  const c = (r.cams || []).find((x) => x.key === key);
  if (!c || !c.present) return ["warn", t("chk.off"), t("chk.fix.cam")];
  if (c.error) return ["warn", t("cam.busy"), t("chk.fix.camapp")];
  if (c.paused_by) return ["ok", t("chk.inuse"), ""];
  return ["ok", t(c.live ? "chk.cam.live" : "chk.cam.ready"), ""];
}

function lidarCheck(r) {
  const g = (st.server && st.server.guard) || {};
  const scanning = !!(st.scan && performance.now() - st.scan.at < 2000);
  if (scanning) {
    const what = st.scan.src === "mock" ? t("chk.lidar.sim") : t("chk.lidar.scan");
    return [g.on ? "ok" : "warn", `${what} · ${t(g.on ? "chk.guard.on" : "chk.guard.off")}`, g.on ? "" : t("chk.fix.guardoff")];
  }
  if (!r.lidar) return ["warn", t("chk.off"), t("chk.fix.lidarusb")];
  return ["warn", t("chk.lidar.idle"), t(r.obstacle_stop ? "chk.fix.lidarbase" : "chk.fix.lidarenv")];
}

function jetsonCheck(r) {
  const j = (r.health && r.health.jetson) || {};
  const d = t("chk.jetson.d", { c: j.temp ?? "-", m: j.mem != null ? Math.round(j.mem) : "-", g: j.disk_free_gb ?? "-" });
  if (j.temp != null && j.temp >= 85) return ["warn", d, t("chk.fix.jhot")];
  if (j.mem != null && j.mem >= 92) return ["warn", d, t("chk.fix.mem")];
  if (j.disk_free_gb != null && j.disk_free_gb < 2) return ["warn", d, t("chk.fix.disk")];
  return ["ok", d, ""];
}

function renderCheck() {
  const r = st.robot, s = st.server;
  const rows = [[t("chk.server"), st.open && s
    ? ["ok", `${s.backend === "mock" ? t("chk.server.mock") : "ROS 2"} · v${s.version} · ${fmtUp(s.up_s)}`, ""]
    : ["warn", t("chk.server.down"), t("chk.fix.server")]]];
  if (st.open && r) {
    const B = (r.health && r.health.boards) || {};
    const left = boardCheck(B.left_follower, "arm");
    rows.push(
      [t("chk.wheels"), wheelsCheck(r, B)],
      [t("chk.rarm"), boardCheck(B.right_follower, "arm")],
      [t("chk.larm"), r.left_arm_ok || left[0] !== "ok" ? left : ["warn", left[1], t("chk.fix.leftlock")]],
      [t("chk.rlead"), boardCheck(B.right_leader, "leader")],
      [t("chk.llead"), boardCheck(B.left_leader, "leader")],
      [t("cam.top"), camCheck(r, "top")],
      [t("cam.right_wrist"), camCheck(r, "right_wrist")],
      [t("cam.left_wrist"), camCheck(r, "left_wrist")],
      [t("chk.lidar"), lidarCheck(r)],
      [t("chk.pad"), r.gamepad == null ? ["off", "-", ""] : r.gamepad ? ["ok", t("chk.connected"), ""] : ["off", t("chk.off"), t("chk.pad.opt")]],
      [t("chk.mic"), r.mic == null ? ["off", "-", ""] : r.mic ? ["ok", t("chk.mic.usb"), ""] : ["off", t("chk.off"), t("chk.mic.opt")]],
      [t("chk.jetson"), jetsonCheck(r)],
    );
  }
  $("chk-grid").innerHTML = rows.map(([name, [s, d, f]]) => `<div class="chk" data-st="${s}"><i class="dot"></i>`
    + `<div class="chk-t"><b>${esc(name)}</b><span>${esc(d)}</span>${f ? `<small>${esc(f)}</small>` : ""}</div></div>`).join("");
  const n = rows.filter(([, [x]]) => x === "warn").length;
  const sum = $("chk-sum");
  sum.textContent = !st.open ? t("chk.server.down") : n ? t("chk.attention", { n }) : t("chk.allok");
  sum.dataset.st = !st.open || n ? "warn" : "ok";
}

// --------------------------------------------------------------- terminal
/* The server log, live: the same lines the server prints in its own terminal. A line that
   repeats right away is counted (x12) instead of filling the window. */
const term = { n: 0, last: null };
const termTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
const termSrc = (name) => (name === "kiro" ? "server" : name.replace(/^kiro\./, ""));

function termLive() {
  const el = $("term-live");
  el.textContent = st.open ? t("d.termlive") : t("d.termoff");
  el.classList.toggle("on", st.open);
}

function onLog(m) {
  const box = $("term"), prompt = $("term-prompt");
  const follow = m.reset || box.scrollHeight - box.scrollTop - box.clientHeight < 30;
  if (m.reset) { for (const el of box.querySelectorAll(".tl:not(.prompt)")) el.remove(); term.n = 0; term.last = null; }
  for (const x of m.lines) {
    if (x.n <= term.n) continue;
    term.n = x.n;
    const key = `${x.lvl}|${x.src}|${x.msg}`;
    if (term.last && term.last.key === key) {
      term.last.count += 1;
      term.last.el.querySelector("time").textContent = termTime(x.ts);
      term.last.el.querySelector(".rep").textContent = `\u00d7${term.last.count}`;
      continue;
    }
    const el = document.createElement("div");
    el.className = "tl";
    el.dataset.lvl = x.lvl;
    el.innerHTML = `<time>${termTime(x.ts)}</time><b>${esc(x.lvl)}</b><span class="src">${esc(termSrc(x.src))}</span><span class="msg">${esc(x.msg)}</span><em class="rep"></em>`;
    box.insertBefore(el, prompt);
    term.last = { key, el, count: 1 };
  }
  const lines = box.querySelectorAll(".tl:not(.prompt)");
  for (let i = 0; i < lines.length - 400; i++) lines[i].remove();
  if (follow) box.scrollTop = box.scrollHeight;
}

// ----------------------------------------------------------------- lidar
function drawLidar() {
  if (!liveOpen()) return;
  const c = $("lidar");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (!w || !h) return;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const s = st.server, o = (s && s.odom) || { x: 0, y: 0, th: 0 }, S = Math.min(w, h) / 6, T = theme;
  const sx = (wy) => w / 2 - (wy - o.y) * S, sy = (wx) => h / 2 - (wx - o.x) * S;

  g.strokeStyle = T.line2; g.lineWidth = 1;
  for (let r = 1; r * S < Math.max(w, h); r++) { g.beginPath(); g.arc(w / 2, h / 2, r * S, 0, 2 * Math.PI); g.stroke(); }
  g.fillStyle = T.mute; g.font = "11px " + T.font;
  for (let r = 1; r <= 3; r++) g.fillText(`${r} m`, w / 2 + 4, h / 2 - r * S - 4);

  const lt = trail[trail.length - 1];
  if (lt && Math.hypot(o.x - lt.x, o.y - lt.y) > 1.5) trail.length = 0;
  if (!lt || Math.hypot(o.x - lt.x, o.y - lt.y) > 0.02) { trail.push({ x: o.x, y: o.y }); if (trail.length > 2000) trail.shift(); }
  if (trail.length > 1) {
    g.strokeStyle = T.accent; g.lineWidth = 2; g.beginPath();
    trail.forEach((p, i) => (i ? g.lineTo(sx(p.y), sy(p.x)) : g.moveTo(sx(p.y), sy(p.x))));
    g.stroke();
  }

  g.save(); g.translate(w / 2, h / 2); g.rotate(-o.th);
  const sc = st.scan && performance.now() - st.scan.at < 1000 ? st.scan : null;
  $("scan-src").textContent = sc ? (sc.src === "mock" ? t("scan.mock") : t("scan.live")) : t("d.nolidar");
  if (sc) {
    g.fillStyle = T.ink;
    for (let i = 0; i < sc.xy.length; i += 2) g.fillRect(-sc.xy[i + 1] / 100 * S - 1.5, -sc.xy[i] / 100 * S - 1.5, 3, 3);
    const n = nearest();
    if (n) {
      g.strokeStyle = T.accent; g.lineWidth = 1; g.setLineDash([4, 4]);
      g.beginPath(); g.moveTo(0, 0); g.lineTo(-Math.sin(n.a) * n.d * S, -Math.cos(n.a) * n.d * S); g.stroke(); g.setLineDash([]);
    }
  }
  const L = 0.45 * S, W = 0.35 * S;
  g.fillStyle = T.raise; g.strokeStyle = s && s.estop ? T.stop : T.accent; g.lineWidth = 2;
  g.fillRect(-W / 2, -L / 2, W, L); g.strokeRect(-W / 2, -L / 2, W, L);
  g.fillStyle = g.strokeStyle; g.beginPath(); g.moveTo(0, -L / 2 + 4); g.lineTo(-7, -L / 2 + 15); g.lineTo(7, -L / 2 + 15); g.closePath(); g.fill();
  g.restore();
}
window.addEventListener("resize", drawLidar);

// The live view (cameras and lidar) opens and closes; the choice is remembered. Open by default.
try { $("d-live").open = localStorage.getItem("kiro-live") !== "0"; } catch (e) { $("d-live").open = true; }
$("d-live").addEventListener("toggle", () => {
  try { localStorage.setItem("kiro-live", $("d-live").open ? "1" : "0"); } catch (e) { /* ignore */ }
  renderCams(); drawLidar();
});

buildTiles();
buildCharts();
render();
termLive();
renderCheck();
connect();
