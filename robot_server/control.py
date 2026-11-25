"""Operator lock, E-STOP and drive watchdog.

Pure logic: no network and no ROS in this file, so it can be tested on any
computer. The server calls `drive()` when a message arrives and `tick()` 20
times per second to get the speed that is safe to send to the wheels.
"""
from __future__ import annotations

import hmac
import math
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config

MAX_BAD_PINS = 5
PIN_LOCKOUT_S = 30.0
PIN_LOCKOUT_MAX_S = 900.0  # later lockouts double, and stop growing at 15 minutes
CONFIRM_TTL_S = 60.0
SESSION_S = 30 * 24 * 3600
ZERO_TICKS = 5  # zeros sent after driving ends, then the server goes silent

Vec = tuple[float, float, float]
ZERO: Vec = (0.0, 0.0, 0.0)


@dataclass
class Client:
    name: str
    last_seen: float
    ip: str = ""
    authed: bool = False   # signed in (session cookie, or a successful PIN on this connection)
    session: str = ""      # set only for a cookie login; that is what unlocks the engineer tools


@dataclass
class PinGuard:
    """Wrong-PIN counter for one device ADDRESS (not one connection): reconnecting does not reset it."""
    bad_pins: int = 0
    locked_until: float = 0.0
    strikes: int = 0       # how many lockouts this address has already served; the next one is longer


@dataclass(frozen=True)
class Command:
    vx: float
    vy: float
    wz: float
    publish: bool  # False = stay silent so another source (robot gamepad) may drive


def _unit(v: object) -> float:
    """Any input -> a safe number between -1 and 1."""
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return max(-1.0, min(1.0, f))


def _slew(cur: float, tgt: float, step: float) -> float:
    """Move cur toward tgt. Speeding up is limited by step. Slowing down is instant."""
    if tgt == 0.0 or (cur * tgt > 0 and abs(tgt) <= abs(cur)):
        return tgt
    if cur * tgt < 0:  # reversing: pass through zero first
        cur = 0.0
    return min(tgt, cur + step) if tgt > cur else max(tgt, cur - step)


