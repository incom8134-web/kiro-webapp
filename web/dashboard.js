"use strict";
/*
 * HAEBOT dashboard. A watching screen: pictures, a ready line, what the robot is doing.
 * It never drives. E-STOP still works. Release stays on the control page.
 * Numbers are measurements. A missing sensor stays blank. Mock battery samples are not shown.
 */
const $ = (id) => document.getElementById(id);
const esc = (s) => Kiro.esc(s);
const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
const WINDOW_S = 300;

const st = { ws: null, open: false, server: null, robot: null, scan: null, rtt: null, pongAt: 0, speed: null };
const hist = {};
const trail = [];
const stalled = new Set();
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

let link = null;
const send = (o) => { if (link) link.send(o); };
function connect() {
  link = Kiro.connect({
    name: "Dashboard",
    onOpen() { st.open = true; send({ t: "logs" }); termLive(); },
    onClose() { st.open = false; st.server = null; st.rtt = null; st.pongAt = 0; render(); termLive(); renderCheck(); },
    onMessage(m) {
      if (m.t === "state") { st.server = m; onState(); }
      else if (m.t === "robot") { st.robot = m; onRobot(); }
      else if (m.t === "scan") { m.at = performance.now(); st.scan = m; drawLidar(); renderDrive(); }
      else if (m.t === "pong") { st.pongAt = Date.now(); st.rtt = performance.now() - m.ts; renderHeader(); }
      else if (m.t === "log") onLog(m);
    },
  });
}

Kiro.bindEstop($("estop"), $("estop-release"), send, () => !!(st.server && st.server.estop), () => false);
window.addEventListener("keydown", (e) => { if (e.code === "Escape" && !Kiro.typing(e)) send({ t: "estop", on: true }); });
for (const b of document.querySelectorAll(".lang button")) b.addEventListener("click", () => { setLang(b.dataset.lang); buildCharts(); render(); onRobot(); termLive(); });
$("theme-btn").addEventListener("click", () => toggleTheme());
window.addEventListener("kiro-theme", () => { readTheme(); themeIcon($("theme-btn")); buildCharts(); drawLidar(); });
themeIcon($("theme-btn"));

const nowS = () => Date.now() / 1000;
function record(key, v) {
  if (v == null || !Number.isFinite(v)) return;
  const h = hist[key] || (hist[key] = []);
  h.push({ x: nowS(), y: v });
  while (h.length && h[0].x < nowS() - WINDOW_S) h.shift();
}

function onState() {
  const s = st.server;
  const o = s && s.odom;
  st.speed = s ? (o ? Math.hypot(o.vx, o.vy) : Math.hypot(s.cmd.vx, s.cmd.vy)) : null;
  render();
}

function onRobot() {
  const r = st.robot;
  if (!r) return;
  const j = (r.health || {}).jetson || {};
  record("cpu", j.cpu);
  record("gpu", j.gpu);
  const tz = j.temps || {};
  record("t_cpu", tz.cpu);
  record("t_gpu", tz.gpu);
  const soc = Object.entries(tz).filter(([k]) => k.startsWith("soc")).map(([, v]) => v);
  record("t_soc", soc.length ? soc.reduce((a, b) => a + b, 0) / soc.length : null);
  const pw = j.power || {};
  record("p_in", pw.VDD_IN);
  record("p_cg", pw.VDD_CPU_GPU_CV);
  record("p_soc", pw.VDD_SOC);
  renderBattery();
  updateCharts();
  renderLists();
  renderJetson();
  renderCams();
  renderCheck();
  renderNow();
}

function renderBattery() {
  const b = (st.robot && st.robot.battery) || null;
  const real = !!(b && b.source === "ina228" && typeof b.percent === "number");
  Kiro.renderBattery(real ? b : null);
  if (!real && $("bat-pct")) $("bat-pct").textContent = t("dash.notwired");
}

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
  if (s == null) return "";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : m ? `${m} min` : `${s} s`;
}

function meDriving() {
  return !!(st.server && st.server.you && st.server.you.role === "operator");
}

function whoText() {
  const s = st.server;
  if (!s || !s.operator) return t("op.none");
  return meDriving() ? t("op.you") : t("op.other", { name: s.operator.name });
}

