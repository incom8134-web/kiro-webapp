"use strict";
/*
 * KIRO Command Center - browser side.
 *
 * 1. Keeps one WebSocket open to the robot server.
 * 2. Reads three inputs (gamepad, on-screen sticks, keyboard) and picks one.
 * 3. While this device is the operator, sends the drive command 20 times per
 *    second. That message is also the heartbeat: if it stops, the robot stops.
 * 4. Shows the camera, arms, skills, voice and health, and sends button presses.
 */

const $ = (id) => document.getElementById(id);
const SEND_MS = 50;      // 20 messages per second
const DEADZONE = 0.12;   // ignore tiny gamepad stick noise
const PX_PER_M = 60;     // map scale (small inset)
const HOLD_MS = 2000;    // hold-to-confirm for dangerous buttons

const state = { ws: null, open: false, server: null, robot: null, rtt: null, cam: null, camKey: null };
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

const DRIVE_KEYS = new Set(["Space", "KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE", "ShiftLeft", "ShiftRight"]);
const typing = (e) => e.target instanceof HTMLInputElement && (e.target.type === "password" || e.target.type === "text");
window.addEventListener("keydown", (e) => {
  if (e.code === "Escape") { send({ t: "estop", on: true }); return; }
  if (typing(e)) return; // typing the PIN
  if (e.code === "KeyV" && !e.repeat && isOperator()) setTalk(true);
  if (DRIVE_KEYS.has(e.code)) {
    input.keys.add(e.code);
    if (e.code === "Space") e.preventDefault(); // no page scroll
  }
});
window.addEventListener("keyup", (e) => {
  input.keys.delete(e.code);
  if (e.code === "KeyV") setTalk(false);
});
// A focused button would react to Space. Remove focus after every click.
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
  b.addEventListener("click", () => { setLang(b.dataset.lang); render(); renderRobot(); });
}

const talk = $("talk");
talk.addEventListener("pointerdown", (e) => { if (!isOperator()) return; talk.setPointerCapture(e.pointerId); setTalk(true); e.preventDefault(); });
for (const n of ["pointerup", "pointercancel", "lostpointercapture"]) talk.addEventListener(n, () => setTalk(false));
talk.addEventListener("contextmenu", (e) => e.preventDefault());

$("base-btn").addEventListener("click", () => send({ t: "base", on: !jobRunning("base") }));
$("voice-btn").addEventListener("click", () => send({ t: "voice", on: !jobRunning("voice") }));

/* Hold a button for HOLD_MS to confirm. Used for shutdown, restart and torque release. */
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
  n.className = "small" + (isError ? " err" : "");
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { n.textContent = ""; n.className = "small"; renderHintNote(); }, 5000);
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
  es.textContent = stopped ? t(me ? "estop.release" : "estop.stopped") : t("estop");

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
  $("src").textContent = s && s.src !== "none" ? s.src : "";

  $("hint").textContent = !me ? t("hint.viewer") : stopped ? t("hint.estop") : s.need_neutral ? t("hint.neutral") : t("hint.go");
  $("stick-move").classList.toggle("off", !me);
  $("stick-turn").classList.toggle("off", !me);
  for (const el of document.querySelectorAll("[data-op]")) el.disabled = !me;

  const p = input.pad || readPad();
  $("pad").textContent = p ? t("pad", { id: p.id.replace(/\s*\(.*\)\s*$/, "").slice(0, 34) }) : t("pad.none");

  const o = s && s.odom;
  $("pose").textContent = o ? t("pose", { x: o.x.toFixed(2), y: o.y.toFixed(2), th: Math.round(o.th * 180 / Math.PI) }) : t("pose.none");
  $("ver").textContent = s ? `KIRO Command Center v${s.version} · ${s.clients} device${s.clients === 1 ? "" : "s"}` : "";
  drawMap();
  renderRobot();
}

// ------------------------------------------------------------- robot panels
const jobs = () => (state.robot && state.robot.jobs) || {};
const jobRunning = (name) => !!(jobs()[name] && jobs()[name].running);

function tail(el, job) {
  if (!job || !job.tail || !job.tail.length) { el.hidden = true; return; }
  el.hidden = false;
  const atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
  el.textContent = job.tail.join("\n");
  if (atEnd) el.scrollTop = el.scrollHeight;
}

function stLine(el, job) {
  el.textContent = job && job.running ? t("skill.running") : "";
  el.className = "st" + (job && job.running ? " live" : "");
}

