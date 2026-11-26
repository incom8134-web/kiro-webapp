"use strict";
/*
 * Battery Management. The header percent comes from the same reading as the gauge.
 * Voltage and current for the chart are kept in this browser for five minutes.
 * Percent, health and cycles are estimates; the chip does not measure them.
 */
const $ = (id) => document.getElementById(id);
const esc = (s) => Kiro.esc(s);
const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

const st = { open: false, server: null, robot: null, rtt: null, batt: null };
const hist = [];
let theme = {};
let chart = null;
let link = null;

const TILES = [
  { id: "voltage", label: "bat.voltage", unit: "V", digits: 2, pick: (b) => b.voltage_v },
  { id: "current", label: "bat.current", unit: "A", digits: 2, pick: (b) => b.current_a },
  { id: "power", label: "bat.power", unit: "W", digits: 1, pick: (b) => b.power_w },
  { id: "temp", label: "bat.temp", unit: "°C", digits: 1, pick: (b) => b.temperature_c },
  { id: "energy", label: "bat.energy", unit: "Wh", digits: 2, pick: (b) => b.energy_wh, sub: () => t("bat.since") },
  { id: "charge", label: "bat.charge", unit: "Ah", digits: 3, pick: (b) => b.charge_ah, sub: () => t("bat.since") },
  { id: "time", label: "bat.time", pick: (b) => b.time_remaining_min, text: (v) => fmtMin(v) },
  { id: "cell", label: "bat.cell", unit: "V", digits: 3, pick: (b) => b.cell_voltage_v, sub: (b) => chemLine(b) },
  { id: "health", label: "bat.health", unit: "%", digits: 0, est: true, pick: (b) => b.health_pct,
    sub: (b) => b && b.health_pct == null ? t("bat.health.pending") : "" },
  { id: "cycles", label: "bat.cycles", digits: 1, est: true, pick: (b) => b.cycles },
];

function readTheme() {
  theme = {
    ink: css("--ink", "#ededf0"), ink2: css("--ink-2", "#a8a8b3"), mute: css("--mute", "#82828c"),
    line: css("--line", "#222227"), line2: css("--line-2", "#2e2e35"), panel: css("--panel", "#0e0e11"),
    font: css("--font", "system-ui"),
    series: css("--series", "#3987e5,#d95926").split(",").map((x) => x.trim()),
  };
}
readTheme();

function fmtMin(m) {
  if (m == null || !isFinite(m)) return "—";
  if (m < 1) return t("bat.under1");
  const total = Math.round(m);
  const h = Math.floor(total / 60), min = total % 60;
  return h ? t("bat.hours", { h, m: min }) : t("bat.mins", { n: total });
}

function chemLine(b) {
  if (!b || !b.chemistry) return "";
  const name = t("bat.chem." + b.chemistry);
  return name + (b.cells ? " · " + b.cells : "");
}

function headerBattery() {
  if (st.batt) return { percent: st.batt.percent, charging: st.batt.charging, source: st.batt.source };
  return st.robot && st.robot.battery;
}

function buildTiles() {
  $("bat-tiles").innerHTML = TILES.map((tl) => `<div class="tile">
    <span class="k">${esc(t(tl.label))}${tl.est ? `<em class="est">${esc(t("bat.estimated"))}</em>` : ""}</span>
    <div class="num"><b id="bv-${tl.id}">—</b>${tl.unit ? `<i>${esc(tl.unit)}</i>` : ""}</div>
    <span class="sub" id="bs-${tl.id}"></span></div>`).join("");
}

function fillTiles(b) {
  for (const tl of TILES) {
    const el = $("bv-" + tl.id);
    if (!el) continue;
    const v = b ? tl.pick(b) : null;
    el.textContent = tl.text ? tl.text(v) : (v == null || !isFinite(v) ? "—" : Number(v).toFixed(tl.digits));
    const sub = $("bs-" + tl.id);
    if (sub) sub.textContent = tl.sub && b ? tl.sub(b) : "";
  }
}

function chargeState(b) {
  if (!b || b.current_a == null || b.charging == null) return t("bat.unknown");
  if (b.charging) return t("bat.charging");
  if (Math.abs(b.current_a) <= 0.05) return t("bat.resting");
  return t("bat.discharging");
}