function renderHeader() {
  const s = st.server;
  $("backend").textContent = s ? t(s.backend === "ros" ? "be.ros" : "be.mock") : "...";
  $("link").textContent = !st.open ? t("link.offline") : st.rtt == null ? t("link.connecting") : `${Math.round(st.rtt)} ms`;
  $("link").className = st.rtt != null && st.rtt < 150 ? "accent" : "";
  const op = $("opstat");
  op.textContent = whoText();
  op.className = meDriving() ? "accent" : "";
  const mode = s ? t(s.backend === "ros" ? "be.ros" : "be.mock") : "...";
  $("ready-who").textContent = `${mode} · ${whoText()}`;
}

function render() {
  const s = st.server;
  applyStrings();
  for (const b of document.querySelectorAll(".lang button")) b.classList.toggle("on", b.dataset.lang === LANG);
  renderHeader();
  renderBattery();
  const stopped = !!s && s.estop;
  $("estop").classList.toggle("on", stopped);
  $("estop-label").textContent = stopped ? t("estop.stopped") : t("estop");
  $("estop").setAttribute("aria-pressed", stopped ? "true" : "false");
  document.title = t("title.dash");
  const banner = $("banner");
  const fold = st.robot && st.robot.estop_fold;
  const foldText = fold ? " " + t(fold === "pending" ? "banner.fold.pending" : "banner.fold.running") : "";
  if (!st.open) { banner.hidden = false; banner.className = "banner"; banner.textContent = t("banner.offline"); }
  else if (stopped) {
    banner.hidden = false; banner.className = "banner stop";
    banner.textContent = t("banner.estop", { by: s.estop_by ? t("banner.estop.by", { name: s.estop_by }) : "" }) + t("banner.estop.viewer") + foldText;
  } else if (fold) { banner.hidden = false; banner.className = "banner"; banner.textContent = foldText.trim(); }
  else banner.hidden = true;
  renderDrive();
  renderCheck();
  drawLidar();
}

function sideText(v) { return v == null ? t("dash.nodist") : v.toFixed(2) + " m"; }

function renderDrive() {
  const n = nearest();
  const sd = sides();
  $("now-speed").textContent = st.speed == null ? "—" : st.speed.toFixed(2) + " m/s";
  $("now-near").textContent = n ? n.d.toFixed(2) + " m · " + t(direction(n.a)) : t("d.nolidar");
  $("lidar-sides").textContent = t("dash.sides", {
    front: sideText(sd && sd.front), left: sideText(sd && sd.left),
    right: sideText(sd && sd.right), back: sideText(sd && sd.back),
  });
  const s = st.server, r = st.robot;
  const baseOn = !!(r && r.jobs && r.jobs.base && r.jobs.base.running);
  const rates = s && s.rates;
  const rateEl = $("now-rates");
  rateEl.hidden = !(baseOn && rates);
  if (!rateEl.hidden) {
    rateEl.textContent = rates.odom_age != null && rates.odom_age > 1
      ? t("base.noodom", { s: Math.round(rates.odom_age) })
      : t("base.rates", { odom: Math.round(rates.odom_hz), scan: Math.round(rates.scan_hz) });
  }
}

function renderNow() {
  const r = st.robot;
  if (!r) { $("now-doing").textContent = t("dash.idle"); $("now-err").textContent = t("dash.none"); return; }
  $("now-doing").textContent = doingText(r);
  $("now-err").textContent = lastError(r);
}

const TASKS = [
  ["base", "sec.base"], ["teleop_right", "d.tele_r"], ["teleop_left", "d.tele_l"],
  ["arm_right", "d.pose_r"], ["arm_left", "d.pose_l"], ["bridge", "leader.title"],
  ["relay", "dash.copy"], ["skill", "sec.skills"], ["demo", "dash.demo"],
  ["voice", "sec.voice"], ["record", "d.record"],
];

function doingText(r) {
  const J = r.jobs || {};
  const parts = [];
  for (const [k, label] of TASKS) {
    const j = J[k];
    if (!j || !j.running) continue;
    const clock = fmtUp(Math.max(0, Math.round(Date.now() / 1000 - j.started)));
    if (k === "skill") {
      const series = r.skill_series;
      const healthT = (r.health && r.health.t) || Date.now() / 1000;
      const loading = healthT - j.started < 45;
      const bits = [series ? t(series.waiting ? "skill.waiting" : "skill.runof", { n: series.run, of: series.of, s: Math.round(series.pause) }) : t(label)];
      if (loading && !(series && series.waiting)) bits.push(t("skill.loading.short"));
      if (clock) bits.push(clock);
      parts.push(bits.join(" · "));
    } else parts.push((j.label || t(label)) + (clock ? " · " + clock : ""));
  }
  return parts.length ? parts.join(" · ") : t("dash.idle");
}

