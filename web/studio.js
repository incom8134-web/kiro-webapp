"use strict";
/*
 * HAEBOT Studio - browser side.
 *
 * Locked until this page is the operator AND unlocked the engineer PIN. Only then does the server
 * send Studio data ("studio" messages: datasets, recording, cameras, engineer values) and accept
 * Studio commands. Camera previews use a token the server gives to the engineer connection only.
 *
 * Recording a dataset, top to bottom: 1 dataset, 2 cameras and checks, 3 record, 4 review.
 * Then training a skill: package and download a dataset, Google Colab, upload the trained model, and
 * the training files (datasets, packages, models: download, delete). Below that: the engineer tools
 * (guard distance, servo tester, check wizard) and the read-only engineer information.
 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const HOLD_MS = 2000;
const REPO_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;   // same rule as the server

const st = { ws: null, open: false, server: null, robot: null, studio: null, at: 0, rtt: null, steps: null,
  pendingStart: false, token: null, dismissed: null, offer: null, colabAct: null, pendingAck: null };
const isOperator = () => !!st.server && st.server.you.role === "operator";
const isEngineer = () => !!st.server && !!st.server.you.engineer;
const sess = () => (st.studio && st.studio.session) || null;
const serverNow = () => (st.studio ? st.studio.now + (performance.now() - st.at) / 1000 : Date.now() / 1000);
try { st.dismissed = Number(localStorage.getItem("kiro-studio-dismissed")) || null; } catch (e) { /* ignore */ }

function setBtn(el, text, icon) {
  const span = el.querySelector("span:not(.fill)"), i = el.querySelector("i");
  if (span) span.textContent = text;
  if (i && icon) i.className = icon;
}

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
  st.ws = ws;
  ws.onopen = () => { st.open = true; renderAll(); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === "state") {
      const was = isEngineer();
      st.server = m;
      if (was && !isEngineer()) relock();
      renderTop(); renderLock();
    } else if (m.t === "robot") { st.robot = m; renderTop(); }
    else if (m.t === "studio") {
      if (!isEngineer()) return;
      const first = !st.studio;
      st.studio = m; st.at = performance.now();
      if (m.token !== st.token) { st.token = m.token; resetPreviews(); }
      if (first) firstData();
      renderStudio();
    } else if (m.t === "wizard_steps") { st.steps = m.steps; renderTools(); }
    else if (m.t === "pong") { st.rtt = performance.now() - m.ts; renderLink(); }
    else if (m.t === "note") onNote(m);
  };
  ws.onclose = () => {
    st.open = false; st.server = null; st.rtt = null;
    relock(); renderAll();
    setTimeout(connect, 1000);
  };
  ws.onerror = () => ws.close();
}

function send(obj) {
  if (st.open && st.ws.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify(obj));
}
// The ping keeps this page the operator (a silent operator loses control after 10 s)
setInterval(() => send({ t: "ping", ts: performance.now() }), 1000);

/* Locked again (Lock button, another operator, connection lost): forget every Studio value. */
function relock() {
  st.studio = null; st.token = null; st.steps = null; st.pendingStart = false;
  resetPreviews();
  if (tr.xhr) tr.xhr.abort();   // the server stops taking it anyway
  tr.confirm = null;
  for (const id of ["camgrid", "cklist", "info", "v-table", "ds-info", "repo-pick", "sv-table", "wz-steps", "wz-detail",
    "t-ds", "t-pkg", "fm-ds", "fm-pkg", "fm-models"]) {
    const el = $(id);
    el.innerHTML = ""; delete el.dataset.html; delete el.dataset.keys;
  }
  for (const id of ["r-log", "wz-tail", "v-err"]) $(id).textContent = "";
}

// ---------------------------------------------------------------- notes
let toastTimer = null;
function toast(text, isError) {
  if (!text) return;
  const n = $("toast");
  n.textContent = text; n.hidden = false;
  n.className = "toast" + (isError ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { n.hidden = true; }, 5000);
}
const CODES = { CHECKS_FAILED: "rec.n.checks", TOO_SOON: "rec.n.soon", NOT_NOW: "rec.n.notnow", STOPPING: "rec.n.stopping",
  FORCE_SENT: "rec.n.force", RUNNING: "rec.n.running", NO_DATASET: "rec.n.nods", LEFT_BLOCKED: "arm.leftblocked",
  NOT_FOUND: "fm.e.notfound", IN_USE: "fm.e.inuse", COLAB_BUSY: "col.e.COLAB_BUSY", COLAB_ADDR: "col.e.COLAB_ADDR",
  COLAB_NORUN: "col.e.COLAB_NORUN", COLAB_MIDWAY: "col.e.COLAB_MIDWAY" };
function onNote(m) {
  const text = CODES[m.msg] ? t(CODES[m.msg]) : m.msg;
  if (m.about === "take" || m.about === "engineer") {
    if (!m.ok) { $("lock-hint").textContent = text; $("lock-hint").className = "small note err"; }
    else if (m.about === "engineer") toast(t("studio.unlockedmsg"));
    return;
  }
  if (m.about === "studio") {
    if (m.cmd === "start") st.pendingStart = false;
    if (m.msg === "STALE" || (m.ok && m.cmd === "check")) { renderStudio(); return; }   // a late double click: nothing to say
    if (m.ok && m.cmd === "start") toast(t("rec.n.started"));
    else if (m.ok && m.cmd === "pack") toast(t("rec.n.packing"));
    else if (m.ok && m.cmd === "delete") toast(t("fm.deleted"));
    else if (m.cmd === "colab") {
      if (st.pendingAck && st.colabAct === "connect" && bc) { bc.postMessage({ t: "ack", id: st.pendingAck, ok: m.ok, msg: text }); st.pendingAck = null; }
      if (st.offer && st.offer.state === "sending" && st.colabAct === "connect") {
        st.offer.state = m.ok ? "sent" : "error"; st.offer.err = m.ok ? "" : text; renderOffer();
      }
      if (m.ok && st.colabAct === "start") toast(t("col.n.started"));
      else if (m.ok && st.colabAct === "connect") toast(t("col.n.connected"));
      else if (!m.ok) toast(text, true);
    }
    else if (!m.ok) toast(text, true);
    renderStudio();
    return;
  }
  if (!m.ok) toast(text, true);
}

// --------------------------------------------------------------- top bar
function renderLink() {
  const el = $("link");
  if (!st.open) { el.textContent = t("link.offline"); el.className = ""; return; }
  if (st.rtt == null) { el.textContent = t("link.connecting"); el.className = ""; return; }
  el.textContent = `${Math.round(st.rtt)} ms`;
  el.className = st.rtt < 150 ? "accent" : "";
}

function renderTop() {
  const s = st.server, me = isOperator();
  applyStrings();
  document.title = "HAEBOT · " + t("studio.name");
  for (const b of document.querySelectorAll(".lang button")) b.classList.toggle("on", b.dataset.lang === LANG);
  renderLink();
  $("backend").textContent = s ? t(s.backend === "ros" ? "be.ros" : "be.mock") : "...";
  const op = $("opstat");
  op.textContent = !s || !s.operator ? t("op.none") : me ? t("op.youstudio") : t("op.other", { name: s.operator.name });
  op.className = me ? "accent" : "";
  const stopped = !!s && s.estop;
  $("estop").classList.toggle("on", stopped);
  $("estop-label").textContent = stopped ? t(me ? "estop.release" : "estop.stopped") : t("estop");
  const banner = $("banner");
  if (!st.open) { banner.hidden = false; banner.className = "banner"; banner.textContent = t("banner.offline"); }
  else if (stopped) {
    banner.hidden = false; banner.className = "banner stop";
    banner.textContent = t("banner.estop", { by: s.estop_by ? t("banner.estop.by", { name: s.estop_by }) : "" }) +
      t(me ? "banner.estop.op" : "banner.estop.viewer");
  } else banner.hidden = true;
  $("ver").textContent = s ? `HAEBOT Robot v${s.version} · ${t("studio.name")}` : "";
}

$("estop").addEventListener("click", () => send({ t: "estop", on: !(st.server && st.server.estop) }));
window.addEventListener("keydown", (e) => { if (e.code === "Escape" || e.key === "Escape") send({ t: "estop", on: true }); });
for (const b of document.querySelectorAll(".lang button")) b.addEventListener("click", () => { setLang(b.dataset.lang); renderAll(); });
$("theme-btn").addEventListener("click", () => toggleTheme());
window.addEventListener("kiro-theme", () => themeIcon($("theme-btn")));
themeIcon($("theme-btn"));
// A focused button would react to Space. Remove focus after every click (as on the control page).
document.addEventListener("click", (e) => { const el = e.target.closest("button, summary"); if (el) el.blur(); });

// ------------------------------------------------------------------ lock
function renderLock() {
  const s = st.server, en = isEngineer(), me = isOperator();
  if (en) st.seenEng = true;
  $("lock").hidden = en || !st.open;
  $("studio").hidden = !en;
  $("studio-more").hidden = !en;
  // The training guide stays once it was seen: following it means leaving the robot's Wi-Fi (step 2)
  $("tsect").hidden = !en && !st.seenEng;
  $("tsect").classList.toggle("away", !en);
  if (en) return;
  $("op-pin-row").hidden = me;
  const other = s && s.operator && !me;
  $("lock-other").hidden = !other;
  if (other) $("lock-other").textContent = t("studio.other", { name: s.operator.name });
  $("lock-op").hidden = !me;
  $("unlock-btn").disabled = !s;
  const hint = $("lock-hint");
  if (!hint.classList.contains("err")) {
    const mock = [s && s.default_pin && !me ? t("mockpin") : "", s && s.mock_engineer_pin ? t("eng.mockpin") : ""].filter(Boolean);
    hint.textContent = mock.join(" ");
  }
}

