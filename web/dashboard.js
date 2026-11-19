"use strict";
/*
 * HAEBOT Robot live dashboard. Read-only view for a big screen: it never drives (E-STOP still works).
 * Every number comes from the robot server. Charts and bar strips keep the last 5 minutes, recorded in
 * this browser since the page was opened. A tile with no data says so; nothing is invented.
 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const WINDOW_S = 300;
const SPEED_TOP = 0.3;      // right end of the speed meter, m/s (the same top the old speed chart used)
const LINK_SLOW_MS = 150;   // a link slower than this is "slow"
const GAUGE_LEN = 194.8;    // length of the gauge arc (pi x 62)

const st = { ws: null, open: false, server: null, robot: null, scan: null, rtt: null };
const hist = {};      // key -> [{x: epoch seconds, y}]
const trail = [];
const ui = { load: "cpu", range: WINDOW_S, cams: true };
try {
  ui.load = ["cpu", "gpu", "mem"].includes(localStorage.getItem("kiro-dash-load")) ? localStorage.getItem("kiro-dash-load") : "cpu";
  ui.range = localStorage.getItem("kiro-dash-range") === "60" ? 60 : WINDOW_S;
  ui.cams = localStorage.getItem("kiro-live") !== "0";
} catch (e) { /* private window */ }
const remember = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } };

let theme = {};
function readTheme() {
  const cs = getComputedStyle(document.body);
  const v = (name, fallback) => cs.getPropertyValue(name).trim() || fallback;
  theme = {
    fg: v("--d-fg", "#fafafa"), mfg: v("--d-mfg", "#a1a1a1"), line: v("--d-line", "rgba(255,255,255,.1)"),
    line2: v("--d-line-2", "rgba(255,255,255,.18)"), card: v("--d-card", "#171717"), muted: v("--d-muted", "#262626"),
    lime: v("--lime-data", "#b4f24e"), lilac: v("--lilac-data", "#b699eb"), s3: v("--s3", "#b2b5d1"),
    stop: v("--stop", "#ff453a"), font: v("--font", "system-ui"), mono: v("--mono", "monospace"),
  };
}
readTheme();
/* "#rrggbb" with an alpha value, for chart fills. */
const alpha = (hex, a) => (/^#[0-9a-f]{6}$/i.test(hex) ? `rgba(${parseInt(hex.slice(1, 3), 16)}, ${parseInt(hex.slice(3, 5), 16)}, ${parseInt(hex.slice(5, 7), 16)}, ${a})` : hex);

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
  ws.onclose = () => { st.open = false; st.server = null; st.rtt = null; render(); termLive(); setTimeout(connect, 1000); };
  ws.onerror = () => ws.close();
}
const send = (o) => { if (st.open && st.ws.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify(o)); };
setInterval(() => send({ t: "ping", ts: performance.now() }), 1000);

$("estop").addEventListener("click", () => {   // anyone may stop the robot; only the operator releases it
  if (!(st.server && st.server.estop)) send({ t: "estop", on: true });
});
window.addEventListener("keydown", (e) => { if (e.code === "Escape" && !document.fullscreenElement) send({ t: "estop", on: true }); });
for (const b of document.querySelectorAll(".lang button")) b.addEventListener("click", () => { setLang(b.dataset.lang); last_.hero = ""; buildCharts(); render(); onRobot(); termLive(); });
$("theme-btn").addEventListener("click", () => toggleTheme());
window.addEventListener("kiro-theme", () => { readTheme(); themeIcon($("theme-btn")); buildCharts(); drawLidar(); });
themeIcon($("theme-btn"));

/* The round "choose one" pills: which load the gauge shows, and how far back the chart goes. */
function pick(id, value, onChange) {
  const box = $(id);
  const paint = (v) => { for (const b of box.querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.v === String(v))); };
  box.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    paint(b.dataset.v);
    onChange(b.dataset.v);
  });
  paint(value);
}
pick("load-pick", ui.load, (v) => { ui.load = v; remember("kiro-dash-load", v); renderLoad(); });
pick("range-pick", ui.range, (v) => { ui.range = +v; remember("kiro-dash-range", v); updateCharts(); });