function renderRobot() {
  const r = state.robot;
  if (!r) return;
  const me = isOperator();
  const J = jobs();

  // Base
  const baseOn = jobRunning("base");
  $("base-btn").textContent = t(baseOn ? "base.stop" : "base.start");
  $("base-btn").classList.toggle("on", baseOn);
  $("base-st").textContent = t(baseOn ? "base.running" : "base.stopped");
  $("base-st").className = "st" + (baseOn ? " live" : "");
  $("base-lidar").textContent = t(r.lidar ? "base.lidar.yes" : "base.lidar.no");
  const bj = J.base;
  $("base-guard").textContent = baseOn && r.voice && r.voice.guard ? `${t("base.guard")}: ${r.voice.guard}` :
    bj && !bj.running && bj.code ? t("base.exited", { code: bj.code }) : "";
  tail($("base-tail"), bj);

  // Arms
  $("left-blocked").hidden = !!r.left_arm_ok;
  const b = (r.health && r.health.boards) || {};
  const armRow = (side) => {
    const f = b[side + "_follower"], l = b[side + "_leader"];
    const blocked = side !== "right" && !r.left_arm_ok;
    const tele = jobRunning("teleop_" + side);
    const dis = (!me || (blocked)) ? "disabled" : "";
    const board = (x, label) => !x ? "" : x.state === "missing" ? `${label} ${t("arm.missing")}` :
      x.state === "busy" ? `${label} ${t("h.inuse", { job: x.by })}` :
      x.state === "ok" ? `${label} ${x.vmin ?? "-"} V · ${x.tmax ?? "-"}°C · ${x.n}/${x.of}` :
      x.state === "silent" ? `${label} ${t("h.noread")}` : `${label} ${x.msg || x.state}`;
    info[side] = side === "both" ? "" : [board(f, t("arm.follower")), board(l, t("arm.leader"))].filter(Boolean).join("\n");
    return `<div class="arm">
      <div class="row"><span class="name grow">${t("arm." + side)}</span>
        <span class="mute small">${t("arm.teleop")}</span>
        <button class="btn${tele ? " on" : ""}" data-tele="${side}" ${dis}>${t(tele ? "arm.off" : "arm.on")}</button></div>
      ${side === "both" ? "" : `<p class="small mute" id="arm-info-${side}"></p>`}
      <div class="row seg">
        <button class="btn" data-arm="${side}" data-act="fold" ${dis}>${t("arm.fold")}</button>
        <button class="btn" data-arm="${side}" data-act="hold" ${dis}>${t("arm.hold")}</button>
        <button class="btn hold" data-arm="${side}" data-act="release" ${me ? "" : "disabled"}><span class="fill"></span>${t("arm.release")}</button>
      </div></div>`;
  };
  const arms = $("arms");
  const info = {};
  const html = ["right", "left", "both"].map(armRow).join("");
  if (arms.dataset.html !== html) {
    arms.innerHTML = html; arms.dataset.html = html;
    for (const el of arms.querySelectorAll("[data-tele]")) el.addEventListener("click", () => send({ t: "teleop", side: el.dataset.tele, on: !jobRunning("teleop_" + el.dataset.tele) }));
    for (const el of arms.querySelectorAll("[data-act]")) {
      if (el.dataset.act === "release") holdButton(el, () => send({ t: "arm", side: el.dataset.arm, action: "release" }));
      else el.addEventListener("click", () => send({ t: "arm", side: el.dataset.arm, action: el.dataset.act }));
    }
  }
  for (const side of ["right", "left"]) { const el = $("arm-info-" + side); if (el) el.innerText = info[side]; }
  const armJob = ["teleop_right", "teleop_left", "teleop_both", "arm_right", "arm_left", "arm_both"].map((n) => J[n])
    .filter(Boolean).sort((a, b2) => (b2.ended || 1e12) - (a.ended || 1e12) || b2.started - a.started)[0];
  tail($("arms-tail"), armJob);
  const anyArm = Object.keys(J).some((n) => (n.startsWith("teleop_") || n.startsWith("arm_")) && J[n].running);
  $("arms-st").textContent = anyArm ? t("skill.running") : "";
  $("arms-st").className = "st" + (anyArm ? " live" : "");

  // Skills
  const sk = J.skill;
  const skillOn = jobRunning("skill");
  const models = r.models || [];
  const sh = models.length ? models.map((m) => `<div class="row"><span class="grow"><b>${esc(m.name)}</b><br><span class="small mute">${esc(m.type.toUpperCase())} · ${esc(t("skill.needs", { cams: m.cams.join(", ") || "-" }))}</span></span>
      <button class="btn${skillOn ? " on" : " primary"}" data-skill="${esc(m.name)}" ${me ? "" : "disabled"}>${t(skillOn ? "skill.stop" : "skill.run")}</button></div>`).join("")
    : `<p class="mute small">${esc(t("skill.none", { dir: "~/policies" }))}</p>`;
  const skills = $("skills");
  if (skills.dataset.html !== sh) {
    skills.innerHTML = sh; skills.dataset.html = sh;
    for (const el of skills.querySelectorAll("[data-skill]")) el.addEventListener("click", () => send({ t: "skill", name: el.dataset.skill, on: !jobRunning("skill") }));
  }
  stLine($("skill-st"), sk);
  tail($("skill-tail"), sk);

  // Voice
  const vOn = jobRunning("voice");
  $("voice-btn").textContent = t(vOn ? "voice.stop" : "voice.start");
  $("voice-btn").classList.toggle("on", vOn);
  $("voice-st").textContent = t(vOn ? "voice.ready" : "voice.off");
  $("voice-st").className = "st" + (vOn ? " live" : "");
  $("talk").disabled = !me || !vOn;
  $("voice-text").textContent = (r.voice && (r.voice.text || r.voice.status)) || "-";
  $("voice-note").hidden = (r.voice_models || []).some((m) => m.includes("-en"));

  // Health
  const h = r.health || {}, jt = h.jetson || {};
  const rows = [];
  rows.push([t("h.jetson"), [jt.temp != null ? `${jt.temp}°C` : null, jt.load != null ? `${t("h.cpu")} ${jt.load}/${jt.cores}` : null,
    jt.mem != null ? `${t("h.mem")} ${jt.mem}%` : null, jt.disk != null ? `${t("h.disk")} ${jt.disk}% (${jt.disk_free_gb} GB)` : null].filter(Boolean).join(" · ")]);
  const names = { right_follower: `${t("arm.right")} ${t("arm.follower")}`, left_follower: `${t("arm.left")} ${t("arm.follower")}`,
    right_leader: `${t("arm.right")} ${t("arm.leader")}`, left_leader: `${t("arm.left")} ${t("arm.leader")}`, base: t("sec.base") };
  for (const [k, label] of Object.entries(names)) {
    const x = b[k];
    if (!x) continue;
    let v = x.state === "missing" ? t("arm.missing") : x.state === "busy" ? t("h.inuse", { job: x.by }) :
      x.state === "silent" ? t("h.noread") : x.state === "ok" ? `<b>${x.vmin ?? "-"} V</b> · ${x.tmax ?? "-"}°C · ${x.n}/${x.of}${x.alarm ? " · ALARM" : ""}` : esc(x.msg || x.state);
    rows.push([label, v]);
  }
  for (const c of r.cams || []) rows.push([`cam ${c.key}`, c.present ? (c.live ? "live" : "ok") : esc(c.reason || t("arm.missing"))]);
  $("health").innerHTML = rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("");

  // Activity
  const ev = (r.events || []).slice().reverse();
  $("log").innerHTML = ev.length ? ev.map((e) => `<li><time>${new Date(e.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time><span>${esc(e.who)}: ${esc(e.text)}</span></li>`).join("")
    : `<li class="mute">${t("log.empty")}</li>`;

  renderCams();
  for (const el of document.querySelectorAll("[data-op]")) if (el.id !== "talk") el.disabled = !me;
}

