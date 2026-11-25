"""The web server: serves the command center page, one WebSocket (/ws) and the camera streams."""
from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

from . import __version__
from . import guard
from .base import make_base
from .config import MOCK_ENGINEER_PIN, Config
from .control import Controller
from .logbuf import install as install_log
from .robot import Robot
from .training import ZIP_MAX, Refused, file_chunks

log = logging.getLogger("kiro")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Messages that only the operator may send. Each maps to a Robot method run in a thread
# (starting or stopping a program can take seconds).
OPERATOR_ACTIONS = {"obstacle_stop", "base", "teleop", "arm", "skill", "voice", "system", "say", "servo", "wizard", "wizard_decide", "stops_clear",
                    "leader", "wave", "follow", "demo", "studio", "guard_profile", "lidar_setup"}
# Also need the engineer PIN: the Studio page (recording, servo tester, check wizard, guard distance, lidar setup)
ENGINEER_ACTIONS = {"servo", "wizard", "wizard_decide", "studio", "guard_profile", "lidar_setup"}


UPLOAD_WAIT_S = 5.0     # how often a quiet upload is looked at
UPLOAD_IDLE_S = 60.0    # an upload that sends nothing for this long is given up


async def copy_upload(chunks, write, alive, limit: int, wait_s: float = UPLOAD_WAIT_S, idle_s: float = UPLOAD_IDLE_S) -> int:
    """Writes an upload while it arrives and returns its size. A laptop that leaves the Wi-Fi or sleeps
    sends no end, so the server would wait for ever (and refuse every later upload as busy): a quiet
    upload is given up after idle_s, and at once when the Studio is locked or left (alive() false)."""
    it = chunks.__aiter__()
    n, idle = 0, 0.0
    while True:
        nxt = asyncio.ensure_future(it.__anext__())
        while not (await asyncio.wait({nxt}, timeout=wait_s))[0]:
            idle += wait_s
            if not alive() or idle >= idle_s:
                nxt.cancel()
                raise Refused("LOCKED" if not alive() else "INCOMPLETE", f"nothing for {idle:.0f} s")
        try:
            chunk = nxt.result()
        except StopAsyncIteration:
            return n
        idle = 0.0
        n += len(chunk)
        if n > limit:
            raise Refused("TOO_BIG", f"{n / 1e6:.0f} MB")
        if not alive():
            raise Refused("LOCKED")
        await write(chunk)


# Pages are classic scripts plus inline style attributes, a data-URI icon and same-origin streams.
# script-src stays 'self' (no inline script). style-src allows the style attributes the pages already use.
CSP = (
    "default-src 'self' blob:; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; img-src 'self' data: blob:; connect-src 'self'; media-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
SESSION_COOKIE = "kiro_session"
SESSION_MAX = 30 * 24 * 3600
_DROP = {b"x-frame-options", b"content-security-policy", b"x-content-type-options",
         b"referrer-policy", b"strict-transport-security"}


def _host_name(value: str) -> str:
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    if v.count(":") == 1:
        return v.split(":", 1)[0]
    return v


def _origin_host(origin: str) -> str:
    try:
        return (urlsplit(origin).hostname or "").lower()
    except ValueError:
        return ""


def host_allowlist() -> set[str]:
    """Names this server will answer. A DNS-rebind Host that is not in here is refused, even when Origin matches it."""
    hosts = {"localhost", "127.0.0.1", "::1", "10.42.0.1"}
    for part in os.environ.get("KIRO_ALLOWED_HOSTS", "").split(","):
        name = _host_name(part)
        if name:
            hosts.add(name)
    # The test client calls itself "testserver". That name is not allowed on a real run.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        hosts.add("testserver")
    try:
        hosts.add(socket.gethostname().lower())
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 9))
            hosts.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    return hosts


def request_allowed(headers: dict[str, str], state_changing: bool) -> bool:
    """Host must be this robot. A state change (POST, WebSocket) also needs an Origin on that list.
    A missing Origin used to be allowed; a page that is not a browser, or a rebinding site, could use that."""
    allow = host_allowlist()
    if _host_name(headers.get("host", "")) not in allow:
        return False
    origin = headers.get("origin", "")
    if state_changing and not origin:
        return False
    if origin and _origin_host(origin) not in allow:
        return False
    return True