function lastError(r) {
  const cands = [];
  for (const e of r.events || []) {
    if (/e-stop|estop|비상/i.test(e.text || "")) cands.push({ ts: e.ts || 0, text: `${e.who}: ${e.text}` });
  }
  for (const j of Object.values(r.jobs || {})) {
    if (!j || j.running) continue;
    if (j.why !== "USB_LOST" && j.why !== "KILLED" && j.why !== "ERROR") continue;
    const head = t("why." + j.why, { name: j.label || j.name });
    const tail = j.why === "ERROR" && j.tail && j.tail.length ? " " + j.tail[j.tail.length - 1] : "";
    cands.push({ ts: j.ended || 0, text: head + tail });
  }
  if (!cands.length) return t("dash.none");
  cands.sort((a, b) => b.ts - a.ts);
  const c = cands[0];
  const tm = c.ts ? new Date(c.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
  return tm ? tm + " · " + c.text : c.text;
}

const charts = {};
const timeTick = (v) => new Date(v * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function baseOptions(unit) {
  const T = theme;
  const grid = { color: T.line, drawTicks: false };
  const ticks = { color: T.mute, padding: 6, font: { size: 11 } };
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: true, position: "top", align: "end", labels: { color: T.ink2, boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "rectRounded", font: { size: 11 } } },
      tooltip: {
        backgroundColor: T.panel, titleColor: T.ink, bodyColor: T.ink2, borderColor: T.line2, borderWidth: 1, padding: 10,
        callbacks: { label: (c) => ` ${c.dataset.label ? c.dataset.label + ": " : ""}${c.parsed.y} ${unit}`, title: (items) => items[0] ? timeTick(items[0].parsed.x) : "" },
      },
    },
    scales: {
      x: { type: "linear", min: nowS() - WINDOW_S, max: nowS(), grid, border: { display: false }, ticks: { ...ticks, maxTicksLimit: 5, callback: timeTick } },
      y: { grid, border: { display: false }, ticks, beginAtZero: true },
    },
  };
}

function lineChart(id, series, unit) {
  const ds = series.map((s, i) => ({
    label: t(s.label), key: s.key, data: [], borderColor: theme.series[i], backgroundColor: theme.series[i] + "22",
    borderWidth: 2, pointRadius: 0, tension: 0.25, fill: series.length === 1,
  }));
  return new Chart($(id), { type: "line", data: { datasets: ds }, options: baseOptions(unit) });
}

function buildCharts() {
  for (const c of Object.values(charts)) c.destroy();
  for (const k of Object.keys(charts)) delete charts[k];
  if (typeof Chart === "undefined") return;
  Chart.defaults.font.family = theme.font;
  Chart.defaults.color = theme.mute;
  charts.cpugpu = lineChart("ch-cpugpu", [{ key: "cpu", label: "s.cpu" }, { key: "gpu", label: "s.gpu" }], "%");
  charts.temps = lineChart("ch-temps", [{ key: "t_cpu", label: "s.cpu" }, { key: "t_gpu", label: "s.gpu" }, { key: "t_soc", label: "s.soc" }], "°C");
  charts.power = lineChart("ch-power", [{ key: "p_in", label: "s.total" }, { key: "p_cg", label: "s.cpugpu" }, { key: "p_soc", label: "s.soc" }], "W");
  updateCharts();
}

function emptyChart(chart, text) {
  const box = chart.canvas.parentElement, e = box.querySelector(".empty");
  e.hidden = !text; e.textContent = text || "";
  chart.canvas.style.visibility = text ? "hidden" : "visible";
}

