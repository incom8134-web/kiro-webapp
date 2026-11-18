"""The web server: serves the command center page and one WebSocket (/ws)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import __version__
from .base import make_base
from .config import Config
from .control import Controller

log = logging.getLogger("kiro")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(cfg: Config | None = None, base=None) -> FastAPI:
    cfg = cfg or Config.from_env()
    ctrl = Controller(cfg)
    sockets: dict[str, tuple[WebSocket, asyncio.Lock]] = {}
    state = {"base": base}

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
                 default_pin=cfg.pin_is_default and b.name == "mock")
        return s

    async def loop() -> None:
        period = 1.0 / cfg.rate_hz
        last = time.monotonic()
        n = 0
        while True:
            await asyncio.sleep(period)
            now = time.monotonic()
            dt, last = now - last, now
            cmd = ctrl.tick()
            b = state["base"]
            b.apply(cmd.vx, cmd.vy, cmd.wz, cmd.publish, dt)
            n += 1
            if n % 4 == 0:  # 5 Hz
                b.set_estop(ctrl.estop)
            if n % 2 == 0:  # 10 Hz
                await asyncio.gather(*(send(cid, snapshot(cid)) for cid in list(sockets)))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["base"] is None:
            state["base"] = make_base(cfg)
        b = state["base"]
        if b.name == "ros" and cfg.pin_is_default:
            raise SystemExit("Refusing to drive a real robot with the default PIN. Set KIRO_OPERATOR_PIN first.")
        log.info("KIRO robot server %s, backend: %s", __version__, b.name)
        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            b.close()

    app = FastAPI(title="KIRO Command Center", version=__version__, lifespan=lifespan)
    app.state.ctrl = ctrl

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "version": __version__, "backend": state["base"].name, "clients": len(sockets)}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        cid = uuid.uuid4().hex[:8]
        sockets[cid] = (ws, asyncio.Lock())
        ctrl.connect(cid, ws.query_params.get("name", "device"))
        await send(cid, snapshot(cid))
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
                    ok, text = ctrl.take(cid, msg.get("pin", ""), bool(msg.get("force")))
                    await send(cid, {"t": "note", "ok": ok, "msg": text, "about": "take"})
                elif t == "release":
                    ctrl.release(cid)
                elif t == "estop":
                    ok, text = ctrl.set_estop(cid, bool(msg.get("on")))
                    state["base"].set_estop(ctrl.estop)
                    await send(cid, {"t": "note", "ok": ok, "msg": text, "about": "estop"})
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            sockets.pop(cid, None)
            ctrl.disconnect(cid)

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
