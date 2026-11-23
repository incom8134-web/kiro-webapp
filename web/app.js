"use strict";
/*
 * HAEBOT Robot command center - browser side.
 *
 * 1. Keeps one WebSocket open to the robot server.
 * 2. Reads three inputs (gamepad, on-screen sticks, keyboard) and picks one.
 * 3. While this device is the operator, sends the drive command 20 times per
 *    second. That message is also the heartbeat: if it stops, the robot stops.
 * 4. Cameras, arms (with the leader arm on this laptop), voice, skills, health.
 * The engineer tools (servo tester, check wizard, guard distance) are on the Studio page.
 */

const $ = (id) => document.getElementById(id);
const SEND_MS = 50;      // 20 messages per second
const DEADZONE = 0.12;   // ignore tiny gamepad stick noise
const PX_PER_M = 36;     // map scale: about 3.5 m each way in the inset
const HOLD_MS = 2000;    // hold-to-confirm for dangerous buttons
const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
const ACCENT = css("--accent", "#34d07f");
let ACC = ACCENT;
let C = {};
function readColors() {   // canvas colours follow the theme
  ACC = css("--accent", "#38bdf8");
  C = { ink: css("--ink", "#e8eef8"), line: css("--line", "#1f2b47"), panel: css("--panel", "#0f172a"), stop: css("--stop", "#ff3b30") };
}
readColors();

/* A button with an icon and a label: change both without losing the icon. */
function setBtn(el, text, icon) {
  const span = el.querySelector("span:not(.fill)"), i = el.querySelector("i");
  if (span) span.textContent = text;
  if (i && icon) i.className = icon;
}

const state = { ws: null, open: false, server: null, robot: null, rtt: null, scan: null, cams: {} };
const input = {
  keys: new Set(),
  move: { x: 0, y: 0, active: false },   // left on-screen stick
  turn: { x: 0, y: 0, active: false },   // right on-screen stick
  pad: null,
  padBack: false,
  talking: false,
};
const trail = [];
let noteTimer = null;

const isOperator = () => !!state.server && state.server.you.role === "operator";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const jobs = () => (state.robot && state.robot.jobs) || {};
const jobRunning = (name) => !!(jobs()[name] && jobs()[name].running);