// --------------------------------------------------------------- history
const now = () => Date.now() / 1000;
function record(key, v) {
  if (v == null || !Number.isFinite(v)) return;
  const h = hist[key] || (hist[key] = []);
  h.push({ x: now(), y: v });
  while (h.length && h[0].x < now() - WINDOW_S) h.shift();
}
const last = (key) => { const h = hist[key]; return h && h.length ? h[h.length - 1].y : null; };

/* The last 5 minutes of one reading, cut into n equal slots: the mean of each slot, or null if empty. */
function slots(key, n) {
  const end = now(), w = WINDOW_S / n, sum = new Array(n).fill(0), cnt = new Array(n).fill(0);
  for (const p of hist[key] || []) {
    const i = Math.floor((p.x - (end - WINDOW_S)) / w);
    if (i >= 0 && i < n) { sum[i] += p.y; cnt[i] += 1; }
  }
  return sum.map((s, i) => (cnt[i] ? s / cnt[i] : null));
}
const slotTime = (i, n) => timeTick(now() - WINDOW_S + (i + 0.5) * (WINDOW_S / n));

let lastSpeedSample = 0, lastStrips = 0;
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
  renderNumbers();
  renderStrips();
  updateCharts();
  renderLists();
  renderCams();
  renderGuard();
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
const num = (v, d = 0) => (v == null || !Number.isFinite(v) ? "-" : v.toFixed(d));
const text = (id, v) => { const el = $(id); if (el && el.textContent !== String(v)) el.textContent = v; };
const timeTick = (v) => new Date(v * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

// ------------------------------------------------------- strips and meters
/* A row of bars, one per time slot. vals: numbers or null (an empty slot stays a short grey stub).
   o.max: value of a full-height bar; o.paint(bar, i): extra styling; o.say(i): text shown while a bar is hovered. */
function strip(el, vals, o = {}) {
  if (el.children.length !== vals.length) {
    el.innerHTML = vals.map(() => "<i></i>").join("");
    if (o.say && !el.dataset.wired) {
      el.dataset.wired = "1";
      el.addEventListener("pointermove", (e) => {
        const r = el.getBoundingClientRect();
        el._hot = Math.max(0, Math.min(el.children.length - 1, Math.floor(((e.clientX - r.left) / r.width) * el.children.length)));
        el._redraw && el._redraw();
      });
      el.addEventListener("pointerleave", () => { el._hot = null; el._redraw && el._redraw(); });
    }
  }
  const top = Math.max(o.max || 0, ...vals.map((v) => v || 0), 1e-9);
  [...el.children].forEach((b, i) => {
    const v = vals[i];
    b.className = v == null ? "" : "v";
    b.style.height = v == null ? "6%" : `${14 + (v / top) * 86}%`;
    b.style.opacity = v == null ? "" : (0.38 + (v / top) * 0.62).toFixed(2);
    b.style.background = "";
    if (v != null && o.paint) o.paint(b, i);
    b.classList.toggle("hot", el._hot === i);
  });
  el.classList.toggle("hovering", el._hot != null);
  if (o.label) el.setAttribute("aria-label", o.label);
}

function renderStrips() {
  lastStrips = Date.now();
  // speed, last 5 minutes (the big strip under the headline)
  const sp = slots("speed", 40), spEl = $("st-speed");
  const spMax = Math.max(0, ...sp.map((v) => v || 0));
  const draw = () => {
    strip(spEl, sp, { max: SPEED_TOP, say: true, label: `${t("b.speedhist")}: ${t("b.peak")} ${spMax.toFixed(2)} m/s` });
    const i = spEl._hot;
    text("st-speed-x", i == null ? t("b.speedhist") : `${slotTime(i, 40)} · ${sp[i] == null ? t("c.nodata") : sp[i].toFixed(2) + " m/s"}`);
  };
  spEl._redraw = draw;
  draw();

  const pw = slots("p_in", 24);
  strip($("st-power"), pw, { label: `${t("c.power")}, ${t("d.last5")}` });

  const rx = slots("rx", 24), tx = slots("tx", 24);
  const both = rx.map((v, i) => (v == null && tx[i] == null ? null : (v || 0) + (tx[i] || 0)));
  strip($("st-net"), both, {
    label: `${t("c.net")}, ${t("d.last5")}`,
    paint: (b, i) => {   // received below (lime), sent on top (lilac)
      const share = both[i] > 0 ? ((rx[i] || 0) / both[i]) * 100 : 100;
      b.style.background = `linear-gradient(to top, var(--lime-data) ${share}%, var(--lilac-data) ${share}%)`;
    },
  });

  strip($("st-rtt"), slots("rtt", 24), { max: LINK_SLOW_MS, label: `${t("b.pings")}, ${t("d.last5")}` });
}

function buildMeter() {
  $("speed-meter").innerHTML = "<i></i>".repeat(40);
}
function renderMeter(v) {
  const el = $("speed-meter"), n = el.children.length, on = v == null ? 0 : Math.round(Math.min(1, v / SPEED_TOP) * n);
  for (let i = 0; i < n; i++) el.children[i].classList.toggle("on", i < on);
  el.setAttribute("aria-valuenow", v == null ? "0" : v.toFixed(2));
  el.setAttribute("aria-valuetext", v == null ? "-" : `${v.toFixed(2)} m/s`);
  el.setAttribute("aria-label", t("d.speed"));
}

function renderLoad() {
  const r = st.robot, j = (r && r.health && r.health.jetson) || {};
  const v = last(ui.load);
  const frac = v == null ? 0 : Math.max(0, Math.min(1, v / 100));
  const fill = $("g-fill");
  fill.style.strokeDashoffset = String(GAUGE_LEN * (1 - frac));
  fill.classList.toggle("is-warn", v != null && v >= 85);   // amber: needs attention
  $("g-needle").style.transform = `rotate(${-90 + frac * 180}deg)`;
  text("v-load", num(v));
  const sub = { cpu: j.cores ? `${j.cores} ${t("d.cores")}` : "", gpu: "Orin Nano",
    mem: j.mem_mb ? `${(j.mem_mb[0] / 1024).toFixed(1)} / ${(j.mem_mb[1] / 1024).toFixed(1)} GB` : "" }[ui.load];
  text("s-load", v == null ? t("c.nodata") : sub);
  text("load-live", st.open ? t("d.termlive") : t("d.termoff"));
}

const SW = (c) => `<i class="sw" style="background:${c}"></i>`;
function renderNumbers() {
  const s = st.server, r = st.robot, j = (r && r.health && r.health.jetson) || {};
  // camera block: speed now, and what the lidar sees
  const o = s && (s.odom || s.cmd);
  const speed = last("speed");
  text("v-speed", s ? num(speed, 2) : "-");
  renderMeter(s ? speed : null);
  text("v-vx", o ? num(o.vx, 2) : "-");
  text("v-vy", o ? num(o.vy, 2) : "-");
  text("v-wz", o ? num(o.wz, 2) : "-");
  const n = nearest(), sd = sides();
  text("v-nearest", n ? n.d.toFixed(2) : "-");
  text("s-nearest", n ? t(direction(n.a)) : "");
  const sidesHtml = ["front", "left", "right", "back"].map((k) =>
    `<div><dt>${esc(t("dir." + k))}</dt><dd>${sd && sd[k] != null ? sd[k].toFixed(2) + "<i>m</i>" : "-"}</dd></div>`).join("");
  if ($("sides").innerHTML !== sidesHtml) $("sides").innerHTML = sidesHtml;

  // the tiles
  text("v-dist", s ? num(s.odo_m, 1) : "-");
  renderLoad();
  const tz = j.temps || {};
  text("v-t_cpu", num(last("t_cpu"), 1));
  text("s-temps", [tz.gpu != null ? `GPU ${tz.gpu}°C` : "", last("t_soc") != null ? `SoC ${last("t_soc").toFixed(1)}°C` : ""].filter(Boolean).join(" · "));
  text("v-cpu", num(last("cpu")));
  text("v-gpu", num(last("gpu")));

  const cores = j.cpu_cores || [];
  const coresHtml = cores.map((c, i) => `<span title="${esc(t("s.core"))} ${i}: ${Math.round(c)} %"><i style="height:${Math.max(4, Math.min(100, c))}%"></i>${i}</span>`).join("");
  const cb = $("cores");
  if (cb.dataset.n !== String(cores.length)) { cb.innerHTML = coresHtml; cb.dataset.n = String(cores.length); }
  else cores.forEach((c, i) => { const el = cb.children[i]; el.firstChild.style.height = `${Math.max(4, Math.min(100, c))}%`; el.title = `${t("s.core")} ${i}: ${Math.round(c)} %`; });
  cb.setAttribute("aria-label", cores.length ? `${t("c.cores")}: ${cores.map((c) => Math.round(c) + " %").join(", ")}` : t("c.nodata"));
  cb.parentElement.hidden = !cores.length;

  // power: the total and what it is made of
  const pw = j.power || {}, total = pw.VDD_IN;
  text("v-p_in", num(total, 1));
  const parts = total == null ? [] : [
    [t("s.cpugpu"), pw.VDD_CPU_GPU_CV, theme.lime], [t("s.soc"), pw.VDD_SOC, theme.lilac],
    [t("b.other"), Math.max(0, total - (pw.VDD_CPU_GPU_CV || 0) - (pw.VDD_SOC || 0)), theme.s3],
  ].filter((p) => p[1] != null);
  const al = $("alloc");
  al.classList.toggle("none", !parts.length);
  const alHtml = parts.length ? parts.map((p) => `<i style="flex-grow:${Math.max(0.02, p[1])};background:${p[2]}"></i>`).join("") : esc(t("c.nodata"));
  if (al.innerHTML !== alHtml) al.innerHTML = alHtml;
  const lgP = parts.map((p) => `<li><span>${SW(p[2])}${esc(p[0])}</span><b>${(+p[1]).toFixed(1)} W</b></li>`).join("");
  if ($("lg-power").innerHTML !== lgP) $("lg-power").innerHTML = lgP;

  // Wi-Fi
  text("v-net", j.net ? num(j.net.rx + j.net.tx, 1) : "-");
  const lgN = `<li><span>${SW(theme.lime)}${esc(t("s.rx"))}</span><b>${j.net ? j.net.rx : "-"}</b></li>` +
    `<li><span>${SW(theme.lilac)}${esc(t("s.tx"))}</span><b>${j.net ? j.net.tx : "-"}</b></li>`;
  if ($("lg-net").innerHTML !== lgN) $("lg-net").innerHTML = lgN;

  const lgT = [["s.cpu", "t_cpu", theme.lime], ["s.gpu", "t_gpu", theme.lilac], ["s.soc", "t_soc", theme.s3]]
    .map(([k, key, c]) => `<li><span>${SW(c)}${esc(t(k))}</span><b>${num(last(key), 1)}</b></li>`).join("");
  if ($("lg-temps").innerHTML !== lgT) $("lg-temps").innerHTML = lgT;
}

/* The server's obstacle stop: on or off, with or without lidar data, and holding the robot right now or not. */
function renderGuard() {
  const s = st.server, chip = $("guard-chip"), gd = s && s.guard;
  if (!gd) { chip.className = "chip"; text("guard-txt", "..."); return; }
  const held = gd.block && gd.block.length ? t("guard.held", { where: gd.block.map((d) => t("dir." + d)).join(", ") }) : "";
  const [cls, label] = !gd.on ? ["is-warn", t("b.guard.off")] : !gd.lidar ? ["is-warn", t("b.guard.nolidar")]
    : held ? ["is-warn", held] : ["is-live", t("b.guard.on")];
  chip.className = "chip " + cls;
  text("guard-txt", label);
}

// --------------------------------------------------------------- charts
const charts = {};

function baseOptions(unit, opts = {}) {
  const T = theme;
  const tooltip = {
    backgroundColor: T.card, titleColor: T.fg, bodyColor: T.mfg, borderColor: T.line2, borderWidth: 1,
    padding: 10, boxPadding: 4, usePointStyle: true, cornerRadius: 12, titleFont: { family: T.mono, size: 11 }, bodyFont: { family: T.mono, size: 11 },
    callbacks: { label: (c) => ` ${c.dataset.label ? c.dataset.label + ": " : ""}${c.parsed.y} ${unit}`,
                 title: (items) => (opts.time && items[0] ? timeTick(items[0].parsed.x) : items[0] ? items[0].label : "") },
  };
  const grid = { color: T.line, drawTicks: false };
  const border = { display: false, dash: [2, 5] };
  const ticks = { color: T.mfg, padding: 8, font: { family: T.mono, size: 11 } };
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: opts.time ? { mode: "index", intersect: false } : { mode: "nearest", intersect: true },
    plugins: { legend: { display: false }, tooltip },
    scales: {
      x: opts.time
        ? { type: "linear", min: now() - WINDOW_S, max: now(), grid: { display: false }, border, display: !opts.compact,
            ticks: { ...ticks, maxTicksLimit: 4, maxRotation: 0, callback: timeTick } }
        : { grid: { display: false }, border, ticks: { ...ticks, maxRotation: 0 } },
      y: { grid, border, ticks: { ...ticks, maxTicksLimit: opts.compact ? 3 : 5 }, beginAtZero: !opts.compact, suggestedMax: opts.max },
    },
  };
}

