"""LeRobot 0.6.1 ignores a calibration's drive_mode (a joint whose motor counts the other way round) when it
works in degrees, which our arms do. The yellow leader's shoulder is such a joint (24 Nov 2026): it rests at
the top of its range while every other arm rests at the bottom, so the green arm was told "shoulder up" and
overloaded. apply() makes degrees honour drive_mode, for the leader (reading) and the follower (writing).
It changes nothing for joints with drive_mode 0, i.e. every other arm.

Used by teleop_runner.py and record_runner.py, inside LeRobot's own Python."""
from __future__ import annotations


def apply() -> None:
    from lerobot.motors.motors_bus import MotorNormMode, SerialMotorsBus

    if getattr(SerialMotorsBus, "_kiro_drive_mode", False):
        return
    orig_norm, orig_unnorm = SerialMotorsBus._normalize, SerialMotorsBus._unnormalize

    def reversed_deg(bus, id_) -> bool:
        motor = bus._id_to_name(id_)
        cal = (bus.calibration or {}).get(motor)
        return bool(cal and cal.drive_mode and bus.motors[motor].norm_mode is MotorNormMode.DEGREES)

    def _normalize(self, ids_values):
        out = orig_norm(self, ids_values)
        return {i: (-v if reversed_deg(self, i) else v) for i, v in out.items()}

    def _unnormalize(self, ids_values):
        return orig_unnorm(self, {i: (-v if reversed_deg(self, i) else v) for i, v in ids_values.items()})

    SerialMotorsBus._normalize, SerialMotorsBus._unnormalize = _normalize, _unnormalize
    SerialMotorsBus._kiro_drive_mode = True