$("unlock").addEventListener("submit", (e) => {
  e.preventDefault();
  const s = st.server;
  if (!s) return;
  $("lock-hint").className = "small mute"; $("lock-hint").textContent = "";
  if (!isOperator()) {
    let force = false;
    if (s.operator) {
      force = confirm(t("takeover", { name: s.operator.name }));
      if (!force) return;
    }
    send({ t: "take", pin: $("op-pin").value, force });
  }
  send({ t: "engineer", pin: $("eng-pin").value });   // handled after "take": the server reads messages in order
  $("op-pin").value = ""; $("eng-pin").value = "";
  document.activeElement && document.activeElement.blur();
});
$("release").addEventListener("click", () => send({ t: "release" }));
$("lock-btn").addEventListener("click", () => send({ t: "engineer", lock: true }));

// -------------------------------------------------------------- the form
/* The dataset settings, remembered on this device (no PINs). */
const form = { arm: "right", mode: "new", repo: "", pick: "", task: "", episodes: 10, episode_s: 30, reset_s: 10,
  fps: 30, vcodec: "h264", streaming: true, flicker: false, cams: null };
try { Object.assign(form, JSON.parse(localStorage.getItem("kiro-studio-form") || "{}")); } catch (e) { /* ignore */ }
const saveForm = () => { try { localStorage.setItem("kiro-studio-form", JSON.stringify(form)); } catch (e) { /* ignore */ } };
const repoOf = () => (form.mode === "add" ? form.pick : form.repo.trim());
const datasets = () => (st.studio && st.studio.datasets) || [];
const dsOf = (repo) => datasets().find((d) => d.repo === repo);
const camKeys = () => ((st.studio && st.studio.cams) || []).map((c) => c.key);
function camsSel() {
  if (Array.isArray(form.cams)) return form.cams.slice();
  const want = (st.studio && st.studio.cams_for && st.studio.cams_for[form.arm]) || [];
  const have = camKeys();
  return want.filter((k) => have.includes(k));
}
function settings() {
  return { arm: form.arm, mode: form.mode, repo: repoOf(), task: form.task.trim().split(/\s+/).filter(Boolean).join(" "),
    episodes: +form.episodes, episode_s: +form.episode_s, reset_s: +form.reset_s, fps: +form.fps,
    vcodec: form.vcodec, streaming: !!form.streaming, flicker: !!form.flicker, cams: camsSel() };
}
function sameSettings(a, b) {
  if (!a || !b) return false;
  for (const k of ["arm", "mode", "repo", "task", "episodes", "episode_s", "reset_s", "fps", "vcodec", "streaming", "flicker"]) if (a[k] !== b[k]) return false;
  return [...a.cams].sort().join() === [...b.cams].sort().join();
}
const check = () => (st.studio && st.studio.check) || null;
const checkFresh = () => !!check() && sameSettings(check().settings, settings());

function fillInputs() {
  for (const k of ["repo", "task", "episodes", "episode_s", "reset_s", "fps"]) $(k).value = form[k];
  $("vcodec").value = form.vcodec;
  $("streaming").checked = !!form.streaming;
  $("flicker").checked = !!form.flicker;
}
for (const k of ["repo", "task", "episodes", "episode_s", "reset_s", "fps"]) {
  $(k).addEventListener("input", () => { form[k] = $(k).value; saveForm(); renderStudio(); });
}
$("vcodec").addEventListener("change", () => { form.vcodec = $("vcodec").value; saveForm(); renderStudio(); });
for (const k of ["streaming", "flicker"]) $(k).addEventListener("change", () => { form[k] = $(k).checked; saveForm(); renderStudio(); });
$("repo-pick").addEventListener("change", () => { form.pick = $("repo-pick").value; adoptDataset(); saveForm(); renderStudio(); });
for (const b of document.querySelectorAll("#arm-seg button")) b.addEventListener("click", () => {
  form.arm = b.dataset.v; form.cams = null; saveForm(); renderStudio();
});
for (const b of document.querySelectorAll("#mode-seg button")) b.addEventListener("click", () => setMode(b.dataset.v));

function setMode(mode, repo) {
  form.mode = mode;
  if (mode === "add") {
    form.pick = repo || (dsOf(form.repo.trim()) ? form.repo.trim() : form.pick) || (datasets()[0] || {}).repo || "";
    adoptDataset();
  } else if (repo !== undefined) form.repo = repo;
  saveForm(); fillInputs(); renderStudio();
}

/* Adding episodes: the arm, cameras and frame rate must be those of the dataset. */
function adoptDataset() {
  const d = dsOf(form.pick);
  if (!d) return;
  if (d.robot_type) form.arm = d.robot_type === "bi_so_follower" ? "both" : form.arm === "both" ? "right" : form.arm;
  if (d.cams) form.cams = d.cams.slice();
  if (d.fps) form.fps = d.fps;
  fillInputs();
}

/* First Studio data after unlocking: defaults for an empty form (user part of the newest dataset). */
function firstData() {
  if (!form.repo) {
    const d = datasets()[0];
    form.repo = d ? d.repo.split("/")[0] + "/" : "";
  }
  fillInputs();
}