class Controller:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self.clients: dict[str, Client] = {}
        self.operator: str | None = None
        self.estop = False
        self.estop_by: str | None = None
        self._target: Vec = ZERO
        self._out: Vec = ZERO
        self._src = "none"
        self._last_drive = -math.inf
        self._last_tick: float | None = None
        self._need_neutral = True  # dead-man must be let go once before motion
        self._held = False  # the operator holds the dead-man now: only then does the server drive
        self._was_active = False
        self._zero_left = 0
        self.engineer_pin = cfg.engineer_pin  # the server sets the mock default in mock mode
        self.engineers: set[str] = set()      # operators who also unlocked the engineer tools
        self.tokens: dict[str, str] = {}      # Studio header token -> connection (not accepted from a URL)
        self.pin_guard: dict[str, PinGuard] = {}
        self.sessions: dict[str, float] = {}  # cookie id -> wall-clock expiry (lost on restart)
        self.confirms: dict[tuple[str, str], tuple[str, float]] = {}  # (connection, action) -> (token, monotonic expiry)

    # ---- connections -------------------------------------------------
    def connect(self, cid: str, name: str, ip: str | None = None) -> None:
        self.clients[cid] = Client(name=(name or "device")[:24], last_seen=self.clock(), ip=ip or cid)

    def disconnect(self, cid: str) -> None:
        self.clients.pop(cid, None)
        self.lock_engineer(cid)
        if self.operator == cid:
            self._set_operator(None)

    def seen(self, cid: str) -> None:
        c = self.clients.get(cid)
        if c:
            c.last_seen = self.clock()

    # ---- operator lock -----------------------------------------------
    def check_pin(self, ip: str, pin: object, want: str | None = None) -> str | None:
        """None if the PIN is right. Wrong PINs share one lockout per address, and each lockout is longer than the last."""
        now = self.clock()
        want = self.cfg.pin if want is None else want
        g = self.pin_guard.setdefault(ip or "?", PinGuard())
        if now < g.locked_until:
            return f"Too many wrong PINs. Wait {int(g.locked_until - now) + 1} s."
        if not hmac.compare_digest(str(pin if pin is not None else "").encode(), str(want).encode()):
            g.bad_pins += 1
            if g.bad_pins >= MAX_BAD_PINS:
                g.bad_pins = 0
                g.strikes += 1
                delay = min(PIN_LOCKOUT_MAX_S, PIN_LOCKOUT_S * (2 ** (g.strikes - 1)))
                g.locked_until = now + delay
            return "Wrong PIN."
        g.bad_pins = 0
        g.strikes = 0
        return None

    def _check_pin(self, c: Client, pin: object, want: str) -> str | None:
        c.last_seen = self.clock()
        return self.check_pin(c.ip, pin, want)

    def take(self, cid: str, pin: object = None, force: bool = False) -> tuple[bool, str]:
        c = self.clients.get(cid)
        if c is None:
            return False, "Not connected."
        # A signed-in browser does not send the PIN again. A connection with no session still must
        # (the tests, and any old client). An empty PIN is not a guess, so it does not count.
        if not c.authed:
            if pin is None or pin == "":
                return False, "Log in first."
            err = self._check_pin(c, pin, self.cfg.pin)
            if err:
                return False, err
            c.authed = True
        if self.operator == cid:
            if c.session:
                self.engineers.add(cid)
            return True, "You already have control."
        if self.operator is not None and not force:
            return False, f"{self.clients[self.operator].name} is in control."
        self._set_operator(cid)
        return True, "You are in control."

    def release(self, cid: str) -> bool:
        if self.operator != cid:
            return False
        self._set_operator(None)
        return True

    def _set_operator(self, cid: str | None) -> None:
        self.operator = cid
        self.engineers.clear()  # a new operator starts locked; a cookie session is unlocked again below
        self.tokens.clear()
        self._halt()
        # One PIN, owner's choice: a browser that signed in (a session cookie) is the engineer as soon
        # as it has control. A PIN typed only on this socket, with no cookie, is not: the old tests
        # still unlock the engineer tools with their own PIN.
        c = self.clients.get(cid) if cid else None
        if c and c.session:
            self.engineers.add(cid)

    # ---- engineer tools (servo tester, check wizard) -------------------
    def unlock_engineer(self, cid: str, pin: object) -> tuple[bool, str]:
        c = self.clients.get(cid)
        if c is None:
            return False, "Not connected."
        if cid != self.operator:
            return False, "Take control first."
        if not self.engineer_pin:
            return False, "Engineer tools are off. Set KIRO_ENGINEER_PIN on the robot."
        err = self._check_pin(c, pin, self.engineer_pin)
        if err:
            return False, err
        self.engineers.add(cid)
        return True, "Engineer tools unlocked."

    def lock_engineer(self, cid: str) -> None:
        self.engineers.discard(cid)
        for tok in [k for k, v in self.tokens.items() if v == cid]:
            del self.tokens[tok]

    def is_engineer(self, cid: str) -> bool:
        return cid == self.operator and cid in self.engineers

    def engineer_token(self, cid: str) -> str | None:
        """A random token for this engineer connection, for the Studio page's HTTP requests (camera
        previews), which cannot carry the WebSocket. Valid only while this connection is the unlocked
        engineer: it dies with lock, disconnect or a new operator. None for everyone else."""
        if not self.is_engineer(cid):
            return None
        tok = next((k for k, v in self.tokens.items() if v == cid), None)
        if tok is None:
            tok = secrets.token_urlsafe(18)
            self.tokens[tok] = cid
        return tok

    def token_ok(self, token: object) -> bool:
        cid = self.tokens.get(str(token or ""))
        return cid is not None and self.is_engineer(cid)

    def is_authed(self, cid: str) -> bool:
        c = self.clients.get(cid)
        return bool(c and c.authed)

    # ---- one login, remembered in a cookie ----------------------------
    def open_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = time.time() + SESSION_S
        return sid

    def session_ok(self, sid: object) -> bool:
        key = str(sid or "")
        exp = self.sessions.get(key)
        if not exp:
            return False
        if time.time() >= exp:
            self.sessions.pop(key, None)
            return False
        return True

    def drop_session(self, sid: object) -> None:
        self.sessions.pop(str(sid or ""), None)

    def attach_session(self, cid: str, sid: object) -> None:
        """A WebSocket that presented a live session cookie is signed in. Engineer tools follow control."""
        c = self.clients.get(cid)
        if c and self.session_ok(sid):
            c.authed = True
            c.session = str(sid)

    def session_is_engineer(self, sid: object) -> bool:
        """True when some live connection of this login is the current engineer (Studio downloads)."""
        key = str(sid or "")
        if not self.session_ok(key):
            return False
        return any(c.session == key and self.is_engineer(cid) for cid, c in self.clients.items())

    def grant_session_engineer(self, cid: str) -> bool:
        """The signed-in operator may open the engineer tools again without a second PIN."""
        c = self.clients.get(cid)
        if c and c.session and cid == self.operator:
            self.engineers.add(cid)
            return True
        return False

    # ---- second step for shutdown, reboot, torque release, ID change ----
    def begin_confirm(self, cid: str, action: str) -> tuple[bool, str]:
        c = self.clients.get(cid)
        if c is None:
            return False, "Not connected."
        if not c.authed:
            return False, "Log in first."
        if cid != self.operator:
            return False, "Take control first."
        token = secrets.token_urlsafe(18)
        self.confirms[(cid, action)] = (token, self.clock() + CONFIRM_TTL_S)
        return True, token

    def consume_confirm(self, cid: str, action: str, token: object) -> bool:
        """One use. A wrong or late token is thrown away with the real one."""
        got = self.confirms.pop((cid, action), None)
        if not got:
            return False
        tok, exp = got
        if self.clock() > exp:
            return False
        try:
            return hmac.compare_digest(str(token or "").encode(), tok.encode())
        except (TypeError, ValueError):
            return False

    def _halt(self) -> None:
        self._target = ZERO
        self._src = "none"
        self._last_drive = -math.inf
        self._need_neutral = True
        self._held = False

    # ---- E-STOP ------------------------------------------------------
    def set_estop(self, cid: str, on: bool) -> tuple[bool, str]:
        c = self.clients.get(cid)
        if c is None:
            return False, "Not connected."
        c.last_seen = self.clock()
        if on:  # anyone who can see the robot may stop it
            self.estop = True
            self.estop_by = c.name
            self._target = ZERO
            self._src = "none"
            return True, "E-STOP is on."
        if cid != self.operator:
            return False, "Only the operator can release E-STOP."
        self.estop = False
        self.estop_by = None
        self._need_neutral = True
        return True, "E-STOP released."

    # ---- driving -----------------------------------------------------
    def drive(self, cid: str, x: object, y: object, z: object, en: object = False,
              turbo: object = False, src: object = "none") -> bool:
        """x forward, y left, z counter-clockwise, each -1..1. en = dead-man held."""
        now = self.clock()
        c = self.clients.get(cid)
        if c is None:
            return False
        c.last_seen = now
        if cid != self.operator:
            return False
        self._last_drive = now  # this message is also the heartbeat
        self._held = bool(en)
        if not en:
            self._need_neutral = False
            self._target = ZERO
            self._src = "none"
            return True
        if self._need_neutral or self.estop:
            self._target = ZERO
            return True
        fx, fy, fz = _unit(x), _unit(y), _unit(z)
        n = math.hypot(fx, fy)
        if n > 1.0:
            fx, fy = fx / n, fy / n
        cfg = self.cfg
        lim = (cfg.turbo_vx, cfg.turbo_vy, cfg.turbo_wz) if turbo else (cfg.max_vx, cfg.max_vy, cfg.max_wz)
        self._target = (fx * lim[0], fy * lim[1], fz * lim[2])
        self._src = str(src)[:12]
        return True

    @property
    def held(self) -> bool:
        """The operator holds the dead-man right now (the stop meter notes when it is let go)."""
        return self.operator is not None and self._held

    @property
    def link_ok(self) -> bool:
        return self.operator is not None and (self.clock() - self._last_drive) <= self.cfg.heartbeat_timeout

    def tick(self) -> Command:
        now = self.clock()
        dt = 0.0 if self._last_tick is None else max(0.0, min(0.2, now - self._last_tick))
        self._last_tick = now

        op = self.clients.get(self.operator) if self.operator else None
        if self.operator and (op is None or now - op.last_seen > self.cfg.operator_stale_s):
            self._set_operator(None)

        fresh = self.operator is not None and (now - self._last_drive) <= self.cfg.heartbeat_timeout
        # Dead-man let go: a few zeros, then silence. The page keeps sending its heartbeat while nobody
        # drives; publishing those zeros would fight the voice node and the robot's gamepad on the same topic.
        if not fresh or self.estop or not self._held:
            if self._was_active:
                self._zero_left = ZERO_TICKS
            self._was_active = False
            self._out = ZERO
            if not fresh:
                self._target = ZERO
                self._src = "none"
            publish = self._zero_left > 0
            if publish:
                self._zero_left -= 1
            return Command(0.0, 0.0, 0.0, publish)

        self._was_active = True
        sl, sa = self.cfg.accel_lin * dt, self.cfg.accel_ang * dt
        o, t = self._out, self._target
        self._out = (_slew(o[0], t[0], sl), _slew(o[1], t[1], sl), _slew(o[2], t[2], sa))
        return Command(*self._out, True)

    # ---- state for the web page --------------------------------------
    def snapshot(self, cid: str) -> dict:
        op = self.clients.get(self.operator) if self.operator else None
        return {
            "you": {"id": cid, "role": "operator" if cid == self.operator else "viewer", "engineer": self.is_engineer(cid)},
            "operator": {"id": self.operator, "name": op.name} if op else None,
            "estop": self.estop,
            "estop_by": self.estop_by,
            "link_ok": self.link_ok,
            "need_neutral": self._need_neutral and cid == self.operator,
            "src": self._src,
            "cmd": {"vx": round(self._out[0], 3), "vy": round(self._out[1], 3), "wz": round(self._out[2], 3)},
            "clients": len(self.clients),
        }
