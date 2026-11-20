"""Stop meter for the test reports (E-STOP and voice tests). Pure logic with tests.

Every stop has a trigger (E-STOP, dead-man let go, the voice "정지", the lidar guard covering, the
base stopped, the leader arm's link lost...). From that moment the meter measures, from the robot's
own data:

  cmd_s    trigger -> the motion command is zero (the "response time" of the test forms)
  still_s  trigger -> the robot (or arm) really stands still
  dist     how far it went from the trigger to standing still (cm for the base, degrees for an arm)
  creep    how far it moved in the 5 s after it stopped, with no command ("unintended motion": must be 0)

A trigger only counts while something moves, so pressing E-STOP on a robot standing still adds nothing.
"""
from __future__ import annotations

import collections
import math
import time
from dataclasses import dataclass, field

# Test item and pass limits per trigger (test forms: 비상정지 및 오류 복귀, 음성 기반 작업 수행)
LIMITS = {
    "gamepad": ("E1", {"cmd_s": 0.5, "dist": 10.0}),
    "deadman": ("E1", {"cmd_s": 0.5, "dist": 10.0}),
    "voice": ("E2", {"cmd_s": 1.0, "dist": 10.0}),
    "base_stop": ("E3", {"still_s": 2.0}),
    "covered": ("E4", {"cmd_s": 0.5}),
    "no_scan": ("E4'", {"cmd_s": 1.0}),
    "lidar_lost": ("E4'", {"cmd_s": 1.0}),
    "arm_link": ("E5", {"still_s": 1.0}),
    "teleop_stop": ("E6", {"still_s": 2.0}),
}


@dataclass
class Stop:
    kind: str
    detail: str
    wall: float
    t0: float
    p0: tuple
    speed: float
    cmd_s: float | None = None
    still_s: float | None = None
    dist: float | None = None
    creep: float = 0.0
    state: str = "measuring"           # measuring -> watch (5 s for unintended motion) -> done | timeout
    p_still: tuple | None = None
    watch_end: float = 0.0
    below: tuple | None = None         # (time, position) when the speed first fell under the still limit

    def row(self, unit_scale: float) -> dict:
        test, lim = LIMITS.get(self.kind, ("", {}))
        r = {"kind": self.kind, "detail": self.detail, "ts": self.wall, "test": test, "state": self.state,
             "speed": round(self.speed * unit_scale, 1),
             "cmd_s": None if self.cmd_s is None else round(self.cmd_s, 2),
             "still_s": None if self.still_s is None else round(self.still_s, 2),
             "dist": None if self.dist is None else round(self.dist * unit_scale, 1),
             "creep": round(self.creep * unit_scale, 1)}
        ok = None
        if self.state in ("watch", "done", "timeout") and lim:
            ok = self.state != "timeout" and all(r.get(k) is not None and r[k] <= v for k, v in lim.items())
        r["ok"] = ok
        return r