$("go-check").addEventListener("click", () => {
  send({ t: "studio", cmd: "check", settings: settings() });
  $("s2").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("recheck").addEventListener("click", () => send({ t: "studio", cmd: "check", settings: settings() }));

/* Hold a button for HOLD_MS to confirm (start recording, force stop, servo writes). */
function holdButton(el, action) {
  let timer = null, start = 0, raf = 0;
  const fill = el.querySelector(".fill");
  const reset = () => { clearTimeout(timer); cancelAnimationFrame(raf); timer = null; if (fill) fill.style.width = "0"; };
  const step = () => { if (fill) fill.style.width = Math.min(100, (performance.now() - start) / HOLD_MS * 100) + "%"; raf = requestAnimationFrame(step); };
  el.addEventListener("pointerdown", (e) => {
    if (el.disabled) return;
    el.setPointerCapture(e.pointerId); start = performance.now(); step();
    timer = setTimeout(() => { reset(); if (el.isConnected) action(); }, HOLD_MS);   // re-rendered while held: not this button any more
  });
  for (const n of ["pointerup", "pointercancel", "lostpointercapture"]) el.addEventListener(n, reset);
  el.addEventListener("contextmenu", (e) => e.preventDefault());
}
holdButton($("start"), () => {
  if ($("start").disabled) return;
  st.pendingStart = true;   // no second start until the server answered (double click, slow Wi-Fi)
  send({ t: "studio", cmd: "start", settings: settings() });
  renderStudio();
});

// ------------------------------------------------------------- recording
const recCmd = (cmd) => { const s = sess(); if (s) send({ t: "studio", cmd, ep: s.ep, phase: s.phase }); };
$("r-next").addEventListener("click", () => recCmd("next"));
$("r-redo").addEventListener("click", () => recCmd("redo"));
$("r-stop").addEventListener("click", () => recCmd("stop"));
holdButton($("r-kill"), () => send({ t: "studio", cmd: "kill" }));
function dismiss() {
  const s = sess();
  if (s) { st.dismissed = s.started; try { localStorage.setItem("kiro-studio-dismissed", String(s.started)); } catch (e) { /* ignore */ } }
}
$("v-more").addEventListener("click", () => {
  const s = sess();
  dismiss();
  if (s) { setMode("add", s.settings.repo); form.task = s.settings.task; fillInputs(); saveForm(); }
  renderStudio();
  $("s1").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("v-new").addEventListener("click", () => {
  const s = sess();
  dismiss();
  setMode("new", s ? s.settings.repo.split("/")[0] + "/" : form.repo);
  $("s1").scrollIntoView({ behavior: "smooth", block: "start" });
  $("repo").focus();
});
$("v-pack").addEventListener("click", () => { const s = sess(); if (s) send({ t: "studio", cmd: "pack", repo: s.settings.repo }); });

/* Which step is the current one. */
function stepNow() {
  const s = sess();
  if (s && !s.result) return 3;
  if (s && s.result && s.started !== st.dismissed) return 4;
  return checkFresh() ? 2 : 1;
}
const ACTIVE = ["starting", "tuning", "connecting", "rec", "reset", "saving"];
const running = () => { const s = sess(); return !!s && !s.result; };

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), r = s % 60;
  return m ? t("rec.dur.m", { m, s: r }) : t("rec.dur.s", { s: r });
}

// ---------------------------------------------------------------- render
function renderAll() { renderTop(); renderLock(); renderStudio(); renderOffer(); }

function renderStudio() {
  if (!isEngineer() || !st.studio) return;
  const step = stepNow();
  for (const li of document.querySelectorAll("#stepper li")) {
    const n = +li.dataset.s;
    li.className = n === step ? "on" : n < step ? "done" : "";
  }
  const busy = running() || step === 4;
  setCard("s1", busy ? "done" : "open");
  setCard("s2", busy ? "done" : "open");
  setCard("s3", step === 3 ? "open" : step === 4 ? "done" : "todo");
  setCard("s4", step === 4 ? "open" : "todo");
  renderSetup(step);
  renderChecks(step);
  renderRecord(step);
  renderReview(step);
  renderTrain();
  renderLidar();
  renderTools();
  renderInfo();
  updatePreviews();
}

function setCard(id, mode) {
  const el = $(id);
  el.classList.toggle("open", mode === "open");
  el.classList.toggle("done", mode === "done");
  el.classList.toggle("todo", mode === "todo");
}

function renderSetup(step) {
  const r = st.robot || {}, s = st.studio;
  const leftOk = !!(r.left_arm_ok || (s.info && s.info.left_arm_ok));
  for (const b of document.querySelectorAll("#arm-seg button")) {
    b.classList.toggle("on", b.dataset.v === form.arm);
    b.disabled = b.dataset.v !== "right" && !leftOk;
  }
  $("arm-note").hidden = leftOk;
  $("arm-note").textContent = leftOk ? "" : t("arm.leftblocked");
  for (const b of document.querySelectorAll("#mode-seg button")) b.classList.toggle("on", b.dataset.v === form.mode);
  $("repo-new").hidden = form.mode !== "new";
  $("repo-add").hidden = form.mode !== "add";
  const opts = datasets().map((d) => `<option value="${esc(d.repo)}">${esc(d.repo)} · ${t("rec.nep", { n: d.episodes })}</option>`).join("")
    || `<option value="" disabled>${esc(t("rec.nods"))}</option>`;
  const pick = $("repo-pick");
  if (pick.dataset.html !== opts) { pick.innerHTML = opts; pick.dataset.html = opts; }
  if (form.mode === "add" && !dsOf(form.pick) && datasets().length) { form.pick = datasets()[0].repo; adoptDataset(); }
  pick.value = form.pick;

  const repo = repoOf(), d = dsOf(repo), path = `${s.root}/${repo}`;
  let html;
  if (form.mode === "new") {
    if (!repo || repo.endsWith("/")) html = `<span class="mute">${esc(t("rec.name.hint"))}</span>`;
    else if (!REPO_RE.test(repo)) html = `<span class="warn">${esc(t("fix.name.format"))}</span>`;
    else if (d) html = `<span class="warn">${esc(t("rec.taken", { n: d.episodes }))}</span> <button type="button" class="linkbtn" id="use-existing">${esc(t("rec.addinstead"))}</button>`;
    else html = `<span>${esc(t("rec.newat"))}</span> <code>${esc(path)}</code>`;
  } else if (d) {
    html = `<b>${esc(t("rec.nep", { n: d.episodes }))}</b> · ${esc(t("rec.nframes", { n: d.frames.toLocaleString() }))} · ${esc(d.fps)} fps · ${esc((d.cams || []).join(", ") || "-")} · ${esc(d.mb ?? "-")} MB`
      + `<br><span class="mute">${esc(t("rec.addat"))}</span> <code>${esc(d.path)}</code><br><span class="mute">${esc(t("rec.addsafe"))}</span>`;
  } else html = `<span class="mute">${esc(t("rec.nods"))}</span>`;
  const info = $("ds-info");
  if (info.dataset.html !== html) {
    info.innerHTML = html; info.dataset.html = html;
    const u = $("use-existing");
    if (u) u.addEventListener("click", () => setMode("add", repo));
  }
  const task = settings().task;
  const err = !task ? "" : !/^[\x20-\x7e]*$/.test(task) ? t("fix.task.english") : "";
  $("s1-err").hidden = !err;
  $("s1-err").textContent = err;
  const n = +form.episodes || 0, es = +form.episode_s || 0, rs = +form.reset_s || 0;
  $("s1-total").textContent = n > 0 ? t("rec.total", { d: fmtDur(n * es + Math.max(0, n - 1) * rs) }) : "";
  const goPrimary = step === 1 && !checkFresh();   // exactly one blue button: the next thing to do
  $("go-check").classList.toggle("primary", goPrimary);
  $("go-check").disabled = running();
  for (const el of $("s1").querySelectorAll("input, select, .segbar button")) if (running()) el.disabled = true;
  if (!running()) for (const el of $("s1").querySelectorAll("input, select, #mode-seg button")) el.disabled = false;
  const x = settings();
  $("s1-sum").textContent = `${t("rec.arm." + x.arm)} · ${x.repo || "-"} · ${t("rec.nep", { n: x.episodes })} × ${x.episode_s} s`;
  $("s1-st").textContent = step > 1 ? "✓" : "";
  $("s1-st").className = "st" + (step > 1 ? " live" : "");
}

const PARTS = { right_follower: ["arm.right", "arm.follower"], left_follower: ["arm.left", "arm.follower"],
  right_leader: ["arm.right", "arm.leader"], left_leader: ["arm.left", "arm.leader"] };
const camName = (k) => (STRINGS.en["cam." + k] ? t("cam." + k) : k);
function rowLabel(r) {
  const a = r.a || {};
  if (r.k === "ck.port") return t("ck.port", { part: (PARTS[a.part] || [a.part]).map((x) => t(x)).join(" ") });
  if (r.k === "ck.cam") return t("ck.cam", { name: camName(a.key) });
  if (r.k === "ck.number") return t("rec.n_" + a.what);
  if (r.k === "ck.ds") return t(a.mode === "add" ? "ck.ds.add" : "ck.ds.new");
  if (r.k === "ck.arm") return t("ck.arm", { arm: t("rec.arm." + a.arm) });
  return t(r.k, a);
}
function fixText(r) {
  if (!r.fix) return "";
  const fa = { ...(r.fa || {}) };
  if (fa.what) fa.what = t("rec.n_" + fa.what);
  return t(r.fix, fa);
}

function renderChecks(step) {
  const s = st.studio, c = check(), fresh = checkFresh(), sel = camsSel(), run = running();
  // camera tiles: built once per camera list, then updated
  const cams = s.cams || [];
  const grid = $("camgrid"), keys = cams.map((x) => x.key).join();
  if (grid.dataset.keys !== keys) {
    grid.innerHTML = cams.map((x) => `<div class="camtile" data-cam="${esc(x.key)}">
      <div class="qbody"><img alt="" hidden><div class="empty"><i class="ri-camera-off-line"></i><span class="msg"></span></div></div>
      <div class="camfoot"><label class="check"><input type="checkbox" data-pick="${esc(x.key)}"><span class="cname"></span></label><span class="cst"></span></div>
    </div>`).join("") || `<p class="mute small">${esc(t("rec.nocams"))}</p>`;
    grid.dataset.keys = keys;
    for (const cb of grid.querySelectorAll("[data-pick]")) cb.addEventListener("change", () => {
      const cur = camsSel();
      form.cams = cb.checked ? [...new Set([...cur, cb.dataset.pick])] : cur.filter((k) => k !== cb.dataset.pick);
      saveForm(); renderStudio();
    });
  }
  for (const x of cams) {
    const tile = grid.querySelector(`[data-cam="${CSS.escape(x.key)}"]`);
    if (!tile) continue;
    const ok = x.present && !x.auto;
    tile.querySelector(".cname").textContent = camName(x.key);
    const cst = tile.querySelector(".cst");
    cst.textContent = ok ? t("rec.cam.ok") : x.present && x.auto ? t("rec.cam.auto", { port: x.auto }) : t("cam.notconnected");
    cst.className = "cst " + (ok ? "ok" : "warn");
    const cb = tile.querySelector("[data-pick]");
    cb.checked = sel.includes(x.key);
    cb.disabled = run || form.mode === "add";
    tile.classList.toggle("off", !sel.includes(x.key));
  }
  // check results
  const rows = c ? c.rows : [];
  const list = $("cklist");
  const html = !c ? `<p class="mute small">${esc(t("rec.nocheck"))}</p>`
    : rows.map((r) => `<div class="chk" data-st="${r.st === "ok" ? "ok" : "warn"}"><i class="dot"></i><div class="chk-t">`
      + `<b>${esc(rowLabel(r))}${r.st === "fail" ? ` <span class="tag">${esc(t("rec.blocks"))}</span>` : ""}</b>`
      + (r.d ? `<span class="mono">${esc(r.d)}</span>` : "") + (r.fix ? `<small>${esc(fixText(r))}</small>` : "") + `</div></div>`).join("");
  if (list.dataset.html !== html) { list.innerHTML = html; list.dataset.html = html; }
  const nbad = rows.filter((r) => r.st === "fail").length;
  const sum = $("ck-sum");
  sum.textContent = !c ? t("rec.ck.none") : !fresh ? t("rec.ck.stale") : nbad ? t("rec.ck.bad", { n: nbad }) : t("rec.ck.ok");
  sum.dataset.st = !c ? "" : !fresh || nbad ? "warn" : "ok";
  const estop = !!(st.server && st.server.estop);
  const canStart = fresh && c.ok && !run && !estop && !st.pendingStart && !s.starting;
  $("start").disabled = !canStart;
  $("start").classList.toggle("primary", canStart);
  $("recheck").classList.toggle("primary", !!c && fresh && !c.ok && step <= 2);
  $("recheck").disabled = run;
  setBtn($("start"), t(st.pendingStart || s.starting ? "rec.starting" : "rec.start"));
  $("s2-sum").textContent = c && fresh ? (c.ok ? t("rec.ck.ok") : t("rec.ck.bad", { n: nbad })) : "";
  $("s2-st").textContent = step > 2 ? "✓" : "";
  $("s2-st").className = "st" + (step > 2 ? " live" : "");
}

const PHASE = { starting: ["rec.p.connect", "accent"], tuning: ["rec.p.tune", "accent"], connecting: ["rec.p.connect", "accent"],
  rec: ["rec.p.rec", "live"], reset: ["rec.p.reset", "warn"], saving: ["rec.p.save", "accent"],
  done: ["rec.p.done", "live"], stopped: ["rec.p.stopped", "warn"], failed: ["rec.p.failed", "warn"], estop: ["rec.p.estop", "warn"] };

function renderRecord(step) {
  const s = sess();
  if (!s || step < 3) {
    $("s3-sum").textContent = t("rec.s3.wait");
    $("s3-st").textContent = "";
    $("s3-st").className = "st";
    return;
  }
  const set = s.settings, n = set.episodes;
  const k = s.ep == null ? null : s.ep - s.base + 1;
  const [pk, pc] = PHASE[s.phase] || ["rec.p.connect", "accent"];
  const ph = $("r-phase");
  ph.textContent = s.stopping && ACTIVE.includes(s.phase) && s.phase !== "saving" ? t("rec.p.stopping") : t(pk);
  ph.className = "phase " + pc;
  $("r-what").textContent = `${t("rec.arm." + set.arm)} · ${set.repo}`;
  $("r-ep").textContent = k == null ? "-" : t("rec.kofn", { k: Math.min(k, n), n });
  $("r-saved").textContent = String(s.result ? s.result.added : s.saved || 0);
  tickTimer(false);
  const p = s.phase, live = (p === "rec" || p === "reset") && s.running;
  const age = serverNow() - s.since;
  const can = live && !s.sent && !s.stopping;
  const last = k != null && k >= n;
  setBtn($("r-next"), t(p === "reset" ? "rec.nextnow" : last ? "rec.finish" : "rec.next"));
  $("r-next").disabled = !can || age < 1;
  $("r-next").classList.toggle("primary", can);
  $("r-redo").disabled = !can || age < 1;
  $("r-stop").disabled = !live || s.stopping || age < 1;
  $("r-kill").disabled = !s.running;
  $("r-hint").textContent = s.result ? "" : t({ starting: "rec.h.connect", tuning: "rec.h.tune", connecting: "rec.h.connect", rec: "rec.h.rec",
    reset: "rec.h.reset", saving: "rec.h.save" }[p] || "rec.h.connect");
  $("r-explain").textContent = live ? t(p === "reset" ? "rec.x.reset" : "rec.x.rec") : "";
  const log = $("r-log"), text = (s.log || []).join("\n");
  if (log.textContent !== text) { log.textContent = text; log.scrollTop = log.scrollHeight; }
  $("s3-st").textContent = t(pk);
  $("s3-st").className = "st " + pc;
  $("s3-sum").textContent = s.result ? t("rec.s3.end", { n: s.result.added }) : "";
}

/* The countdown runs between the server's updates (4 times a second). The buttons wait until the
   phase ran 1 s (as the server does); the timer re-renders them once that second has passed. */
function tickTimer(rerender = true) {
  const s = sess();
  if (!s || !isEngineer()) return;
  const len = s.phase === "rec" ? s.settings.episode_s : s.phase === "reset" ? s.settings.reset_s : 0;
  const el = serverNow() - s.since;
  const live = len > 0 && s.running && !s.result;
  $("r-left").textContent = live ? `${Math.ceil(Math.max(0, len - el))} s` : "-";
  $("r-left-k").textContent = t(s.phase === "reset" ? "rec.leftreset" : "rec.left");
  $("r-bar").style.width = live ? Math.min(100, (el / len) * 100) + "%" : s.result ? "100%" : "0";
  $("r-bar").className = s.phase === "reset" ? "warn" : "";
  if (rerender && live && el >= 1 && el < 2 && !s.sent && !s.stopping) renderRecord(3);
}
setInterval(tickTimer, 250);

function renderReview(step) {
  const s = sess();
  if (step !== 4 || !s || !s.result) {
    $("s4-sum").textContent = t("rec.s4.wait");
    $("s4-st").textContent = "";
    $("s4-st").className = "st";
    return;
  }
  const res = s.result, i = res.info || {};
  const msg = $("v-msg");
  msg.textContent = t({ done: "rec.v.done", stopped: "rec.v.stopped", estop: "rec.v.estop" }[res.state] || "rec.v.failed",
    { hint: t("rec.hint." + (res.hint || "other")), code: res.code });
  msg.className = "vmsg " + (res.state === "done" ? "ok" : "warn");
  const rows = [[t("rec.dataset"), `<b>${esc(s.settings.repo)}</b>`]];
  if (i.ok) {
    rows.push([t("rec.eps"), `<b>${esc(i.episodes)}</b> <span class="mute">(${esc(t("rec.added", { n: res.added }))})</span>`],
      [t("rec.frames"), esc((i.frames || 0).toLocaleString())], [t("rec.fps"), esc(i.fps)],
      [t("rec.cams"), esc((i.cams || []).map(camName).join(", ") || "-")], [t("rec.size"), `${esc(i.mb ?? "-")} MB`]);
  } else rows.push([t("rec.eps"), `<span class="mute">${esc(t("rec.v.nods"))}</span>`]);
  rows.push([t("rec.where"), `<code>${esc(i.path || "")}</code>`]);
  if (s.backup) rows.push([t("rec.backup"), `<code>${esc(s.backup)}</code>`]);
  const html = rows.map(([a, b]) => `<tr><th>${esc(a)}</th><td>${b}</td></tr>`).join("");
  const tb = $("v-table");
  if (tb.dataset.html !== html) { tb.innerHTML = html; tb.dataset.html = html; }
  const err = $("v-err");
  err.hidden = res.state !== "failed" || !(s.errors || []).length;
  err.textContent = (s.errors || []).join("\n");
  const pk = st.studio.pack, mine = pk && pk.repo === s.settings.repo && pk.started > s.started;   // a package of this recording
  $("v-pack").disabled = !i.ok || (pk && pk.running);
  $("v-pack-st").textContent = !mine ? t("rec.packwhat") : pk.running ? t("rec.packing") : pk.ok ? t("rec.packed", { file: pk.file })
    : t("rec.packfail", { code: pk.code });
  $("v-pack-st").className = "small " + (mine && !pk.running && !pk.ok ? "note err" : mine && pk.ok ? "accent" : "mute");
  $("s4-st").textContent = t(PHASE[res.state][0]);
  $("s4-st").className = "st " + PHASE[res.state][1];
  $("s4-sum").textContent = "";
}

// --------------------------------------------------------- camera previews
/* Previews only while the checks step is open and the page is visible: each one uses Wi-Fi, and
   the recorder needs the cameras. The address carries this connection's token; it stops working
   when the Studio locks. */
function resetPreviews() {
  for (const img of document.querySelectorAll(".camtile img")) { img.removeAttribute("src"); delete img.dataset.on; img.hidden = true; }
}
function updatePreviews() {
  const s = st.studio;
  const want = !!s && isEngineer() && !!st.token && !document.hidden && !running() && stepNow() <= 2;
  for (const x of (s && s.cams) || []) {
    const tile = document.querySelector(`.camtile[data-cam="${CSS.escape(x.key)}"]`);
    if (!tile) continue;
    const img = tile.querySelector("img"), empty = tile.querySelector(".empty"), msg = tile.querySelector(".msg");
    const why = !want ? t("rec.cam.paused") : !x.present ? t("cam.notconnected") : x.paused_by ? t("cam.inuse") : x.error ? t("cam.busy") : "";
    if (why) {
      if (img.dataset.on) { img.removeAttribute("src"); delete img.dataset.on; }
      img.hidden = true; empty.hidden = false; msg.textContent = why;
      continue;
    }
    if (!img.dataset.on) {
      img.dataset.on = "1";
      img.onload = () => { img.hidden = false; empty.hidden = true; };
      img.onerror = () => { delete img.dataset.on; img.hidden = true; empty.hidden = false; msg.textContent = t("cam.connecting"); setTimeout(updatePreviews, 2000); };
      img.src = `/api/studio/camera/${encodeURIComponent(x.key)}.mjpg?token=${encodeURIComponent(st.token)}&t=${Date.now()}`;
      msg.textContent = t("cam.connecting");
    }
  }
}
document.addEventListener("visibilitychange", updatePreviews);

// -------------------------------------------------------- train a skill
/* Dataset package -> Google Colab -> model zip -> Autonomous skills. Downloads carry this connection's
   token in the address (it stops working when the Studio locks); the upload sends it in a header. */
const tr = { ds: "", file: null, name: "", nameTouched: false, xhr: null, pct: 0, checking: false, msg: null, confirm: null };
try { tr.ds = localStorage.getItem("kiro-studio-train-ds") || ""; } catch (e) { /* ignore */ }
const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;   // same rule as the server
const UP_CODES = ["NAME", "EXISTS", "TOO_BIG", "BUSY", "DISK", "NOT_ZIP", "BAD_PATH", "ENCRYPTED", "TOO_MANY", "NO_MODEL", "MANY_MODELS",
  "EXTRA_FILES", "BAD_FILE", "BAD_CONFIG", "BAD_WEIGHTS", "MISSING", "LOCKED", "INCOMPLETE", "NETWORK", "ABORTED", "ZIP_METHOD", "WRITE"];
const train = () => (st.studio && st.studio.train) || { models: [], packages: [] };
const qtok = () => encodeURIComponent(st.token || "");
const pkgUrl = (f) => `/api/studio/packages/${encodeURIComponent(f)}?token=${qtok()}`;
const modelUrl = (n) => `/api/studio/models/${encodeURIComponent(n)}.zip?token=${qtok()}`;
const fmtMB = (mb) => (mb == null ? "-" : mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : mb >= 10 ? `${Math.round(mb)} MB` : `${mb.toFixed(1)} MB`);
const fmtDate = (sec) => (sec ? new Date(sec * 1000).toLocaleString(LANG === "ko" ? "ko-KR" : "en-GB",
  { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "-");
const pkgsOf = (repo) => train().packages.filter((p) => p.complete && p.name === repo.split("/")[1]);   // newest first, finished only
const armText = (a) => ({ right: t("rec.arm.right"), left: t("rec.arm.left"), both: t("rec.arm.both"), other: t("fm.arm.other") }[a] || "");
const jobOn = (name) => !!(st.robot && st.robot.jobs && st.robot.jobs[name] && st.robot.jobs[name].running);
const packing = () => !!(st.studio && st.studio.pack && st.studio.pack.running);

$("t-ds").addEventListener("change", () => {
  tr.ds = $("t-ds").value;
  try { localStorage.setItem("kiro-studio-train-ds", tr.ds); } catch (e) { /* ignore */ }
  renderTrain();
});

function renderTrain() {
  const T = train(), ds = datasets(), s = sess();
  if (!dsOf(tr.ds)) tr.ds = (s && dsOf(s.settings.repo) ? s.settings.repo : (ds[0] || {}).repo) || "";
  const opts = ds.map((d) => `<option value="${esc(d.repo)}">${esc(d.repo)} · ${esc(t("rec.nep", { n: d.episodes }))}</option>`).join("")
    || `<option value="" disabled>${esc(t("rec.nods"))}</option>`;
  const sel = $("t-ds");
  if (sel.dataset.html !== opts) { sel.innerHTML = opts; sel.dataset.html = opts; }
  sel.value = tr.ds;
  sel.disabled = !ds.length;

  // 1. the newest package of the chosen dataset
  const d = dsOf(tr.ds), pk = st.studio.pack, mine = !!(pk && pk.repo === tr.ds);
  const p = d ? pkgsOf(d.repo)[0] : null, stale = !!(p && d.changed && p.mtime < d.changed);
  const busy = packing() || running();
  let html;
  if (!d) html = `<p class="small mute">${esc(t("tr.nods"))}</p>`;
  else if (mine && pk.running) html = `<p class="small accent">${esc(t("tr.packing"))}</p>`;
  else {
    html = "";
    if (mine && !pk.ok && pk.code != null) html += `<p class="small note err">${esc(t("tr.packfail", { code: pk.code }))}</p><pre class="tail">${esc((pk.tail || []).join("\n"))}</pre>`;
    if (p) {
      html += `<div class="pkgline"><i class="ri-archive-line"></i><div class="grow"><b class="mono">${esc(p.file)}</b>`
        + `<span class="small mute">${esc(fmtMB(p.mb))} · ${esc(fmtDate(p.mtime))}</span>${stale ? `<span class="small warn">${esc(t("tr.stale"))}</span>` : ""}</div></div>`
        + `<div class="stepfoot"><a class="btn${stale ? "" : " primary"}" href="${pkgUrl(p.file)}" download><i class="ri-download-2-line"></i><span>${esc(t("tr.dl"))}</span></a>`
        + (p.sha ? `<a class="btn" href="${pkgUrl(p.sha)}" download><i class="ri-shield-check-line"></i><span>${esc(t("tr.dlsha"))}</span></a>` : "")
        + `<button class="btn${stale ? " primary" : ""}" type="button" data-pack ${busy ? "disabled" : ""}><i class="ri-archive-line"></i><span>${esc(t("tr.repack"))}</span></button></div>`;
    } else {
      html += `<p class="small mute">${esc(t("tr.nopkg"))}</p><div class="stepfoot"><button class="btn primary" type="button" data-pack ${busy ? "disabled" : ""}>`
        + `<i class="ri-archive-line"></i><span>${esc(t("tr.pack"))}</span></button></div>`;
    }
  }
  const box = $("t-pkg");
  if (box.dataset.html !== html) {
    box.innerHTML = html; box.dataset.html = html;
    const b = box.querySelector("[data-pack]");
    if (b) b.addEventListener("click", () => send({ t: "studio", cmd: "pack", repo: tr.ds }));
  }
  // 3. Colab
  if (T.colab) $("t-colab").href = T.colab;
  $("t-nb-row").hidden = !T.notebook;
  if (T.notebook) $("t-nb").href = `/api/studio/notebook.ipynb?token=${qtok()}`;
  renderUpload();
  renderColab();
  renderFiles();
}

// 6. upload the model zip
function suggestName(fileName) {
  let n = fileName.replace(/\.zip$/i, "").replace(/\s*\(\d+\)$/, "").replace(/_pretrained_model$/, "")
    .replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[^A-Za-z0-9]+/, "").slice(0, 72) || "skill";
  const taken = new Set(train().models.map((m) => m.name));
  if (taken.has(n)) { let i = 2; while (taken.has(`${n}_${i}`)) i++; n = `${n}_${i}`; }
  return n;
}
function uploadProblem() {
  const f = tr.file;
  if (!f) return "";
  if (!/\.zip$/i.test(f.name)) return t("up.e.NOT_ZIP");
  if (f.size > (train().zip_max_mb || 3000) * 1e6) return t("up.e.TOO_BIG", { detail: fmtMB(f.size / 1e6) });
  if (!MODEL_RE.test(tr.name)) return t("up.e.NAME");
  if (train().models.some((m) => m.name === tr.name)) return t("up.e.EXISTS", { detail: tr.name });
  return "";
}
$("t-file").addEventListener("change", () => {
  tr.file = $("t-file").files[0] || null; tr.msg = null;
  if (tr.file && !tr.nameTouched) { tr.name = suggestName(tr.file.name); $("t-name").value = tr.name; }
  renderUpload();
});
$("t-name").addEventListener("input", () => { tr.name = $("t-name").value.trim(); tr.nameTouched = !!tr.name; tr.msg = null; renderUpload(); });
$("t-up").addEventListener("click", () => {
  if (tr.xhr || !tr.file || uploadProblem() || !st.token) return;
  const xhr = new XMLHttpRequest();
  tr.xhr = xhr; tr.pct = 0; tr.checking = false; tr.msg = null;
  xhr.open("POST", `/api/studio/models?name=${encodeURIComponent(tr.name)}`);
  xhr.setRequestHeader("X-Studio-Token", st.token);
  xhr.setRequestHeader("Content-Type", "application/zip");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) { tr.pct = e.loaded / e.total; renderUpload(); } };
  xhr.upload.onload = () => { tr.checking = true; renderUpload(); };
  xhr.onload = () => { let r = {}; try { r = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ } uploaded(r, xhr.status); };
  xhr.onerror = () => uploaded({ code: "NETWORK" }, 0);
  xhr.onabort = () => uploaded({ code: "ABORTED" }, 0);
  xhr.send(tr.file);
  renderUpload();
});
function uploaded(r, status) {
  tr.xhr = null; tr.checking = false;
  if (r.ok && r.model) {
    const m = r.model;
    tr.msg = { ok: true, key: "up.ok", args: { name: m.name }, extra: m.playable ? "" : "fm.noplay." + m.arms };
    tr.file = null; tr.name = ""; tr.nameTouched = false; $("t-file").value = ""; $("t-name").value = "";
    toast(t("up.okshort", { name: m.name }));
  } else {
    const code = status === 403 ? "LOCKED" : UP_CODES.includes(r.code) ? r.code : "OTHER";
    tr.msg = { ok: false, key: "up.e." + code, args: { detail: r.detail || r.code || status } };
  }
  renderUpload();
}
window.addEventListener("beforeunload", (e) => { if (tr.xhr) { e.preventDefault(); e.returnValue = ""; } });

function renderUpload() {
  const problem = uploadProblem(), sending = !!tr.xhr;
  const other = train().uploading && !sending ? train().uploading : null;
  const can = !!tr.file && !problem && !sending && !other && !!st.token;
  $("t-up").disabled = !can;
  $("t-up").classList.toggle("primary", can);
  $("t-file").disabled = sending;
  $("t-name").disabled = sending;
  setBtn($("t-up"), t(sending ? "up.busy" : "tr.upload"));
  $("t-up-bar").hidden = !sending;
  $("t-up-bar").querySelector("i").style.width = `${Math.round((tr.checking ? 1 : tr.pct) * 100)}%`;
  const f = tr.file;
  $("t-up-pct").textContent = sending ? (tr.checking ? t("up.checking")
    : t("up.sending", { p: Math.round(tr.pct * 100), a: fmtMB(tr.pct * f.size / 1e6), b: fmtMB(f.size / 1e6) })) : f ? fmtMB(f.size / 1e6) : "";
  const msg = $("t-up-msg");
  const done = tr.msg ? t(tr.msg.key, tr.msg.args) + (tr.msg.extra ? " " + t(tr.msg.extra) : "") : "";   // translated now: the language may change
  const text = sending ? "" : problem || (other ? t("up.e.BUSY", { detail: other.by }) : done);
  msg.hidden = !text;
  msg.textContent = text;
  msg.className = "small " + (!problem && tr.msg && tr.msg.ok ? "accent" : "note err");
}

// ------------------------------------------------ train in Colab (automatic)
/* The robot (robot_server/colab.py, needs internet) packages the dataset and waits for the Colab session.
   The notebook's "Connect to the robot" link opens this page with the session's address and key (#colab=...&k=...):
   the page asks before sending (the offer card), and hands it to the unlocked Studio tab if this one is not. */
const COLAB_ACTIVE = ["packing", "waiting", "sending", "training", "fetching", "installing", "lost"];
const colab = () => (st.studio && st.studio.colab) || null;
const colabUrl = () => train().colab || $("t-colab").href;
const PH = { packing: "accent", waiting: "accent", sending: "accent", training: "live", fetching: "accent", installing: "accent",
  installed: "live", lost: "warn", failed: "warn", stopped: "warn" };
const bc = (() => { try { return new BroadcastChannel("kiro-studio"); } catch (e) { return null; } })();

function colabSend(action, extra) {
  st.colabAct = action;
  send({ t: "studio", cmd: "colab", action, ...(extra || {}) });
}
$("c-go").addEventListener("click", () => {
  const c = colab();
  window.open(colabUrl(), "_blank", "noopener");     // at the click itself: no pop-up blocker
  if (!(c && COLAB_ACTIVE.includes(c.state))) colabSend("start", { repo: tr.ds });
});
$("c-stop").addEventListener("click", () => colabSend("stop"));

function colabWhy(c) {
  const code = c.error || "", a = { detail: c.detail || "" };
  if (STRINGS.en["col.e." + code]) return t("col.e." + code, a);
  if (STRINGS.en["up.e." + code]) return t("up.e." + code, a);
  return [code, c.detail].filter(Boolean).join(": ");
}

function renderColab() {
  const c = colab(), active = !!c && COLAB_ACTIVE.includes(c.state);
  $("c-go").disabled = active || !tr.ds;
  $("c-go").classList.toggle("primary", !active);
  $("c-run").hidden = !c;
  if (!c) return;
  const mb = (b) => fmtMB((b || 0) / 1e6);
  const ph = $("c-phase");
  ph.textContent = t("col.ph." + c.state);
  ph.className = "phase " + (PH[c.state] || "accent");
  $("c-host").textContent = c.host ? t("col.host", { host: c.host }) : "";
  const starting = c.state === "training" && (c.step == null || ["received", "waiting", "receiving"].includes(c.remote));
  $("c-say").textContent = c.state === "sending" ? t("col.p.sending", { a: mb(c.sent), b: mb(c.total) })
    : c.state === "fetching" ? t("col.p.fetching", { a: mb(c.sent), b: mb(c.total) })
    : c.state === "training" ? (starting ? t("col.p.trainstart") : t("col.p.training", { n: (c.step || 0).toLocaleString(), of: (c.steps || 20000).toLocaleString() }))
    : c.state === "installed" ? t("col.p.installed", { name: c.model || "" })
    : c.state === "failed" ? t("col.p.failed", { why: colabWhy(c) })
    : t("col.p." + c.state);
  const frac = c.state === "sending" || c.state === "fetching" ? (c.total ? c.sent / c.total : 0)
    : c.state === "training" && c.steps ? (c.step || 0) / c.steps : c.state === "installed" ? 1 : null;
  $("c-bar").hidden = frac == null;
  $("c-bar").classList.toggle("ok", c.state === "training" || c.state === "installed");
  $("c-bar").querySelector("i").style.width = `${Math.round(Math.min(1, frac || 0) * 100)}%`;
  const detail = c.state === "lost" && c.detail ? c.detail : c.quiet_s ? t("col.quiet", { min: Math.round(c.quiet_s / 60) }) : "";
  $("c-detail").hidden = !detail;
  $("c-detail").textContent = detail;
  setBtn($("c-stop"), t(active ? "col.stop" : "col.clear"));
  $("c-colab").hidden = !active;
  $("c-colab").href = colabUrl();
  $("c-play").hidden = c.state !== "installed";
}

// The offer: this page was opened by the notebook's "Connect to the robot" link
(function readOffer() {
  const m = /^#colab=([^&]+)&k=([A-Za-z0-9_-]+)$/.exec(location.hash);
  if (!m) return;
  history.replaceState(null, "", location.pathname + location.search);   // the key does not stay in the address bar
  try { st.offer = { url: decodeURIComponent(m[1]), key: m[2], state: "ask" }; } catch (e) { /* ignore */ }
})();
function renderOffer() {
  const o = st.offer;
  $("offer").hidden = !o;
  if (!o) return;
  const host = o.url.replace(/^https?:\/\//, ""), c = isEngineer() ? colab() : null;
  $("offer-t").textContent = t("col.offer.t", { host })
    + (c && c.host && c.host !== host && COLAB_ACTIVE.includes(c.state) ? " " + t("col.offer.replaces", { host: c.host }) : "");
  $("offer-send").disabled = o.state === "sending" || o.state === "sent";
  const msg = $("offer-msg");
  const text = o.state === "sent" ? t("col.offer.sent") : o.state === "unlock" ? t("col.offer.unlock") : o.state === "error" ? o.err : "";
  msg.hidden = !text;
  msg.textContent = text;
  msg.className = "small " + (o.state === "sent" ? "accent" : "note err");
}
$("offer-send").addEventListener("click", () => {
  const o = st.offer;
  if (!o) return;
  o.state = "sending";
  if (isEngineer()) colabSend("connect", { url: o.url, key: o.key });
  else if (bc) {                                    // hand it to the unlocked Studio tab
    o.id = Math.random().toString(36).slice(2);
    bc.postMessage({ t: "connect", id: o.id, url: o.url, key: o.key });
    setTimeout(() => { if (st.offer === o && o.state === "sending") { o.state = "unlock"; renderOffer(); } }, 2000);
  } else o.state = "unlock";
  renderOffer();
});
$("offer-no").addEventListener("click", () => { st.offer = null; renderOffer(); });
if (bc) bc.onmessage = (ev) => {
  const m = ev.data || {};
  if (m.t === "connect" && isEngineer()) {
    st.pendingAck = m.id;                             // answered once the robot has said yes or no
    colabSend("connect", { url: String(m.url || ""), key: String(m.key || "") });
  } else if (m.t === "ack" && st.offer && st.offer.id === m.id) {
    st.offer.state = m.ok ? "sent" : "error";
    st.offer.err = m.ok ? "" : String(m.msg || "");
    renderOffer();
  }
};

// ------------------------------------------------------- training files
/* Datasets, packages and trained models on the robot: download, package, delete (hold to confirm). */
function fitem(kind, name, title, meta, actions, warn, canDelete) {
  const key = `${kind}:${name}`, asking = tr.confirm === key;
  return `<div class="fitem${asking ? " asking" : ""}" data-key="${esc(key)}"><div class="fmain">${title}`
    + `<span class="small mute">${meta.filter(Boolean).map(esc).join(" · ")}</span>${warn ? `<span class="small warn">${esc(warn)}</span>` : ""}</div>`
    + `<div class="facts">${actions}<button class="btn" type="button" data-act="ask" ${canDelete && !asking ? "" : "disabled"}><i class="ri-delete-bin-line"></i><span>${esc(t("fm.del"))}</span></button></div>`
    + (asking ? `<div class="fconfirm"><p>${esc(t("fm.sure." + kind, { name }))}</p><div class="stepfoot">`
      + `<button class="btn hold" type="button" data-act="really"><span class="fill"></span><i class="ri-delete-bin-line"></i><span>${esc(t("fm.hold"))}</span></button>`
      + `<button class="btn" type="button" data-act="cancel">${esc(t("fm.cancel"))}</button></div></div>` : "")
    + `</div>`;
}
const none = (k) => `<p class="small mute">${esc(t(k))}</p>`;

function renderFiles() {
  const T = train(), recBusy = running() || packing() || !!(st.studio && st.studio.starting);
  const dsH = datasets().map((d) => fitem("dataset", d.repo, `<b>${esc(d.repo)}</b>`,
    [t("rec.nep", { n: d.episodes }), t("rec.nframes", { n: (d.frames || 0).toLocaleString() }), fmtMB(d.mb), fmtDate(d.changed || d.mtime),
      (d.cams || []).map(camName).join(" + ")],
    `<button class="btn" type="button" data-act="pack" ${recBusy ? "disabled" : ""}><i class="ri-archive-line"></i><span>${esc(t("tr.pack"))}</span></button>`,
    "", !recBusy)).join("") || none("rec.nods");
  const pkH = T.packages.map((p) => fitem("package", p.file, `<b class="mono">${esc(p.file)}</b>`, p.complete ? [fmtMB(p.mb), fmtDate(p.mtime)] : [fmtDate(p.mtime)],
    p.complete ? `<a class="btn" href="${pkgUrl(p.file)}" download><i class="ri-download-2-line"></i><span>${esc(t("fm.dl"))}</span></a>`
      + `<a class="btn" href="${pkgUrl(p.sha)}" download><span>.sha256</span></a>` : "",
    p.complete ? "" : t(packing() ? "fm.pkg.writing" : "fm.pkg.broken"), !packing())).join("") || none("fm.none.pkg");
  const skillOn = jobOn("skill");
  const mH = T.models.map((m) => fitem("model", m.name, `<b>${esc(m.name)}</b>`,
    [String(m.type).toUpperCase(), armText(m.arms), m.steps ? t("fm.steps", { n: m.steps.toLocaleString() }) : "", m.dataset,
      (m.cams || []).map(camName).join(" + "), fmtMB(m.mb), fmtDate(m.mtime)],
    `<a class="btn" href="${modelUrl(m.name)}" download><i class="ri-download-2-line"></i><span>${esc(t("fm.dl"))}</span></a>`,
    m.playable ? "" : t("fm.noplay." + m.arms), !skillOn)).join("") || none("fm.none.models");
  for (const [id, html] of [["fm-ds", dsH], ["fm-pkg", pkH], ["fm-models", mH]]) {
    const el = $(id);
    if (el.dataset.html === html) continue;
    el.innerHTML = html; el.dataset.html = html;
    const really = el.querySelector("[data-act=really]");
    if (really) holdButton(really, () => {
      const [kind, ...rest] = tr.confirm.split(":");
      send({ t: "studio", cmd: "delete", kind, name: rest.join(":") });
      tr.confirm = null; renderFiles();
    });
  }
}
for (const id of ["fm-ds", "fm-pkg", "fm-models"]) $(id).addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]"), item = e.target.closest(".fitem");
  if (!b || !item || b.disabled) return;
  const act = b.dataset.act, key = item.dataset.key;
  if (act === "ask") tr.confirm = key;
  else if (act === "cancel") tr.confirm = null;
  else if (act === "pack") send({ t: "studio", cmd: "pack", repo: key.slice("dataset:".length) });
  else return;
  renderFiles();
});

