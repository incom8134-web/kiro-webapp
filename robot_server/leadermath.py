"""Leader arm maths shared by the server (leader arm on the laptop) and leader_relay.py (leader arm
on the Jetson). Kept free of other imports so the relay starts fast."""
from __future__ import annotations

JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]  # servo IDs 1..6


def leader_to_target(cal: dict, raw: list) -> dict:
    """Leader servo positions (raw ticks, IDs 1..6) -> twin_bridge target, the same maths as LeRobot
    (motors_bus._normalize): joints in degrees, 0 = middle of the calibrated range; gripper 0..100 %."""
    out = {}
    for i, j in enumerate(JOINTS):
        c, v = cal[j], float(raw[i])
        lo, hi = c["range_min"], c["range_max"]
        if j == "gripper":
            n = (min(hi, max(lo, v)) - lo) / (hi - lo) * 100.0
            out[j] = round(100.0 - n if c.get("drive_mode") else n, 2)
        else:
            out[j] = round((v - (lo + hi) / 2.0) * 360.0 / 4095.0, 2)
    return out


def apply_offsets(target: dict, offsets: dict) -> dict:
    """Shift some joints of one follower arm, in degrees, kept in -180..180 (no jump at the seam).
    Arms calibrated in different ways read the same pose with different numbers; the offset maps
    the leader's number to this arm's number for the same pose. The gripper is never shifted."""
    out = dict(target)
    for j, d in offsets.items():
        if j in out and j != "gripper":
            out[j] = round((out[j] + float(d) + 180.0) % 360.0 - 180.0, 2)
    return out


def _half(cal: dict) -> float:
    return (cal["range_max"] - cal["range_min"]) / 2.0


def one_leader_action(action: dict, sides: list[str], offsets: dict, leader_cal: dict, follower_cals: dict,
                      degrees: bool = True) -> dict:
    """LeRobot's action from ONE leader arm (keys 'joint.pos'; joints in degrees when `degrees`, the
    default of LeRobot 0.6 for the SO-101, else -100..100 % of the leader's range; gripper 0..100) -> the
    action for one or two follower arms (keys 'left_joint.pos' / 'right_joint.pos' for two). Joints with
    an offset for that arm go through the angle: + offset, kept in -180..180 and inside that arm's
    calibrated range. All other joints are copied as they are, exactly like LeRobot's leader -> follower copy."""
    out = {}
    for side in sides:
        off = offsets.get(side, {})
        for k, v in action.items():
            j = k.rsplit(".", 1)[0]
            if j in off and j != "gripper":
                lim = _half(follower_cals[side][j]) * 360.0 / 4095.0          # this arm's half range, degrees
                deg = float(v) if degrees else float(v) / 100.0 * _half(leader_cal[j]) * 360.0 / 4095.0
                deg = max(-lim, min(lim, (deg + float(off[j]) + 180.0) % 360.0 - 180.0))
                v = deg if degrees else deg / lim * 100.0
            out[f"{side}_{k}" if len(sides) > 1 else k] = v
    return out
