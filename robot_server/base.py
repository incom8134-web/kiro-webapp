"""The robot base. Two versions with the same methods:

MockBase - a pretend robot, for trying the page on any computer.
RosBase  - the real robot. Publishes speed commands to /cmd_vel_raw (through the lidar guard) and reads /odom.
"""
from __future__ import annotations

import math
import threading

from .config import Config


class MockBase:
    name = "mock"

    def __init__(self) -> None:
        self.x = self.y = self.th = 0.0
        self.v = (0.0, 0.0, 0.0)

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

        self._rclpy, self._Twist, self._Bool, self._String = rclpy, Twist, Bool, String
        if not rclpy.ok():
            rclpy.init()
        self._node = rclpy.create_node("kiro_webapp")
        self._cmd_pub = self._node.create_publisher(Twist, cfg.cmd_topic, 10)
        self._estop_pub = self._node.create_publisher(Bool, cfg.estop_topic, 10)
        self._lock = threading.Lock()
        self._odom: dict | None = None
        self._node.create_subscription(Odometry, cfg.odom_topic, self._on_odom, 10)
        # Text topics of the old voice node and lidar guard, shown on the page
        self._str_pubs = {t: self._node.create_publisher(String, t, 10) for t in ("/voice/listen", "/voice/say")}
        self._str_latest: dict[str, str] = {}
        for topic in ("/voice/text", "/voice/status", "/obstacle_guard/status"):
            self._node.create_subscription(String, topic, lambda m, t=topic: self._on_str(t, m), 10)
        self._thread = threading.Thread(target=rclpy.spin, args=(self._node,), daemon=True)
        self._thread.start()

    def _on_odom(self, msg) -> None:
        q = msg.pose.pose.orientation
        th = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        t = msg.twist.twist
        with self._lock:
            self._odom = {"x": round(msg.pose.pose.position.x, 3), "y": round(msg.pose.pose.position.y, 3),
                          "th": round(th, 3), "vx": round(t.linear.x, 3), "vy": round(t.linear.y, 3),
                          "wz": round(t.angular.z, 3)}

    def _on_str(self, topic: str, msg) -> None:
        with self._lock:
            self._str_latest[topic] = str(msg.data)[:300]

    def publish_str(self, topic: str, text: str) -> None:
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
        m = self._Twist()
        m.linear.x, m.linear.y, m.angular.z = float(vx), float(vy), float(wz)
        self._cmd_pub.publish(m)

    def set_estop(self, on: bool) -> None:
        self._estop_pub.publish(self._Bool(data=bool(on)))

    def odom(self) -> dict | None:
        with self._lock:
            return dict(self._odom) if self._odom else None

    def close(self) -> None:
        try:
            self._cmd_pub.publish(self._Twist())
            self._node.destroy_node()
            if self._rclpy.ok():
                self._rclpy.shutdown()
        except Exception:
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
