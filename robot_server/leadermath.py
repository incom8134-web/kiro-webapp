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
