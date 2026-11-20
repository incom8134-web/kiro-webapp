"use strict";
/*
 * HAEBOT Robot command center - browser side.
 *
 * 1. Keeps one WebSocket open to the robot server.
 * 2. Reads three inputs (gamepad, on-screen sticks, keyboard) and picks one.
 * 3. While this device is the operator, sends the drive command 20 times per
 *    second. That message is also the heartbeat: if it stops, the robot stops.
 * 4. Cameras, arms (with the leader arm on this laptop), voice, skills, health.
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
    else if (m.t === "wizard_steps") { eng.steps = m.steps; renderEngineer(); }
    else if (m.t === "pong") { state.rtt = performance.now() - m.ts; renderLink(); }
    else if (m.t === "note") showNote(m.msg, !m.ok);
  };
  ws.onclose = () => {
    state.open = false; state.server = null; state.rtt = null;
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

$("base-btn").addEventListener("click", () => send({ t: "base", on: !jobRunning("base"), profile: $("guard-profile").value }));
$("guard-sw").addEventListener("change", () => send({ t: "guard", on: $("guard-sw").checked }));   // the server's obstacle stop
$("voice-btn").addEventListener("click", () => send({ t: "voice", on: !jobRunning("voice"), lang: LANG }));
$("wave-btn").addEventListener("click", () => send({ t: "wave", side: "right" }));
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
  const known = { "LEFT_BLOCKED": "arm.leftblocked" };
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
  if (!state.open) {
    banner.hidden = false; banner.className = "banner";
    banner.textContent = t("banner.offline");
  } else if (stopped) {
    banner.hidden = false; banner.className = "banner stop";
    banner.textContent = t("banner.estop", { by: s.estop_by ? t("banner.estop.by", { name: s.estop_by }) : "" }) +
      t(me ? "banner.estop.op" : "banner.estop.viewer");
  } else {
    banner.hidden = true;
  }

  const cmd = s ? s.cmd : { vx: 0, vy: 0, wz: 0 };
  $("vx").textContent = cmd.vx.toFixed(2);
  $("vy").textContent = cmd.vy.toFixed(2);
  $("wz").textContent = cmd.wz.toFixed(2);

  // Obstacle stop of the server: on / off, and whether it is holding the robot right now
  const gd = (s && s.guard) || { on: false, lidar: false, block: [] };
  const held = gd.block && gd.block.length ? t("guard.held", { where: gd.block.map((d) => t("dir." + d)).join(", ") }) : "";
  $("guard-sw").checked = !!gd.on;
  $("guard-st").textContent = !s ? "" : !gd.on ? t("guard.off") : !gd.lidar ? t("guard.nolidar") : held || t("guard.on", { cm: Math.round((gd.stop_m || 0) * 100) });
  $("guard-st").className = "st grow" + (!s ? "" : gd.on && gd.lidar && !held ? " live" : " warn");
  $("guard-note").textContent = !s ? "" : !gd.on ? t("guard.offnote") : !gd.lidar ? t("guard.nolidarnote") : "";

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

/* A job's last lines, shown only when it ended with an error (not when we stopped it). */
function failTail(el, job) {
  const failed = !!job && !job.running && job.code > 0 && job.code !== 130;
  el.hidden = !failed;
  if (failed) el.textContent = job.tail.slice(-5).join("\n");
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
  if (failTail(be, J.base)) be.textContent = t("base.exited", { code: J.base.code }) + "\n" + be.textContent;
  else if (baseOn && r.wheels === "off") { be.hidden = false; be.textContent = t("base.nowheels"); }
  $("base-st").textContent = baseOn ? (r.wheels === "off" ? t("st.nowheels") : r.wheels === "ok" ? t("st.on") : t("st.starting")) : "";
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
  const armJobs = ["teleop_right", "teleop_left", "arm_right", "arm_left", "bridge"].map((n) => J[n]).filter(Boolean);
  const anyArm = armJobs.some((j) => j.running);
  $("arms-st").textContent = anyArm ? t("st.busy") : "";
  $("arms-st").className = "st" + (anyArm ? " live" : "");
  const lastArm = armJobs.sort((a, c) => (c.ended || 1e12) - (a.ended || 1e12) || c.started - a.started)[0];
  failTail($("arms-err"), lastArm);

  // Leader arm on this laptop, and the wave
  const br = r.bridge || {};
  if (leader.on && br.running && br.mode === "leader") leader.sawRunning = true;
  if (leader.on && (estop || !me || (leader.sawRunning && !br.running))) leaderStop(estop ? t("hint.estop") : "");
  setBtn($("leader-btn"), t(leader.on ? "leader.stop" : "leader.use"), leader.on ? "ri-stop-fill" : "ri-usb-line");
  $("leader-btn").classList.toggle("on", leader.on);
  $("leader-st").textContent = leader.on ? t("leader.live", { n: leader.ok, state: br.state || "..." })
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
  $("voice-st").className = "st" + (vOn ? " live" : "");
  $("voice-on").hidden = !vOn;
  $("talk").disabled = !me || !vOn;
  $("voice-text").textContent = (r.voice && r.voice.text) || "-";
  const noEn = LANG === "en" && !(r.voice_models || []).some((m) => m.includes("-en"));
  $("voice-note").hidden = vOn || !noEn;
  $("voice-note").textContent = noEn ? t("voice.koonly") : "";
  renderQuick();

  // Skills
  const sk = J.skill;
  const skillOn = jobRunning("skill");
  const models = r.models || [];
  const sh = models.length ? models.map((m) => `<div class="row"><span class="grow"><b>${esc(m.name)}</b> <span class="small mute">${esc(m.cams.join(" + "))}</span></span>
      <button class="btn${skillOn ? " on" : " primary"}" data-skill="${esc(m.name)}" ${me ? "" : "disabled"}>${t(skillOn ? "btn.stop" : "skill.run")}</button></div>`).join("")
    : `<p class="mute small">${esc(t("skill.none", { dir: "~/policies" }))}</p>`;
  const skills = $("skills");
  if (skills.dataset.html !== sh) {
    skills.innerHTML = sh; skills.dataset.html = sh;
    for (const el of skills.querySelectorAll("[data-skill]")) el.addEventListener("click", () => send({ t: "skill", name: el.dataset.skill, on: !jobRunning("skill") }));
  }
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
  renderEngineer();
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
const leader = { arm: null, on: false, side: "right", ok: 0, fail: 0, sawRunning: false };

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
  leader.on = false;
  if (was) send({ t: "leader", on: false, side: leader.side });
  const a = leader.arm;
  leader.arm = null;
  if (a) await a.close();
  if (reason) showNote(reason, true);
  renderRobot();
}
if (LeaderArm.supported()) navigator.serial.addEventListener("disconnect", () => { if (leader.on) leaderStop(t("leader.unplugged")); });