function lineChart(id, series, unit, opts = {}) {
  const ds = series.map((s) => ({
    label: t(s.label), key: s.key, data: [], borderColor: s.color, borderWidth: s.dash ? 1.75 : 2.5, borderDash: s.dash || [],
    borderCapStyle: "round", pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: s.color, pointHoverBorderColor: theme.card,
    pointHoverBorderWidth: 2, tension: 0.35, fill: !!s.fill,
    backgroundColor: !s.fill ? "transparent" : (c) => {
      const a = c.chart.chartArea;
      if (!a) return "transparent";
      const g = c.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom);
      g.addColorStop(0, alpha(s.color, 0.26));
      g.addColorStop(1, alpha(s.color, 0));
      return g;
    },
  }));
  return new Chart($(id), { type: "line", data: { datasets: ds }, options: baseOptions(unit, { time: true, ...opts }) });
}

function barChart(id, unit, color, opts = {}) {
  return new Chart($(id), {
    type: "bar",
    data: { labels: [], datasets: [{ data: [], backgroundColor: color, borderRadius: 6, borderSkipped: false, maxBarThickness: 30, categoryPercentage: 0.7, barPercentage: 0.9 }] },
    options: baseOptions(unit, opts),
  });
}

function buildCharts() {
  for (const k of Object.keys(charts)) { charts[k].destroy(); delete charts[k]; }
  if (typeof Chart === "undefined") return;
  Chart.defaults.font.family = theme.font;
  Chart.defaults.color = theme.mfg;
  charts.cpugpu = lineChart("ch-cpugpu", [{ key: "cpu", label: "s.cpu", color: theme.lime, fill: true }, { key: "gpu", label: "s.gpu", color: theme.lilac, dash: [5, 5] }], "%", { max: 100 });
  charts.temps = lineChart("ch-temps", [{ key: "t_cpu", label: "s.cpu", color: theme.lime }, { key: "t_gpu", label: "s.gpu", color: theme.lilac },
    { key: "t_soc", label: "s.soc", color: theme.s3 }], "°C", { compact: true });
  charts.svolt = barChart("ch-svolt", "V", theme.lime);
  charts.stemp = barChart("ch-stemp", "°C", theme.lilac);
  updateCharts();
}

