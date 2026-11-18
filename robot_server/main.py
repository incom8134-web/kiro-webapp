"""The web server: serves the command center page, one WebSocket (/ws) and the camera streams."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .base import make_base
from .config import MOCK_ENGINEER_PIN, Config
from .control import Controller
from .robot import Robot

log = logging.getLogger("kiro")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Messages that only the operator may send. Each maps to a Robot method run in a thread
# (starting or stopping a program can take seconds).
OPERATOR_ACTIONS = {"base", "teleop", "arm", "skill", "voice", "system", "say", "servo", "wizard", "wizard_decide"}
ENGINEER_ACTIONS = {"servo", "wizard", "wizard_decide"}  # also need the engineer PIN


def create_app(cfg: Config | None = None, base=None, robot: Robot | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    ctrl = Controller(cfg)
    sockets: dict[str, tuple[WebSocket, asyncio.Lock]] = {}
    state: dict = {"base": base, "robot": robot, "odo_m": 0.0, "last_xy": None, "started": time.time()}

    async def send(cid: str, msg: dict) -> None:
        entry = sockets.get(cid)
        if not entry:
            return
        ws, lock = entry
        try:
            async with lock:
                await ws.send_json(msg)
        except Exception:
            pass  # the receive loop notices the closed socket and cleans up

    def snapshot(cid: str) -> dict:
        b = state["base"]
        s = ctrl.snapshot(cid)
        s.update(t="state", backend=b.name, odom=b.odom(), version=__version__,
                 default_pin=cfg.pin_is_default and b.name == "mock",
                 mock_engineer_pin=b.name == "mock" and ctrl.engineer_pin == MOCK_ENGINEER_PIN,
                 odo_m=round(state["odo_m"], 2), up_s=round(time.time() - state["started"]))
        return s

    def who(cid: str) -> str:
        c = ctrl.clients.get(cid)
        return c.name if c else cid

    async def loop() -> None:
        period = 1.0 / cfg.rate_hz
        st = {"last": time.monotonic(), "n": 0, "estop_seen": False}
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
        b.apply(cmd.vx, cmd.vy, cmd.wz, cmd.publish, dt)
        st["n"] += 1
        n = st["n"]
        if ctrl.estop and not st["estop_seen"]:  # E-STOP just went on: stop skills, voice and wizard too
            asyncio.get_running_loop().run_in_executor(None, state["robot"].on_estop, ctrl.estop_by or "?")
        st["estop_seen"] = ctrl.estop
        o = b.odom()
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
                msg = {"t": "scan", **sc}
                await asyncio.gather(*(send(cid, msg) for cid in list(sockets)))
        if n % 2 == 0:  # 10 Hz
            await asyncio.gather(*(send(cid, snapshot(cid)) for cid in list(sockets)))
        if n % 20 == 0:  # 1 Hz
            r = state["robot"].snapshot()
            await asyncio.gather(*(send(cid, r) for cid in list(sockets)))

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
        starting = on and not (t == "arm" and msg.get("action") == "release")
        if t == "servo":  # reading and letting go are always allowed; moving and writing are not during E-STOP
            starting = msg.get("cmd") in ("move", "setid") or (msg.get("cmd") == "torque" and bool(msg.get("value")))
        elif t == "wizard_decide":
            starting = False
        if ctrl.estop and starting and t != "system":
            await send(cid, {"t": "note", "ok": False, "msg": "E-STOP is on.", "about": t})
            return
        name = who(cid)
        if t == "base":
            call = lambda: r.base(on, name, str(msg.get("profile", "close")))  # noqa: E731
        elif t == "say":
            call = lambda: r.say(str(msg.get("text", "")), name)  # noqa: E731
        elif t == "servo":
            def call():
                return r.servo(str(msg.get("cmd")), str(msg.get("port", "")), name, int(msg.get("id") or 0),
                               int(msg.get("value") or 0), int(msg.get("lo") or 1), int(msg.get("hi") or 12), bool(msg.get("arm_ok")))
        elif t == "wizard":
            call = lambda: r.wizard(str(msg.get("step")), on, name, int(msg.get("seconds") or 20))  # noqa: E731
        elif t == "wizard_decide":
            call = lambda: r.wizard_decide(str(msg.get("step")), str(msg.get("status")), name)  # noqa: E731
        elif t == "teleop":
            call = lambda: r.teleop(str(msg.get("side")), on, name)  # noqa: E731
        elif t == "arm":
            call = lambda: r.arm(str(msg.get("side")), str(msg.get("action")), name)  # noqa: E731
        elif t == "skill":
            call = lambda: r.skill(str(msg.get("name", "")), on, name)  # noqa: E731
        elif t == "voice":
            call = lambda: r.voice(on, name, str(msg.get("lang", "ko")))  # noqa: E731
        elif t == "system":
            call = lambda: r.system(str(msg.get("action")), name)  # noqa: E731
        else:
            return
        try:
            ok, text = await asyncio.to_thread(call)
        except Exception as e:  # noqa: BLE001
            log.exception("action %s failed", t)
            ok, text = False, f"Error: {e}"
        await send(cid, {"t": "note", "ok": ok, "msg": text, "about": t})
        await send(cid, r.snapshot())

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
    app.state.ctrl = ctrl
    app.state.data = state

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "version": __version__, "backend": state["base"].name, "clients": len(sockets)}

    @app.get("/camera/{key}.mjpg")
    def camera(key: str):
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

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        cid = uuid.uuid4().hex[:8]
        sockets[cid] = (ws, asyncio.Lock())
        ctrl.connect(cid, ws.query_params.get("name", "device"))
        await send(cid, snapshot(cid))
        await send(cid, state["robot"].snapshot())
        tasks: set[asyncio.Task] = set()
        try:
            while True:
                msg = await ws.receive_json()
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
                    await send(cid, {"t": "note", "ok": ok, "msg": text, "about": "estop"})
                elif t == "engineer":
                    if msg.get("lock"):
                        ctrl.lock_engineer(cid)
                    else:
                        ok, text = ctrl.unlock_engineer(cid, msg.get("pin", ""))
                        await send(cid, {"t": "note", "ok": ok, "msg": text, "about": "engineer"})
                        if ok:
                            state["robot"].event(who(cid), "unlocked engineer tools")
                            steps = await asyncio.to_thread(state["robot"].wizard_steps)
                            await send(cid, {"t": "wizard_steps", "steps": steps})
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
            if ctrl.operator == cid:
                state["robot"].event(who(cid), "disconnected while in control")
            ctrl.disconnect(cid)

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