@dataclass
class StopMeter:
    """unit_scale turns positions into the reported unit (m -> cm = 100; degrees stay 1).
    norm 'xy' = straight-line distance (base), 'max' = the largest single joint change (arm)."""
    name: str
    unit_scale: float = 100.0
    still_speed: float = 0.01          # below this (units/s, before scaling) the robot counts as still
    move_speed: float = 0.03           # above this it counts as moving (a trigger then starts a measurement)
    norm: str = "xy"
    watch_s: float = 5.0
    max_s: float = 8.0
    still_hold: float = 0.3            # it must stay still this long
    wall: callable = time.time
    stops: collections.deque = field(default_factory=lambda: collections.deque(maxlen=40))
    _hist: collections.deque = field(default_factory=lambda: collections.deque(maxlen=60))
    _pos: tuple | None = None
    _commanding: bool = False
    speed: float = 0.0

    def _d(self, a: tuple, b: tuple) -> float:
        if self.norm == "max":
            return max((abs(x - y) for x, y in zip(a, b)), default=0.0)
        return math.hypot(*(x - y for x, y in zip(a, b)))

    def sample(self, now: float, pos: tuple | None, commanding: bool) -> None:
        """Called about 20 times a second: the current position (None = unknown) and whether a
        non-zero motion command is being sent to the wheels / arm right now."""
        self._commanding = commanding
        if pos is None:
            return
        pos = tuple(float(v) for v in pos)
        self._pos = pos
        self._hist.append((now, pos))
        old = next(((t, p) for t, p in reversed(self._hist) if now - t >= 0.2), None)
        if old is None or len(old[1]) != len(pos):
            self.speed = 0.0
        else:
            self.speed = self._d(pos, old[1]) / max(1e-6, now - old[0])
        for m in self.stops:
            if m.state in ("done", "timeout"):
                continue
            if m.cmd_s is None and not commanding:
                m.cmd_s = now - m.t0
            if m.state == "measuring":
                if self.speed < self.still_speed:
                    if m.below is None:
                        m.below = (now, pos)
                    elif now - m.below[0] >= self.still_hold:
                        m.p_still = m.below[1]
                        # the averaged speed lags: the robot stood still from just after its last real move
                        eps = self.still_speed * 0.1
                        t_still = m.below[0]
                        for t, p in reversed(self._hist):
                            if t <= m.t0 or self._d(p, m.p_still) > eps:
                                break
                            t_still = t
                        m.still_s = max(0.0, t_still - m.t0)
                        m.dist = self._d(m.p_still, m.p0)
                        m.state, m.watch_end = "watch", now + self.watch_s
                else:
                    m.below = None
                if m.state == "measuring" and now - m.t0 > self.max_s:
                    m.state = "timeout"
            elif m.state == "watch":
                if commanding:          # someone drives again: the stop is over
                    m.state = "done"
                else:
                    m.creep = max(m.creep, self._d(pos, m.p_still))
                    if now >= m.watch_end:
                        m.state = "done"

    def trigger(self, now: float, kind: str, detail: str = "", at: float | None = None) -> bool:
        """A stop was ordered (at = when, if that was a moment ago: the last packet of a lost link).
        Starts a measurement if something moves and none started in the last second (one E-STOP press
        also stops the base and the voice: that is one stop, not three)."""
        if self._pos is None or not (self._commanding or self.speed > self.move_speed):
            return False
        if any(m.state == "measuring" and now - m.t0 < 1.0 for m in self.stops):
            return False
        t0 = now if at is None else min(at, now)
        p0 = next((p for t, p in reversed(self._hist) if t <= t0), self._pos)
        self.stops.append(Stop(kind=kind, detail=detail[:80], wall=self.wall() - (now - t0), t0=t0, p0=p0, speed=self.speed))
        return True

    def add_done(self, kind: str, detail: str, still_s: float) -> None:
        """A stop measured elsewhere: how long a program took to end after it was told to (E3, E6)."""
        self.stops.append(Stop(kind=kind, detail=detail[:80], wall=self.wall(), t0=0.0, p0=(), speed=0.0,
                               still_s=still_s, state="done"))

    def rows(self) -> list[dict]:
        return [m.row(self.unit_scale) for m in self.stops]

    def clear(self) -> None:
        self.stops.clear()


CSV_HEAD = ("time", "meter", "test", "trigger", "detail", "speed_before", "to_command_zero_s", "to_still_s",
            "stop_distance", "creep_in_5s", "state", "pass")


def csv_lines(meters: list[StopMeter]) -> list[str]:
    """All measurements as CSV (distance in cm for the base, degrees for an arm)."""
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_HEAD)
    rows = sorted(((m.name, r) for m in meters for r in m.rows()), key=lambda x: x[1]["ts"])
    for name, r in rows:
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), name, r["test"], r["kind"], r["detail"],
                    r["speed"], r["cmd_s"], r["still_s"], r["dist"], r["creep"], r["state"],
                    "" if r["ok"] is None else ("yes" if r["ok"] else "no")])
    return out.getvalue().splitlines()