function empty(chart, msg) {   // show "no data" over a chart instead of drawing anything made up
  const box = chart.canvas.parentElement, e = box.querySelector(".empty");
  e.hidden = !msg; e.textContent = msg || "";
  chart.canvas.style.visibility = msg ? "hidden" : "visible";
}

function updateCharts() {
  if (!charts.cpugpu) return;
  const r = st.robot;
  for (const name of ["cpugpu", "temps"]) {
    const c = charts[name], span = name === "cpugpu" ? ui.range : WINDOW_S;
    let any = false;
    for (const d of c.data.datasets) { d.data = (hist[d.key] || []).filter((p) => p.x >= now() - span); any = any || d.data.length > 0; }
    // the time axis grows from the first reading up to the chosen span, so short histories fill the chart
    const first = Math.min(...c.data.datasets.map((d) => (d.data[0] ? d.data[0].x : Infinity)));
    c.options.scales.x.min = Math.max(now() - span, Number.isFinite(first) ? first : now() - span);
    c.options.scales.x.max = now();
    empty(c, any ? "" : t("c.nodata"));
    c.update("none");
  }

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
setInterval(() => { if (st.robot) { updateCharts(); renderStrips(); } }, 2000);   // keep the time axis moving

// --------------------------------------------------------------- render
const last_ = { hero: "" };
function renderHero() {
  const s = st.server;
  const moving = (last("speed") || 0) > 0.01;
  const key = !st.open || !s ? "offline" : s.estop ? "stopped" : moving ? "driving" : "ready";
  const cls = key === "offline" ? "s is-warn" : key === "stopped" ? "s is-stop" : "s";
  const [a, b] = t("b.hero", { s: "\u0000" }).split("\u0000");
  const html = `${esc(a)}<span class="${cls}">${esc(t("b.st." + key))}</span>${esc(b || "")}`;
  if (html !== last_.hero) { $("hero-line").innerHTML = html; last_.hero = html; }
}

function render() {
  const s = st.server;
  applyStrings();
  for (const b of document.querySelectorAll(".lang button")) b.classList.toggle("on", b.dataset.lang === LANG);
  // top bar: which robot, and who drives
  text("backend", s ? t(s.backend === "ros" ? "be.ros" : "be.mock") : "...");
  $("p-robot").classList.toggle("is-live", !!s);
  text("opstat", !s || !s.operator ? t("op.none") : s.operator.name);
  $("p-op").classList.toggle("is-on", !!(s && s.operator));
  // the camera readouts and the link tile
  const linkTxt = !st.open ? t("link.offline") : st.rtt == null ? t("link.connecting") : `${Math.round(st.rtt)} ms`;
  text("link", linkTxt);
  const bars = !st.open || st.rtt == null ? 0 : st.rtt < 50 ? 4 : st.rtt < 100 ? 3 : st.rtt < LINK_SLOW_MS ? 2 : 1;
  [...$("sig").children].forEach((b, i) => b.classList.toggle("on", i < bars));
  text("uptime", s ? fmtUp(s.up_s) : "-");
  text("lk-up", s ? fmtUp(s.up_s) : "-");
  text("lk-robot", s ? t(s.backend === "ros" ? "be.ros" : "be.mock") : "-");
  text("devices", s ? s.clients : "-");
  text("v-rtt", st.open && st.rtt != null ? Math.round(st.rtt) : "-");
  const live = st.open && st.rtt != null, slow = live && st.rtt >= LINK_SLOW_MS;
  $("link-t").classList.toggle("is-live", live && !slow);
  $("link-chip").className = "chip" + (slow ? " is-warn" : live ? " is-live" : "");
  text("link-state", !st.open ? t("d.termoff") : slow ? t("b.slow") : live ? t("d.termlive") : t("link.connecting"));
  // who is driving, under the camera
  const stopped = !!s && s.estop;
  const moving = (last("speed") || 0) > 0.01;
  $("drive-chip").className = "chip" + (stopped ? " is-stop" : s && s.operator && moving ? " is-live" : "");
  text("drive-who", stopped ? t("estop.stopped") : !s || !s.operator ? t("op.none") : s.operator.name);

  $("estop").classList.toggle("on", stopped);
  text("estop-label", stopped ? t("estop.stopped") : t("estop"));
  const banner = $("banner");
  if (!st.open) { banner.hidden = false; banner.className = "banner"; banner.textContent = t("banner.offline"); }
  else if (stopped) {
    banner.hidden = false; banner.className = "banner stop";
    banner.textContent = t("banner.estop", { by: s.estop_by ? t("banner.estop.by", { name: s.estop_by }) : "" }) + t("banner.estop.viewer");
  } else banner.hidden = true;
  text("ver", s ? `HAEBOT Robot v${s.version}` : "");
  renderHero();
  renderNumbers();
  if (Date.now() - lastStrips > 1000) renderStrips();
  camButtons();
  renderGuard();
  drawLidar();
}

function renderLists() {
  const r = st.robot;
  if (!r) return;
  const names = { right_follower: ["arm.right", "arm.follower"], left_follower: ["arm.left", "arm.follower"],
    right_leader: ["arm.right", "arm.leader"], left_leader: ["arm.left", "arm.leader"], base: ["sec.base", null] };
  const b = (r.health && r.health.boards) || {};
  const rows = [`<tr><th>${esc(t("d.board"))}</th><th>${esc(t("d.volt"))}</th><th>${esc(t("d.temp2"))}</th><th>${esc(t("d.ma"))}</th></tr>`];
  if (r.mock) rows.push(`<tr><td colspan="4" class="why">${esc(t("d.mockservos"))}</td></tr>`);
  for (const [k, [a, c]] of Object.entries(names)) {
    const x = b[k];
    if (!x) continue;
    const label = t(a) + (c ? " " + t(c) : "");
    if (x.state !== "ok") {
      const why = x.state === "missing" ? t("arm.missing") : x.state === "busy" ? t("h.inuse", { job: x.by }) : x.state === "silent" ? t("arm.nopower") : (x.msg || x.state);
      rows.push(`<tr><td>${esc(label)}</td><td colspan="3" class="why">${esc(why)}</td></tr>`);
      continue;
    }
    const ma = Object.values(x.servos || {}).reduce((sum, v) => sum + (v.ma || 0), 0);
    rows.push(`<tr><td>${esc(label)} <span class="mute">${x.n}/${x.of}</span></td><td class="n"><b>${x.vmin ?? "-"}</b> V</td><td class="n">${x.tmax ?? "-"} °C</td><td class="n">${Math.round(ma)} mA${x.alarm ? " · ALARM" : ""}</td></tr>`);
  }
  $("servos").innerHTML = rows.join("");

  const J = r.jobs || {};
  const progs = [["base", "sec.base"], ["teleop_right", "d.tele_r"], ["teleop_left", "d.tele_l"], ["arm_right", "d.pose_r"],
    ["arm_left", "d.pose_l"], ["bridge", "leader.title"], ["skill", "sec.skills"], ["voice", "sec.voice"], ["wizard", "eng.wizard"]];
  $("programs").innerHTML = progs.map(([k, label]) => {
    const j = J[k];
    const stt = !j ? `<span class="stt">${esc(t("d.never"))}</span>` : j.running
      ? `<span class="stt run">${esc(t("d.running"))} ${esc(fmtUp(Math.round(Date.now() / 1000 - j.started)))}</span>`
      : `<span class="stt">${esc(t("d.stopped"))}${j.code ? ` (${esc(j.code)})` : ""}</span>`;
    return `<li><span class="nm">${esc(j ? j.label : t(label))}</span>${stt}</li>`;
  }).join("");

  text("voice", (r.voice && r.voice.text) || "-");
  const ev = (r.events || []).slice().reverse();
  $("log").innerHTML = ev.length ? ev.map((e) => `<li><span class="who" aria-hidden="true">${esc(String(e.who || "?").trim().charAt(0) || "?")}</span>` +
    `<div class="what"><p>${esc(e.text)}</p><p>${esc(e.who)}</p></div><time>${timeTick(e.ts)}</time></li>`).join("")
    : `<li class="none">${esc(t("log.empty"))}</li>`;
}

// ---------------------------------------------------------------- cameras
/* The three camera tiles (main camera, left hand, right hand). The pause button on the main camera
   stops every stream, so the cameras use no Wi-Fi; the choice is remembered. */
function camButtons() {
  const tg = $("cams-toggle"), label = t(ui.cams ? "b.cams.pause" : "b.cams.play");
  tg.title = label; tg.setAttribute("aria-label", label);
  tg.firstElementChild.className = ui.cams ? "ri-pause-line" : "ri-play-line";
  const fb = $("cam-full");
  fb.hidden = !document.fullscreenEnabled;
  fb.setAttribute("aria-label", t("b.full"));
  fb.firstElementChild.className = document.fullscreenElement ? "ri-fullscreen-exit-line" : "ri-fullscreen-line";
}
$("cams-toggle").addEventListener("click", () => { ui.cams = !ui.cams; remember("kiro-live", ui.cams ? "1" : "0"); camButtons(); renderCams(); });
$("cam-full").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else $("cam-main").requestFullscreen().catch(() => { /* the browser said no */ });
});
document.addEventListener("fullscreenchange", camButtons);