// ---------------------------------------------------------------- connection
function deviceName() {
  try {
    let n = sessionStorage.getItem("kiro-name");
    if (!n) {
      const kind = /Mobi|Android|iPhone|iPad/.test(navigator.userAgent) ? "Phone" : "Laptop";
      n = kind + "-" + Math.random().toString(36).slice(2, 5).toUpperCase();
      sessionStorage.setItem("kiro-name", n);
    }
    return n;
  } catch (e) {
    return "Device";
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?name=${encodeURIComponent(deviceName())}`);
  state.ws = ws;
  ws.onopen = () => { state.open = true; render(); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === "state") { state.server = m; render(); }
    else if (m.t === "robot") { state.robot = m; renderRobot(); }
    else if (m.t === "scan") { m.at = performance.now(); state.scan = m; }
    else if (m.t === "pong") { state.rtt = performance.now() - m.ts; renderLink(); }
    else if (m.t === "note") showNote(m.msg, !m.ok);
  };
  ws.onclose = () => {
    state.open = false; state.server = null; state.rtt = null;
    if (leader.on) leader.paused = true;   // Wi-Fi lost: keep the leader arm open; the robot's bridge holds the arm (test R5)
    dropInputs(); render();
    setTimeout(connect, 1000);
  };
  ws.onerror = () => ws.close();
}

function send(obj) {
  if (state.open && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(obj));
}

// -------------------------------------------------------------------- inputs
function dz(v) {
  return Math.abs(v) < DEADZONE ? 0 : (v - Math.sign(v) * DEADZONE) / (1 - DEADZONE);
}

function readPad() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  for (const p of pads) if (p && p.connected) return p;
  return null;
}

const pressed = (p, i) => !!(p && p.buttons[i] && p.buttons[i].pressed);

/* Keyboard: holding a movement key IS the dead-man. Letting go of all keys stops. */
const KEYMAP = {
  KeyW: ["x", 1], ArrowUp: ["x", 1], KeyS: ["x", -1], ArrowDown: ["x", -1],
  KeyA: ["y", 1], KeyD: ["y", -1],
  KeyQ: ["z", 1], ArrowLeft: ["z", 1], KeyE: ["z", -1], ArrowRight: ["z", -1],
};
const LETTER = { w: "KeyW", a: "KeyA", s: "KeyS", d: "KeyD", q: "KeyQ", e: "KeyE", v: "KeyV" };
const keyCode = (e) => (e.code && e.code !== "Unidentified" ? e.code : LETTER[(e.key || "").toLowerCase()] || "");

/* Pick ONE input. Order: real gamepad (LB held) > on-screen sticks > keyboard.
 * x = forward, y = left, z = turn left, each from -1 to 1. en = dead-man is held. */
function currentInput() {
  const turboUi = $("turbo").checked;
  const p = readPad();
  input.pad = p;
  if (pressed(p, 4)) {
    return { src: "gamepad", en: true, turbo: turboUi || pressed(p, 5),
             x: -dz(p.axes[1] || 0), y: -dz(p.axes[0] || 0), z: -dz(p.axes[2] || 0) };
  }
  if (input.move.active || input.turn.active) {
    return { src: "screen", en: true, turbo: turboUi,
             x: -input.move.y, y: -input.move.x, z: -input.turn.x };
  }
  const v = { x: 0, y: 0, z: 0 };
  let held = input.keys.has("Space");
  for (const k of input.keys) {
    const m = KEYMAP[k];
    if (m) { v[m[0]] += m[1]; held = true; }
  }
  if (held) {
    const cl = (a) => Math.max(-1, Math.min(1, a));
    return { src: "keyboard", en: true, turbo: turboUi || input.keys.has("ShiftLeft") || input.keys.has("ShiftRight"),
             x: cl(v.x), y: cl(v.y), z: cl(v.z) };
  }
  return { src: "none", en: false, turbo: false, x: 0, y: 0, z: 0 };
}

function setupStick(el, target, horizontalOnly) {
  const knob = el.querySelector(".knob");
  let pid = null;
  const set = (cx, cy) => {
    const r = el.getBoundingClientRect();
    const lim = r.width / 2 - 22;
    let dx = cx - (r.left + r.width / 2);
    let dy = horizontalOnly ? 0 : cy - (r.top + r.height / 2);
    const d = Math.hypot(dx, dy);
    if (d > lim) { dx *= lim / d; dy *= lim / d; }
    knob.style.transform = `translate(${dx}px, ${dy}px)`;
    target.x = dx / lim; target.y = dy / lim;
  };
  const end = () => {
    pid = null; target.x = 0; target.y = 0; target.active = false;
    knob.style.transform = ""; el.classList.remove("active");
  };
  el.addEventListener("pointerdown", (e) => {
    if (!isOperator()) return;
    pid = e.pointerId; el.setPointerCapture(pid);
    target.active = true; el.classList.add("active");
    set(e.clientX, e.clientY); e.preventDefault();
  });
  el.addEventListener("pointermove", (e) => { if (e.pointerId === pid) set(e.clientX, e.clientY); });
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
    el.addEventListener(name, (e) => { if (e.pointerId === pid) end(); });
  }
  return end;
}
const endMove = setupStick($("stick-move"), input.move, false);
const endTurn = setupStick($("stick-turn"), input.turn, true);

/* Hold-to-talk: on-screen button, V key, or Y on the gamepad. */
function setTalk(on) {
  if (on === input.talking) return;
  input.talking = on;
  $("talk").classList.toggle("on", on);
  send({ t: "talk", on });
}

/* Anything that could leave the robot moving by mistake clears every input. */
function dropInputs() { input.keys.clear(); endMove(); endTurn(); setTalk(false); }
window.addEventListener("blur", dropInputs);
document.addEventListener("visibilitychange", () => { if (document.hidden) dropInputs(); });

const typing = (e) => {
  const el = e.target;
  return el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement ||
    (el instanceof HTMLInputElement && ["password", "text", "number"].includes(el.type));
};
window.addEventListener("keydown", (e) => {
  if (e.code === "Escape" || e.key === "Escape") { send({ t: "estop", on: true }); return; }
  if (typing(e)) return;
  const code = keyCode(e);
  if (code === "KeyV" && !e.repeat && isOperator()) setTalk(true);
  if (KEYMAP[code] || code === "Space" || code === "ShiftLeft" || code === "ShiftRight") {
    input.keys.add(code);
    if (isOperator() && (KEYMAP[code] || code === "Space")) e.preventDefault(); // no page scroll, no button press
  }
});
window.addEventListener("keyup", (e) => {
  const code = keyCode(e);
  input.keys.delete(code);
  if (code === "KeyV") setTalk(false);
});
// A focused button would react to Space or arrows. Remove focus after every click.
document.addEventListener("click", (e) => {
  const el = e.target.closest("button, summary, input[type=checkbox]");
  if (el) el.blur();
});
window.addEventListener("gamepadconnected", render);
window.addEventListener("gamepaddisconnected", render);

// The send loop. Browsers slow this down in a hidden tab, which stops the robot: good.
let padY = false;
setInterval(() => {
  const cur = currentInput();
  const back = pressed(input.pad, 8);          // Back / Select button = E-STOP
  if (back && !input.padBack) send({ t: "estop", on: true });
  input.padBack = back;
  const y = pressed(input.pad, 3);             // Y = hold to talk
  if (y !== padY && isOperator()) setTalk(y);
  padY = y;
  if (isOperator()) send({ t: "drive", x: cur.x, y: cur.y, z: cur.z, en: cur.en, turbo: cur.turbo, src: cur.src });
}, SEND_MS);
setInterval(() => send({ t: "ping", ts: performance.now() }), 1000);

// ------------------------------------------------------------------- buttons
$("take-box").addEventListener("submit", (e) => {
  e.preventDefault();
  const s = state.server;
  let force = false;
  if (s && s.operator && s.you.role !== "operator") {
    force = confirm(t("takeover", { name: s.operator.name }));
    if (!force) return;
  }
  send({ t: "take", pin: $("pin").value, force });
  $("pin").value = ""; $("pin").blur();
});
$("release").addEventListener("click", () => send({ t: "release" }));
$("estop").addEventListener("click", () => {
  const on = !(state.server && state.server.estop);
  send({ t: "estop", on });
});
for (const b of document.querySelectorAll(".lang button")) {
  b.addEventListener("click", () => { setLang(b.dataset.lang); render(); });
}
$("turbo").addEventListener("change", () => $("turbo").blur());
$("theme-btn").addEventListener("click", () => toggleTheme());
window.addEventListener("kiro-theme", () => { readColors(); themeIcon($("theme-btn")); drawMap(); });
themeIcon($("theme-btn"));

const talk = $("talk");
talk.addEventListener("pointerdown", (e) => { if (!isOperator()) return; talk.setPointerCapture(e.pointerId); setTalk(true); e.preventDefault(); });
for (const n of ["pointerup", "pointercancel", "lostpointercapture"]) talk.addEventListener(n, () => setTalk(false));
talk.addEventListener("contextmenu", (e) => e.preventDefault());

$("base-btn").addEventListener("click", () => send({ t: "base", on: !jobRunning("base") }));   // guard distance: set in the Studio
$("guard-sw").addEventListener("change", () => send({ t: "guard", on: $("guard-sw").checked }));   // the server's obstacle stop
// Speech language: the operator's choice (remembered in this browser), not the page language
let voiceLang = (() => { try { return localStorage.getItem("kiro-voice-lang"); } catch (e) { return null; } })() || (LANG === "en" ? "en" : "ko");
// Step per command: "big" 0.5 m / 90 degrees, or "test" 0.2 m / 45 degrees as the voice test form asks
let voiceStep = (() => { try { return localStorage.getItem("kiro-voice-step"); } catch (e) { return null; } })() || "big";
$("voice-btn").addEventListener("click", () => send({ t: "voice", on: !jobRunning("voice"), lang: voiceLang, step: voiceStep }));
const voiceRestart = () => {
  const r = state.robot;
  if (jobRunning("voice") && r && (r.voice_lang !== voiceLang || r.voice_step !== voiceStep)) send({ t: "voice", on: true, lang: voiceLang, step: voiceStep });
  renderRobot();
};
for (const b of document.querySelectorAll("[data-vlang]")) b.addEventListener("click", () => {
  voiceLang = b.dataset.vlang;
  try { localStorage.setItem("kiro-voice-lang", voiceLang); } catch (e) { /* private window */ }
  voiceRestart();
});
for (const b of document.querySelectorAll("[data-vstep]")) b.addEventListener("click", () => {
  voiceStep = b.dataset.vstep;
  try { localStorage.setItem("kiro-voice-step", voiceStep); } catch (e) { /* private window */ }
  voiceRestart();
});
$("wave-btn").addEventListener("click", () => send({ t: "wave", side: "right" }));
$("stops-clear").addEventListener("click", () => send({ t: "stops_clear" }));
$("follow-btn").addEventListener("click", () => send({ t: "follow", on: !following() }));
const following = () => jobRunning("relay") && ((state.robot && state.robot.bridge) || {}).mode === "follow";
$("leader-btn").addEventListener("click", () => (leader.on ? leaderStop() : leaderStart()));

// Tabs in the dock: Drive, Arms, Voice, System. The last one used is remembered.
function showTab(name) {
  for (const b of document.querySelectorAll(".tabs [data-tab]")) b.classList.toggle("on", b.dataset.tab === name);
  for (const p of document.querySelectorAll(".panel[data-panel]")) p.classList.toggle("on", p.dataset.panel === name);
  try { localStorage.setItem("kiro-tab", name); } catch (e) { /* ignore */ }
}
for (const b of document.querySelectorAll(".tabs [data-tab]")) b.addEventListener("click", () => showTab(b.dataset.tab));
let startTab = "drive";
try { startTab = localStorage.getItem("kiro-tab") || "drive"; } catch (e) { /* ignore */ }
showTab(document.querySelector(`.panel[data-panel="${startTab}"]`) ? startTab : "drive");

/* A dot on a tab: green when something there runs, amber when something there failed. */
function tabDot(name, busy, alert) {
  const b = document.querySelector(`.tabs [data-tab="${name}"]`);
  if (!b) return;
  b.classList.toggle("busy", !!busy && !alert);
  b.classList.toggle("alert", !!alert);
}

// Folded sections remember whether they were open
for (const d of document.querySelectorAll("details.fold")) {
  try { d.open = localStorage.getItem("kiro-open-" + d.id) === "1"; } catch (e) { /* ignore */ }
  d.addEventListener("toggle", () => { try { localStorage.setItem("kiro-open-" + d.id, d.open ? "1" : "0"); } catch (e) { /* ignore */ } });
}

/* Hold a button for HOLD_MS to confirm. Used for shutdown, restart, torque release, servo writes. */
function holdButton(el, action) {
  let timer = null, start = 0, raf = 0;
  const fill = el.querySelector(".fill");
  const reset = () => { clearTimeout(timer); cancelAnimationFrame(raf); timer = null; if (fill) fill.style.width = "0"; };
  const step = () => { if (fill) fill.style.width = Math.min(100, (performance.now() - start) / HOLD_MS * 100) + "%"; raf = requestAnimationFrame(step); };
  el.addEventListener("pointerdown", (e) => {
    if (el.disabled) return;
    el.setPointerCapture(e.pointerId); start = performance.now(); step();
    timer = setTimeout(() => { reset(); action(); }, HOLD_MS);
  });
  for (const n of ["pointerup", "pointercancel", "lostpointercapture"]) el.addEventListener(n, reset);
  el.addEventListener("contextmenu", (e) => e.preventDefault());
}
holdButton($("shutdown"), () => send({ t: "system", action: "shutdown" }));
holdButton($("reboot"), () => send({ t: "system", action: "reboot" }));

function showNote(text, isError) {
  if (!text) return;
  const known = { "LEFT_BLOCKED": "arm.leftblocked", "TWO_ARM": "fm.noplay.both", "LEFT_ARM": "fm.noplay.left", "NO_FIT": "fm.noplay.other",
    "LIDAR_MISSING": "base.nolidar", "NO_MIC": "voice.nomic" };
  const n = $("note");
  n.textContent = known[text] ? t(known[text]) : text;
  n.className = "note" + (isError ? " err" : "");
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { n.textContent = ""; n.className = "note"; renderHintNote(); }, 5000);
}

function renderHintNote() {
  const s = state.server, n = $("note");
  if (!n.textContent && s && s.default_pin && s.you.role !== "operator") n.textContent = t("mockpin");
}

// -------------------------------------------------------------------- render
function renderLink() {
  const el = $("link");
  if (!state.open) { el.textContent = t("link.offline"); el.className = ""; return; }
  if (state.rtt == null) { el.textContent = t("link.connecting"); el.className = ""; return; }
  el.textContent = `${Math.round(state.rtt)} ms`;
  el.className = state.rtt < 150 ? "accent" : "";
}

function render() {
  const s = state.server;
  const me = isOperator();
  applyStrings();
  for (const b of document.querySelectorAll(".lang button")) b.classList.toggle("on", b.dataset.lang === LANG);
  renderLink();

  $("backend").textContent = s ? t(s.backend === "ros" ? "be.ros" : "be.mock") : "...";
  const op = $("opstat");
  op.textContent = !s || !s.operator ? t("op.none") : me ? t("op.you") : t("op.other", { name: s.operator.name });
  op.className = me ? "accent" : "";

  $("take-box").hidden = me;
  $("held-box").hidden = !me;
  $("take").disabled = !s;
  renderHintNote();

  const es = $("estop");
  const stopped = !!s && s.estop;
  es.classList.toggle("on", stopped);
  $("estop-label").textContent = stopped ? t(me ? "estop.release" : "estop.stopped") : t("estop");

  const banner = $("banner");
  const fold = state.robot && state.robot.estop_fold;   // after E-STOP the arms fold home by themselves
  const foldText = fold ? " " + t(fold === "pending" ? "banner.fold.pending" : "banner.fold.running") : "";
  if (!state.open) {
    banner.hidden = false; banner.className = "banner";
    banner.textContent = t("banner.offline");
  } else if (stopped) {
    banner.hidden = false; banner.className = "banner stop";
    banner.textContent = t("banner.estop", { by: s.estop_by ? t("banner.estop.by", { name: s.estop_by }) : "" }) +
      t(me ? "banner.estop.op" : "banner.estop.viewer") + foldText;
  } else if (fold) {
    banner.hidden = false; banner.className = "banner";
    banner.textContent = foldText.trim();
  } else {
    banner.hidden = true;
  }

  const cmd = s ? s.cmd : { vx: 0, vy: 0, wz: 0 };
  $("vx").textContent = cmd.vx.toFixed(2);
  $("vy").textContent = cmd.vy.toFixed(2);
  $("wz").textContent = cmd.wz.toFixed(2);

  // Obstacle stop of the server: on / off, and whether it is holding the robot right now
  const gd = (s && s.guard) || { on: false, lidar: false, block: [] };
  const held = gd.lost ? t("guard.lost")
    : gd.block && gd.block.length ? t("guard.held", { where: gd.block.map((d) => t("dir." + d)).join(", ") }) : "";
  $("guard-sw").checked = !!gd.on;
  $("guard-st").textContent = !s ? "" : !gd.on ? t("guard.off") : gd.lost ? held : !gd.lidar ? t("guard.nolidar") : held || t("guard.on", { cm: Math.round((gd.stop_m || 0) * 100) });
  $("guard-st").className = "st grow" + (!s ? "" : gd.on && gd.lidar && !held ? " live" : " warn");
  const rg = state.robot && state.robot.voice && state.robot.voice.guard;  // the old lidar guard on the robot (gamepad, voice, page)
  $("guard-note").textContent = !s ? "" : !gd.on ? t("guard.offnote") : gd.lost ? t("guard.lostnote") : !gd.lidar ? t("guard.nolidarnote")
    : rg && jobRunning("base") ? t("guard.robot", { st: rg }) : "";

  const hint = !me ? t("hint.viewer") : stopped ? t("hint.estop") : s.need_neutral ? t("hint.neutral")
    : held ? held
    : !jobRunning("base") && s.backend === "ros" ? t("hint.nobase")
    : state.robot && state.robot.wheels === "off" ? t("st.nowheels") : "";
  $("hint").textContent = hint;
  $("hint").hidden = !hint;
  $("hint").classList.toggle("warn", !!hint && me);
  $("stick-move").classList.toggle("off", !me);
  $("stick-turn").classList.toggle("off", !me);
  for (const el of document.querySelectorAll("[data-op]")) el.disabled = !me;

  const o = s && s.odom;
  $("pose").textContent = o ? t("pose", { x: o.x.toFixed(2), y: o.y.toFixed(2), th: Math.round(o.th * 180 / Math.PI) }) : "";
  $("ver").textContent = s ? `HAEBOT Robot v${s.version} · ${s.clients} device${s.clients === 1 ? "" : "s"}` : "";
  drawMap();
  renderRobot();
}

/* Stop meter: each stop with its test item and pass limit (robot_server/stopmeter.py), newest first. */
function renderStops(rows) {
  const tm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
  const num = (v, d = 2) => (v == null ? "-" : Number(v).toFixed(d));
  const body = rows.slice().reverse().map((x) => {
    const unit = x.meter === "arm" ? "°" : " cm";
    const ok = x.state === "measuring" ? t("stops.measuring") : x.state === "timeout" ? t("stops.timeout") : x.ok == null ? "" : x.ok ? "✓" : "✗";
    const cls = x.ok === true ? "go" : x.ok === false ? "no" : "";
    return `<tr><td>${tm(x.ts)}</td><td>${esc(x.test || "")}</td><td title="${esc(x.detail)}">${esc(t("stops.k." + x.kind))}</td>
      <td>${num(x.cmd_s)}</td><td>${num(x.still_s)}</td><td>${x.dist == null ? "-" : num(x.dist, 1) + unit}</td>
      <td>${x.creep ? num(x.creep, 1) + unit : "0"}</td><td class="${cls}">${esc(ok)}</td></tr>`;
  }).join("");
  const head = ["time", "test", "what", "cmd", "still", "dist", "creep", "ok"].map((k) => `<th>${esc(t("stops.h." + k))}</th>`).join("");
  const html = rows.length ? `<tr>${head}</tr>${body}` : `<tr><td class="mute">${esc(t("stops.none"))}</td></tr>`;
  const el = $("stops");
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
}

/* Why a job ended, shown only when it was a surprise (not when we stopped it): its USB board went away,
   something killed it (an arm then keeps its torque), or it failed (its last lines). */
function failTail(el, job) {
  const why = job && !job.running ? job.why : null;
  const failed = why === "USB_LOST" || why === "KILLED" || why === "ERROR";
  el.hidden = !failed;
  if (failed) {
    const head = t("why." + why, { name: job.label || job.name });
    el.textContent = why === "ERROR" ? head + "\n" + job.tail.slice(-5).join("\n") : head;
  }
  return failed;
}

function renderRobot() {
  const r = state.robot;
  if (!r) return;
  const me = isOperator();
  const s = state.server;
  const estop = !!(s && s.estop);
  const J = jobs();

  // Base
  const baseOn = jobRunning("base");
  setBtn($("base-btn"), t(baseOn ? "btn.stop" : "btn.start"), baseOn ? "ri-stop-fill" : "ri-play-fill");
  $("base-btn").classList.toggle("on", baseOn);
  const be = $("base-err");
  if (failTail(be, J.base)) be.textContent = t("base.exited", { code: J.base.code }) + "\n" + J.base.tail.slice(-5).join("\n");
  else if (baseOn && r.wheels === "off") { be.hidden = false; be.textContent = t("base.nowheels"); }
  $("base-st").textContent = baseOn ? (r.wheels === "off" ? t("st.nowheels") : r.wheels === "ok" ? t("st.on") : t("st.starting")) : "";
  if (be.hidden && baseOn && r.base_guard === "pass" && r.obstacle_stop) { be.hidden = false; be.textContent = t("base.pass"); }
  const rates = s && s.rates, br0 = $("base-rates");   // test R2: wheel odometry back to 20 Hz after a restart
  br0.hidden = !(baseOn && rates);
  if (!br0.hidden) br0.textContent = rates.odom_age != null && rates.odom_age > 1 ? t("base.noodom", { s: Math.round(rates.odom_age) })
    : t("base.rates", { odom: Math.round(rates.odom_hz), scan: Math.round(rates.scan_hz) });
  $("base-st").className = "st" + (baseOn && r.wheels === "ok" ? " live" : "");

  // Arms: right and left, each with teleop and the three poses
  const b = (r.health && r.health.boards) || {};
  const armCol = (side) => {
    const blocked = side === "left" && !r.left_arm_ok;
    const tele = jobRunning("teleop_" + side);
    const f = b[side + "_follower"];
    const off = !me || blocked ? "disabled" : "";
    const status = blocked ? t("arm.locked") : f && f.state === "missing" ? t("arm.missing")
      : f && f.state === "ok" ? `${f.vmin ?? "-"} V` : f && f.state === "silent" ? t("arm.nopower") : "";
    return `<div class="armcol">
      <div class="row"><b class="grow">${t("arm." + side)}</b><span class="small mute">${esc(status)}</span></div>
      <button class="btn wide${tele ? " on" : ""}" data-tele="${side}" ${off}>${t(tele ? "arm.teleop.off" : "arm.teleop.on")}</button>
      <div class="trio">
        <button class="btn" data-arm="${side}" data-act="fold" ${off}>${t("arm.fold")}</button>
        <button class="btn" data-arm="${side}" data-act="hold" ${off}>${t("arm.hold")}</button>
        <button class="btn hold" data-arm="${side}" data-act="release" ${me ? "" : "disabled"}><span class="fill"></span>${t("arm.release")}</button>
      </div></div>`;
  };
  const arms = $("arms");
  const html = armCol("right") + armCol("left");
  if (arms.dataset.html !== html) {
    arms.innerHTML = html; arms.dataset.html = html;
    for (const el of arms.querySelectorAll("[data-tele]")) el.addEventListener("click", () => send({ t: "teleop", side: el.dataset.tele, on: !jobRunning("teleop_" + el.dataset.tele) }));
    for (const el of arms.querySelectorAll("[data-act]")) {
      if (el.dataset.act === "release") holdButton(el, () => send({ t: "arm", side: el.dataset.arm, action: "release" }));
      else el.addEventListener("click", () => send({ t: "arm", side: el.dataset.arm, action: el.dataset.act }));
    }
  }
  const armJobs = ["teleop_right", "teleop_left", "arm_right", "arm_left", "bridge", "record"].map((n) => J[n]).filter(Boolean);
  const anyArm = armJobs.some((j) => j.running);
  $("arms-st").textContent = anyArm ? t("st.busy") : "";
  $("arms-st").className = "st" + (anyArm ? " live" : "");
  const lastArm = armJobs.sort((a, c) => (c.ended || 1e12) - (a.ended || 1e12) || c.started - a.started)[0];
  failTail($("arms-err"), lastArm);

  // Leader arm on this laptop, and the wave
  const br = r.bridge || {};
  if (leader.on && br.running && br.mode === "leader") leader.sawRunning = true;
  if (leader.on && estop) leaderStop(t("hint.estop"));
  else if (leader.on && leader.paused && me) {        // control taken back after a lost link: carry on from the arm's pose
    leader.paused = false; leader.sawRunning = false;
    send({ t: "leader", on: true, side: leader.side });
  } else if (leader.on && !leader.paused && (!me || (leader.sawRunning && !br.running))) leaderStop("");
  setBtn($("leader-btn"), t(leader.on ? "leader.stop" : "leader.use"), leader.on ? "ri-stop-fill" : "ri-usb-line");
  $("leader-btn").classList.toggle("on", leader.on);
  $("leader-st").textContent = leader.on && leader.paused ? t("leader.paused") : leader.on ? t("leader.live", { n: leader.ok, state: br.state || "..." })
    : !LeaderArm.supported() ? t(location.protocol === "https:" ? "leader.browser" : "leader.https", { url: `https://${location.hostname}:8443` })
    : t("leader.hint");
  const waving = !!(br.running && br.mode === "wave");
  const follows = following();
  $("wave-btn").disabled = !me || leader.on || waving || follows || estop;
  // One leader arm on the Jetson moves both arms (copy)
  setBtn($("follow-btn"), t(follows ? "follow.stop" : "follow.start"), follows ? "ri-stop-fill" : "ri-git-fork-line");
  $("follow-btn").classList.toggle("on", follows);
  $("follow-btn").disabled = !me || leader.on || (estop && !follows) || (!follows && !r.left_arm_ok);
  $("follow-st").textContent = follows ? t("follow.live") : !r.left_arm_ok ? t("follow.locked") : t("follow.hint");
  $("follow-st").className = "small " + (follows ? "accent" : !r.left_arm_ok ? "note err" : "mute");
  $("wave-btn").classList.toggle("on", waving);

  // Voice
  const vOn = jobRunning("voice");
  setBtn($("voice-btn"), t(vOn ? "btn.stop" : "btn.start"), vOn ? "ri-stop-fill" : "ri-play-fill");
  $("voice-btn").classList.toggle("on", vOn);
  $("voice-st").textContent = vOn ? (r.voice_lang || "ko").toUpperCase() : "";
  for (const b of document.querySelectorAll("[data-vstep]")) {
    b.classList.toggle("on", b.dataset.vstep === voiceStep);
    b.disabled = !me;
  }
  for (const b of document.querySelectorAll("[data-vlang]")) {
    b.classList.toggle("on", b.dataset.vlang === voiceLang);
    b.disabled = !me || (b.dataset.vlang === "en" && !(r.voice_models || []).some((m) => m.includes("-en-")));
  }
  $("voice-st").className = "st" + (vOn ? " live" : "");
  $("voice-on").hidden = !vOn;
  $("talk").disabled = !me || !vOn;
  $("voice-text").textContent = (r.voice && r.voice.text) || "-";
  // The voice node's own record, newest first: what it heard (with confidence) and what it did, with the time
  const vlog = ((r.voice && r.voice.log) || []).slice().reverse();
  const vhtml = vlog.map((e) => {
    const d = new Date(e.ts * 1000), cls = /^\[(실행|완료)\]/.test(e.text) ? "go" : /^\[(무시|중단|취소|오류)\]/.test(e.text) ? "no" : "";
    const tm = d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0").slice(0, 1);
    return `<li><time>${tm}</time><span class="${cls}">${esc(e.text)}</span></li>`;
  }).join("");
  renderStops(r.stops || []);
  const vl = $("voice-log");
  if (vl.dataset.html !== vhtml) { vl.innerHTML = vhtml; vl.dataset.html = vhtml; }
  const noEn = voiceLang === "en" && !(r.voice_models || []).some((m) => m.includes("-en"));
  $("voice-note").hidden = vOn || !noEn;
  $("voice-note").textContent = noEn ? t("voice.koonly") : "";
  renderQuick();

  // Skills
  const sk = J.skill;
  const skillOn = jobRunning("skill");
  const models = r.models || [];
  const camOk = (k) => ((r.cams || []).find((c) => c.key === k) || {}).present;
  const sh = models.length ? models.map((m) => {
    const need = m.cams.length ? m.cams : ["top", "right_wrist"];
    const lacking = need.filter((k) => !camOk(k));
    const noplay = m.playable === false;   // trained on both arms or the left arm: the skill runner drives the black arm only
    const blocked = !me || (!skillOn && (noplay || lacking.length > 0 || estop));
    const armK = { both: "rec.arm.both", left: "rec.arm.left" }[m.arms];
    return `<div class="skill"><div class="skill-t"><b>${esc(m.name)}</b><span class="small mute">${esc(String(m.type).toUpperCase())} · ${esc(armK ? t(armK) : t("skill.arm"))} · ${esc(need.map((k) => t("cam." + k)).join(" + "))}</span>`
      + (noplay ? `<span class="small note err">${esc(t("fm.noplay." + m.arms))}</span>`
        : lacking.length ? `<span class="small note err">${esc(t("skill.nocam", { cams: lacking.map((k) => t("cam." + k)).join(", ") }))}</span>` : "")
      + `</div><button class="btn big${skillOn ? " on" : " primary"}" data-skill="${esc(m.name)}" ${blocked ? "disabled" : ""}>`
      + `<i class="${skillOn ? "ri-stop-fill" : "ri-play-fill"}"></i><span>${t(skillOn ? "skill.stop" : "skill.play")}</span></button></div>`;
  }).join("") : `<p class="mute small">${esc(t("skill.none", { dir: "~/policies" }))}</p>`;
  const skills = $("skills");
  if (skills.dataset.html !== sh) {
    skills.innerHTML = sh; skills.dataset.html = sh;
    for (const el of skills.querySelectorAll("[data-skill]")) el.addEventListener("click", () => send({ t: "skill", name: el.dataset.skill, on: !jobRunning("skill") }));
  }
  $("skill-hint").textContent = models.length ? t("skill.hint", { s: Math.round(r.skill_s || 20) }) : t("skill.add");
  $("skill-st").textContent = skillOn ? t("st.on") : "";
  $("skill-st").className = "st" + (skillOn ? " live" : "");
  failTail($("skill-tail"), sk);

  // System
  const h = r.health || {}, jt = h.jetson || {};
  $("sys-sum").textContent = [jt.temp != null ? `${jt.temp}°C` : null, jt.load != null && jt.cores ? `CPU ${Math.round(100 * jt.load / jt.cores)}%` : null,
    b.base && b.base.state === "ok" ? `${b.base.vmin} V` : null].filter(Boolean).join(" · ");
  const rows = [[t("h.jetson"), [jt.temp != null ? `${jt.temp}°C` : null, jt.mem != null ? `${t("h.mem")} ${jt.mem}%` : null,
    jt.disk != null ? `${t("h.disk")} ${jt.disk}%` : null].filter(Boolean).join(" · ")]];
  const names = { right_follower: `${t("arm.right")} ${t("arm.follower")}`, left_follower: `${t("arm.left")} ${t("arm.follower")}`,
    right_leader: `${t("arm.right")} ${t("arm.leader")}`, left_leader: `${t("arm.left")} ${t("arm.leader")}`, base: t("sec.base") };
  for (const [k, label] of Object.entries(names)) {
    const x = b[k];
    if (!x) continue;
    const v = x.state === "missing" ? t("arm.missing") : x.state === "busy" ? t("h.inuse", { job: x.by }) :
      x.state === "silent" ? t("arm.nopower") : x.state === "ok" ? `<b>${x.vmin ?? "-"} V</b> · ${x.tmax ?? "-"}°C${x.alarm ? " · ALARM" : ""}` : esc(x.msg || x.state);
    rows.push([label, v]);
  }
  $("health").innerHTML = rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("");

  renderCams();
  for (const el of document.querySelectorAll("[data-op]")) if (!["talk", "wave-btn", "follow-btn"].includes(el.id)) el.disabled = !me;

  tabDot("drive", baseOn, !$("base-err").hidden);
  tabDot("arms", anyArm || skillOn || leader.on, !$("arms-err").hidden || !$("skill-tail").hidden);
  tabDot("voice", vOn, false);
  tabDot("system", jobRunning("wizard"), Object.values(b).some((x) => x && x.alarm));
}

// ------------------------------------------------------------------ cameras
/* Main camera on top, the two hand cameras below it. Each view shows its stream or why not. */
function renderCams() {
  const cams = (state.robot && state.robot.cams) || [];
  for (const view of document.querySelectorAll(".view[data-cam]")) {
    const key = view.dataset.cam;
    const c = cams.find((x) => x.key === key);
    const img = view.querySelector(":scope > img.cam"), empty = view.querySelector(":scope > .empty"), msg = view.querySelector(":scope > .empty .msg");
    const why = !c ? t("cam.notconfigured") : !c.present ? t("cam.notconnected") : c.paused_by ? t("cam.inuse") : "";
    if (why) {
      if (state.cams[key]) { img.removeAttribute("src"); state.cams[key] = false; }
      img.hidden = true; empty.hidden = false; msg.textContent = why;
      empty.title = c && c.reason && c.reason !== "정상" ? c.reason : "";
      continue;
    }
    if (c.error) {   // the server could not open it (another program has it): say so, retry every 5 s
      empty.hidden = false; img.hidden = true; msg.textContent = t("cam.busy");
      const now = Date.now();
      if (!state.cams[key + ":retry"] || now - state.cams[key + ":retry"] > 5000) {
        state.cams[key + ":retry"] = now;
        img.src = `/camera/${encodeURIComponent(key)}.mjpg?t=${now}`;
        state.cams[key] = true;
      }
      continue;
    }
    if (!state.cams[key]) {
      state.cams[key] = true;
      img.onerror = () => {   // stream ended (camera busy or server restarting): say so, try again shortly
        state.cams[key] = false; img.hidden = true; empty.hidden = false; msg.textContent = t("cam.connecting");
        setTimeout(renderCams, 2000);
      };
      img.src = `/camera/${encodeURIComponent(key)}.mjpg?t=${Date.now()}`;
    }
    img.hidden = false; empty.hidden = true;
    const tag = view.querySelector(":scope > .tag .tagtext");
    if (tag) { tag.textContent = t("cam." + key); tag.title = c.auto ? `${t("cam.auto")} ${c.auto}` : ""; }
  }
}

/* Top-down view. The robot stays in the middle; forward is up. */
function drawMap() {
  const c = $("map");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (!w || !h) return;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const s = state.server;
  const o = (s && s.odom) || { x: 0, y: 0, th: 0 };
  const S = PX_PER_M;
  const sx = (wy) => w / 2 - (wy - o.y) * S;
  const sy = (wx) => h / 2 - (wx - o.x) * S;

  const step = 0.5 * S;
  g.strokeStyle = C.line; g.lineWidth = 1; g.beginPath();
  for (let x = ((w / 2 + o.y * S) % step + step) % step; x < w; x += step) { g.moveTo(x, 0); g.lineTo(x, h); }
  for (let y = ((h / 2 + o.x * S) % step + step) % step; y < h; y += step) { g.moveTo(0, y); g.lineTo(w, y); }
  g.stroke();

  const last = trail[trail.length - 1];
  if (last && Math.hypot(o.x - last.x, o.y - last.y) > 1.5) trail.length = 0;
  if (!last || Math.hypot(o.x - last.x, o.y - last.y) > 0.02) { trail.push({ x: o.x, y: o.y }); if (trail.length > 600) trail.shift(); }
  if (trail.length > 1) {
    g.strokeStyle = ACC; g.lineWidth = 1.5; g.beginPath();
    trail.forEach((p, i) => (i ? g.lineTo(sx(p.y), sy(p.x)) : g.moveTo(sx(p.y), sy(p.x))));
    g.lineTo(w / 2, h / 2); g.stroke();
  }

  g.save(); g.translate(w / 2, h / 2); g.rotate(-o.th);
  // lidar points, robot frame (x forward = up, y left = left). Hidden when older than 1 s.
  const sc = state.scan && performance.now() - state.scan.at < 1000 ? state.scan : null;
  $("scan-src").textContent = sc ? (sc.src === "mock" ? t("scan.mock") : t("scan.live")) : "";
  if (sc) {
    g.fillStyle = C.ink;
    const xy = sc.xy;
    for (let i = 0; i < xy.length; i += 2) g.fillRect(-xy[i + 1] / 100 * S - 1, -xy[i] / 100 * S - 1, 2, 2);
  }
  const L = 0.45 * S, W = 0.35 * S;
  const stop = s && s.estop;
  g.fillStyle = C.panel; g.strokeStyle = stop ? C.stop : ACC; g.lineWidth = 1.5;
  g.fillRect(-W / 2, -L / 2, W, L); g.strokeRect(-W / 2, -L / 2, W, L);
  g.fillStyle = g.strokeStyle; g.beginPath(); g.moveTo(0, -L / 2 + 3); g.lineTo(-5, -L / 2 + 11); g.lineTo(5, -L / 2 + 11); g.closePath(); g.fill();
  const cmd = s ? s.cmd : { vx: 0, vy: 0 };
  const ax = -cmd.vy * S * 3, ay = -cmd.vx * S * 3, al = Math.hypot(ax, ay);
  if (al > 3) {
    g.strokeStyle = ACC; g.fillStyle = ACC; g.lineWidth = 2;
    g.beginPath(); g.moveTo(0, 0); g.lineTo(ax, ay); g.stroke();
    const a = Math.atan2(ay, ax);
    g.beginPath(); g.moveTo(ax, ay); g.lineTo(ax - 7 * Math.cos(a - 0.5), ay - 7 * Math.sin(a - 0.5));
    g.lineTo(ax - 7 * Math.cos(a + 0.5), ay - 7 * Math.sin(a + 0.5)); g.closePath(); g.fill();
  }
  g.restore();
}
window.addEventListener("resize", drawMap);

// ------------------------------------------------- quick and typed commands
/* Sent to the old voice node (/voice/say) in the language it was started with. */
const QUICK = {
  ko: { fwd: "앞으로 가", back: "뒤로 가", left: "왼쪽 으로 가", right: "오른쪽 으로 가", rotl: "좌회전", rotr: "우회전", stop: "멈춰" },
  en: { fwd: "go forward", back: "go back", left: "go left", right: "go right", rotl: "turn left", rotr: "turn right", stop: "stop" },
};
const QUICK_GRID = ["rotl", "fwd", "rotr", "left", "stop", "right", "", "back", ""];

function renderQuick() {
  const me = isOperator();
  const ok = me && jobRunning("voice") && !(state.server && state.server.estop);
  const html = QUICK_GRID.map((k) => k ? `<button type="button" class="btn${k === "stop" ? " stopq" : ""}" data-q="${k}" ${ok || (k === "stop" && me) ? "" : "disabled"}>${t("q." + k)}</button>`
    : `<span class="gap"></span>`).join("");
  const box = $("quick");
  if (box.dataset.html !== html + LANG) {
    box.innerHTML = html; box.dataset.html = html + LANG;
    for (const b of box.querySelectorAll("[data-q]")) b.addEventListener("click", () => {
      const lang = (state.robot && state.robot.voice_lang) || "ko";
      send({ t: "say", text: QUICK[lang][b.dataset.q] });
    });
  }
}
$("say-box").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("say-text").value.trim();
  if (v) send({ t: "say", text: v });
  $("say-text").value = ""; $("say-text").blur();
});

