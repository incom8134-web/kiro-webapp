"""LeRobot 0.6.1 ignores a calibration's drive_mode (a joint whose motor counts the other way round) when it
works in degrees, which our arms do. The yellow leader's shoulder is such a joint (24 Nov 2026): it rests at
the top of its range while every other arm rests at the bottom, so the green arm was told "shoulder up" and
overloaded. apply() makes degrees honour drive_mode, for the leader (reading) and the follower (writing).
It changes nothing for joints with drive_mode 0, i.e. every other arm.

Used by teleop_runner.py and record_runner.py, inside LeRobot's own Python."""
from __future__ import annotations

import json
from pathlib import Path


TUNE = Path("~/.config/kiro-webapp/leader_tune.json").expanduser()


def load_tune(path: Path = TUNE) -> dict:
    """{port: {joint: (scale, offset)}} from {port: {joint: scale | {"scale": s, "offset": deg}}}: map a leader
    joint onto its follower when the two travel differently (24 Nov 2026: yellow leader's shoulder vs the green
    arm, measured in two poses, rest and straight up, by matching both arms)."""
    try:
        out = {}
        for port, joints in json.loads(path.read_text()).items():
            out[port] = {}
            for j, v in joints.items():
                out[port][j] = (float(v["scale"]), float(v.get("offset", 0.0))) if isinstance(v, dict) else (float(v), 0.0)
        return out
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return {}


def apply() -> None:
    from lerobot.motors.motors_bus import MotorNormMode, SerialMotorsBus

    tune = load_tune()

    if getattr(SerialMotorsBus, "_kiro_drive_mode", False):
        return
    orig_norm, orig_unnorm = SerialMotorsBus._normalize, SerialMotorsBus._unnormalize

    def reversed_deg(bus, id_) -> bool:
        motor = bus._id_to_name(id_)
        cal = (bus.calibration or {}).get(motor)
        return bool(cal and cal.drive_mode and bus.motors[motor].norm_mode is MotorNormMode.DEGREES)

    def tuned(bus, id_) -> tuple[float, float]:
        motor = bus._id_to_name(id_)
        if bus.motors[motor].norm_mode is not MotorNormMode.DEGREES:
            return 1.0, 0.0
        return tune.get(str(getattr(bus, "port", "")), {}).get(motor, (1.0, 0.0))

    def _normalize(self, ids_values):
        out = {}
        for i, v in orig_norm(self, ids_values).items():
            sc, off = tuned(self, i)
            out[i] = (-v if reversed_deg(self, i) else v) * sc + off
        return out

    def _unnormalize(self, ids_values):
        back = {}
        for i, v in ids_values.items():
            sc, off = tuned(self, i)
            v = (v - off) / sc
            back[i] = -v if reversed_deg(self, i) else v
        return orig_unnorm(self, back)

    SerialMotorsBus._normalize, SerialMotorsBus._unnormalize = _normalize, _unnormalize
    SerialMotorsBus._kiro_drive_mode = True