function updateCharts() {
  if (!charts.cpugpu) return;
  for (const name of ["cpugpu", "temps", "power"]) {
    const c = charts[name];
    let any = false;
    for (const d of c.data.datasets) { d.data = (hist[d.key] || []).slice(); any = any || d.data.length > 0; }
    const first = Math.min(...c.data.datasets.map((d) => (d.data[0] ? d.data[0].x : Infinity)));
    c.options.scales.x.min = Math.max(nowS() - WINDOW_S, Number.isFinite(first) ? first : nowS() - WINDOW_S);
    c.options.scales.x.max = nowS();
    emptyChart(c, any ? "" : t("c.nodata"));
    c.update("none");
  }
}
setInterval(() => { if (st.robot) updateCharts(); }, 2000);

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
    rows.push(`<tr><td>${esc(label)} <span class="mute">${esc(x.n)}/${esc(x.of)}</span></td><td><b>${esc(x.vmin ?? "-")}</b> V</td><td>${esc(x.tmax ?? "-")} °C</td><td>${esc(Math.round(ma))} mA${x.alarm ? " · ALARM" : ""}</td></tr>`);
  }
  $("servos").innerHTML = rows.join("");
  const ev = (r.events || []).slice().reverse();
  $("log").innerHTML = ev.length ? ev.map((e) => `<li><time>${new Date(e.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time><span>${esc(e.who)}: ${esc(e.text)}</span></li>`).join("")
    : `<li class="mute">${esc(t("log.empty"))}</li>`;
}

function renderJetson() {
  const j = (st.robot && st.robot.health && st.robot.health.jetson) || {};
  const bits = [];
  if (j.cpu != null) bits.push(`${t("h.cpu")} ${Math.round(j.cpu)}%`);
  if (j.gpu != null) bits.push(`${t("s.gpu")} ${Math.round(j.gpu)}%`);
  if (j.temp != null) bits.push(`${j.temp}°C`);
  if (j.mem != null) bits.push(`${t("h.mem")} ${Math.round(j.mem)}%`);
  if (j.disk_free_gb != null) bits.push(`${t("h.disk")} ${j.disk_free_gb} GB`);
  const pw = j.power || {};
  if (pw.VDD_IN != null) bits.push(`${pw.VDD_IN} W`);
  $("jetson-line").textContent = bits.length ? bits.join(" · ") : t("c.nodata");
}

let camsOff = false;
$("cam-pause").addEventListener("click", () => { camsOff = !camsOff; renderCams(); });

function renderCams() {
  const cams = (st.robot && st.robot.cams) || [];
  const pb = $("cam-pause");
  pb.querySelector("i").className = camsOff ? "ri-play-fill" : "ri-pause-fill";
  pb.querySelector("span").textContent = t(camsOff ? "cam.resume" : "cam.pause");
  pb.title = t(camsOff ? "b.cams.play" : "b.cams.pause");
  pb.setAttribute("aria-pressed", String(camsOff));
  for (const box of document.querySelectorAll(".qbox[data-cam]")) {
    const key = box.dataset.cam, c = cams.find((x) => x.key === key);
    const im = box.querySelector("img"), em = box.querySelector(".empty"), msg = box.querySelector(".msg");
    if (im) im.alt = t("cam." + key);
    const why = camsOff ? t("b.cams.off") : !c || !c.present ? t("cam.notconnected") : c.paused_by ? t("cam.inuse") : c.error ? t("cam.busy") : "";
    if (why) {
      if (box.dataset.on) { im.removeAttribute("src"); delete box.dataset.on; }
      im.hidden = true; em.hidden = false; msg.textContent = why;
      if (camsOff || !c || !c.present) stalled.delete(key);
      continue;
    }
    if (!box.dataset.on) {
      box.dataset.on = "1";
      im.onload = () => { if (stalled.delete(key)) renderCheck(); };
      im.onerror = () => {
        stalled.add(key); delete box.dataset.on; im.hidden = true; em.hidden = false;
        msg.textContent = t("dash.videostall", { name: t("cam." + key) });
        renderCheck();
        setTimeout(renderCams, 2000);
      };
      im.src = `/camera/${encodeURIComponent(key)}.mjpg?t=${Date.now()}`;
    }
    im.hidden = false; em.hidden = true;
  }
}

const ARM_MIN_V = 4.5, WHEEL_MIN_V = 11.0, HOT_C = 55;

