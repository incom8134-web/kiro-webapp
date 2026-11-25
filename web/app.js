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
const esc = (s) => Kiro.esc(s);
const jobs = () => (state.robot && state.robot.jobs) || {};
const jobRunning = (name) => !!(jobs()[name] && jobs()[name].running);

// ---------------------------------------------------------------- connection
let link = null;
function send(obj) { if (link) link.send(obj); }
function connect() {
  link = Kiro.connect({
    name: () => Kiro.deviceName(),
    onOpen(ws) { state.ws = ws; state.open = true; render(); },
    onClose() {
      state.open = false; state.server = null; state.rtt = null; state.ws = null;
      if (leader.on) leader.paused = true;   // Wi-Fi lost: keep the leader arm open; the robot's bridge holds the arm
      dropInputs(); render();
    },
    onMessage(m) {
      if (m.t === "state") { state.server = m; render(); }
      else if (m.t === "robot") { state.robot = m; renderRobot(); }
      else if (m.t === "scan") { m.at = performance.now(); state.scan = m; }
      else if (m.t === "pong") { state.rtt = performance.now() - m.ts; renderLink(); }
      else if (m.t === "note") showNote(m.msg, !m.ok);
    },
  });
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

window.addEventListener("keydown", (e) => {
  if (e.code === "Escape" || e.key === "Escape") {
    if (!Kiro.typing(e)) send({ t: "estop", on: true });   // not while a PIN or a command is being typed
    return;
  }
  if (Kiro.typing(e) || Kiro.interactive(e)) return;       // Space and Enter stay with the focused control
  const code = keyCode(e);
  if (code === "KeyV" && !e.repeat && isOperator()) setTalk(true);
  if (KEYMAP[code] || code === "Space" || code === "ShiftLeft" || code === "ShiftRight") {
    input.keys.add(code);
    if (isOperator() && (KEYMAP[code] || code === "Space")) e.preventDefault(); // no page scroll
  }
});
window.addEventListener("keyup", (e) => {
  const code = keyCode(e);
  input.keys.delete(code);
  if (code === "KeyV") setTalk(false);
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

// ------------------------------------------------------------------- buttons
$("take-box").addEventListener("submit", (e) => {
  e.preventDefault();
  const s = state.server;
  let force = false;
  if (s && s.operator && s.you.role !== "operator") {
    force = confirm(t("takeover", { name: s.operator.name }));
    if (!force) return;
  }
  send({ t: "take", force });
});
$("release").addEventListener("click", () => send({ t: "release" }));
Kiro.bindEstop($("estop"), $("estop-release"), send, () => !!(state.server && state.server.estop), isOperator);
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
$("guard-sw").addEventListener("change", () => {
  if (!$("guard-sw").checked && !confirm(t("guard.confirm.off"))) { $("guard-sw").checked = true; return; }
  send({ t: "guard", on: $("guard-sw").checked });
});
// Speech language: the operator's choice (remembered in this browser), not the page language
let voiceLang = (() => { try { return localStorage.getItem("kiro-voice-lang"); } catch (e) { return null; } })() || (LANG === "en" ? "en" : "ko");
// Step per command: "big" 0.5 m / 90 degrees, or "test" 0.2 m / 45 degrees as the voice test form asks
let voiceStep = (() => { try { return localStorage.getItem("kiro-voice-step"); } catch (e) { return null; } })() || "big";
// Microphone: "device" = this phone or laptop (the page sends the sound), "robot" = the robot's USB microphone
let voiceMic = (() => { try { return localStorage.getItem("kiro-voice-mic"); } catch (e) { return null; } })() || "device";
const voiceMsg = (on) => ({ t: "voice", on, lang: voiceLang, step: voiceStep, mic: voiceMic });
// Tap to listen, tap again to stop (no holding). It keeps listening in between.
// With "this phone / laptop": the button follows THIS device's microphone, even if voice was started elsewhere.
const listeningHere = () => (voiceMic === "device" ? mic.on : jobRunning("voice"));
$("voice-btn").addEventListener("click", async () => {
  if (listeningHere()) { micStop(); send(voiceMsg(false)); return; }
  if (voiceMic === "device" && !(await micStart())) return;
  send(voiceMsg(true));                        // starts voice, or switches it to this microphone
});
const voiceRestart = () => {
  const r = state.robot;
  if (jobRunning("voice") && r && (r.voice_lang !== voiceLang || r.voice_step !== voiceStep || (r.voice_mic || "device") !== voiceMic)) {
    if (voiceMic !== "device") micStop();
    send(voiceMsg(true));
  }
  renderRobot();
};
for (const b of document.querySelectorAll("[data-vmic]")) b.addEventListener("click", async () => {
  voiceMic = b.dataset.vmic;
  try { localStorage.setItem("kiro-voice-mic", voiceMic); } catch (e) { /* private window */ }
  if (voiceMic === "device" && jobRunning("voice") && !mic.on && !(await micStart())) return;
  voiceRestart();
});

/* This device's microphone -> 16 kHz mono 16-bit pieces over the WebSocket (binary), about 8 a second.
 * Browsers allow the microphone only on https (or localhost). The page is already https. */
const mic = { on: false, stream: null, ctx: null, src: null, node: null, level: 0, sawVoice: false };
async function micStart() {
  if (!window.isSecureContext) {               // http: browsers block the microphone. Go to the https page (same robot).
    location.href = `https://${location.host}${location.pathname}#voice`;
    return false;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showNote(t("voice.nomicapi"), true);
    return false;
  }
  try {
    mic.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) {
    showNote(t("voice.denied"), true);
    return false;
  }
  const AC = window.AudioContext || window.webkitAudioContext;
  mic.ctx = new AC();
  mic.src = mic.ctx.createMediaStreamSource(mic.stream);
  mic.node = mic.ctx.createScriptProcessor(4096, 1, 1);
  mic.node.onaudioprocess = (ev) => {
    const x = ev.inputBuffer.getChannelData(0), ratio = mic.ctx.sampleRate / 16000;
    const n = Math.floor(x.length / ratio), out = new Int16Array(n);
    let sum = 0;
    for (let i = 0; i < n; i++) {
      const p = i * ratio, k = Math.floor(p), f = p - k;
      const v = Math.max(-1, Math.min(1, x[k] * (1 - f) + (k + 1 < x.length ? x[k + 1] : x[k]) * f));
      out[i] = v * 32767;
      sum += v * v;
    }
    mic.level = Math.min(1, Math.sqrt(sum / Math.max(1, n)) * 6);
    const bar = $("mic-level").firstElementChild;
    if (bar) bar.style.width = Math.round(mic.level * 100) + "%";
    if (state.open && state.ws.readyState === WebSocket.OPEN && isOperator()) state.ws.send(out.buffer);
  };
  mic.src.connect(mic.node);
  mic.node.connect(mic.ctx.destination);  // needed for the processor to run; its output is silence
  if (mic.ctx.state === "suspended") await mic.ctx.resume();
  mic.on = true;
  mic.sawVoice = false;
  renderRobot();
  return true;
}
function micStop() {
  if (!mic.on && !mic.stream) return;
  mic.on = false;
  try { mic.node && mic.node.disconnect(); mic.src && mic.src.disconnect(); } catch (e) { /* already gone */ }
  if (mic.stream) for (const tr of mic.stream.getTracks()) tr.stop();
  if (mic.ctx) mic.ctx.close().catch(() => {});
  Object.assign(mic, { stream: null, ctx: null, src: null, node: null, level: 0 });
}
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
$("voice-base").addEventListener("click", () => send({ t: "base", on: true }));   // voice needs the wheel program
$("wave-btn").addEventListener("click", () => send({ t: "wave", side: "right" }));
$("stops-clear").addEventListener("click", () => send({ t: "stops_clear" }));
// Copy mode: ONE leader (the grey one, on the robot) moves both arms the same way
const following = () => jobRunning("relay") && ((state.robot && state.robot.bridge) || {}).mode === "follow";
// Teleop uses the leader wherever it is plugged in: on the robot (teleop program) or on this laptop (Web Serial)
const DEMO = "__demo__";   // picker value for the built-in demo. A real model name cannot start with "_".
let skillPick = "";
function clampInt(v, lo, hi, fallback) {
  const n = parseInt(v, 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(lo, Math.min(hi, n));
}
function clampNum(v, lo, hi, fallback) {
  const n = parseFloat(v);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(lo, Math.min(hi, n));
}
/* Once is always a single run. Loop reads Times and Pause. Seconds per run always come from Advanced.
 * The old repeat button ignored these fields and always said "30 runs × 30 s (5 s apart)". */
function skillPlan(mode, runs, pause, seconds) {
  const loop = mode === "loop";
  return {
    repeat: loop ? clampInt(runs, 1, 50, 1) : 1,
    pause: loop ? clampNum(pause, 0, 120, 0) : 0,
    seconds: Math.round(clampNum(seconds, 5, 120, 30)),
  };
}
function skillNums() {
  const on = document.querySelector("#skill-mode .on");
  return skillPlan(on && on.dataset.runmode, $("skill-repeat").value, $("skill-pause").value, $("skill-secs").value);
}
$("skill-pick").addEventListener("change", () => { skillPick = $("skill-pick").value; render(); });
$("skill-mode").addEventListener("click", (e) => {
  const b = e.target.closest("[data-runmode]");
  if (!b || b.disabled) return;
  for (const x of $("skill-mode").querySelectorAll("[data-runmode]")) x.classList.toggle("on", x === b);
  render();
});
for (const id of ["skill-repeat", "skill-pause", "skill-secs"]) $(id).addEventListener("input", () => render());
$("skill-play").addEventListener("click", () => {
  const r2 = state.robot || {};
  const skillOn = !!(jobRunning("skill") || r2.skill_series);
  const demoOn = jobRunning("demo");
  if (skillOn) {
    send({ t: "skill", name: (r2.skill_series && r2.skill_series.name) || $("skill-pick").value, on: false, ...skillNums() });
    return;
  }
  if (demoOn) { send({ t: "demo", on: false }); return; }
  const name = $("skill-pick").value;
  const nums = skillNums();
  if (name === DEMO) {
    if (!confirm(nums.repeat > 1 ? t("skill.confirm30", { n: nums.repeat }) : t("demo.confirm"))) return;
    send({ t: "demo", on: true, repeat: nums.repeat, pause: nums.pause });
    return;
  }
  if (!name || !confirm(nums.repeat > 1 ? t("skill.confirm30", { n: nums.repeat }) : t("skill.confirm"))) return;
  send({ t: "skill", name, on: true, ...nums });
});
const robotLeader = (side) => {
  const l = ((state.robot && state.robot.health && state.robot.health.boards) || {})[side + "_leader"];
  return !!l && l.state !== "missing";
};
const laptopTele = (side) => leader.on && (leader.side === side || leader.side === "both");
function teleClick(side) {
  if (jobRunning("teleop_" + side)) { send({ t: "teleop", side, on: false }); return; }
  if (laptopTele(side)) { leaderStop(""); return; }
  if (robotLeader(side)) { send({ t: "teleop", side, on: true }); return; }
  if (leader.on) { showNote(t("leader.busy"), true); return; }   // the other laptop leader runs: stop it first
  leader.side = side;
  try { localStorage.setItem("kiro-leader-side", side); } catch (e) { /* private window */ }
  leaderStart();
}
// Both arms: one choice. "each" is a leader per arm. "one" is the grey leader copied onto both.
const eachLeadersOn = () => (jobRunning("teleop_right") && jobRunning("teleop_left")) || (laptopTele("right") && laptopTele("left"));
function stopEach() {
  if (laptopTele("right") && laptopTele("left")) { leaderStop(""); return; }
  for (const side of ["right", "left"]) if (jobRunning("teleop_" + side)) send({ t: "teleop", side, on: false });
}
function startEach() {
  if (!(robotLeader("right") && robotLeader("left"))) {
    if (leader.on) { showNote(t("leader.busy"), true); return; }
    leader.side = "both";
    try { localStorage.setItem("kiro-leader-side", "both"); } catch (e) { /* private window */ }
    leaderStart();
    return;
  }
  for (const side of ["right", "left"]) if (!jobRunning("teleop_" + side)) send({ t: "teleop", side, on: true });
}
$("both-go").addEventListener("click", () => {
  const each = eachLeadersOn(), one = following();
  if (each || one) {
    if (one) send({ t: "follow", on: false });
    if (each) stopEach();
    return;
  }
  if ($("both-mode").value === "one") send({ t: "follow", on: true });
  else startEach();
});
$("leader-btn").addEventListener("click", () => (leader.on ? leaderStop() : leaderStart()));

// Tabs in the dock: Drive, Arms, Voice, System. The last one used is remembered.
function showTab(name) {
  for (const b of document.querySelectorAll(".tabs [data-tab]")) {
    const on = b.dataset.tab === name;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
    b.tabIndex = on ? 0 : -1;
  }
  for (const p of document.querySelectorAll(".panel[data-panel]")) {
    const on = p.dataset.panel === name;
    p.classList.toggle("on", on);
    p.setAttribute("aria-hidden", on ? "false" : "true");
  }
  try { localStorage.setItem("kiro-tab", name); } catch (e) { /* ignore */ }
}
for (const b of document.querySelectorAll(".tabs [data-tab]")) b.addEventListener("click", () => showTab(b.dataset.tab));
const tablist = document.querySelector(".tabs");
if (tablist) tablist.addEventListener("keydown", (e) => {
  const tabs = [...tablist.querySelectorAll("[role=tab]")];
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  let n = i;
  if (e.key === "ArrowRight") n = (i + 1) % tabs.length;
  else if (e.key === "ArrowLeft") n = (i + tabs.length - 1) % tabs.length;
  else if (e.key === "Home") n = 0;
  else if (e.key === "End") n = tabs.length - 1;
  else return;
  e.preventDefault();
  showTab(tabs[n].dataset.tab);
  tabs[n].focus();
});
let startTab = "drive";
try { startTab = localStorage.getItem("kiro-tab") || "drive"; } catch (e) { /* ignore */ }
if (location.hash === "#voice" || location.hash === "#arms") startTab = location.hash.slice(1);   // sent here from the http page (microphone, USB)
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

const holdButton = (el, action) => Kiro.holdButton(el, action);
holdButton($("shutdown"), () => Kiro.confirmSend(send, "system:shutdown", { t: "system", action: "shutdown" }));
holdButton($("reboot"), () => Kiro.confirmSend(send, "system:reboot", { t: "system", action: "reboot" }));

function showNote(text, isError) {
  if (!text) return;
  const known = { "LEFT_BLOCKED": "arm.leftblocked", "TWO_ARM": "fm.noplay.both", "LEFT_ARM": "fm.noplay.left", "NO_FIT": "fm.noplay.other",
    "LIDAR_MISSING": "base.nolidar", "NO_MIC": "voice.nomic" };
  const n = $("note");
  n.textContent = known[text] ? t(known[text]) : Kiro.serverText(text);
  n.className = "note" + (isError ? " err" : "");
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { n.textContent = ""; n.className = "note"; renderHintNote(); }, 5000);
}

function renderHintNote() { /* the PIN is not shown on the page */ }

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
  Kiro.renderBattery(state.robot && state.robot.battery);
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
  $("estop-label").textContent = stopped ? t("estop.stopped") : t("estop");
  es.setAttribute("aria-pressed", stopped ? "true" : "false");
  if ($("estop-release")) $("estop-release").hidden = !(stopped && me);

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
  const rob = state.robot || {};
  $("guard-sw").checked = !!gd.on && (s.backend !== "ros" || rob.obstacle_stop !== false);  // the page's stop and the robot's own lidar guard
  $("guard-st").textContent = !s ? "" : !gd.on ? t("guard.off") : gd.lost ? held : !gd.lidar ? t("guard.nolidar") : held || t("guard.on", { cm: Math.round((gd.stop_m || 0) * 100) });
  $("guard-st").className = "st grow" + (!s ? "" : gd.on && gd.lidar && !held ? " live" : " warn");
  const rg = state.robot && state.robot.voice && state.robot.voice.guard;  // the old lidar guard on the robot (gamepad, voice, page)
  $("guard-note").textContent = !s ? "" : !$("guard-sw").checked ? t("guard.offnote") : gd.lost ? t("guard.lostnote")
    : s.backend === "ros" && rob.lidar_measured === false ? t("guard.unmeasured") : !gd.lidar ? t("guard.nolidarnote")
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
  document.title = t("title.control");
  $("ver").textContent = s ? t("foot.ver", { v: s.version, n: t(s.clients === 1 ? "devices.one" : "devices.many", { n: s.clients }) }) : "";
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
  Kiro.renderBattery(state.robot && state.robot.battery);
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

  // Arms: one row each. Release torque stays a hold-to-confirm, quieter than the other buttons.
  const b = (r.health && r.health.boards) || {};
  const armCol = (side) => {
    const blocked = side === "left" && !r.left_arm_ok;
    const tele = jobRunning("teleop_" + side) || laptopTele(side);
    const f = b[side + "_follower"];
    const off = !me || blocked ? "disabled" : "";
    const status = f && f.state === "missing" ? t("arm.missing")
      : f && f.state === "ok" ? `${f.vmin ?? "-"} V` : f && f.state === "silent" ? t("arm.nopower") : "";
    const badge = blocked
      ? `<span class="badge lock">${esc(t("arm.locked.badge"))}</span><a class="recal" href="/studio.html" title="${esc(t("fix.leftlock"))}">${esc(t("arm.recal"))}</a>`
      : `<span class="badge">${esc(status)}</span>`;
    return `<div class="armrow">
      <div class="row armname"><b>${esc(t("arm." + side))}</b>${badge}</div>
      <div class="armacts">
        <button class="btn${tele ? " on" : ""}" type="button" data-tele="${side}" title="${esc(t("arm.teleop.hint"))}" ${off}>${esc(t(tele ? "arm.teleop.off" : "arm.teleop.on"))}</button>
        <button class="btn" type="button" data-arm="${side}" data-act="fold" ${off}>${esc(t("arm.fold"))}</button>
        <button class="btn" type="button" data-arm="${side}" data-act="hold" ${off}>${esc(t("arm.hold"))}</button>
        <button class="btn quiet hold" type="button" data-arm="${side}" data-act="release" title="${esc(t("arm.fall"))}" ${me ? "" : "disabled"}><span class="fill"></span>${esc(t("arm.release"))}</button>
      </div></div>`;
  };
  const arms = $("arms");
  const html = armCol("right") + armCol("left");
  if (arms.dataset.html !== html) {
    arms.innerHTML = html; arms.dataset.html = html;
    for (const el of arms.querySelectorAll("[data-tele]")) el.addEventListener("click", () => teleClick(el.dataset.tele));
    for (const el of arms.querySelectorAll("[data-act]")) {
      if (el.dataset.act === "release") holdButton(el, () => Kiro.confirmSend(send, "arm:release", { t: "arm", side: el.dataset.arm, action: "release" }));
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
  for (const b of document.querySelectorAll("[data-lside]")) {
    b.classList.toggle("on", b.dataset.lside === leader.side);
    b.disabled = !me || leader.on || ((b.dataset.lside !== "right") && !r.left_arm_ok);
  }
  $("leader-sum").textContent = leader.on ? t("st.on") : t("leader.sum");
  if (leader.on) $("leader-box").open = true;
  $("leader-st").textContent = leader.on && leader.paused ? t("leader.paused") : leader.on ? t("leader.live", { arm: t(leader.side === "both" ? "leader.two" : "arm." + leader.side), n: leader.ok, state: br.state || "..." })
    : !LeaderArm.supported() ? t(location.protocol === "https:" ? "leader.browser" : "leader.https", { url: `https://${location.host}` })
    : "";
  const waving = !!(br.running && br.mode === "wave");
  const follows = following();
  const eachOn = eachLeadersOn();
  $("wave-btn").disabled = !me || leader.on || waving || follows || estop;
  $("wave-btn").classList.toggle("on", waving);
  if (follows) $("both-mode").value = "one";
  else if (eachOn) $("both-mode").value = "each";
  const bothOn = follows || eachOn;
  $("both-mode").disabled = !me || bothOn;
  const go = $("both-go");
  const mode = $("both-mode").value;
  go.disabled = !me || (!bothOn && (!r.left_arm_ok || estop || (mode === "one" && leader.on)));
  go.classList.toggle("on", bothOn);
  setBtn(go, t(bothOn ? "both.stop" : "both.start"), bothOn ? "ri-stop-fill" : "ri-play-fill");
  go.title = !r.left_arm_ok ? t("both.locked.tip") : t(mode === "one" ? "both.one.tip" : "both.each.tip");
  $("both-mode").title = go.title;
  $("both-st").textContent = follows ? t("follow.live") : eachOn ? t("both.each.live") : "";
  $("both-st").className = "small " + (bothOn ? "accent" : "mute");

  // Voice
  const vOn = jobRunning("voice");
  // The page's microphone ends with the voice program, E-STOP, or losing control
  if (mic.on && vOn) mic.sawVoice = true;
  if (mic.on && ((mic.sawVoice && !vOn) || !me || (state.server && state.server.estop) || (r.voice_mic && r.voice_mic !== "device" && vOn))) micStop();
  const listening = vOn && listeningHere();
  setBtn($("voice-btn"), t(listening ? "voice.stoplisten" : "voice.listen"), listening ? "ri-mic-off-fill" : "ri-mic-fill");
  $("voice-btn").classList.toggle("on", listening);
  // Is the sound arriving at the robot? (the server counts it)
  const rx = r.voice_rx || {};
  $("voice-rx").hidden = !mic.on || !r.voice_rx;   // an older server does not report it
  $("voice-rx").textContent = rx.age != null && rx.age < 2 ? t("voice.rxok") : t("voice.rxnone");
  $("voice-rx").className = "small " + (rx.age != null && rx.age < 2 ? "ok" : "err");
  for (const b of document.querySelectorAll("[data-vmic]")) {
    b.classList.toggle("on", b.dataset.vmic === voiceMic);
    b.disabled = !me;
  }
  $("mic-level").hidden = !mic.on;
  $("voice-nobase").hidden = !(vOn && !jobRunning("base") && state.server && state.server.backend === "ros");
  const insecure = voiceMic === "device" && !window.isSecureContext;
  $("voice-https").hidden = !insecure;
  const secureVoice = Kiro.httpsUrl(`https://${location.host}${location.pathname}#voice`);
  if (secureVoice) $("voice-https-go").href = secureVoice;
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

  // One picker: installed models, plus the built-in demo. Run uses Once or Loop.
  const sk = J.skill;
  const series = r.skill_series;            // repeat: runs left, and the pause in between (no program runs then)
  const skillOn = jobRunning("skill") || !!series;
  const demoOn = jobRunning("demo");
  const running = skillOn || demoOn;
  const models = r.models || [];
  const camOk = (k) => ((r.cams || []).find((c) => c.key === k) || {}).present;
  const camLabel = (k) => (STRINGS.en["cam." + k] ? t("cam." + k) : k);
  if (series && series.name) skillPick = series.name;
  else if (demoOn) skillPick = DEMO;
  if (skillPick !== DEMO && !models.some((m) => m.name === skillPick)) skillPick = (models.find((m) => m.playable !== false) || models[0] || {}).name || DEMO;
  const pick = $("skill-pick");
  const opts = `<option value="${DEMO}">${esc(t("skill.demoopt"))}</option>`
    + models.map((m) => `<option value="${esc(m.name)}">${esc(m.name)} · ${esc(String(m.type).toUpperCase())}</option>`).join("");
  if (pick.dataset.html !== opts) { pick.innerHTML = opts; pick.dataset.html = opts; }
  if (skillPick) pick.value = skillPick;
  pick.disabled = running;
  const demoPick = pick.value === DEMO;
  const m = demoPick ? null : models.find((x) => x.name === pick.value);
  const need = m ? (m.cams.length ? m.cams : ["top", "right_wrist"]) : [];
  const lacking = need.filter((k) => !camOk(k));
  const noplay = !!(m && m.playable === false);
  const blocked = !me || estop || (!demoPick && (!m || noplay || lacking.length > 0));
  const play = $("skill-play");
  play.disabled = running ? !me : blocked;
  play.classList.toggle("on", running);
  play.classList.toggle("primary", !running && !play.disabled);
  setBtn(play, t(running ? "skill.stop" : "skill.run"), running ? "ri-stop-fill" : "ri-play-fill");
  for (const btn of $("skill-mode").querySelectorAll("[data-runmode]")) btn.disabled = running;
  const modeOn = document.querySelector("#skill-mode .on") || $("skill-mode").querySelector("[data-runmode=once]");
  if (modeOn && !modeOn.classList.contains("on")) modeOn.classList.add("on");
  const looping = modeOn && modeOn.dataset.runmode === "loop";
  $("skill-loopfields").hidden = !looping;
  $("skill-adv").hidden = demoPick;
  $("skill-repeat").disabled = $("skill-pause").disabled = $("skill-secs").disabled = running;
  $("skill-empty").hidden = models.length > 0;
  $("skill-how").hidden = models.length > 0;
  const armK = m && { both: "rec.arm.both", left: "rec.arm.left", right: "rec.arm.right" }[m.arms];
  const why = !m ? "" : m.block === "cams" ? t("skill.badcam", { cams: m.cams.join(", ") })
    : noplay ? t("fm.noplay." + m.arms)
    : lacking.length ? t("skill.nocam", { cams: lacking.map(camLabel).join(", ") }) : "";
  // The first ~40 s of a run: countdown, then the model loads. Say so, or people press Stop before the arm moves.
  const skJob = J.skill, now = (r.health && r.health.t) || Date.now() / 1000;
  const loading = !!(skJob && skJob.running && now - skJob.started < 45);
  const prog = [series ? t(series.waiting ? "skill.waiting" : "skill.runof", { n: series.run, of: series.of, s: Math.round(series.pause) }) : "",
    loading && !(series && series.waiting) ? t("skill.loading.short") : ""].filter(Boolean).join(" · ");
  const demoJob = J.demo;
  const demoLine = demoOn && demoJob ? (demoJob.tail || []).slice().reverse().find((l) => /^run \d+ of \d+/.test(l)) : "";
  const plan = skillNums();
  const when = plan.repeat > 1 ? t("skill.status.loop", { n: plan.repeat, s: plan.pause }) : t("skill.status.once");
  let line = "", kind = "mute";
  if (skillOn && prog) { line = prog; kind = "accent"; }
  else if (skillOn) { line = t("skill.running"); kind = "accent"; }
  else if (demoOn) { line = demoLine ? t("demo.run", { run: demoLine.replace(/^run (\d+) of (\d+).*/, "$1/$2") }) : t("demo.starting"); kind = "accent"; }
  else if (why) { line = why; kind = "note err"; }
  else if (demoPick) line = `${t("skill.demo.line")} · ${when}`;
  else if (m) line = `${t(armK || "skill.arm")} · ${need.map(camLabel).join(" + ")} · ${when}`;
  const status = $("skill-status");
  status.textContent = line;
  status.className = "small " + kind;
  $("skill-st").textContent = running ? t("st.on") : "";
  $("skill-st").className = "st" + (running ? " live" : "");
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
    const v = x.state === "missing" ? esc(t("arm.missing")) : x.state === "busy" ? esc(t("h.inuse", { job: x.by })) :
      x.state === "silent" ? esc(t("arm.nopower")) : x.state === "ok" ? `<b>${esc(x.vmin ?? "-")} V</b> · ${esc(x.tmax ?? "-")}°C${x.alarm ? " · ALARM" : ""}` : esc(x.msg || x.state);
    rows.push([label, v]);
  }
  $("health").innerHTML = rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("");

  renderCams();
  for (const el of document.querySelectorAll("[data-op]")) if (!["talk", "wave-btn"].includes(el.id)) el.disabled = !me;

  tabDot("drive", baseOn, !$("base-err").hidden);
  tabDot("arms", anyArm || running || leader.on, !$("arms-err").hidden || !$("skill-tail").hidden);
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
    if (img) img.alt = t("cam." + key);
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
const leader = { arms: {}, on: false, side: "right", ok: 0, fail: 0, sawRunning: false, paused: false };
// Leader arm(s) plugged into this laptop: right, left, or both (two leaders). Each moves the arm of its own side.
try { leader.side = ["right", "left", "both"].includes(localStorage.getItem("kiro-leader-side")) ? localStorage.getItem("kiro-leader-side") : "right"; } catch (e) { /* private window */ }
for (const b of document.querySelectorAll("[data-lside]")) b.addEventListener("click", () => {
  if (leader.on) return;                     // stop the leader arm first
  leader.side = b.dataset.lside;
  try { localStorage.setItem("kiro-leader-side", leader.side); } catch (e) { /* private window */ }
  renderRobot();
});
const FEETECH_FILTERS = () => FEETECH_VENDORS.map((v) => ({ usbVendorId: v }));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* Every board this browser may use (granted before) where six servos answer: the leader arms on this laptop. */
async function grantedLeaders() {
  const out = [];
  for (const port of (await navigator.serial.getPorts()).filter((p) => FEETECH_VENDORS.includes(p.getInfo().usbVendorId))) {
    const arm = new LeaderArm(port);
    try { await arm.open(); } catch (e) { continue; }
    if (await arm.readPositions()) out.push(arm); else await arm.close();
  }
  return out;
}
const closeAll = async (arms) => { for (const a of arms) await a.close(); };

/* Two leaders look the same to the browser. The person moves the RIGHT one a little: the board that moves is
 * the right leader, the other one the left. Returns { right: arm, left: arm } or null (nothing moved). */
async function whichIsWhich(arms, side) {
  showNote(t("leader.wiggle." + side));
  const p0 = [];
  for (const a of arms) p0.push(await a.readPositions());
  const moved = arms.map(() => 0);
  const end = performance.now() + 15000;
  while (performance.now() < end) {
    for (let i = 0; i < arms.length; i++) {
      const p = await arms[i].readPositions();
      if (p && p0[i]) moved[i] = Math.max(moved[i], p.slice(0, 5).reduce((sum, v, j) => sum + Math.abs(v - p0[i][j]), 0));
    }
    const best = moved.indexOf(Math.max(...moved));
    const rest = Math.max(0, ...moved.filter((_, i) => i !== best));
    if (moved[best] > 150 && moved[best] > 3 * rest) {
      const other = arms.find((_, i) => i !== best);
      showNote(t("leader.found"));
      return side === "left" ? { left: arms[best], right: other } : { right: arms[best], left: other };
    }
    await sleep(50);
  }
  return null;
}

async function leaderStart() {
  if (!window.isSecureContext) {               // http: browsers block USB. Same robot, the https page, Arms tab.
    location.href = `https://${location.host}${location.pathname}#arms`;
    return;
  }
  if (!LeaderArm.supported()) {
    showNote(t(location.protocol === "https:" ? "leader.browser" : "leader.https", { url: `https://${location.host}` }), true);
    return;
  }
  const sides = leader.side === "both" ? ["right", "left"] : [leader.side];
  try {
    let arms = await grantedLeaders();
    if (arms.length < sides.length) {          // the browser's list: pick (another) leader arm's USB port
      await closeAll(arms);
      await navigator.serial.requestPort({ filters: FEETECH_FILTERS() });
      arms = await grantedLeaders();
      if (arms.length < sides.length) {
        await closeAll(arms);
        if (arms.length) showNote(t("leader.picksecond")); else showNote(t("leader.noservos"), true);
        return;
      }
    }
    let map;
    if (arms.length === 1) map = { [sides[0]]: arms[0] };
    else {
      map = await whichIsWhich(arms.slice(0, 2), sides[0]);
      await closeAll(arms.slice(2));
      if (!map) { await closeAll(arms.slice(0, 2)); showNote(t("leader.nomove." + sides[0]), true); return; }
      if (sides.length === 1) {                // one arm wanted: let the other leader go
        const other = sides[0] === "right" ? "left" : "right";
        await map[other].close();
        delete map[other];
      }
    }
    Object.assign(leader, { arms: map, on: true, ok: 0, fail: 0, sawRunning: false });
    send({ t: "leader", on: true, side: leader.side });
    renderRobot();
    leaderLoop();
  } catch (e) {
    if (e && e.name !== "NotFoundError") showNote(String((e && e.message) || e), true);   // NotFoundError = chooser closed
  }
}

async function leaderLoop() {
  while (leader.on) {
    const t0 = performance.now();
    const list = Object.entries(leader.arms);
    if (!list.length) return;
    for (const [side, arm] of list) {          // each leader for the arm of its own side
      let pos = null;
      try { pos = await arm.readPositions(); } catch (e) { leaderStop(t("leader.lost")); return; }
      if (pos) {
        leader.ok++; leader.fail = 0;
        if (isOperator()) send({ t: "leader_pos", side, raw: pos });
      } else if (++leader.fail > 15 * list.length) { leaderStop(t("leader.lost")); return; }
    }
    const wait = 33 - (performance.now() - t0);
    if (wait > 0) await sleep(wait);
  }
}

async function leaderStop(reason) {
  const was = leader.on;
  leader.on = false; leader.paused = false;
  if (was) send({ t: "leader", on: false, side: leader.side });
  const arms = Object.values(leader.arms);
  leader.arms = {};
  await closeAll(arms);
  if (reason) showNote(reason, true);
  renderRobot();
}
if (LeaderArm.supported()) navigator.serial.addEventListener("disconnect", () => { if (leader.on) leaderStop(t("leader.unplugged")); });

applyStrings();
render();
connect();
Kiro.bindLogin({ onLogin() { state.cams = {}; if (link) link.reconnect(); } });