// ----------------------------------------------------------- engineer tools
const eng = { steps: null, sel: 0, servo: null, prep: {} };
const MOTION_STEPS = new Set(["teleop_left", "teleop_right", "teleop_both", "wheels", "mecanum_floor", "ros_base"]);
const isEngineer = () => !!(state.server && state.server.you.engineer);

$("eng-box").addEventListener("submit", (e) => {
  e.preventDefault();
  send({ t: "engineer", pin: $("eng-pin").value });
  $("eng-pin").value = ""; $("eng-pin").blur();
});
$("eng-lock").addEventListener("click", () => send({ t: "engineer", lock: true }));
$("guard-profile").addEventListener("change", () => $("guard-profile").blur());

const svPort = () => $("sv-port").value;
$("sv-scan").addEventListener("click", () => send({ t: "servo", cmd: "scan", port: svPort(), lo: +$("sv-lo").value, hi: +$("sv-hi").value }));
const svSend = (cmd, value) => send({ t: "servo", cmd, port: svPort(), id: eng.servo, value, arm_ok: $("sv-armok").checked });
$("sv-ton").addEventListener("click", () => svSend("torque", 1));
$("sv-toff").addEventListener("click", () => svSend("torque", 0));
holdButton($("sv-move"), () => svSend("move", +$("sv-pos").value));
holdButton($("sv-setid"), () => svSend("setid", +$("sv-newid").value));