def response_headers(existing: list[tuple[bytes, bytes]], scheme: str, path: str) -> list[tuple[bytes, bytes]]:
    """Security headers on every response, including camera streams and errors.
    Camera streams keep their own Cache-Control (no-store). Pages stay no-cache so an update cannot mix files.
    HSTS only on HTTPS: the test client speaks HTTP and must not be told to upgrade."""
    headers = [(k, v) for k, v in existing if k.lower() not in _DROP]
    headers += [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"DENY"),
        (b"content-security-policy", CSP.encode()),
    ]
    if scheme == "https":
        headers.append((b"strict-transport-security", b"max-age=31536000"))
    if not path.startswith(("/camera/", "/api/studio/camera/")):
        headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
        headers.append((b"cache-control", b"no-cache"))
    return headers


def _scope_headers(scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        out[key.decode("latin1").lower()] = value.decode("latin1")
    return out


class SecurityMiddleware:
    """Host allowlist, Origin on state changes, and the security headers. Cameras are not skipped."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if scope["type"] == "http":
            changing = scope.get("method", "GET") not in ("GET", "HEAD", "OPTIONS")
            if not request_allowed(_scope_headers(scope), changing):
                body = b"Forbidden"
                headers = response_headers([], scope.get("scheme", "http"), scope.get("path", ""))
                headers += [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]
                await send({"type": "http.response.start", "status": 403, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return

            async def send_safe(msg):
                if msg["type"] == "http.response.start":
                    msg = {**msg, "headers": response_headers(msg.get("headers") or [], scope.get("scheme", "http"), scope.get("path", ""))}
                await send(msg)
            return await self.app(scope, receive, send_safe)
        return await self.app(scope, receive, send)


def confirm_action(msg: dict) -> str | None:
    """Actions that need a one-use confirm token: shutdown, reboot, torque release, servo ID change."""
    kind = msg.get("t")
    if kind == "system" and msg.get("action") in ("shutdown", "reboot"):
        return "system:" + str(msg.get("action"))
    if kind == "arm" and msg.get("action") == "release":
        return "arm:release"
    if kind == "servo" and msg.get("cmd") == "setid":
        return "servo:setid"
    if kind == "servo" and msg.get("cmd") == "torque" and not msg.get("value"):
        return "servo:torque_off"
    return None


def robot_view(authed: bool, snap: dict) -> dict:
    """Health, voice transcripts, the event log and stop records are only for a signed-in connection."""
    if authed:
        return snap
    out = dict(snap)
    out["health"] = {}
    out["voice"] = {}
    out["events"] = []
    out["stops"] = []
    out["voice_models"] = []
    out["voice_rx"] = {}
    return out


def guard_stop_kind(status: str | None) -> str | None:
    """The old lidar guard's status line -> 'no_scan' (scans stopped), 'covered' (covered or touched), or None.
    Its form: 'scan OK(432점) | 앞 ... | 최근접 ... | 가림 없음 | ...' ('scan 없음' when the scans stopped)."""
    if not status:
        return None
    if status.startswith("scan 없음"):
        return "no_scan"
    parts = status.split(" | ")
    return "covered" if len(parts) > 3 and parts[3].startswith("가림") and parts[3] != "가림 없음" else None


def create_app(cfg: Config | None = None, base=None, robot: Robot | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    ctrl = Controller(cfg)
    sockets: dict[str, tuple[WebSocket, asyncio.Lock]] = {}
    logs = install_log()
    log_subs: set[str] = set()  # pages showing the server log (the dashboard terminal)
    state: dict = {"base": base, "robot": robot, "odo_m": 0.0, "last_xy": None, "started": time.time()}
    # Obstacle stop: on unless KIRO_GUARD=0; the operator can switch it on the Drive tab.
    gcfg = guard.GuardCfg(half_len=cfg.robot_len / 2, half_wid=cfg.robot_wid / 2, stop=cfg.guard_stop,
                          slow=max(cfg.guard_slow, cfg.guard_stop))
    # seen = when the last lidar scan arrived (None = no lidar since the base started: drive without it)
    state["guard"] = {"on": cfg.guard, "lidar": False, "lost": False, "block": [], "seen": None}

    async def send(cid: str, msg: dict) -> None:
        """Never lets one device stall the server: a page that stops reading (left the Wi-Fi,
        frozen tab) is dropped after 0.3 s instead of holding up the 20 Hz control loop."""
        entry = sockets.get(cid)
        if not entry:
            return
        ws, lock = entry
        try:
            async with lock:
                await asyncio.wait_for(ws.send_json(msg), 0.3)
        except Exception:
            if sockets.get(cid) is entry:
                sockets.pop(cid, None)
                asyncio.get_running_loop().create_task(_close(ws))  # its receive loop then cleans up

    async def _close(ws: WebSocket) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), 1.0)

    def snapshot(cid: str) -> dict:
        b = state["base"]
        s = ctrl.snapshot(cid)
        s.update(t="state", backend=b.name, odom=b.odom(), rates=b.rates() if hasattr(b, "rates") else None, version=__version__,
                 odo_m=round(state["odo_m"], 2), up_s=round(time.time() - state["started"]),
                 guard={**{k: v for k, v in state["guard"].items() if k != "seen"}, "stop_m": gcfg.stop})
        return s

    def who(cid: str) -> str:
        c = ctrl.clients.get(cid)
        return c.name if c else cid

    async def loop() -> None:
        period = 1.0 / cfg.rate_hz
        st = {"last": time.monotonic(), "n": 0, "log_n": logs.n}
        last_err = 0.0
        while True:
            await asyncio.sleep(period)
            try:
                await step(st)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - this loop is the heartbeat stop: it must keep running
                if time.monotonic() - last_err > 5:
                    log.exception("control loop")
                    last_err = time.monotonic()

    async def step(st: dict) -> None:
        now = time.monotonic()
        dt, st["last"] = now - st["last"], now
        cmd = ctrl.tick()
        b = state["base"]
        vx, vy, wz = cmd.vx, cmd.vy, cmd.wz
        g = state["guard"]
        sc = b.scan(cfg.scan_points) if g["on"] and hasattr(b, "scan") else None
        g["lidar"] = bool(sc)
        if sc:
            g["seen"] = sc.get("at", now)  # when the scan really arrived (the map may show one up to 1 s old)
        g["lost"] = guard.lidar_lost(g["on"], g["seen"], now)
        if g["lost"]:  # the lidar was working and its scans stopped: nothing may move, turning included
            g["block"] = ["lidar"] if cmd.publish and (vx or vy or wz) else []
            vx = vy = wz = 0.0
        elif sc:  # obstacle stop: an obstacle close in the way stops that direction; turning stays free
            lim = guard.limits(sc["xy"], gcfg)
            g["block"] = guard.blocked(vx, vy, lim) if cmd.publish else []
            vx, vy = guard.apply(vx, vy, lim)
        else:
            g["block"] = []
        b.apply(vx, vy, wz, cmd.publish, dt)
        st["n"] += 1
        n = st["n"]
        o = b.odom()
        meter_step(st, now, b, o, cmd.publish and any(abs(v) > 1e-4 for v in (vx, vy, wz)))
        if o:  # odometer: distance driven since the server started (jumps = odometry reset)
            prev = state["last_xy"]
            d = 0.0 if prev is None else ((o["x"] - prev[0]) ** 2 + (o["y"] - prev[1]) ** 2) ** 0.5
            if d < 0.5:
                state["odo_m"] += d
            state["last_xy"] = (o["x"], o["y"])
        if n % 4 == 0:  # 5 Hz
            b.set_estop(ctrl.estop)
            sc = b.scan(cfg.scan_points) if hasattr(b, "scan") else None
            if sc and sockets:
                msg = {"t": "scan", "xy": sc["xy"], "src": sc.get("src")}
                await asyncio.gather(*(send(cid, msg) for cid in list(sockets)))
            new = logs.since(st["log_n"]) if log_subs else []
            st["log_n"] = new[-1]["n"] if new else (st["log_n"] if log_subs else logs.n)
            if new:
                msg = {"t": "log", "lines": new}
                readers = [cid for cid in list(log_subs) if ctrl.is_authed(cid)]
                await asyncio.gather(*(send(cid, msg) for cid in readers))
        if n % 2 == 0:  # 10 Hz
            await asyncio.gather(*(send(cid, snapshot(cid)) for cid in list(sockets)))
        if n % 20 == 0:  # 1 Hz
            r = state["robot"].snapshot()
            await asyncio.gather(*(send(cid, robot_view(ctrl.is_authed(cid), r)) for cid in list(sockets)))
            await send_studio([cid for cid in list(sockets) if ctrl.is_engineer(cid)])

    def meter_step(st: dict, now: float, b, o: dict | None, sent: bool) -> None:
        """Stop meter (robot_server/stopmeter.py): where the robot is, whether a speed goes to the wheels, and
        the stop triggers seen here (web dead-man let go, lidar lost, the old guard covered / no scans, the
        robot gamepad's LB). E-STOP, voice, base stop and the arm link are noted by the Robot itself."""
        r: Robot = state["robot"]
        wc = b.wheel_cmd() if hasattr(b, "wheel_cmd") else None
        r.base_meter.sample(now, (o["x"], o["y"]) if o else None, sent if wc is None else wc)
        for kind, detail, at in (b.take_events() if hasattr(b, "take_events") else []):
            r.base_meter.trigger(now, kind, detail, at=at)
        held = ctrl.held
        if st.get("held") and not held:
            r.base_meter.trigger(now, "deadman", "web dead-man let go")
        st["held"] = held
        lost = state["guard"]["lost"]
        if lost and not st.get("lost"):
            r.base_meter.trigger(now, "lidar_lost", "web obstacle stop: lidar lost")
        st["lost"] = lost
        gs = b.latest_str("/obstacle_guard/status") if hasattr(b, "latest_str") else None
        gk = guard_stop_kind(gs)
        if gk and gk != st.get("gk"):
            r.base_meter.trigger(now, gk, f"robot guard: {gs[:60]}")
        st["gk"] = gk
        r.meter_tick(now)

    async def send_studio(cids: list[str]) -> None:
        """Training data and engineer values: only to connections that are the unlocked engineer."""
        if not cids:
            return
        snap = await asyncio.to_thread(state["robot"].studio_snapshot)
        await asyncio.gather(*(send(cid, {**snap, "token": ctrl.engineer_token(cid)}) for cid in cids if ctrl.is_engineer(cid)))

    async def act(cid: str, msg: dict) -> None:
        """Robot actions. Only the operator; never while E-STOP is on (except stopping things)."""
        t = msg.get("t")
        r: Robot = state["robot"]
        on = bool(msg.get("on", True))
        if cid != ctrl.operator:
            await send(cid, {"t": "note", "ok": False, "msg": "Take control first.", "about": t})
            return
        if t in ENGINEER_ACTIONS and not ctrl.is_engineer(cid):
            await send(cid, {"t": "note", "ok": False, "msg": "Unlock the engineer tools first.", "about": t})
            return
        starting = bool(on)
        if t == "studio":  # only starting a recording moves an arm; next / again / stop / force stop / checks never do
            starting = msg.get("cmd") == "start"
        elif t in ("guard_profile", "stops_clear", "obstacle_stop"):
            starting = False
        elif t == "lidar_setup":  # measuring never moves anything; saving restarts the wheel program if it runs
            starting = msg.get("cmd") == "apply"
        elif t == "arm" and msg.get("action") == "release":
            starting = True  # torque off is blocked during E-STOP: the arm must not be let go while stopped
        elif t == "servo":  # torque off is blocked too; reading stays allowed
            starting = msg.get("cmd") in ("move", "setid", "torque")
        elif t == "wizard_decide":
            starting = False
        if ctrl.estop and starting and t != "system":
            await send(cid, {"t": "note", "ok": False, "msg": "E-STOP is on.", "about": t})
            return
        need = confirm_action(msg)
        if need and not ctrl.consume_confirm(cid, need, msg.get("confirm")):
            await send(cid, {"t": "note", "ok": False, "msg": "Confirmation required.", "about": t})
            return
        name = who(cid)
        if t == "base":  # guard distance: the engineer's choice on the Studio page, not the message's
            call = lambda: r.base(on, name, r.guard_profile)  # noqa: E731
        elif t == "guard_profile":
            call = lambda: r.set_guard_profile(str(msg.get("profile")), name)  # noqa: E731
        elif t == "obstacle_stop":
            call = lambda: r.set_obstacle_stop(on, name)  # noqa: E731
        elif t == "lidar_setup":
            call = lambda: r.lidar(str(msg.get("cmd")), name)  # noqa: E731
        elif t == "studio":
            cmd = str(msg.get("cmd"))
            if cmd == "check":
                call = lambda: (r.rec.check(msg.get("settings"), ctrl.estop)["ok"], "")  # noqa: E731
            elif cmd == "start":
                call = lambda: r.rec.start(msg.get("settings"), name, ctrl.estop)  # noqa: E731
            elif cmd in ("next", "redo", "stop", "kill"):
                call = lambda: r.rec.control(cmd, msg.get("ep"), msg.get("phase"), name)  # noqa: E731
            elif cmd == "pack":
                call = lambda: r.rec.pack(str(msg.get("repo", "")), name)  # noqa: E731
            elif cmd == "delete":   # a dataset, package or model, for good (the page asks first)
                call = lambda: r.train.delete(str(msg.get("kind")), str(msg.get("name", "")), name)  # noqa: E731
            elif cmd == "cam_repair":  # re-plugs the top camera's USB in software; never moves anything
                call = lambda: r.cam_repair(name)  # noqa: E731
            elif cmd == "colab":    # train in Colab, the automatic way (robot_server/colab.py)
                act_ = str(msg.get("action"))
                if act_ == "start":
                    call = lambda: r.colab.start(str(msg.get("repo", "")), name)  # noqa: E731
                elif act_ == "connect":
                    call = lambda: r.colab.connect(str(msg.get("url", "")), str(msg.get("key", "")), name)  # noqa: E731
                elif act_ == "stop":
                    call = lambda: r.colab.stop(name)  # noqa: E731
                else:
                    return
            elif cmd == "hello":
                call = lambda: (True, "")  # noqa: E731
            else:
                return
        elif t == "say":
            call = lambda: r.say(str(msg.get("text", "")), name)  # noqa: E731
        elif t == "stops_clear":
            call = lambda: r.stops_clear(name)  # noqa: E731
        elif t == "servo":
            def call():
                return r.servo(str(msg.get("cmd")), str(msg.get("port", "")), name, int(msg.get("id") or 0),
                               int(msg.get("value") or 0), int(msg.get("lo") or 1), int(msg.get("hi") or 12), bool(msg.get("arm_ok")))
        elif t == "wizard":
            call = lambda: r.wizard(str(msg.get("step")), on, name, int(msg.get("seconds") or 20))  # noqa: E731
        elif t == "wizard_decide":
            call = lambda: r.wizard_decide(str(msg.get("step")), str(msg.get("status")), name)  # noqa: E731
        elif t == "leader":
            call = lambda: r.leader(on, str(msg.get("side", "right")), name)  # noqa: E731
        elif t == "wave":
            call = lambda: r.wave(str(msg.get("side", "right")), name)  # noqa: E731
        elif t == "demo":
            call = lambda: r.demo(on, name, int(msg.get("repeat") or 5), float(msg.get("pause") or 3))  # noqa: E731
        elif t == "follow":
            call = lambda: r.follow(on, name)  # noqa: E731
        elif t == "teleop":
            call = lambda: r.teleop(str(msg.get("side")), on, name)  # noqa: E731
        elif t == "arm":
            call = lambda: r.arm(str(msg.get("side")), str(msg.get("action")), name)  # noqa: E731
        elif t == "skill":
            call = lambda: r.skill(str(msg.get("name", "")), on, name, int(msg.get("repeat") or 1), float(msg.get("pause") or 10), msg.get("seconds"))  # noqa: E731
        elif t == "voice":
            call = lambda: r.voice(on, name, str(msg.get("lang", "ko")), str(msg.get("step", "big")), str(msg.get("mic", "device")))  # noqa: E731
        elif t == "system":
            call = lambda: r.system(str(msg.get("action")), name)  # noqa: E731
        else:
            return
        try:
            ok, text = await asyncio.to_thread(call)
        except Exception as e:  # noqa: BLE001
            log.exception("action %s failed", t)
            ok, text = False, f"Error: {e}"
        if t == "base" and ok:  # a new base run: the lidar has to show itself again before its loss stops the robot
            state["guard"]["seen"] = None
        if starting and ctrl.estop:  # E-STOP was pressed while this was starting: stop it now
            await asyncio.to_thread(r.on_estop, ctrl.estop_by or name)
        await send(cid, {"t": "note", "ok": ok, "msg": text, "about": t, "cmd": msg.get("cmd")})
        await send(cid, r.snapshot())
        if t in ENGINEER_ACTIONS:
            await send_studio([cid])

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["base"] is None:
            state["base"] = make_base(cfg)
        b = state["base"]
        if b.name == "ros" and cfg.pin_is_default:
            raise SystemExit("Refusing to drive a real robot with the default PIN. Set KIRO_OPERATOR_PIN first.")
        if state["robot"] is None:
            state["robot"] = Robot(cfg, b)
        if b.name == "mock" and not ctrl.engineer_pin:
            ctrl.engineer_pin = MOCK_ENGINEER_PIN
        log.info("KIRO robot server %s, backend: %s", __version__, b.name)
        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            try:  # stop every program this server started (base, teleop, skill, voice...)
                await asyncio.to_thread(state["robot"].close)
            finally:
                b.close()

    app = FastAPI(title="KIRO Command Center", version=__version__, lifespan=lifespan)
    app.add_middleware(SecurityMiddleware)

    def logged_in(request: Request) -> bool:
        return ctrl.session_ok(request.cookies.get(SESSION_COOKIE, ""))

    def require_login(request: Request) -> None:
        if not logged_in(request):
            raise HTTPException(401, "Log in first.")
    app.state.ctrl = ctrl
    app.state.data = state

    def _session_cookie(response: JSONResponse, request: Request, sid: str) -> JSONResponse:
        # Secure only on HTTPS so the HTTP test client can still send the cookie. SameSite=Lax
        # so a top-level download (and the Colab tab's first GET) still carries it.
        response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_MAX, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", path="/")
        return response

    @app.post("/api/login")
    async def login(request: Request):
        """One PIN, asked once. The cookie is the login after that; the page does not ask again."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        ip = request.client.host if request.client else ""
        err = ctrl.check_pin(ip, body.get("pin", ""))
        if err:
            return JSONResponse({"ok": False, "msg": err}, status_code=401)
        return _session_cookie(JSONResponse({"ok": True}), request, ctrl.open_session())

    @app.get("/api/session")
    def session(request: Request):
        if not logged_in(request):
            raise HTTPException(401, "Log in first.")
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request):
        ctrl.drop_session(request.cookies.get(SESSION_COOKIE, ""))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/stops.csv")
    def stops_csv(request: Request):
        """Every stop measured since the server started (or the last Clear): for the test report sheets."""
        require_login(request)
        return Response(state["robot"].stops_csv(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="stops_{time.strftime("%Y%m%d_%H%M")}.csv"'})

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        require_login(request)
        return {"ok": True, "version": __version__, "backend": state["base"].name, "clients": len(sockets)}

    @app.get("/api/battery")
    def battery(request: Request) -> dict:
        """INA228 readings plus the estimated percent, time remaining, health and cycles."""
        require_login(request)
        return state["robot"].battery_detail()

    @app.get("/camera/{key}.mjpg")
    def camera(key: str, request: Request):
        require_login(request)
        cam = state["robot"].cams.get(key) if state["robot"] else None
        if cam is None:
            raise HTTPException(404, "no such camera")
        if cam.paused_by:
            raise HTTPException(409, f"camera in use by {cam.paused_by}")

        def stream():
            cam.attach()
            try:
                for jpg in cam.frames():
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
            finally:
                cam.detach()

        return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    def _studio_alive(request: Request, header_token: str):
        """Keeps a Studio stream open only while this login (or header token) is still the engineer.
        A token in the query string is ignored on purpose: it would be saved in logs and history."""
        if header_token and ctrl.token_ok(header_token):
            return lambda: ctrl.token_ok(header_token)
        sid = request.cookies.get(SESSION_COOKIE, "")
        if ctrl.session_is_engineer(sid):
            return lambda: ctrl.session_is_engineer(sid)
        return None

    @app.get("/api/studio/camera/{key}.mjpg")
    def studio_camera(key: str, request: Request):
        """The Studio page's camera previews. Cookie of the engineer login, or the X-Studio-Token header.
        The stream ends as soon as the engineer locks or leaves."""
        alive = _studio_alive(request, request.headers.get("x-studio-token", ""))
        if alive is None:
            raise HTTPException(403, "Studio is locked")
        cam = state["robot"].cams.get(key) if state["robot"] else None
        if cam is None:
            raise HTTPException(404, "no such camera")
        if cam.paused_by:
            raise HTTPException(409, f"camera in use by {cam.paused_by}")

        def stream():
            cam.attach()
            try:
                for jpg in cam.frames():
                    if not alive():
                        return
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
            finally:
                cam.detach()

        return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    # ------------------------------------------------ training files (Studio)
    def studio_who(request: Request) -> tuple[str, object]:
        """(who, still_open) of the unlocked engineer, or 403. The browser sends its session cookie.
        Tests may send X-Studio-Token. A ?token= in the URL does not count."""
        alive = _studio_alive(request, request.headers.get("x-studio-token", ""))
        if alive is None:
            raise HTTPException(403, "Studio is locked")
        tok = request.headers.get("x-studio-token", "")
        if tok and ctrl.token_ok(tok):
            return who(ctrl.tokens[tok]), alive
        return "session", alive

    @app.get("/api/studio/packages/{file}")
    def studio_package(file: str, request: Request):
        """A dataset package (.tar.gz) or its checksum (.sha256), to take to Google Colab."""
        _, alive = studio_who(request)
        p = state["robot"].train.package_path(file)
        if p is None:
            raise HTTPException(404, "no such package")
        return StreamingResponse(file_chunks(p, alive),
                                 media_type="application/gzip" if file.endswith(".gz") else "text/plain",
                                 headers={"Content-Disposition": f'attachment; filename="{file}"', "Content-Length": str(p.stat().st_size)})

    @app.get("/api/studio/models/{name}.zip")
    def studio_model_zip(name: str, request: Request):
        """A trained model as a zip (built while it is sent)."""
        _, alive = studio_who(request)
        tr = state["robot"].train
        if tr.model_dir(name) is None:
            raise HTTPException(404, "no such model")
        return StreamingResponse(tr.zip_stream(name, alive), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{name}_pretrained_model.zip"'})

    @app.get("/api/studio/notebook.ipynb")
    def studio_notebook(request: Request):
        """The Colab notebook itself, for uploading it in Colab when the GitHub link does not work."""
        studio_who(request)
        nb = state["robot"].train.notebook
        if not nb.is_file():
            raise HTTPException(404, "no notebook")
        return FileResponse(nb, filename=nb.name, media_type="application/x-ipynb+json")

    @app.post("/api/studio/models")
    async def studio_upload(request: Request, name: str = ""):
        """A trained model zip from the browser (the request body). Written to a temporary file while it
        arrives, then checked and installed as <policies>/<name>/pretrained_model (training.py)."""
        by, alive = studio_who(request)
        tr = state["robot"].train

        def refused(e: Refused) -> JSONResponse:
            code = 409 if e.code in ("EXISTS", "BUSY") else 413 if e.code == "TOO_BIG" else 400
            return JSONResponse({"ok": False, "code": e.code, "detail": e.detail}, status_code=code)
        try:
            size = int(request.headers["content-length"])
        except (KeyError, ValueError):
            size = None
        try:
            tmp = await asyncio.to_thread(tr.begin_upload, name, size, by)
        except Refused as e:
            return refused(e)
        try:
            with open(tmp, "wb") as f:
                n = await copy_upload(request.stream(), lambda c: asyncio.to_thread(f.write, c), alive, ZIP_MAX)
            if size is not None and n != size:
                raise Refused("INCOMPLETE", f"{n} of {size} bytes")
            info = await asyncio.to_thread(tr.install, tmp, name, by)
        except Refused as e:
            log.info("model upload %s refused: %s %s", name, e.code, e.detail)
            return refused(e)
        except ClientDisconnect:
            return JSONResponse({"ok": False, "code": "INCOMPLETE"}, status_code=400)
        except OSError as e:     # writing the zip failed
            return refused(Refused("DISK" if e.errno == errno.ENOSPC else "WRITE", str(e)[:120]))
        finally:
            await asyncio.to_thread(tr.end_upload, tmp)
        return {"ok": True, "model": info}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        # Browsers send Origin. A missing one is refused, and so is a Host that is not this robot
        # (DNS rebinding, where Origin and Host are both the attacker's name).
        hdrs = {k.lower(): v for k, v in ws.headers.items()}
        if not request_allowed(hdrs, True):
            await ws.close(code=1008)
            return
        await ws.accept()
        cid = uuid.uuid4().hex[:8]
        sockets[cid] = (ws, asyncio.Lock())
        ctrl.connect(cid, ws.query_params.get("name", "device"), ip=ws.client.host if ws.client else None)
        ctrl.attach_session(cid, ws.cookies.get(SESSION_COOKIE, ""))
        tasks: set[asyncio.Task] = set()
        try:
            await send(cid, snapshot(cid))
            await send(cid, robot_view(ctrl.is_authed(cid), state["robot"].snapshot()))
            while True:
                raw = await ws.receive()
                if raw.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect(raw.get("code", 1000))
                if raw.get("bytes") is not None:  # the phone's / laptop's microphone for voice (16 kHz mono 16-bit)
                    if cid == ctrl.operator and not ctrl.estop:
                        state["robot"].voice_audio(raw["bytes"])
                    continue
                msg = json.loads(raw.get("text") or "null")
                if not isinstance(msg, dict):
                    continue
                t = msg.get("t")
                if t == "drive":
                    ctrl.drive(cid, msg.get("x"), msg.get("y"), msg.get("z"), msg.get("en"),
                               msg.get("turbo"), msg.get("src", "none"))
                elif t == "ping":
                    ctrl.seen(cid)
                    await send(cid, {"t": "pong", "ts": msg.get("ts")})
                elif t == "take":
                    had = ctrl.operator
                    ok, text = ctrl.take(cid, msg.get("pin", ""), bool(msg.get("force")))
                    if ok and had != cid:
                        state["robot"].event(who(cid), "took control" + (" (took over)" if had else ""))
                    await send(cid, {"t": "note", "ok": ok, "msg": text, "about": "take"})
                elif t == "release":
                    if ctrl.release(cid):
                        state["robot"].event(who(cid), "released control")
                elif t == "estop":
                    on = bool(msg.get("on"))
                    ok, text = ctrl.set_estop(cid, on)
                    state["base"].set_estop(ctrl.estop)
                    if ok:
                        state["robot"].event(who(cid), "E-STOP on" if on else "E-STOP released")
                    if ok and on:  # every press, also a second one: stop skill, arms, voice, wizard, base
                        asyncio.get_running_loop().run_in_executor(None, state["robot"].on_estop, who(cid))
                    await send(cid, {"t": "note", "ok": ok, "msg": text, "about": "estop"})
                elif t == "engineer":
                    if msg.get("lock"):
                        ctrl.lock_engineer(cid)
                    elif ctrl.grant_session_engineer(cid):
                        await send(cid, {"t": "note", "ok": True, "msg": "Engineer tools unlocked.", "about": "engineer"})
                        state["robot"].event(who(cid), "unlocked engineer tools")
                        await send_studio([cid])
                        steps = await asyncio.to_thread(state["robot"].wizard_steps)
                        await send(cid, {"t": "wizard_steps", "steps": steps})
                    else:
                        ok, text = ctrl.unlock_engineer(cid, msg.get("pin", ""))
                        await send(cid, {"t": "note", "ok": ok, "msg": text, "about": "engineer"})
                        if ok:
                            state["robot"].event(who(cid), "unlocked engineer tools")
                            await send_studio([cid])
                            steps = await asyncio.to_thread(state["robot"].wizard_steps)
                            await send(cid, {"t": "wizard_steps", "steps": steps})
                elif t == "confirm":
                    action = str(msg.get("action") or "")
                    if action not in ("system:shutdown", "system:reboot", "arm:release", "servo:setid", "servo:torque_off"):
                        await send(cid, {"t": "confirm", "ok": False, "action": action, "msg": "Unknown action."})
                    else:
                        ok, text = ctrl.begin_confirm(cid, action)
                        await send(cid, {"t": "confirm", "ok": ok, "action": action, "token": text if ok else "",
                                         "msg": "" if ok else text})
                elif t == "logs":  # the dashboard terminal: the recent server log once, then every new line
                    if not ctrl.is_authed(cid):
                        await send(cid, {"t": "note", "ok": False, "msg": "Log in first.", "about": "logs"})
                    elif not msg.get("on", True):
                        log_subs.discard(cid)
                    elif cid not in log_subs:
                        log_subs.add(cid)
                        await send(cid, {"t": "log", "lines": logs.since(0), "reset": True})
                elif t == "leader_pos":  # leader arm on the laptop, about 30 per second: fast path
                    if cid == ctrl.operator and not ctrl.estop:
                        state["robot"].leader_pos(str(msg.get("side", "right")), msg.get("raw"))
                elif t == "guard":  # obstacle stop on or off: the operator's decision, written to the log
                    if cid == ctrl.operator:
                        state["guard"]["on"] = bool(msg.get("on"))
                        state["robot"].event(who(cid), "obstacle stop " + ("on" if state["guard"]["on"] else "OFF"))
                        # the same switch for the robot's own lidar guard (gamepad, voice): may restart the wheel program
                        task = asyncio.create_task(act(cid, {"t": "obstacle_stop", "on": state["guard"]["on"]}))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    else:
                        await send(cid, {"t": "note", "ok": False, "msg": "Take control first.", "about": "guard"})
                elif t == "talk":  # hold-to-talk: fast, no thread needed
                    if cid == ctrl.operator and not ctrl.estop:
                        ok, text = state["robot"].talk(bool(msg.get("on")))
                        if not ok:
                            await send(cid, {"t": "note", "ok": False, "msg": text, "about": "talk"})
                elif t in OPERATOR_ACTIONS:
                    task = asyncio.create_task(act(cid, msg))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            sockets.pop(cid, None)
            log_subs.discard(cid)
            if ctrl.operator == cid:
                state["robot"].event(who(cid), "disconnected while in control")
            ctrl.disconnect(cid)

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