// ------------------------------------------------------------------ camera
function renderCams() {
  const r = state.robot;
  const cams = (r && r.cams) || [];
  const present = cams.filter((c) => c.present);
  let key = state.camKey;
  if (!key) { try { key = localStorage.getItem("kiro-cam"); } catch (e) { /* ignore */ } }
  if (!present.some((c) => c.key === key)) key = present.length ? present[0].key : null;
  state.camKey = key;

  const tabs = $("camtabs");
  const th = present.length > 1 ? present.map((c) => `<button type="button" data-cam="${esc(c.key)}" class="${c.key === key ? "on" : ""}">${esc(c.key)}</button>`).join("") : "";
  if (tabs.dataset.html !== th) {
    tabs.innerHTML = th; tabs.dataset.html = th;
    for (const b of tabs.querySelectorAll("[data-cam]")) b.addEventListener("click", () => {
      state.camKey = b.dataset.cam;
      try { localStorage.setItem("kiro-cam", b.dataset.cam); } catch (e) { /* ignore */ }
      renderCams();
    });
  }

  const cam = present.find((c) => c.key === key);
  const img = $("cam");
  const missing = cams.filter((c) => !c.present).map((c) => t("cam.reason", { key: c.key, reason: c.reason || "-" })).join(" · ");
  if (!cam) {
    stopCam();
    $("cam-msg").textContent = t("cam.none"); $("cam-why").textContent = missing;
    $("cam-empty").hidden = false; return;
  }
  if (cam.paused_by) {
    stopCam();
    const running = Object.values(jobs()).find((j) => j.running && j.uses.includes("cam:" + cam.key));
    $("cam-msg").textContent = t("cam.paused", { job: running ? running.label : cam.paused_by });
    $("cam-why").textContent = ""; $("cam-empty").hidden = false; return;
  }
  if (state.cam !== key) {
    state.cam = key;
    img.src = `/camera/${encodeURIComponent(key)}.mjpg?t=${Date.now()}`;
  }
  img.hidden = false; $("cam-empty").hidden = true;
}