function renderEngineer() {
  const r = state.robot, s = state.server;
  const me = isOperator(), en = isEngineer();
  $("eng-box").hidden = en;
  $("eng-body").hidden = !en;
  $("eng-st").textContent = en ? "✓" : "";
  $("eng-st").className = "st" + (en ? " live" : "");
  $("eng-hint").textContent = en ? "" : !me ? t("eng.needop") : s && s.mock_engineer_pin ? t("eng.mockpin") : "";
  if (!r) return;
  $("guard-off").hidden = r.obstacle_stop !== false;
  $("guard-profile").disabled = !me || jobRunning("base");
  if (!en) return;

  const ports = r.ports || [];
  const sel = $("sv-port"), cur = sel.value;
  const opts = ports.map((p) => `<option value="${esc(p.dev)}">${esc(p.dev)}${p.dev !== p.real ? " (" + esc(p.real.replace("/dev/", "")) + ")" : ""}</option>`).join("");
  if (sel.dataset.html !== opts) { sel.innerHTML = opts || `<option value="">-</option>`; sel.dataset.html = opts; if (cur) sel.value = cur; }

  const sc = r.servo_scan;
  const cols = t("eng.cols").split("|");
  let tb;
  if (!sc || sc.port !== svPort()) tb = `<tr><td class="mute">${t("eng.noscan")}</td></tr>`;
  else if (!sc.servos.length) tb = `<tr><td class="mute">${t("eng.none")}</td></tr>`;
  else tb = `<tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>` + sc.servos.map((v) =>
    `<tr class="pick${v.id === eng.servo ? " sel" : ""}" data-id="${v.id}"><td>${v.id}</td><td>${v.model ?? "-"}</td><td>${v.volt ?? "-"}</td><td>${v.temp ?? "-"}</td><td>${v.pos ?? "-"}</td><td>${v.mode === 1 ? "vel" : v.mode === 0 ? "pos" : (v.mode ?? "-")}</td><td>${v.torque ? "on" : "off"}</td><td>${v.current != null ? Math.round(Math.abs(v.current) * 6.5) : "-"}</td><td>${v.error ? esc(v.error) : v.status ? "0x" + v.status.toString(16) : "ok"}</td></tr>`).join("");
  const table = $("sv-table");
  if (table.dataset.html !== tb) {
    table.innerHTML = tb; table.dataset.html = tb;
    for (const row of table.querySelectorAll("tr.pick")) row.addEventListener("click", () => {
      eng.servo = +row.dataset.id;
      const v = sc.servos.find((x) => x.id === eng.servo);
      if (v && v.pos != null) $("sv-pos").value = v.pos;
      $("sv-newid").value = eng.servo;
      table.dataset.html = ""; renderEngineer();
    });
  }
  const has = !!(sc && sc.port === svPort() && sc.servos.some((v) => v.id === eng.servo));
  $("sv-one").hidden = !has;
  if (has) $("sv-sel").textContent = `${svPort()} · ID ${eng.servo}`;
  renderWizard();
}

