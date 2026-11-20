"""Obstacle stop of the web server. Pure logic with tests.

The lidar points decide how fast the robot may move in each direction. An obstacle close in front
blocks driving forward (and so on for back, left and right). Turning is never blocked, and driving
away from an obstacle is never blocked, so the operator can always get out.

This is a second layer in front of the old lidar guard in ~/so101. The old guard only runs while
the base program runs, can be switched off in ~/so101/config/lidar.env, and does not exist in mock
mode. This one works on every speed command the web page sends, in mock mode too.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

DIRS = ("front", "back", "left", "right")
FREE = {d: 1.0 for d in DIRS}
LOST_S = 0.5  # s without a lidar scan, after it was working, before everything stops (test E4': stop within 1 s)


def lidar_lost(on: bool, seen: float | None, now: float, lost_s: float = LOST_S) -> bool:
    """True = stop everything: the obstacle stop is on and the lidar worked (seen = arrival time of its
    last scan), but no scan came for lost_s. A robot that had no lidar since the base started drives
    without it (the old guard's pass mode); the operator can switch the obstacle stop off to drive on."""
    return on and seen is not None and now - seen > lost_s


@dataclass(frozen=True)
class GuardCfg:
    half_len: float = 0.225   # m, robot centre to its front or back edge
    half_wid: float = 0.175   # m, robot centre to its left or right edge
    stop: float = 0.20        # m of free space at which the robot stops
    slow: float = 0.45        # m of free space at which it starts to slow down
    margin: float = 0.03      # m added to each side of the path the robot sweeps


def limits(xy: list[int] | tuple[int, ...] | None, g: GuardCfg) -> dict[str, float]:
    """Speed factor 0..1 for each direction, from lidar points [x0, y0, x1, y1, ...] in cm
    (robot frame: x forward, y left). 1 = free, 0 = stop. No points = everything free."""
    gap = {d: math.inf for d in DIRS}
    if xy:
        for i in range(0, len(xy) - 1, 2):
            x, y = xy[i] / 100.0, xy[i + 1] / 100.0
            dx, dy = abs(x) - g.half_len, abs(y) - g.half_wid
            if dx <= 0 and dy <= 0:
                continue  # inside the robot's own outline: the robot itself, not an obstacle
            fb = "front" if x > 0 else "back"
            lr = "left" if y > 0 else "right"
            if dx > 0 and dy <= g.margin:        # in the path of driving forward or back
                gap[fb] = min(gap[fb], dx)
            if dy > 0 and dx <= g.margin:        # in the path of driving sideways
                gap[lr] = min(gap[lr], dy)
            if dx > g.margin and dy > g.margin:  # off a corner: counts for both, by straight distance
                d = math.hypot(dx, dy)
                gap[fb] = min(gap[fb], d)
                gap[lr] = min(gap[lr], d)
    span = max(g.slow - g.stop, 1e-6)
    return {d: 0.0 if gap[d] <= g.stop else min(1.0, (gap[d] - g.stop) / span) for d in DIRS}


def apply(vx: float, vy: float, lim: dict[str, float]) -> tuple[float, float]:
    """Scale a speed command (vx forward, vy left) by the limits of the directions it moves in."""
    fx = lim["front"] if vx > 0 else lim["back"] if vx < 0 else 1.0
    fy = lim["left"] if vy > 0 else lim["right"] if vy < 0 else 1.0
    return vx * fx, vy * fy


def blocked(vx: float, vy: float, lim: dict[str, float]) -> list[str]:
    """The directions this command wants to move in that are fully stopped by an obstacle."""
    out = []
    if vx > 0 and lim["front"] <= 0:
        out.append("front")
    if vx < 0 and lim["back"] <= 0:
        out.append("back")
    if vy > 0 and lim["left"] <= 0:
        out.append("left")
    if vy < 0 and lim["right"] <= 0:
        out.append("right")
    return out