function renderCams() {
  const cams = (st.robot && st.robot.cams) || [];
  for (const box of document.querySelectorAll(".cam[data-cam]")) {
    const key = box.dataset.cam, c = cams.find((x) => x.key === key);
    const im = box.querySelector(".cam-img"), em = box.querySelector(".empty"), msg = box.querySelector(".msg");
    box.querySelector(".qauto").textContent = c && c.auto ? `${t("cam.auto")} ${c.auto}` : "";
    const why = !ui.cams ? t("b.cams.off") : !c || !c.present ? t("cam.notconnected") : c.paused_by ? t("cam.inuse") : c.error ? t("cam.busy") : "";
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

// --------------------------------------------------------------- terminal
/* The server log, live: the same lines the server prints in its own terminal. A line that
   repeats right away is counted (x12) instead of filling the window. */
const term = { n: 0, last: null };
const termTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
const termSrc = (name) => (name === "kiro" ? "server" : name.replace(/^kiro\./, ""));

function termLive() {
  $("term-live").className = "chip" + (st.open ? " is-live" : "");
  text("term-live-txt", st.open ? t("d.termlive") : t("d.termoff"));
  $("term").setAttribute("aria-label", t("d.term"));
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
      term.last.el.querySelector(".rep").textContent = `×${term.last.count}`;
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

  g.strokeStyle = T.line2; g.lineWidth = 1; g.setLineDash([2, 5]);
  for (let r = 1; r * S < Math.max(w, h); r++) { g.beginPath(); g.arc(w / 2, h / 2, r * S, 0, 2 * Math.PI); g.stroke(); }
  g.setLineDash([]);
  g.fillStyle = T.mfg; g.font = "500 11px " + T.mono;
  for (let r = 1; r <= 3; r++) g.fillText(`${r} m`, w / 2 + 5, h / 2 - r * S - 5);

  const lt = trail[trail.length - 1];
  if (lt && Math.hypot(o.x - lt.x, o.y - lt.y) > 1.5) trail.length = 0;
  if (!lt || Math.hypot(o.x - lt.x, o.y - lt.y) > 0.02) { trail.push({ x: o.x, y: o.y }); if (trail.length > 2000) trail.shift(); }
  if (trail.length > 1) {
    g.strokeStyle = T.lime; g.lineWidth = 2; g.lineJoin = "round"; g.beginPath();
    trail.forEach((p, i) => (i ? g.lineTo(sx(p.y), sy(p.x)) : g.moveTo(sx(p.y), sy(p.x))));
    g.stroke();
  }

  g.save(); g.translate(w / 2, h / 2); g.rotate(-o.th);
  const sc = st.scan && performance.now() - st.scan.at < 1000 ? st.scan : null;
  text("scan-src", sc ? (sc.src === "mock" ? t("scan.mock") : t("scan.live")) : `· ${t("d.nolidar")}`);
  if (sc) {
    g.fillStyle = T.fg;
    for (let i = 0; i < sc.xy.length; i += 2) g.fillRect(-sc.xy[i + 1] / 100 * S - 1.5, -sc.xy[i] / 100 * S - 1.5, 3, 3);
    const n = nearest();
    if (n) {
      g.strokeStyle = T.lilac; g.lineWidth = 1.5; g.setLineDash([4, 4]);
      g.beginPath(); g.moveTo(0, 0); g.lineTo(-Math.sin(n.a) * n.d * S, -Math.cos(n.a) * n.d * S); g.stroke(); g.setLineDash([]);
    }
  }
  const L = 0.45 * S, W = 0.35 * S;
  g.fillStyle = T.card; g.strokeStyle = s && s.estop ? T.stop : T.lime; g.lineWidth = 2;
  g.beginPath(); if (g.roundRect) g.roundRect(-W / 2, -L / 2, W, L, 5); else g.rect(-W / 2, -L / 2, W, L); g.fill(); g.stroke();
  g.fillStyle = g.strokeStyle; g.beginPath(); g.moveTo(0, -L / 2 + 4); g.lineTo(-7, -L / 2 + 15); g.lineTo(7, -L / 2 + 15); g.closePath(); g.fill();
  g.restore();
}
window.addEventListener("resize", drawLidar);
if (typeof ResizeObserver !== "undefined") new ResizeObserver(drawLidar).observe($("lidar").parentElement);

buildMeter();
buildCharts();
render();
renderStrips();
renderGuard();
renderCams();
termLive();
connect();