// ------------------------------------------------- leader arm on this laptop
/* Plug the leader arm into this laptop's USB and press the button. The browser finds the
 * servo board, reads the six joints 30 times per second and sends them to the robot, which
 * moves the follower arm through the old twin_bridge (limits, speed ramp, hold on silence). */
const leader = { arm: null, on: false, side: "right", ok: 0, fail: 0, sawRunning: false, paused: false };

async function leaderStart() {
  if (!LeaderArm.supported()) {
    showNote(t(location.protocol === "https:" ? "leader.browser" : "leader.https", { url: `https://${location.hostname}:8443` }), true);
    return;
  }
  try {
    let ports = (await navigator.serial.getPorts()).filter((p) => FEETECH_VENDORS.includes(p.getInfo().usbVendorId));
    if (!ports.length) ports = [await navigator.serial.requestPort({ filters: FEETECH_VENDORS.map((v) => ({ usbVendorId: v })) })];
    let found = null;
    for (const port of ports) {           // try each granted board until six servos answer
      const arm = new LeaderArm(port);
      try { await arm.open(); } catch (e) { continue; }
      if (await arm.readPositions()) { found = arm; break; }
      await arm.close();
    }
    if (!found) { showNote(t("leader.noservos"), true); return; }
    Object.assign(leader, { arm: found, on: true, ok: 0, fail: 0, sawRunning: false });
    send({ t: "leader", on: true, side: leader.side });
    renderRobot();
    leaderLoop();
  } catch (e) {
    if (e && e.name !== "NotFoundError") showNote(String((e && e.message) || e), true);   // NotFoundError = chooser closed
  }
}

async function leaderLoop() {
  while (leader.on && leader.arm) {
    const t0 = performance.now();
    let pos = null;
    try { pos = await leader.arm.readPositions(); } catch (e) { leaderStop(t("leader.lost")); return; }
    if (pos) {
      leader.ok++; leader.fail = 0;
      if (isOperator()) send({ t: "leader_pos", side: leader.side, raw: pos });
    } else if (++leader.fail > 15) { leaderStop(t("leader.lost")); return; }
    const wait = 33 - (performance.now() - t0);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
  }
}

async function leaderStop(reason) {
  const was = leader.on;
  leader.on = false; leader.paused = false;
  if (was) send({ t: "leader", on: false, side: leader.side });
  const a = leader.arm;
  leader.arm = null;
  if (a) await a.close();
  if (reason) showNote(reason, true);
  renderRobot();
}
if (LeaderArm.supported()) navigator.serial.addEventListener("disconnect", () => { if (leader.on) leaderStop(t("leader.unplugged")); });

applyStrings();
render();
connect();