function renderWizard() {
  const steps = eng.steps || [];
  const res = (state.robot && state.robot.wizard) || {};
  const wj = jobs().wizard;
  const status = (k) => (res[k] && res[k].status) || "pending";
  const list = steps.map((st, i) => `<li data-i="${i}" class="${i === eng.sel ? "sel" : ""}"><span>${esc(st.title)}${st.optional ? ` <span class="mute small">(${t("wz.optional")})</span>` : ""}</span><span class="chip ${status(st.key)}">${t("wz." + status(st.key))}</span></li>`).join("");
  const ol = $("wz-steps");
  if (ol.dataset.html !== list) {
    ol.innerHTML = list; ol.dataset.html = list;
    for (const li of ol.querySelectorAll("li")) li.addEventListener("click", () => { eng.sel = +li.dataset.i; ol.dataset.html = ""; $("wz-detail").dataset.html = ""; renderWizard(); });
  }
  const st = steps[eng.sel];
  if (!st) { $("wz-detail").innerHTML = ""; return; }
  const running = !!(wj && wj.running);
  const prep = eng.prep[st.key] || (eng.prep[st.key] = st.prep.map(() => false));
  const li = (a) => a.map((x) => `<li>${esc(x)}</li>`).join("");
  const motion = MOTION_STEPS.has(st.key);
  const html = `<div class="wz">
    <h4>${esc(st.title)}</h4><p>${esc(st.desc)}</p>${LANG === "en" ? `<p class="small mute">${t("eng.kotext")}</p>` : ""}
    ${st.prep.length ? `<b class="small">${t("eng.prep")}</b>${st.prep.map(([txt, req], i) => `<label><input type="checkbox" data-prep="${i}" ${prep[i] ? "checked" : ""}><span>${esc(txt)}${req ? `<span class="req">${t("eng.required")}</span>` : ""}</span></label>`).join("")}` : ""}
    ${st.do.length ? `<b class="small">${t("eng.do")}</b><ol>${li(st.do)}</ol>` : ""}
    ${st.expect.length ? `<b class="small">${t("eng.expect")}</b><ul>${li(st.expect)}</ul>` : ""}
    ${st.fail.length ? `<details><summary>${t("eng.iffail")}</summary><ul>${li(st.fail)}</ul></details>` : ""}
    ${st.key.startsWith("teleop") ? `<div class="row"><label class="small mute" for="wz-sec">${t("eng.seconds")}</label><input id="wz-sec" class="num" type="number" min="5" max="120" value="${eng.secs || 20}"></div>` : ""}
    <div class="row" style="margin-top:10px">
      ${running ? `<button class="btn" id="wz-stop" type="button">${t("eng.stop")}</button>`
        : `<button class="btn primary${motion ? " hold" : ""}" id="wz-run" type="button">${motion ? `<span class="fill"></span>${t("eng.runhold")}` : t("eng.run")}</button>`}
      <span class="grow"></span>
      <button class="btn" id="wz-pass" type="button" ${running ? "disabled" : ""}>${t("eng.pass")}</button>
      <button class="btn" id="wz-fail" type="button" ${running ? "disabled" : ""}>${t("eng.fail")}</button>
      ${st.optional ? `<button class="btn" id="wz-skip" type="button" ${running ? "disabled" : ""}>${t("eng.skip")}</button>` : ""}
    </div></div>`;
  const det = $("wz-detail");
  if (det.dataset.html !== html) {
    det.innerHTML = html; det.dataset.html = html;
    for (const cb of det.querySelectorAll("[data-prep]")) cb.addEventListener("change", () => { prep[+cb.dataset.prep] = cb.checked; det.dataset.html = ""; });
    const sec = $("wz-sec");
    if (sec) sec.addEventListener("change", () => { eng.secs = +sec.value; });
    const run = () => {
      const missing = st.prep.some(([, req], i) => req && !prep[i]);
      if (missing) { showNote(t("eng.prepmissing"), true); return; }
      send({ t: "wizard", step: st.key, on: true, seconds: eng.secs || 20 });
    };
    const rb = $("wz-run");
    if (rb) { if (motion) holdButton(rb, run); else rb.addEventListener("click", run); }
    const sb = $("wz-stop");
    if (sb) sb.addEventListener("click", () => send({ t: "wizard", step: st.key, on: false }));
    for (const [id, v] of [["wz-pass", "pass"], ["wz-fail", "fail"], ["wz-skip", "skip"]]) {
      const b = $(id);
      if (b) b.addEventListener("click", () => send({ t: "wizard_decide", step: st.key, status: v }));
    }
  }
  const lines = running && wj ? wj.tail : (res[st.key] && res[st.key].lines) || [];
  const tl = $("wz-tail");
  tl.hidden = !lines.length;
  if (lines.length) { tl.textContent = lines.join("\n"); tl.scrollTop = tl.scrollHeight; }
}

$("wz-report").addEventListener("click", () => {
  const steps = eng.steps || [], res = (state.robot && state.robot.wizard) || {};
  const now = new Date();
  const md = [`# HAEBOT Robot hardware check`, ``, `Date: ${now.toLocaleString()}`, `Robot: ${state.server ? state.server.backend : "?"}`, ``,
    `| Step | Result | Auto |`, `|---|---|---|`,
    ...steps.map((s) => `| ${s.title} | ${(res[s.key] && res[s.key].status) || "not run"} | ${res[s.key] && res[s.key].auto != null ? res[s.key].auto : "-"} |`), ``,
    ...steps.filter((s) => res[s.key] && res[s.key].lines).flatMap((s) => [`## ${s.title}`, ``, "```", ...res[s.key].lines, "```", ``])].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
  a.download = `kiro_check_${now.toISOString().slice(0, 10)}.md`;
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});

applyStrings();
render();
connect();