// ------------------------------------------------------------- lidar setup
// Two snapshots (clear, box in front) -> front angle + the robot's own parts -> lidar.env, obstacle stop on.
const lsSend = (cmd) => send({ t: "lidar_setup", cmd });
$("ls-start").addEventListener("click", () => lsSend("start"));
$("ls-cancel").addEventListener("click", () => lsSend("cancel"));
$("ls-clear").addEventListener("click", () => lsSend("clear"));
$("ls-box").addEventListener("click", () => lsSend("box"));
$("ls-apply").addEventListener("click", () => lsSend("apply"));

function lsMap(res) {
  // Robot frame seen from above: front up. Amber wedges = directions where the lidar sees the robot itself.
  const R = 50, toXY = (deg, r) => [-Math.sin(deg * Math.PI / 180) * r, -Math.cos(deg * Math.PI / 180) * r];
  let g = `<circle cx="0" cy="0" r="${R}" class="ring"/><circle cx="0" cy="0" r="${R / 2}" class="ring"/>`;
  for (const [a, m] of (res && res.mask) || []) {
    const r = Math.min(R, 8 + m * 90);
    const [x1, y1] = toXY(a, r), [x2, y2] = toXY(a + 5, r);
    g += `<path d="M0 0L${x1.toFixed(1)} ${y1.toFixed(1)}A${r.toFixed(1)} ${r.toFixed(1)} 0 0 0 ${x2.toFixed(1)} ${y2.toFixed(1)}Z" class="self"/>`;
  }
  g += `<rect x="-9" y="-11" width="18" height="22" rx="3" class="body"/><path d="M0 -${R + 4}L-4 -${R - 3}L4 -${R - 3}Z" class="front"/>`;
  return g;
}