function stopCam() {
  const img = $("cam");
  if (state.cam) { img.removeAttribute("src"); state.cam = null; }
  img.hidden = true;
}

// The stream ends when a job takes the camera or the server restarts: try again shortly.
$("cam").addEventListener("error", () => { state.cam = null; $("cam").hidden = true; setTimeout(renderCams, 2000); });

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
  g.strokeStyle = "#e4e4e8"; g.lineWidth = 1; g.beginPath();
  for (let x = ((w / 2 + o.y * S) % step + step) % step; x < w; x += step) { g.moveTo(x, 0); g.lineTo(x, h); }
  for (let y = ((h / 2 + o.x * S) % step + step) % step; y < h; y += step) { g.moveTo(0, y); g.lineTo(w, y); }
  g.stroke();

  // start point
  g.strokeStyle = "#6b6b70"; g.lineWidth = 1; g.beginPath();
  g.moveTo(sx(0) - 5, sy(0)); g.lineTo(sx(0) + 5, sy(0)); g.moveTo(sx(0), sy(0) - 5); g.lineTo(sx(0), sy(0) + 5); g.stroke();

  // path driven so far
  const last = trail[trail.length - 1];
  if (last && Math.hypot(o.x - last.x, o.y - last.y) > 1.5) trail.length = 0;   // position was reset
  if (!last || Math.hypot(o.x - last.x, o.y - last.y) > 0.02) { trail.push({ x: o.x, y: o.y }); if (trail.length > 600) trail.shift(); }
  if (trail.length > 1) {
    g.strokeStyle = "#002fa7"; g.lineWidth = 1.5; g.beginPath();
    trail.forEach((p, i) => (i ? g.lineTo(sx(p.y), sy(p.x)) : g.moveTo(sx(p.y), sy(p.x))));
    g.lineTo(w / 2, h / 2); g.stroke();
  }

  // robot body (0.45 m long, 0.35 m wide)
  g.save(); g.translate(w / 2, h / 2); g.rotate(-o.th);
  const L = 0.45 * S, W = 0.35 * S;
  const stop = s && s.estop;
  g.fillStyle = "#ffffff"; g.strokeStyle = stop ? "#e10600" : "#111111"; g.lineWidth = 1.5;
  g.fillRect(-W / 2, -L / 2, W, L); g.strokeRect(-W / 2, -L / 2, W, L);
  g.fillStyle = g.strokeStyle; g.beginPath(); g.moveTo(0, -L / 2 + 3); g.lineTo(-5, -L / 2 + 11); g.lineTo(5, -L / 2 + 11); g.closePath(); g.fill();

  // speed arrow
  const cmd = s ? s.cmd : { vx: 0, vy: 0 };
  const ax = -cmd.vy * S * 3, ay = -cmd.vx * S * 3, al = Math.hypot(ax, ay);
  if (al > 3) {
    g.strokeStyle = "#002fa7"; g.fillStyle = "#002fa7"; g.lineWidth = 2;
    g.beginPath(); g.moveTo(0, 0); g.lineTo(ax, ay); g.stroke();
    const a = Math.atan2(ay, ax);
    g.beginPath(); g.moveTo(ax, ay); g.lineTo(ax - 7 * Math.cos(a - 0.5), ay - 7 * Math.sin(a - 0.5));
    g.lineTo(ax - 7 * Math.cos(a + 0.5), ay - 7 * Math.sin(a + 0.5)); g.closePath(); g.fill();
  }
  g.restore();
  g.fillStyle = "#6b6b70"; g.font = "10px " + getComputedStyle(document.body).fontFamily; g.fillText("0.5 m", 6, h - 6);
}
window.addEventListener("resize", drawMap);

applyStrings();
render();
connect();
