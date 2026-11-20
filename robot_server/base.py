"""The robot base. Two versions with the same methods:

MockBase - a pretend robot, for trying the page on any computer.
RosBase  - the real robot. Publishes speed commands to /cmd_vel_raw (through the lidar guard) and reads /odom.
"""
from __future__ import annotations

import collections
import math
import threading
import time

from .config import Config


def rate_hz(stamps, now: float, window: float = 2.0) -> float:
    """Messages per second over the last `window` seconds, from their arrival times."""
    return round(sum(1 for t in stamps if now - t < window) / window, 1)


def scan_points(ranges, angle_min: float, angle_inc: float, rmin: float, rmax: float,
                yaw: float, lx: float, n_out: int) -> list[int]:
    """Laser ranges -> flat [x0, y0, x1, y1, ...] in cm in the robot frame (x forward, y left).
    Keeps the nearest point of each angle bin, so nothing close is ever thinned away."""
    n = len(ranges)
    if n == 0:
        return []
    step = max(1, n // max(1, n_out))
    out: list[int] = []
    for start in range(0, n, step):
        best = None
        for i in range(start, min(n, start + step)):
            r = ranges[i]
            if math.isfinite(r) and rmin < r < rmax and (best is None or r < best[0]):
                best = (r, i)
        if best:
            r, i = best
            a = angle_min + i * angle_inc + yaw
            out += [round((lx + r * math.cos(a)) * 100), round(r * math.sin(a) * 100)]
    return out


# A pretend room for mock mode, in world metres: walls and one box (x0, y0, x1, y1).
MOCK_ROOM = (-2.0, -1.8, 3.5, 2.2)
MOCK_BOXES = ((1.4, 0.6, 1.9, 1.1), (-1.2, -1.6, -0.6, -1.0))


def _ray_box(ox, oy, dx, dy, box):
    """Distance along the ray to an axis-aligned box, or None (slab method)."""
    tmin, tmax = 0.0, math.inf
    for o, d, lo, hi in ((ox, dx, box[0], box[2]), (oy, dy, box[1], box[3])):
        if abs(d) < 1e-9:
            if o < lo or o > hi:
                return None
            continue
        t1, t2 = (lo - o) / d, (hi - o) / d
        tmin, tmax = max(tmin, min(t1, t2)), min(tmax, max(t1, t2))
    return tmin if tmin <= tmax and tmin > 0 else None


class MockBase:
    name = "mock"

    def __init__(self) -> None:
        self.x = self.y = self.th = 0.0
        self.v = (0.0, 0.0, 0.0)

    def scan(self, n_out: int = 360) -> dict:
        """Ray-cast the pretend room from the robot. Points in cm, robot frame."""
        x0, y0, x1, y1 = MOCK_ROOM
        pts: list[int] = []
        for k in range(n_out):
            a = -math.pi + 2 * math.pi * k / n_out
            dx, dy = math.cos(self.th + a), math.sin(self.th + a)
            ts = [t for t in ((x1 - self.x) / dx if dx > 1e-9 else None, (x0 - self.x) / dx if dx < -1e-9 else None,
                              (y1 - self.y) / dy if dy > 1e-9 else None, (y0 - self.y) / dy if dy < -1e-9 else None) if t]
            r = min([t for t in ts if t > 0] + [8.0])
            for b in MOCK_BOXES:
                t = _ray_box(self.x, self.y, dx, dy, b)
                if t is not None:
                    r = min(r, t)
            pts += [round(r * math.cos(a) * 100), round(r * math.sin(a) * 100)]
        return {"xy": pts, "src": "mock"}

    def apply(self, vx: float, vy: float, wz: float, publish: bool, dt: float) -> None:
        if not publish:
            vx = vy = wz = 0.0
        self.v = (vx, vy, wz)
        self.x += (vx * math.cos(self.th) - vy * math.sin(self.th)) * dt
        self.y += (vx * math.sin(self.th) + vy * math.cos(self.th)) * dt
        self.th = math.atan2(math.sin(self.th + wz * dt), math.cos(self.th + wz * dt))

    def set_estop(self, on: bool) -> None:
        pass

    def publish_str(self, topic: str, text: str) -> None:
        pass

    def latest_str(self, topic: str) -> str | None:
        return None

    def rates(self) -> dict | None:
        return None  # pretend robot: no topics to measure

    def wheel_cmd(self) -> bool | None:
        return None  # the server's own command is the only one: the caller knows it

    def take_events(self) -> list[tuple[str, str, float]]:
        return []

    def odom(self) -> dict:
        return {"x": round(self.x, 3), "y": round(self.y, 3), "th": round(self.th, 3),
                "vx": round(self.v[0], 3), "vy": round(self.v[1], 3), "wz": round(self.v[2], 3)}

    def close(self) -> None:
        pass


class RosBase:
    """Needs ROS 2 (rclpy). NOT yet tested on the real robot: see deploy/README.md."""

    name = "ros"

    def __init__(self, cfg: Config) -> None:
        import rclpy
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from std_msgs.msg import Bool, String

        from rclpy.executors import SingleThreadedExecutor
        from rclpy.signals import SignalHandlerOptions

        self._rclpy, self._Twist, self._Bool, self._String = rclpy, Twist, Bool, String
        if not rclpy.ok():
            # The server handles Ctrl+C itself, so it can stop the programs it started before ROS goes away
            rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
        self._node = rclpy.create_node("kiro_webapp")
        self._cmd_pub = self._node.create_publisher(Twist, cfg.cmd_topic, 10)
        self._estop_pub = self._node.create_publisher(Bool, cfg.estop_topic, 10)
        self._lock = threading.Lock()
        self._odom: dict | None = None
        # Arrival times, for the message rates the page shows (test R2: /odom 20 Hz after a restart)
        self._odom_ts: collections.deque[float] = collections.deque(maxlen=200)
        self._scan_ts: collections.deque[float] = collections.deque(maxlen=100)
        # For the stop meter: what really goes to the wheels (/cmd_vel, after the old guard), and the
        # robot gamepad's dead-man (LB) being let go
        from sensor_msgs.msg import Joy
        self._wheel_nonzero, self._wheel_t = False, 0.0
        self._lb = False
        self._events: collections.deque[tuple[str, str, float]] = collections.deque(maxlen=20)
        self._node.create_subscription(Twist, "/cmd_vel", self._on_wheel_cmd, 20)
        self._node.create_subscription(Joy, "/joy", self._on_joy, 20)
        self._node.create_subscription(Odometry, cfg.odom_topic, self._on_odom, 10)
        # Lidar: points around the robot for the page map (same mounting as the old guard)
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import LaserScan
        self._yaw, self._lx, self._n_scan = cfg.lidar_yaw(), cfg.laser_x, cfg.scan_points
        self._scan: dict | None = None
        self._scan_t = 0.0
        self._node.create_subscription(LaserScan, "/scan", self._on_scan, qos_profile_sensor_data)
        # Text topics of the old voice node and lidar guard, shown on the page
        self._str_pubs = {t: self._node.create_publisher(String, t, 10) for t in ("/voice/listen", "/voice/say")}
        self._str_latest: dict[str, str] = {}
        for topic in ("/voice/text", "/voice/status", "/obstacle_guard/status"):
            self._node.create_subscription(String, topic, lambda m, t=topic: self._on_str(t, m), 10)
        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        try:
            self._executor.spin()
        except Exception:  # noqa: BLE001 - ROS was shut down (server stopping)
            pass

    def _ok(self) -> bool:
        return self._rclpy.ok()

    def _on_wheel_cmd(self, msg) -> None:
        self._wheel_nonzero = any(abs(v) > 1e-4 for v in (msg.linear.x, msg.linear.y, msg.angular.z))
        self._wheel_t = time.monotonic()

    def _on_joy(self, msg) -> None:
        lb = len(msg.buttons) > 4 and msg.buttons[4] == 1
        if self._lb and not lb:
            self._events.append(("gamepad", "robot gamepad LB let go", time.monotonic()))
        self._lb = lb

    def wheel_cmd(self) -> bool:
        """A non-zero speed is going to the wheels now (the wheel node stops by itself 0.5 s after the last one)."""
        return self._wheel_nonzero and time.monotonic() - self._wheel_t < 0.5

    def take_events(self) -> list[tuple[str, str, float]]:
        out = []
        while self._events:
            out.append(self._events.popleft())
        return out

    def _on_odom(self, msg) -> None:
        self._odom_ts.append(time.monotonic())
        q = msg.pose.pose.orientation
        th = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        t = msg.twist.twist
        with self._lock:
            self._odom = {"x": round(msg.pose.pose.position.x, 3), "y": round(msg.pose.pose.position.y, 3),
                          "th": round(th, 3), "vx": round(t.linear.x, 3), "vy": round(t.linear.y, 3),
                          "wz": round(t.angular.z, 3)}

    def _on_scan(self, msg) -> None:
        now = time.monotonic()
        self._scan_ts.append(now)
        if now - self._scan_t < 0.15:  # the page needs about 5 per second
            return
        pts = scan_points(msg.ranges, msg.angle_min, msg.angle_increment, msg.range_min, msg.range_max,
                          self._yaw, self._lx, self._n_scan)
        with self._lock:
            self._scan, self._scan_t = {"xy": pts, "src": "lidar", "at": now}, now

    def scan(self, n_out: int = 360) -> dict | None:
        with self._lock:
            if self._scan is None or time.monotonic() - self._scan_t > 1.0:
                return None  # no lidar, or it stopped: show nothing rather than an old picture
            return self._scan

    def _on_str(self, topic: str, msg) -> None:
        with self._lock:
            self._str_latest[topic] = str(msg.data)[:300]

    def publish_str(self, topic: str, text: str) -> None:
        if not self._ok():
            return
        pub = self._str_pubs.get(topic)
        if pub is None:
            pub = self._str_pubs[topic] = self._node.create_publisher(self._String, topic, 10)
        pub.publish(self._String(data=text))

    def latest_str(self, topic: str) -> str | None:
        with self._lock:
            return self._str_latest.get(topic)

    def apply(self, vx: float, vy: float, wz: float, publish: bool, dt: float) -> None:
        if not publish:
            return  # stay silent so the robot's own gamepad and voice can drive
        if not self._ok():
            return
        m = self._Twist()
        m.linear.x, m.linear.y, m.angular.z = float(vx), float(vy), float(wz)
        self._cmd_pub.publish(m)

    def set_estop(self, on: bool) -> None:
        if self._ok():
            self._estop_pub.publish(self._Bool(data=bool(on)))

    def odom(self) -> dict | None:
        with self._lock:
            if self._odom and self._odom_ts and time.monotonic() - self._odom_ts[-1] > 1.0:
                return None  # the wheel node stopped: show nothing rather than an old position
            return dict(self._odom) if self._odom else None

    def rates(self) -> dict:
        now = time.monotonic()
        odom, scan = list(self._odom_ts), list(self._scan_ts)
        return {"odom_hz": rate_hz(odom, now), "scan_hz": rate_hz(scan, now),
                "odom_age": round(now - odom[-1], 1) if odom else None}

    def close(self) -> None:
        try:
            if self._ok():
                self._cmd_pub.publish(self._Twist())  # last word: stop
            self._executor.shutdown(timeout_sec=1.0)
            self._node.destroy_node()
            if self._ok():
                self._rclpy.shutdown()
        except Exception:  # noqa: BLE001
            pass


def make_base(cfg: Config):
    if cfg.backend == "mock":
        return MockBase()
    if cfg.backend == "ros":
        return RosBase(cfg)
    try:  # auto
        import rclpy  # noqa: F401
    except ImportError:
        return MockBase()
    return RosBase(cfg)