function renderLidar() {
  const s = st.studio, L = s && s.lidar_setup;
  if (!L) return;
  const step = L.step, busy = !!L.busy;
  $("ls-st").textContent = t(L.stop_on ? "ls.on" : "ls.off");
  $("ls-st").dataset.st = L.stop_on ? "ok" : "warn";
  const scans = !L.plugged ? t("ls.v.unplugged") : L.scan_age == null ? t("ls.v.none")
    : L.scan_age < 2 ? t("ls.v.live") : t("ls.v.age", { s: Math.round(L.scan_age) });
  const rows = [["ls.k.stop", t(L.stop_on ? "ls.v.on" : "ls.v.off")],
    ["ls.k.front", L.yaw_deg == null ? t("ls.v.nofront") : t("ls.v.front", { deg: -L.yaw_deg })],
    ["ls.k.mask", L.mask_bins ? t("ls.v.mask", { k: L.mask_bins }) : t("ls.v.nomask")],
    ["ls.k.scans", scans]];
  const html = rows.map(([k, v]) => `<tr><th>${esc(t(k))}</th><td>${esc(v)}</td></tr>`).join("") + LANG;
  if ($("ls-now").dataset.html !== html) { $("ls-now").innerHTML = html.slice(0, -LANG.length); $("ls-now").dataset.html = html; }
  const map = lsMap(L.result || L);  // the new measurement, else what lidar.env holds now
  if ($("ls-map").dataset.html !== map) { $("ls-map").innerHTML = map; $("ls-map").dataset.html = map; }

  const order = ["clear", "box", "result"], at = order.indexOf(step);
  for (const li of $("ls-steps").children) {
    const i = order.indexOf(li.dataset.s);
    li.className = at < 0 ? "todo" : i === at ? "on" : i < at ? "done" : "todo";
  }
  $("ls-steps").hidden = at < 0;
  $("ls-clear").disabled = busy || step !== "clear";
  $("ls-box").disabled = busy || step !== "box";
  $("ls-apply").disabled = busy || step !== "result";
  $("ls-start-t").textContent = t(at < 0 ? "ls.start" : "ls.again");  // no data-i18n: applyStrings would reset it
  $("ls-start").disabled = busy;
  $("ls-cancel").hidden = at < 0;
  $("ls-busy").hidden = !busy;

  const r = L.result;
  $("ls-res").textContent = r ? t("ls.res", { deg: -r.yaw_deg, cm: Math.round(r.box.dist * 100), n: r.box.points, k: r.mask.length }) : "";
  const warns = (r && r.warnings) || [];
  const wh = warns.map((w) => `<p class="small note err">${esc(t(w.key, { lo: w.deg ? w.deg[0] : "", hi: w.deg ? w.deg[1] : "", cm: w.cm ?? "", pct: w.pct ?? "" }))}</p>`).join("");
  if ($("ls-warn").dataset.html !== wh + LANG) { $("ls-warn").innerHTML = wh; $("ls-warn").dataset.html = wh + LANG; }

  const e = L.error;
  let msg = "";
  if (e) msg = STRINGS.en[e.key] ? t(e.key, e) : e.msg;
  else if (L.saved && at < 0) msg = "";
  $("ls-err").textContent = msg;
  $("ls-err").hidden = !msg;
  $("ls-err").className = "small note err";
  if (L.saved && at < 0 && !e) {
    $("ls-err").textContent = t("ls.saved", { time: new Date(L.saved.t * 1000).toLocaleTimeString(), deg: -L.saved.yaw_deg });
    $("ls-err").className = "small note ok";
    $("ls-err").hidden = false;
  }
}

