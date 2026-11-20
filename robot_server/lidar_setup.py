"""Lidar setup: which way is the robot's front, and which close points are the robot itself. Pure logic with tests.

The lidar can sit at any angle, and parts of the robot (arms, cart, cables, a tray rim) can sit in its view.
Both used to make the obstacle stop refuse to drive with nothing in front. Two snapshots fix that:

1. "Front clear": nothing within 1 m around the robot, people step away. Every point closer than NEAR now is
   the robot itself. Kept as a SELF MASK: per 5-degree direction (robot frame) the distance of the robot's own
   parts. A later point in that direction is the robot if it is no further than mask + MARGIN; anything further
   out in the same direction is still an obstacle.
2. "Box in front": a box 30-60 cm in front of the robot's middle. The points that came closer are the box and
   their direction is the robot's front, so LASER_YAW = -(that angle). Same method as ~/so101/lidar_yaw.py.

The result goes to ~/so101/config/lidar.env (LASER_YAW, LIDAR_MASK, OBSTACLE_STOP=1), read by the old base
launcher (run_base_teleop.sh -> obstacle_guard self_mask) and by this server (base.py, the page map, guard.py).
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

BIN_DEG = 5                 # mask resolution, degrees
NBINS = 360 // BIN_DEG
NEAR = 0.45                 # m from the lidar: closer points in the clear scan are the robot itself
MARGIN = 0.03               # m added to the mask distance
FRONT_HALF = 40.0           # degrees: a mask here hides part of the driving direction (warning)
MIN_CHANGE = 0.10           # m a point must come closer to count as the box
MAX_BOX = 0.80              # m: the box must be closer than this
NEAR_FLICKER = 0.15         # m: points this close flicker on and off, never the box
CLEAR_SCANS = 20
BOX_SCANS = 10


@dataclass
class Scan:
    """One laser scan in the lidar's own frame. ranges: metres, None or inf = no reading."""
    angle_min: float
    inc: float
    ranges: list[float | None]
    rmin: float = 0.05
    rmax: float = 12.0

    def valid(self, i: int) -> float | None:
        r = self.ranges[i]
        return r if r is not None and math.isfinite(r) and self.rmin < r < self.rmax else None


def wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def bin_of(a_deg: float) -> int:
    """5-degree bin of a robot-frame angle (0 = front, + = left)."""
    return int((wrap_deg(a_deg) + 180.0) // BIN_DEG) % NBINS


def bin_start(b: int) -> int:
    return -180 + b * BIN_DEG


def median_ranges(scans: list[Scan]) -> list[float | None]:
    """Median distance per index over the scans. None where fewer than half had a reading."""
    n = len(scans[0].ranges)
    out: list[float | None] = []
    for i in range(n):
        v = [r for s in scans if i < len(s.ranges) and (r := s.valid(i)) is not None]
        out.append(statistics.median(v) if len(v) * 2 > len(scans) else None)
    return out


def near_lidar(scans: list[Scan], near: float = NEAR) -> list[float]:
    """Per index, the furthest reading closer than `near` seen in ANY scan (0 = none). Points this close
    flicker on and off, so one sighting is enough to call that direction 'robot'."""
    n = len(scans[0].ranges)
    out = [0.0] * n
    for s in scans:
        for i in range(min(n, len(s.ranges))):
            r = s.valid(i)
            if r is not None and r < near and r > out[i]:
                out[i] = r
    return out


class SetupError(Exception):
    """A step failed; the message tells the person what to do. key = i18n key on the page."""

    def __init__(self, key: str, msg: str, **kw):
        super().__init__(msg)
        self.key, self.kw = key, kw


def find_front(clear: list[Scan], box: list[Scan], max_box: float = MAX_BOX) -> tuple[float, dict]:
    """Angle of the box in the lidar frame (rad), from scans without and with the box. Raises SetupError."""
    base, now = median_ranges(clear), median_ranges(box)
    self_near = near_lidar(clear)
    s0 = box[0]
    n = min(len(base), len(now))
    flag = [False] * n
    for i in range(n):
        b, c = base[i], now[i]
        if c is None or c > max_box or c < NEAR_FLICKER:
            continue
        if self_near[i] and c <= self_near[i] + MARGIN:
            continue  # the robot's own flickering part, not the box
        flag[i] = (b is None) or (b - c >= MIN_CHANGE)
    clusters, cur = [], []
    for k in range(2 * n):  # twice round, so a cluster across the 0/360 seam stays one
        if flag[k % n]:
            cur.append(k)
        elif cur:
            clusters.append(cur)
            cur = []
    if cur:
        clusters.append(cur)
    clusters = [c for c in clusters if len(c) <= n and c[0] < n]
    if not clusters:
        raise SetupError("ls.err.nobox", "Nothing came closer. Put the box 30-60 cm in front of the robot's middle.")
    best = max(clusters, key=len)
    others = sorted((len(c) for c in clusters if c is not best), reverse=True)
    if len(best) < 5:
        raise SetupError("ls.err.small", f"The box is too small to see ({len(best)} points). Use a bigger box, 30-60 cm away.",
                         n=len(best))
    if others and others[0] * 2 > len(best):
        raise SetupError("ls.err.two", f"Two things came closer ({len(best)} and {others[0]} points). "
                                       "Someone walked past or something moved. Try again.", a=len(best), b=others[0])
    hits = [(s0.angle_min + (k % n) * s0.inc, now[k % n]) for k in best]
    sx, sy = sum(math.cos(t) for t, _ in hits), sum(math.sin(t) for t, _ in hits)
    front = math.atan2(sy, sx)
    dist = statistics.median(d for _, d in hits)
    return front, {"points": len(hits), "dist": round(dist, 2), "front_deg": round(math.degrees(front), 1)}


def self_mask(clear: list[Scan], yaw: float, near: float = NEAR) -> dict[int, float]:
    """Robot-frame mask {bin: metres} from the clear scans, widened by one bin each side (edges flicker)."""
    s0 = clear[0]
    raw: dict[int, float] = {}
    for i, r in enumerate(near_lidar(clear, near)):
        if r > 0:
            b = bin_of(math.degrees(s0.angle_min + i * s0.inc + yaw))
            raw[b] = max(raw.get(b, 0.0), r)
    out: dict[int, float] = {}
    for b, r in raw.items():
        for k in (b - 1, b, b + 1):
            k %= NBINS
            out[k] = max(out.get(k, 0.0), r)
    return {b: round(out[b], 2) for b in sorted(out)}


def mask_str(mask: dict[int, float]) -> str:
    """'-180:0.32,-175:0.30' (bin start angle in degrees : metres), the form lidar.env and the old guard use."""
    return ",".join(f"{bin_start(b)}:{r:.2f}" for b, r in sorted(mask.items()))


def parse_mask(text: str | None) -> dict[int, float]:
    out: dict[int, float] = {}
    for part in (text or "").split(","):
        a, _, r = part.partition(":")
        try:
            out[bin_of(float(a) + 0.5)] = float(r)
        except ValueError:
            continue
    return out


def is_self(a_deg: float, r: float, mask: dict[int, float]) -> bool:
    """This robot-frame point is the robot itself (within its mask distance)."""
    m = mask.get(bin_of(a_deg))
    return m is not None and r <= m + MARGIN


def mask_warnings(mask: dict[int, float]) -> list[dict]:
    """Problems a person should know about before switching the stop on."""
    out = []
    front = [b for b in mask if abs(wrap_deg(bin_start(b) + BIN_DEG / 2)) <= FRONT_HALF]
    if front:
        far = max(mask[b] for b in front)
        out.append({"key": "ls.warn.front", "deg": [bin_start(min(front)), bin_start(max(front)) + BIN_DEG],
                    "cm": round(far * 100)})
    if len(mask) > NBINS // 2:
        out.append({"key": "ls.warn.boxed", "pct": round(100 * len(mask) / NBINS)})
    return out


def env_update(text: str, values: dict[str, str], note: str) -> str:
    """lidar.env with these keys replaced (or added), every other line kept."""
    lines = [ln for ln in text.splitlines() if ln.partition("=")[0].strip() not in values]
    return "\n".join(lines + [f"{k}={v}   # {note}" for k, v in values.items()]) + "\n"


def env_get(text: str, key: str) -> str | None:
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.split("#")[0].strip()
    return None


# ------------------------------------------------------------------ the wizard
@dataclass
class Result:
    yaw: float
    mask: dict[int, float]
    box: dict
    warnings: list[dict] = field(default_factory=list)

    def view(self) -> dict:
        return {"yaw": round(self.yaw, 2), "yaw_deg": round(math.degrees(self.yaw)), "box": self.box,
                "mask": [[bin_start(b), r] for b, r in sorted(self.mask.items())], "warnings": self.warnings}


class LidarSetup:
    """Steps: idle -> clear (waiting for 'front is clear') -> box (waiting for 'box is in place') -> result -> idle.
    grab(step, n, timeout) returns n new raw scans or None. save(result) writes lidar.env, returns (ok, message)."""

    def __init__(self, grab: Callable[[str, int, float], list[Scan] | None], save: Callable[[Result], tuple[bool, str]]):
        self._grab, self._save = grab, save
        self._lock = threading.Lock()
        self.step = "idle"
        self.busy = False
        self.error: dict | None = None
        self.result: Result | None = None
        self.saved: dict | None = None
        self._clear: list[Scan] | None = None
        self.started = 0.0

    def view(self) -> dict:
        return {"step": self.step, "busy": self.busy, "error": self.error, "saved": self.saved,
                "result": self.result.view() if self.result else None}

    def _fail(self, key: str, msg: str, **kw) -> tuple[bool, str]:
        self.error = {"key": key, "msg": msg, **kw}
        return False, msg

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.busy:
                return False, "Measuring, wait a moment."
            self.step, self.error, self.result, self.saved, self._clear = "clear", None, None, None, None
            self.started = time.time()
        return True, ""

    def cancel(self) -> tuple[bool, str]:
        with self._lock:
            self.step, self.busy, self.error, self.result, self._clear = "idle", False, None, None, None
        return True, ""

    def measure(self, which: str) -> tuple[bool, str]:
        """which = 'clear' or 'box': take the scans of that step (about 1-2 s)."""
        with self._lock:
            if self.busy:
                return False, "Measuring, wait a moment."
            if self.step != which:
                return False, "Start the lidar setup again."
            self.busy, self.error = True, None
        try:
            scans = self._grab(which, CLEAR_SCANS if which == "clear" else BOX_SCANS, 6.0)
            if not scans:
                return self._fail("ls.err.noscan", "No lidar scans. Is the lidar plugged in and its program running?")
            if which == "clear":
                self._clear = scans
                self.step = "box"
                return True, ""
            assert self._clear is not None
            try:
                front, box = find_front(self._clear, scans)
            except SetupError as e:
                return self._fail(e.key, str(e), **e.kw)
            yaw = math.atan2(math.sin(-front), math.cos(-front))
            mask = self_mask(self._clear, yaw)
            self.result = Result(yaw, mask, box, mask_warnings(mask))
            self.step = "result"
            return True, ""
        finally:
            self.busy = False

    def apply(self) -> tuple[bool, str]:
        with self._lock:
            if self.step != "result" or not self.result:
                return False, "Measure first."
            res = self.result
        ok, msg = self._save(res)
        if not ok:
            return self._fail("ls.err.save", msg)
        self.saved = {"t": time.time(), "yaw_deg": round(math.degrees(res.yaw)), "bins": len(res.mask)}
        self.step, self.result, self._clear = "idle", None, None
        return True, msg
