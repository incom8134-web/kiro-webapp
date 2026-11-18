"use strict";
/*
 * KIRO Command Center - browser side.
 *
 * 1. Keeps one WebSocket open to the robot server.
 * 2. Reads three inputs (gamepad, on-screen sticks, keyboard) and picks one.
 * 3. While this device is the operator, sends the drive command 20 times per
 *    second. That message is also the heartbeat: if it stops, the robot stops.
 */

const $ = (id) => document.getElementById(id);
const SEND_MS = 50;      // 20 messages per second
const DEADZONE = 0.12;   // ignore tiny gamepad stick noise
const PX_PER_M = 110;    // map scale

const state = { ws: null, open: false, server: null, rtt: null };
const input = {
  keys: new Set(),
  move: { x: 0, y: 0, active: false },   // left on-screen stick
  turn: { x: 0, y: 0, active: false },   // right on-screen stick
  pad: null,
  padBack: false,
};
const trail = [];
let noteTimer = null;

const isOperator = () => !!state.server && state.server.you.role === "operator";

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
    else if (m.t === "pong") { state.rtt = performance.now() - m.ts; renderLink(); }
    else if (m.t === "note" && !m.ok) showNote(m.msg, true);   // only problems need words
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

/* Pick ONE input. Order: real gamepad (LB held) > on-screen sticks > keyboard (Space held).
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
  const k = input.keys;
  if (k.has("Space")) {
    const axis = (pos, neg) => (k.has(pos) ? 1 : 0) - (k.has(neg) ? 1 : 0);
    return { src: "keyboard", en: true, turbo: turboUi || k.has("ShiftLeft") || k.has("ShiftRight"),
             x: axis("KeyW", "KeyS"), y: axis("KeyA", "KeyD"), z: axis("KeyQ", "KeyE") };
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

/* Anything that could leave the robot moving by mistake clears every input. */
function dropInputs() { input.keys.clear(); endMove(); endTurn(); }
window.addEventListener("blur", dropInputs);
document.addEventListener("visibilitychange", () => { if (document.hidden) dropInputs(); });

const DRIVE_KEYS = new Set(["Space", "KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE", "ShiftLeft", "ShiftRight"]);
window.addEventListener("keydown", (e) => {
  if (e.code === "Escape") { send({ t: "estop", on: true }); return; }
  if (e.target instanceof HTMLInputElement && e.target.type === "password") return; // typing the PIN
  if (DRIVE_KEYS.has(e.code)) {
    input.keys.add(e.code);
    if (e.code === "Space") e.preventDefault(); // no page scroll
  }
});
window.addEventListener("keyup", (e) => input.keys.delete(e.code));
// A focused button would react to Space. Remove focus after every click.
document.addEventListener("click", (e) => {
  const el = e.target.closest("button, summary, input[type=checkbox]");
  if (el) el.blur();
});
window.addEventListener("gamepadconnected", render);
window.addEventListener("gamepaddisconnected", render);

// The send loop. Browsers slow this down in a hidden tab, which stops the robot: good.
setInterval(() => {
  const cur = currentInput();
  const back = pressed(input.pad, 8);          // Back / Select button = E-STOP
  if (back && !input.padBack) send({ t: "estop", on: true });
  input.padBack = back;
  if (isOperator()) send({ t: "drive", x: cur.x, y: cur.y, z: cur.z, en: cur.en, turbo: cur.turbo, src: cur.src });
}, SEND_MS);
setInterval(() => send({ t: "ping", ts: performance.now() }), 1000);