// ----------------------------------------------------------- engineer tools
const eng ={ sel: 0, servo: null, prep: {}, secs: 20 };
const MOTION_STEPS = new Set(["teleop_left", "teleop_right", "teleop_both", "wheels", "mecanum_floor", "ros_base"]);
$("guard-profile").addEventListener("change", () => send({ t: "guard_profile", profile: $("guard-profile").value }));
const svPort = () => $("sv-port").value;
$("sv-scan").addEventListener("click", () => send({ t: "servo", cmd: "scan", port: svPort(), lo: +$("sv-lo").value, hi: +$("sv-hi").value }));
const svSend = (cmd, value) => send({ t: "servo", cmd, port: svPort(), id: eng.servo, value, arm_ok: $("sv-armok").checked });
$("sv-ton").addEventListener("click", () => svSend("torque", 1));
$("sv-toff").addEventListener("click", () => svSend("torque", 0));
holdButton($("sv-move"), () => svSend("move", +$("sv-pos").value));
holdButton($("sv-setid"), () => svSend("setid", +$("sv-newid").value));

function renderTools() {
  const s = st.studio;
  if (!s) return;
  if (document.activeElement !== $("guard-profile")) $("guard-profile").value = s.guard_profile || "close";
  $("guard-off").hidden = !(st.robot && st.robot.obstacle_stop === false);
  const jobs = (s.info && s.info.jobs) || {};
  $("guard-profile").disabled = !!(jobs.base && jobs.base.running);

  const ports = s.ports || [];
  const sel = $("sv-port"), cur = sel.value;
  const opts = ports.map((p) => `<option value="${esc(p.dev)}">${esc(p.dev)}${p.dev !== p.real ? " (" + esc(p.real.replace("/dev/", "")) + ")" : ""}</option>`).join("");
  if (sel.dataset.html !== opts + LANG) { sel.innerHTML = opts || `<option value="">${esc(t("studio.noports"))}</option>`; sel.dataset.html = opts + LANG; if (cur) sel.value = cur; }
  $("sv-scan").disabled = !ports.length;

  const sc = s.servo_scan;
  const cols = t("eng.cols").split("|");
  let tb;
  if (!sc || sc.port !== svPort()) tb = `<tr><td class="mute">${t(ports.length ? "eng.noscan" : "studio.noports")}</td></tr>`;
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
      table.dataset.html = ""; renderTools();
    });
  }
  const has = !!(sc && sc.port === svPort() && sc.servos.some((v) => v.id === eng.servo));
  $("sv-one").hidden = !has;
  if (has) $("sv-sel").textContent = `${svPort()} · ID ${eng.servo}`;
  renderWizard();
}

