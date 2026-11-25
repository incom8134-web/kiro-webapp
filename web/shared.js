"use strict";
/*
 * Shared by the control page, the dashboard, Studio and the battery page. One copy of the connection,
 * the emergency stop, escaping, and hold-to-confirm, so the three pages cannot drift.
 * Classic script: sets window.Kiro. No build, no network.
 */
(function () {
  const HOLD_MS = 2000;

  function esc(s) {
    return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

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

  function typing(e) {
    const el = e.target;
    return el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement ||
      (el instanceof HTMLInputElement && ["password", "text", "number", "search", "email", "tel"].includes(el.type));
  }

  /* Space and Enter belong to the focused control (a button, a link, a field), not to driving. */
  function interactive(e) {
    const el = e.target;
    return !!(el && el.closest && el.closest("button, a, input, select, textarea, summary, [role=tab]"));
  }

  function httpsUrl(value) {
    try {
      const u = new URL(String(value || ""), location.href);
      return u.protocol === "https:" ? u.href : "";
    } catch (e) {
      return "";
    }
  }

  /* Known English server lines, shown in the page language. Unknown text is left as the server sent it.
     The English strings the tests look for are unchanged on the server. */
  function serverText(msg) {
    const s = String(msg ?? "");
    const wait = /^Too many wrong PINs\. Wait (\d+)/.exec(s);
    if (wait) return t("err.lockout", { s: wait[1] });
    const rules = [
      [/^Wrong PIN/, "err.pin"],
      [/^Log in first/, "err.login"],
      [/^Take control first/, "err.take"],
      [/^E-STOP is on/, "err.estop"],
      [/^Unlock the engineer tools first/, "err.engineer"],
      [/^Confirmation required/, "err.confirm"],
      [/^Only the operator can release/, "err.releaseop"],
      [/^Mock mode: the Jetson is not shut down/, "err.mockoff"],
      [/^Unknown action/, "err.unknown"],
      [/^You already have control/, "err.already"],
      [/^You are in control/, "err.gotcontrol"],
      [/^Engineer tools unlocked/, "err.engok"],
      [/^E-STOP released/, "err.estopoff"],
      [/^Engineer tools are off/, "err.engoff"],
    ];
    const names = /^CAM_NAMES:(.*)$/.exec(s);
    if (names) return t("skill.badcam", { cams: names[1] });
    const miss = /^MISSING:(.*)$/.exec(s);
    if (miss) return t("skill.missing", { files: miss[1] });
    const busy = /^CAM_BUSY:(.*)$/.exec(s);
    if (busy) return t("skill.cambusy", { cam: busy[1] });
    for (const [re, key] of rules) if (re.test(s)) return t(key);
    const who = /^(.*) is in control\.$/.exec(s);
    if (who) return t("err.busy", { name: who[1] });
    return s;
  }

  /* Hold for 2 seconds with a pointer or with Space / Enter. Letting go cancels. */
  function holdButton(el, action) {
    if (!el || el.dataset.kiroHold) return;
    el.dataset.kiroHold = "1";
    let timer = null, start = 0, raf = 0, active = false;
    const fill = () => el.querySelector(".fill");
    const reset = () => {
      active = false;
      clearTimeout(timer);
      cancelAnimationFrame(raf);
      timer = null;
      const f = fill();
      if (f) f.style.width = "0";
    };
    const begin = () => {
      if (el.disabled || active) return;
      active = true;
      start = performance.now();
      const step = () => {
        const f = fill();
        if (f) f.style.width = Math.min(100, (performance.now() - start) / HOLD_MS * 100) + "%";
        if (active) raf = requestAnimationFrame(step);
      };
      step();
      timer = setTimeout(() => { reset(); if (el.isConnected && !el.disabled) action(); }, HOLD_MS);
    };
    el.addEventListener("pointerdown", (e) => {
      if (el.disabled || (e.button != null && e.button !== 0)) return;
      try { el.setPointerCapture(e.pointerId); } catch (err) { /* already captured */ }
      begin();
    });
    for (const n of ["pointerup", "pointercancel", "lostpointercapture"]) el.addEventListener(n, reset);
    el.addEventListener("keydown", (e) => {
      if (e.repeat) return;
      if (e.key === " " || e.key === "Enter") { e.preventDefault(); e.stopPropagation(); begin(); }
    });
    el.addEventListener("keyup", (e) => {
      if (e.key === " " || e.key === "Enter") { e.preventDefault(); e.stopPropagation(); reset(); }
    });
    el.addEventListener("blur", reset);
    el.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  /* Ask the server for a one-use token, then send the action. The answer is caught in connect(). */
  let pending = null;
  function confirmSend(send, action, payload) {
    pending = { action, payload, send };
    send({ t: "confirm", action });
  }

  function connect(opts) {
    let ws = null, open = false, soon = false, queue = [];
    function enqueue(obj) {
      if (!obj || obj.t === "drive" || obj.t === "ping") return;
      queue = queue.filter((m) => m.t !== obj.t);
      queue.push(obj);
      if (queue.length > 12) queue.shift();
    }
    const api = {
      send(obj) {
        if (open && ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
        else enqueue(obj);   // a take or an E-STOP pressed while the socket is reconnecting is not dropped
      },
      socket() { return ws; },
      reconnect() { soon = true; try { if (ws) ws.close(); } catch (e) { /* already closing */ } },
    };
    function start() {
      soon = false;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const name = typeof opts.name === "function" ? opts.name() : opts.name;
      ws = new WebSocket(`${proto}://${location.host}/ws?name=${encodeURIComponent(name || "device")}`);
      ws.onopen = () => {
        open = true;
        const pending = queue;
        queue = [];
        for (const m of pending) ws.send(JSON.stringify(m));
        if (opts.onOpen) opts.onOpen(ws);
      };
      ws.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m.t === "confirm" && pending && m.action === pending.action) {
          const p = pending;
          pending = null;
          if (m.ok) p.send(Object.assign({}, p.payload, { confirm: m.token }));
          else if (opts.onMessage) opts.onMessage({ t: "note", ok: false, msg: m.msg || "Confirmation required.", about: "confirm" });
          return;
        }
        if (opts.onMessage) opts.onMessage(m);
      };
      ws.onclose = () => {
        open = false;
        if (opts.onClose) opts.onClose();
        setTimeout(start, soon ? 0 : 1000);
      };
      ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
    }
    start();
    setInterval(() => api.send({ t: "ping", ts: performance.now() }), 1000);
    return api;
  }

  /* Press sets E-STOP. A second press does not release it: the server uses that press to
     cancel the fold-home and keep the arms still. Releasing is a separate hold, operator only. */
  function bindEstop(btn, releaseBtn, send, stopped, isOperator) {
    if (btn) btn.addEventListener("click", () => { send({ t: "estop", on: true }); });
    if (releaseBtn) holdButton(releaseBtn, () => { if (stopped() && isOperator()) send({ t: "estop", on: false }); });
  }

  function bindKeys(send) {
    window.addEventListener("keydown", (e) => {
      if ((e.code === "Escape" || e.key === "Escape") && !typing(e)) send({ t: "estop", on: true });
    });
  }

  /* Sign in once. The cookie is HttpOnly; this only shows or hides the form. */
  function bindLogin(opts) {
    const box = document.getElementById("login");
    const form = document.getElementById("login-form");
    if (!box || !form) return;
    const err = document.getElementById("login-err");
    const pin = document.getElementById("login-pin");
    async function refresh() {
      let ok = false;
      try { ok = (await fetch("/api/session", { credentials: "same-origin" })).ok; } catch (e) { ok = false; }
      box.hidden = ok;
      if (ok && opts.onReady) opts.onReady();
      return ok;
    }
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      let r, body = {};
      try {
        r = await fetch("/api/login", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pin: pin ? pin.value : "" }),
        });
        body = await r.json();
      } catch (ex) {
        r = { ok: false };
      }
      if (!r.ok) {
        if (err) { err.hidden = false; err.textContent = serverText(body.msg || "Wrong PIN."); }
        return;
      }
      if (pin) pin.value = "";
      if (err) err.hidden = true;
      box.hidden = true;
      if (opts.onLogin) opts.onLogin();
    });
    refresh();
  }

  function renderBattery(b) {
    const el = document.getElementById("battery");
    if (!el) return;
    const pct = document.getElementById("bat-pct");
    const icon = document.getElementById("bat-icon");
    const known = !!(b && typeof b.percent === "number");
    const n = known ? Math.round(b.percent) : null;
    el.classList.toggle("low", known && n <= 20);
    if (icon) {
      icon.className = !known ? "ri-battery-line"
        : b.charging ? "ri-battery-charge-line"
        : n <= 15 ? "ri-battery-low-line"
        : n <= 45 ? "ri-battery-2-line" : "ri-battery-fill";
    }
    if (pct) pct.textContent = known ? n + "%" : t("bat.unknown");
    el.title = t("bat.manage");
  }

  window.Kiro = { esc, deviceName, typing, interactive, httpsUrl, serverText, holdButton, confirmSend, connect, bindEstop, bindKeys, bindLogin, renderBattery };
})();