function renderGauge(b) {
  const pct = b && typeof b.percent === "number" ? b.percent : null;
  const g = $("gauge");
  g.style.setProperty("--pct", pct == null ? "0" : String(Math.max(0, Math.min(100, pct))));
  g.dataset.st = pct == null ? "none" : b.charging ? "charge" : pct <= 20 ? "low" : "ok";
  $("g-pct").textContent = pct == null ? "—" : Math.round(pct) + "%";
  const state = chargeState(b);
  $("g-state").textContent = state;
  g.setAttribute("aria-label", (pct == null ? t("bat.unknown") : Math.round(pct) + "%") + ", " + state);
}

function renderSource(b) {
  const src = b && (b.source === "ina228" || b.source === "mock") ? b.source : "unavailable";
  const chip = $("bat-source");
  chip.textContent = t("bat.source." + src);
  chip.className = "chip " + (src === "ina228" ? "pass" : src === "mock" ? "confirm" : "fail");
  $("bat-sub").textContent = t(src === "ina228" ? "bat.note.live" : src === "mock" ? "bat.note.mock" : "bat.note.none");
  if (b && b.chemistry) {
    $("bat-using").textContent = t("bat.using", {
      chem: t("bat.chem." + b.chemistry), cells: b.cells, ah: b.capacity_ah,
    });
  }
}

function renderAlerts(b) {
  const items = (b && b.alerts) || [];
  const list = $("bat-alerts");
  if (!items.length) {
    list.innerHTML = `<li class="mute">${esc(t("bat.alerts.none"))}</li>`;
    return;
  }
  list.innerHTML = items.map((code) => {
    const key = "bat.alert." + code;
    const label = t(key);
    return `<li>${esc(label === key ? code : label)}</li>`;
  }).join("");
}

function renderBatt() {
  const b = st.batt;
  renderGauge(b);
  fillTiles(b);
  renderSource(b);
  renderAlerts(b);
  updateChart();
}

function pushHist(b) {
  if (!b || (b.voltage_v == null && b.current_a == null)) return;
  const x = Date.now() / 1000;
  hist.push({ x, v: b.voltage_v, a: b.current_a });
  const cut = x - 300;
  while (hist.length && hist[0].x < cut) hist.shift();
}

const timeTick = (v) => new Date(v * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function buildChart() {
  if (chart) { chart.destroy(); chart = null; }
  if (typeof Chart === "undefined") return;
  Chart.defaults.font.family = theme.font;
  Chart.defaults.color = theme.mute;
  const grid = { color: theme.line, drawTicks: false };
  const ticks = { color: theme.mute, padding: 6, font: { size: 11 } };
  chart = new Chart($("ch-batt"), {
    type: "line",
    data: { datasets: [
      { label: t("bat.voltage"), yAxisID: "y", data: [], borderColor: theme.series[0], backgroundColor: theme.series[0] + "22",
        borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25, fill: false },
      { label: t("bat.current"), yAxisID: "y1", data: [], borderColor: theme.series[1], backgroundColor: theme.series[1] + "22",
        borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25, fill: false },
    ] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: true, position: "top", align: "end", labels: { color: theme.ink2, boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "rectRounded", font: { size: 11 } } },
        tooltip: {
          backgroundColor: theme.panel, titleColor: theme.ink, bodyColor: theme.ink2, borderColor: theme.line2, borderWidth: 1, padding: 10,
          callbacks: { title: (items) => items[0] ? timeTick(items[0].parsed.x) : "" },
        },
      },
      scales: {
        x: { type: "linear", grid, border: { display: false }, ticks: { ...ticks, maxTicksLimit: 5, callback: timeTick } },
        y: { position: "left", grid, border: { display: false }, ticks, title: { display: true, text: "V", color: theme.mute } },
        y1: { position: "right", grid: { drawOnChartArea: false }, border: { display: false }, ticks, title: { display: true, text: "A", color: theme.mute } },
      },
    },
  });
  updateChart();
}

function updateChart() {
  if (!chart) return;
  const now = Date.now() / 1000;
  chart.data.datasets[0].data = hist.filter((p) => p.v != null).map((p) => ({ x: p.x, y: p.v }));
  chart.data.datasets[1].data = hist.filter((p) => p.a != null).map((p) => ({ x: p.x, y: p.a }));
  // Keep five minutes, but while the page is young zoom to the samples so the line is readable.
  const span = hist.length > 1 ? Math.max(8, now - hist[0].x) : 8;
  chart.options.scales.x.min = now - Math.min(300, span);
  chart.options.scales.x.max = now;
  chart.update();
}