function renderWizard() {
  const s = st.studio, steps = st.steps || [];
  const res = (s && s.wizard) || {};
  const wj = ((s && s.info && s.info.jobs) || {}).wizard;
  $("wz-none").hidden = !(st.steps && !steps.length);
  const status = (k) => (res[k] && res[k].status) || "pending";
  const list = steps.map((x, i) => `<li data-i="${i}" class="${i === eng.sel ? "sel" : ""}"><span>${esc(x.title)}${x.optional ? ` <span class="mute small">(${t("wz.optional")})</span>` : ""}</span><span class="chip ${status(x.key)}">${t("wz." + status(x.key))}</span></li>`).join("");
  const ol = $("wz-steps");
  if (ol.dataset.html !== list) {
    ol.innerHTML = list; ol.dataset.html = list;
    for (const li of ol.querySelectorAll("li")) li.addEventListener("click", () => { eng.sel = +li.dataset.i; ol.dataset.html = ""; $("wz-detail").dataset.html = ""; renderWizard(); });
  }
  const x = steps[eng.sel];
  if (!x) { $("wz-detail").innerHTML = ""; $("wz-tail").hidden = true; return; }
  const runningW = !!(wj && wj.running);
  const prep = eng.prep[x.key] || (eng.prep[x.key] = x.prep.map(() => false));
  const li = (a) => a.map((v) => `<li>${esc(v)}</li>`).join("");
  const motion = MOTION_STEPS.has(x.key);
  const html = `<div class="wz">
    <h4>${esc(x.title)}</h4><p>${esc(x.desc)}</p>${LANG === "en" ? `<p class="small mute">${t("eng.kotext")}</p>` : ""}
    ${x.prep.length ? `<b class="small">${t("eng.prep")}</b>${x.prep.map(([txt, req], i) => `<label><input type="checkbox" data-prep="${i}" ${prep[i] ? "checked" : ""}><span>${esc(txt)}${req ? `<span class="req">${t("eng.required")}</span>` : ""}</span></label>`).join("")}` : ""}
    ${x.do.length ? `<b class="small">${t("eng.do")}</b><ol>${li(x.do)}</ol>` : ""}
    ${x.expect.length ? `<b class="small">${t("eng.expect")}</b><ul>${li(x.expect)}</ul>` : ""}
    ${x.fail.length ? `<details><summary>${t("eng.iffail")}</summary><ul>${li(x.fail)}</ul></details>` : ""}
    ${x.key.startsWith("teleop") ? `<div class="row"><label class="small mute" for="wz-sec">${t("eng.seconds")}</label><input id="wz-sec" class="num" type="number" min="5" max="120" value="${eng.secs || 20}"></div>` : ""}
    <div class="row" style="margin-top:10px">
      ${runningW ? `<button class="btn" id="wz-stop" type="button">${t("eng.stop")}</button>`
        : `<button class="btn${motion ? " hold" : ""}" id="wz-run" type="button">${motion ? `<span class="fill"></span>${t("eng.runhold")}` : t("eng.run")}</button>`}
      <span class="grow"></span>
      <button class="btn" id="wz-pass" type="button" ${runningW ? "disabled" : ""}>${t("eng.pass")}</button>
      <button class="btn" id="wz-fail" type="button" ${runningW ? "disabled" : ""}>${t("eng.fail")}</button>
      ${x.optional ? `<button class="btn" id="wz-skip" type="button" ${runningW ? "disabled" : ""}>${t("eng.skip")}</button>` : ""}
    </div></div>`;
  const det = $("wz-detail");
  if (det.dataset.html !== html) {
    det.innerHTML = html; det.dataset.html = html;
    for (const cb of det.querySelectorAll("[data-prep]")) cb.addEventListener("change", () => { prep[+cb.dataset.prep] = cb.checked; det.dataset.html = ""; });
    const sec = $("wz-sec");
    if (sec) sec.addEventListener("change", () => { eng.secs = +sec.value; });
    const run = () => {
      if (x.prep.some(([, req], i) => req && !prep[i])) { toast(t("eng.prepmissing"), true); return; }
      send({ t: "wizard", step: x.key, on: true, seconds: eng.secs || 20 });
    };
    const rb = $("wz-run");
    if (rb) { if (motion) holdButton(rb, run); else rb.addEventListener("click", run); }
    const sb = $("wz-stop");
    if (sb) sb.addEventListener("click", () => send({ t: "wizard", step: x.key, on: false }));
    for (const [id, v] of [["wz-pass", "pass"], ["wz-fail", "fail"], ["wz-skip", "skip"]]) {
      const b = $(id);
      if (b) b.addEventListener("click", () => send({ t: "wizard_decide", step: x.key, status: v }));
    }
  }
  const lines = runningW && wj ? wj.tail : (res[x.key] && res[x.key].lines) || [];
  const tl = $("wz-tail");
  tl.hidden = !lines.length;
  if (lines.length) { tl.textContent = lines.join("\n"); tl.scrollTop = tl.scrollHeight; }
}