// ------------------------------------------------------------------- buttons
$("take-box").addEventListener("submit", (e) => {
  e.preventDefault();
  const s = state.server;
  let force = false;
  if (s && s.operator && s.you.role !== "operator") {
    force = confirm(`${s.operator.name} is in control. Take over?`);
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

function showNote(text, isError) {
  const n = $("note");
  n.textContent = text; n.classList.toggle("err", !!isError);
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { n.textContent = ""; n.classList.remove("err"); renderHintNote(); }, 4000);
}

function renderHintNote() {
  const s = state.server, n = $("note");
  if (!n.textContent && s && s.default_pin && s.you.role !== "operator") n.textContent = "Mock mode: the PIN is 0000.";
}

// -------------------------------------------------------------------- render
function renderLink() {
  const el = $("link");
  if (!state.open) { el.textContent = "Offline"; el.className = "pill bad"; return; }
  if (state.rtt == null) { el.textContent = "Connecting"; el.className = "pill"; return; }
  el.textContent = `Link ${Math.round(state.rtt)} ms`;
  el.className = "pill " + (state.rtt < 150 ? "ok" : "bad");
}

function render() {
  const s = state.server;
  const me = isOperator();
  renderLink();

  const be = $("backend");
  be.textContent = s ? (s.backend === "ros" ? "ROBOT" : "MOCK") : "...";
  be.className = "badge " + (s ? s.backend : "");

  const op = $("opstat");
  op.textContent = !s || !s.operator ? "No operator" : me ? "You are driving" : `${s.operator.name} is driving`;
  op.className = "pill" + (me ? " me" : "");

  $("take-box").hidden = me;
  $("held-box").hidden = !me;
  $("take").disabled = !s;
  renderHintNote();

  const es = $("estop");
  const stopped = !!s && s.estop;
  es.classList.toggle("on", stopped);
  es.textContent = stopped ? (me ? "RELEASE" : "STOPPED") : "E-STOP";

  const banner = $("banner");
  if (!state.open) {
    banner.hidden = false; banner.className = "banner warn";
    banner.textContent = "Not connected to the robot. Trying again...";
  } else if (stopped) {
    banner.hidden = false; banner.className = "banner";
    banner.textContent = `E-STOP is on${s.estop_by ? " (pressed on " + s.estop_by + ")" : ""}. ` +
      (me ? "Press RELEASE when it is safe." : "Only the operator can release it.");
  } else {
    banner.hidden = true;
  }

  const cmd = s ? s.cmd : { vx: 0, vy: 0, wz: 0 };
  $("vx").textContent = cmd.vx.toFixed(2);
  $("vy").textContent = cmd.vy.toFixed(2);
  $("wz").textContent = cmd.wz.toFixed(2);
  $("src").textContent = s ? s.src : "none";

  $("hint").textContent = !me ? "Take control to drive"
    : stopped ? "E-STOP is on"
    : s.need_neutral ? "Let go of the controls once to start"
    : "Touch a stick, hold LB on the gamepad, or hold Space";
  $("stick-move").classList.toggle("off", !me);
  $("stick-turn").classList.toggle("off", !me);

  const p = input.pad || readPad();
  $("pad").textContent = p ? "Gamepad: " + p.id.replace(/\s*\(.*\)\s*$/, "").slice(0, 34) : "Gamepad: not connected (press a button on it)";

  const o = s && s.odom;
  $("pose").textContent = o ? `x ${o.x.toFixed(2)} m   y ${o.y.toFixed(2)} m   heading ${Math.round(o.th * 180 / Math.PI)}°` : "no position yet";
  $("ver").textContent = s ? `KIRO Command Center v${s.version} · ${s.clients} device${s.clients === 1 ? "" : "s"} connected` : "";
  drawMap();
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
  const sx = (wy) => w / 2 - (wy - o.y) * S;   // world y (left) -> screen x
  const sy = (wx) => h / 2 - (wx - o.x) * S;   // world x (forward) -> screen y

  // grid, one line every 0.5 m
  const step = 0.5 * S;
  g.strokeStyle = "#1a2330"; g.lineWidth = 1; g.beginPath();
  for (let x = ((w / 2 + o.y * S) % step + step) % step; x < w; x += step) { g.moveTo(x, 0); g.lineTo(x, h); }
  for (let y = ((h / 2 + o.x * S) % step + step) % step; y < h; y += step) { g.moveTo(0, y); g.lineTo(w, y); }
  g.stroke();

  // start point
  g.strokeStyle = "#5b6c88"; g.lineWidth = 1.5; g.beginPath();
  g.moveTo(sx(0) - 6, sy(0)); g.lineTo(sx(0) + 6, sy(0)); g.moveTo(sx(0), sy(0) - 6); g.lineTo(sx(0), sy(0) + 6); g.stroke();

  // path driven so far
  const last = trail[trail.length - 1];
  if (last && Math.hypot(o.x - last.x, o.y - last.y) > 1.5) trail.length = 0;   // position was reset
  if (!last || Math.hypot(o.x - last.x, o.y - last.y) > 0.02) { trail.push({ x: o.x, y: o.y }); if (trail.length > 600) trail.shift(); }
  if (trail.length > 1) {
    g.strokeStyle = "rgba(79,156,249,.55)"; g.lineWidth = 2; g.beginPath();
    trail.forEach((p, i) => (i ? g.lineTo(sx(p.y), sy(p.x)) : g.moveTo(sx(p.y), sy(p.x))));
    g.lineTo(w / 2, h / 2); g.stroke();
  }

  // robot body (0.45 m long, 0.35 m wide) with four wheels
  g.save(); g.translate(w / 2, h / 2); g.rotate(-o.th);
  const L = 0.45 * S, W = 0.35 * S;
  g.fillStyle = "#3a4860";
  for (const [wx, wy] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) g.fillRect(wx * (W / 2 + 3) - 4, wy * (L / 2 - 9) - 8, 8, 16);
  g.fillStyle = s && s.estop ? "#5a2322" : "#222d3d"; g.strokeStyle = s && s.estop ? "#f0524f" : "#4f9cf9"; g.lineWidth = 2;
  g.beginPath(); g.roundRect(-W / 2, -L / 2, W, L, 7); g.fill(); g.stroke();
  g.fillStyle = g.strokeStyle; g.beginPath(); g.moveTo(0, -L / 2 + 6); g.lineTo(-7, -L / 2 + 18); g.lineTo(7, -L / 2 + 18); g.closePath(); g.fill();

  // speed arrow
  const cmd = s ? s.cmd : { vx: 0, vy: 0 };
  const ax = -cmd.vy * S * 1.6, ay = -cmd.vx * S * 1.6, al = Math.hypot(ax, ay);
  if (al > 3) {
    g.strokeStyle = "#3ecf8e"; g.fillStyle = "#3ecf8e"; g.lineWidth = 3;
    g.beginPath(); g.moveTo(0, 0); g.lineTo(ax, ay); g.stroke();
    const a = Math.atan2(ay, ax);
    g.beginPath(); g.moveTo(ax, ay); g.lineTo(ax - 9 * Math.cos(a - 0.5), ay - 9 * Math.sin(a - 0.5));
    g.lineTo(ax - 9 * Math.cos(a + 0.5), ay - 9 * Math.sin(a + 0.5)); g.closePath(); g.fill();
  }
  g.restore();

  g.fillStyle = "#56627a"; g.font = "11px system-ui, sans-serif"; g.fillText("grid 0.5 m", 10, h - 10);
}
window.addEventListener("resize", drawMap);

render();
connect();
