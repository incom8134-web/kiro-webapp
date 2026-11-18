"""Settings. Every value can be changed with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass

MOCK_PIN = "0000"  # only used in mock mode when KIRO_OPERATOR_PIN is not set


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"
    port: int = 8080
    backend: str = "auto"  # auto | mock | ros
    pin: str = MOCK_PIN

    # Safety timing
    heartbeat_timeout: float = 0.5  # s without operator messages -> stop
    operator_stale_s: float = 10.0  # s without any message -> lock is freed
    rate_hz: float = 20.0  # command loop rate

    # Speed limits (m/s, m/s, rad/s). The wheel node itself caps at 0.3 / 0.3 / 1.5.
    max_vx: float = 0.15
    max_vy: float = 0.15
    max_wz: float = 0.8
    turbo_vx: float = 0.30
    turbo_vy: float = 0.30
    turbo_wz: float = 1.5

    # How fast the speed may rise (slowing down is always instant)
    accel_lin: float = 0.6  # m/s^2
    accel_ang: float = 3.0  # rad/s^2

    # ROS 2 topic names (used only by the ros backend). cmd_vel_raw goes through the
    # old lidar obstacle_guard to /cmd_vel, the same path as the robot gamepad and voice.
    cmd_topic: str = "cmd_vel_raw"
    estop_topic: str = "e_stop"
    odom_topic: str = "odom"

    # The old robot code (scripts this server starts) and the trained models
    so101_dir: str = "~/so101"
    lerobot_venv: str = "~/lerobot-venv"
    policies_dir: str = "~/policies"
    skill_seconds: float = 20.0
    health_period: float = 3.0
    # The left arm calibration was broken on 16 Nov 2026 (see ~/so101/CLAUDE.md).
    # Motion of the left arm stays blocked until it is recalibrated and this is set to 1.
    left_arm_ok: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        d = cls()
        return cls(
            host=os.environ.get("KIRO_HOST", d.host),
            port=int(os.environ.get("KIRO_PORT", d.port)),
            backend=os.environ.get("KIRO_BACKEND", d.backend).lower(),
            pin=os.environ.get("KIRO_OPERATOR_PIN", "") or MOCK_PIN,
            heartbeat_timeout=_f("KIRO_HEARTBEAT_TIMEOUT", d.heartbeat_timeout),
            operator_stale_s=_f("KIRO_OPERATOR_STALE_S", d.operator_stale_s),
            rate_hz=_f("KIRO_RATE_HZ", d.rate_hz),
            max_vx=_f("KIRO_MAX_VX", d.max_vx),
            max_vy=_f("KIRO_MAX_VY", d.max_vy),
            max_wz=_f("KIRO_MAX_WZ", d.max_wz),
            turbo_vx=_f("KIRO_TURBO_VX", d.turbo_vx),
            turbo_vy=_f("KIRO_TURBO_VY", d.turbo_vy),
            turbo_wz=_f("KIRO_TURBO_WZ", d.turbo_wz),
            accel_lin=_f("KIRO_ACCEL_LIN", d.accel_lin),
            accel_ang=_f("KIRO_ACCEL_ANG", d.accel_ang),
            cmd_topic=os.environ.get("KIRO_CMD_TOPIC", d.cmd_topic),
            estop_topic=os.environ.get("KIRO_ESTOP_TOPIC", d.estop_topic),
            odom_topic=os.environ.get("KIRO_ODOM_TOPIC", d.odom_topic),
            so101_dir=os.environ.get("KIRO_SO101_DIR", d.so101_dir),
            lerobot_venv=os.environ.get("KIRO_LEROBOT_VENV", d.lerobot_venv),
            policies_dir=os.environ.get("KIRO_POLICIES_DIR", d.policies_dir),
            skill_seconds=_f("KIRO_SKILL_SECONDS", d.skill_seconds),
            health_period=_f("KIRO_HEALTH_PERIOD", d.health_period),
            left_arm_ok=os.environ.get("KIRO_LEFT_ARM_OK", "0") == "1",
        )

    @property
    def pin_is_default(self) -> bool:
        return self.pin == MOCK_PIN