$("wz-report").addEventListener("click", () => {
  const steps = st.steps || [], res = (st.studio && st.studio.wizard) || {};
  const now = new Date();
  const md = [`# HAEBOT Robot hardware check`, ``, `Date: ${now.toLocaleString()}`, `Robot: ${st.server ? st.server.backend : "?"}`, ``,
    `| Step | Result | Auto |`, `|---|---|---|`,
    ...steps.map((x) => `| ${x.title} | ${(res[x.key] && res[x.key].status) || "not run"} | ${res[x.key] && res[x.key].auto != null ? res[x.key].auto : "-"} |`), ``,
    ...steps.filter((x) => res[x.key] && res[x.key].lines).flatMap((x) => [`## ${x.title}`, ``, "```", ...res[x.key].lines, "```", ``])].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
  a.download = `kiro_check_${now.toISOString().slice(0, 10)}.md`;
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});

// ---------------------------------------------------- engineer information
/* Read only: the technical values that used to sit on the main pages, with plain labels. */
function renderInfo() {
  const s = st.studio;
  if (!s || !$("sec-info").open) return;
  const i = s.info || {}, yes = (v) => t(v ? "inf.yes" : "inf.no");
  const table = (head, rows) => `<div class="scroll-x"><table class="kv dtable">${head ? `<tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>` : ""}`
    + (rows.length ? rows.map((r) => `<tr>${r.map((c, n) => (n === 0 && !head ? `<th>${c}</th>` : `<td>${c}</td>`)).join("")}</tr>`).join("")
      : `<tr><td class="mute" colspan="${head ? head.length : 2}">${esc(t("inf.none"))}</td></tr>`) + `</table></div>`;
  const code = (v) => `<code>${esc(v)}</code>`;
  const srv = st.server || {};
  const parts = [];
  parts.push([t("inf.server"), table(null, [
    [esc(t("inf.backend")), esc(srv.backend === "ros" ? t("be.ros") : t("be.mock")) + ` · v${esc(srv.version)}`],
    [esc(t("inf.leftok")), esc(yes(i.left_arm_ok))],
    [esc(t("inf.obstacle")), esc(yes(i.obstacle_stop_env))],
    [esc(t("base.profile")), esc(t(s.guard_profile === "safe" ? "base.safe" : "base.close"))],
    [esc(t("inf.devices")), esc(srv.clients ?? "-")],
  ])]);
  const P = i.paths || {};
  parts.push([t("inf.paths"), table(null, [["so101", P.so101], ["venv", P.venv], ["datasets", P.datasets], ["backups", P.backups],
    ["calibration", P.calibration], ["policies", P.policies], ["packages", P.packages]].map(([k, v]) => [esc(t("inf.p." + k)), code(v || "-")]))]);
  const B = i.boards || {};
  parts.push([t("inf.boards"), table(t("inf.boards.cols").split("|"), Object.entries(B).filter(([k]) => !k.startsWith("_")).map(([k, b]) => [
    esc((PARTS[k] || [k === "base" ? "sec.base" : k]).map((x) => t(x)).join(" ")), esc(b.state === "busy" ? `${b.state} (${b.by})` : b.state || "-"),
    esc(b.n != null ? `${b.n}/${b.of}` : "-"), esc(b.vmin ?? "-"), esc(b.tmax ?? "-"), esc(b.alarm ? "ALARM" : b.msg || "-")]))]);
  parts.push([t("inf.ports"), table(t("inf.ports.cols").split("|"), (s.ports || []).map((p) => [code(p.dev), code(p.real), esc(yes(p.arm))]))]);
  parts.push([t("inf.cams"), table(t("inf.cams.cols").split("|"), (s.cams || []).map((c) => [esc(camName(c.key)), code(c.device || "-"),
    esc(yes(c.present)), esc(c.auto || "-"), esc(c.live ? t("inf.yes") : "-"), esc(c.paused_by || "-"), esc(c.error || c.reason || "-")]))]);
  const now = serverNow();
  parts.push([t("inf.jobs"), table(t("inf.jobs.cols").split("|"), Object.values(i.jobs || {}).map((j) => [esc(j.label),
    esc(j.running ? t("d.running") + " · " + fmtDur(now - j.started) : t("d.stopped") + (j.code != null ? ` (${j.code})` : "")),
    (j.uses || []).map(code).join(" ") || "-"]))]);
  const se = sess();
  parts.push([t("inf.cmd"), se && se.cmd ? `<pre class="tail">${esc(se.cmd)}</pre>` : `<p class="small mute">${esc(t("inf.nocmd"))}</p>`]);
  const html = parts.map(([h, body]) => `<section><h4 class="sub">${esc(h)}</h4>${body}</section>`).join("");
  const box = $("info");
  if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; }
}
$("sec-info").addEventListener("toggle", renderInfo);

// Folded sections remember whether they were open
for (const d of document.querySelectorAll("details.fold, details.adv")) {
  try { d.open = localStorage.getItem("kiro-open-" + d.id) === "1"; } catch (e) { /* ignore */ }
  d.addEventListener("toggle", () => { try { localStorage.setItem("kiro-open-" + d.id, d.open ? "1" : "0"); } catch (e) { /* ignore */ } });
}

fillInputs();
applyStrings();
renderAll();
connect();
