#!/usr/bin/env python3
"""E-STOP wheel check, run after the wheel program has stopped: read each wheel motor's speed and
switch off any wheel that is still turning (target speed 0, torque off). The wheel motors run in
speed mode, so if the wheel program died without its clean-up a wheel could keep its last speed.
Prints one JSON line. Never starts a wheel.

  python3 wheel_stop.py /dev/so101_base
"""
from __future__ import annotations

import json
import sys
import time

ADDR_TORQUE, ADDR_GOAL_SPEED, ADDR_SPEED = 40, 46, 58  # Feetech STS3215
WHEEL_IDS = (7, 8, 9, 10)
MOVING = 20  # steps per second; below this the wheel is standing still


def speed_value(v: int) -> int:
    """Present_Speed is sign-magnitude (bit 15 = backwards)."""
    return -(v & 0x7FFF) if v & 0x8000 else v


def stop_wheels(read, write, ids=WHEEL_IDS) -> tuple[dict, list]:
    """read(id, addr) -> int | None, write(id, addr, value). Returns ({id: speed or 'no answer'}, turning ids)."""
    out: dict = {}
    turning: list = []
    for i in ids:
        s = [read(i, ADDR_SPEED) for _ in range(3)]
        if None in s:
            out[i] = "no answer"
            continue
        if max(abs(speed_value(v)) for v in s) > MOVING:
            turning.append(i)
            write(i, ADDR_GOAL_SPEED, 0)
            write(i, ADDR_TORQUE, 0)
            after = read(i, ADDR_SPEED)
            out[i] = speed_value(after) if after is not None else "no answer"
        else:
            out[i] = 0
    return out, turning


def main(port: str) -> int:
    from scservo_sdk import COMM_SUCCESS, PacketHandler, PortHandler
    ph, pk = PortHandler(port), PacketHandler(0)
    if not ph.openPort() or not ph.setBaudRate(1_000_000):
        print(json.dumps({"ok": False, "msg": f"cannot open {port}"}), flush=True)
        return 2

    def read(i, addr):
        for _ in range(3):
            try:
                v, r, _e = pk.read2ByteTxRx(ph, i, addr)
                if r == COMM_SUCCESS:
                    return v
            except IndexError:
                pass
            time.sleep(0.01)
        return None

    def write(i, addr, v):
        fn = pk.write1ByteTxRx if addr == ADDR_TORQUE else pk.write2ByteTxRx
        for _ in range(3):
            try:
                if fn(ph, i, addr, v)[0] == COMM_SUCCESS:
                    return
            except IndexError:
                pass
            time.sleep(0.01)

    try:
        wheels, turning = stop_wheels(read, write)
    finally:
        ph.closePort()
    ok = all(isinstance(v, int) and abs(v) <= MOVING for v in wheels.values())
    print(json.dumps({"ok": ok, "wheels": wheels, "turning": turning}), flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/dev/so101_base"))