function boardCheck(b, kind) {
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
  if (g.lost) return ["warn", t("guard.lost"), t("guard.lostnote")];
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

function rowHtml(name, state, detail, fix) {
  return `<div class="chk" data-st="${state}"><i class="dot"></i><div class="chk-t"><b>${esc(name)}</b><span>${esc(detail)}</span>${fix ? `<small>${esc(fix)}</small>` : ""}</div></div>`;
}

function checkRows() {
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
  return rows;
}

function obstacleFlag() {
  const s = st.server, g = (s && s.guard) || {};
  if (!s) return null;
  if (g.lost) return ["warn", t("guard.lost")];
  if (g.block && g.block.length) return ["warn", t("guard.held", { where: g.block.map((d) => t("dir." + d)).join(", ") })];
  if (!g.on) return ["warn", t("guard.off")];
  if (!g.lidar) return ["warn", t("guard.nolidar")];
  return ["ok", t("guard.on", { cm: Math.round((g.stop_m || 0.2) * 100) })];
}

function linkStale() {
  return st.open && st.pongAt > 0 && Date.now() - st.pongAt > 3000;
}

function renderCheck() {
  const rows = checkRows();
  const amber = rows.filter(([, [x]]) => x === "warn");
  const rest = rows.filter(([, [x]]) => x !== "warn");
  const problems = amber.map(([name, [state, detail, fix]]) => rowHtml(name, state, detail, fix)).join("");
  const calm = rest.map(([name, [state, detail, fix]]) => rowHtml(name, state, detail, fix)).join("");
  const boxP = $("ready-problems"), boxC = $("chk-grid");
  if (boxP.dataset.h !== problems) { boxP.innerHTML = problems; boxP.dataset.h = problems; }
  if (boxC.dataset.h !== calm) { boxC.innerHTML = calm; boxC.dataset.h = calm; }

  const s = st.server;
  const stopped = !!(s && s.estop);
  const names = amber.map(([name]) => name);
  if (stopped) names.unshift(t("estop"));
  const obs = obstacleFlag();
  if (obs && obs[0] === "warn" && !names.includes(t("chk.lidar"))) names.push(t("guard.sw"));
  const line = $("ready-line");
  line.textContent = names.length ? t("dash.notready", { what: names.join(", ") }) : t("dash.ready");
  line.dataset.st = names.length ? "warn" : "ok";

  const flags = [];
  if (s) flags.push([stopped ? "warn" : "ok", t(stopped ? "dash.estop.latched" : "dash.estop.clear")]);
  if (obs) flags.push(obs);
  if (st.rtt != null && st.rtt >= 150) flags.push(["warn", t("dash.linkslow", { ms: Math.round(st.rtt) })]);
  if (linkStale()) flags.push(["warn", t("dash.linkstale")]);
  for (const key of stalled) flags.push(["warn", t("dash.videostall", { name: t("cam." + key) })]);
  const flagHtml = flags.map(([state, text]) => `<span class="flag" data-st="${state}">${esc(text)}</span>`).join("");
  const flagEl = $("ready-flags");
  if (flagEl.dataset.h !== flagHtml) { flagEl.innerHTML = flagHtml; flagEl.dataset.h = flagHtml; }
  renderNow();
}

const term = { n: 0, last: null };
const termTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
const termSrc = (name) => (name === "kiro" ? "server" : name.replace(/^kiro\./, ""));

function termLive() {
  const el = $("term-live");
  el.textContent = st.open ? t("d.termlive") : t("d.termoff");
  el.classList.toggle("on", st.open);
}

function onLog(m) {
  const box = $("term");
  const follow = m.reset || box.scrollHeight - box.scrollTop - box.clientHeight < 30;
  if (m.reset) { box.replaceChildren(); term.n = 0; term.last = null; }
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
    box.appendChild(el);
    term.last = { key, el, count: 1 };
  }
  const lines = box.querySelectorAll(".tl");
  for (let i = 0; i < lines.length - 400; i++) lines[i].remove();
  if (follow) box.scrollTop = box.scrollHeight;
}

function drawLidar() {
  const c = $("lidar");
  if (!c) return;
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
  const L = 0.45 * S, Wd = 0.35 * S;
  g.fillStyle = T.raise; g.strokeStyle = s && s.estop ? T.stop : T.accent; g.lineWidth = 2;
  g.fillRect(-Wd / 2, -L / 2, Wd, L); g.strokeRect(-Wd / 2, -L / 2, Wd, L);
  g.fillStyle = g.strokeStyle; g.beginPath(); g.moveTo(0, -L / 2 + 4); g.lineTo(-7, -L / 2 + 15); g.lineTo(7, -L / 2 + 15); g.closePath(); g.fill();
  g.restore();
}
window.addEventListener("resize", drawLidar);

$("d-details").addEventListener("toggle", () => {
  if ($("d-details").open) for (const c of Object.values(charts)) c.resize();
  drawLidar();
});

buildCharts();
render();
termLive();
renderCheck();
connect();
Kiro.bindLogin({ onLogin() { document.querySelectorAll(".qbox[data-cam]").forEach((b) => delete b.dataset.on); stalled.clear(); if (link) link.reconnect(); } });