function renderTop() {
  const s = st.server;
  const me = !!(s && s.you && s.you.role === "operator");
  applyStrings();
  document.title = t("title.battery");
  for (const b of document.querySelectorAll(".lang button")) b.classList.toggle("on", b.dataset.lang === LANG);
  $("backend").textContent = s ? t(s.backend === "ros" ? "be.ros" : "be.mock") : "...";
  const linkEl = $("link");
  if (!st.open) { linkEl.textContent = t("link.offline"); linkEl.className = ""; }
  else if (st.rtt == null) { linkEl.textContent = t("link.connecting"); linkEl.className = ""; }
  else { linkEl.textContent = Math.round(st.rtt) + " ms"; linkEl.className = st.rtt < 150 ? "accent" : ""; }
  const op = $("opstat");
  op.textContent = !s || !s.operator ? t("op.none") : me ? t("op.you") : t("op.other", { name: s.operator.name });
  op.className = me ? "accent" : "";
  Kiro.renderBattery(headerBattery());
  const stopped = !!(s && s.estop);
  $("estop").classList.toggle("on", stopped);
  $("estop-label").textContent = stopped ? t("estop.stopped") : t("estop");
  $("estop").setAttribute("aria-pressed", stopped ? "true" : "false");
  $("estop-release").hidden = !(stopped && me);
  const banner = $("banner");
  const fold = st.robot && st.robot.estop_fold;
  const foldText = fold ? " " + t(fold === "pending" ? "banner.fold.pending" : "banner.fold.running") : "";
  if (!st.open) { banner.hidden = false; banner.className = "banner"; banner.textContent = t("banner.offline"); }
  else if (stopped) {
    banner.hidden = false; banner.className = "banner stop";
    banner.textContent = t("banner.estop", { by: s.estop_by ? t("banner.estop.by", { name: s.estop_by }) : "" })
      + t(me ? "banner.estop.op" : "banner.estop.viewer") + foldText;
  } else if (fold) { banner.hidden = false; banner.className = "banner"; banner.textContent = foldText.trim(); }
  else banner.hidden = true;
  $("ver").textContent = s ? t("foot.ver", { v: s.version, n: t(s.clients === 1 ? "devices.one" : "devices.many", { n: s.clients }) }) : "";
}

const send = (o) => { if (link) link.send(o); };
function connect() {
  link = Kiro.connect({
    name: () => Kiro.deviceName(),
    onOpen() { st.open = true; renderTop(); },
    onClose() { st.open = false; st.server = null; st.rtt = null; renderTop(); },
    onMessage(m) {
      if (m.t === "state") st.server = m;
      else if (m.t === "robot") st.robot = m;
      else if (m.t === "pong") st.rtt = performance.now() - m.ts;
      if (m.t === "state" || m.t === "robot") renderTop();
    },
  });
}

async function pull() {
  let r;
  try {
    r = await fetch("/api/battery", { credentials: "same-origin" });
  } catch (e) {
    return;
  }
  if (r.status === 401) {
    const box = $("login");
    if (box) box.hidden = false;
    return;
  }
  if (!r.ok) return;
  let body;
  try { body = await r.json(); } catch (e) { return; }
  st.batt = body;
  pushHist(body);
  renderBatt();
  renderTop();
}

Kiro.bindEstop($("estop"), $("estop-release"), send, () => !!(st.server && st.server.estop), () => !!(st.server && st.server.you && st.server.you.role === "operator"));
window.addEventListener("keydown", (e) => { if ((e.code === "Escape" || e.key === "Escape") && !Kiro.typing(e)) send({ t: "estop", on: true }); });
for (const b of document.querySelectorAll(".lang button")) {
  b.addEventListener("click", () => { setLang(b.dataset.lang); buildTiles(); buildChart(); renderTop(); renderBatt(); });
}
$("theme-btn").addEventListener("click", () => toggleTheme());
window.addEventListener("kiro-theme", () => { readTheme(); themeIcon($("theme-btn")); buildChart(); });
themeIcon($("theme-btn"));

let pollTimer = 0;
function startPoll() {
  if (pollTimer) return;
  pull();
  pollTimer = setInterval(pull, 1000);
}

buildTiles();
buildChart();
renderTop();
renderBatt();
connect();
Kiro.bindLogin({
  onReady() { startPoll(); },
  onLogin() { startPoll(); if (link) link.reconnect(); },
});
