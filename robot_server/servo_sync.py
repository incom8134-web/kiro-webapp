#!/usr/bin/env python3
"""Run before an arm program switches the motors on: set every motor's target (Goal_Position) to
where the motor is now. Feetech motors start with target 0 after power-up (and keep stale targets
after a recalibration); switched on like that, each joint drives to its limit and the arm flips.
Each position is read 5 times and written only when the readings agree; the torque is left as it
was. Prints one JSON line; exit code 0 = every motor synced, 2 = not (the arm must not start).

  python3 servo_sync.py /dev/so101_right_follower [/dev/so101_left_follower]
"""
from __future__ import annotations

import json
import statistics
import sys
import time

ADDR_TORQUE, ADDR_GOAL, ADDR_POS = 40, 42, 56
IDS = range(1, 7)
AGREE = 6  # steps (about 0.5 degree)


def sync_motors(read, write, ids=IDS) -> dict:
    """read(id, addr) -> int | None, write(id, addr, value). Returns {id: position or reason}."""
    out: dict = {}
    for i in ids:
        reads = [read(i, ADDR_POS) for _ in range(5)]
        if None in reads:
            out[i] = "no answer"
            continue
        if max(reads) - min(reads) > AGREE:
            out[i] = "readings disagree"
            continue
        p = int(statistics.median(reads))
        if read(i, ADDR_GOAL) != p:
            torque = read(i, ADDR_TORQUE)
            write(i, ADDR_GOAL, p)              # these motors switch torque on with a new target: it holds still
            if torque == 0:
                write(i, ADDR_TORQUE, 0)        # leave it as it was
        out[i] = p
    return out


def main(ports: list[str]) -> int:
    from scservo_sdk import COMM_SUCCESS, PacketHandler, PortHandler
    result, ok = {}, True
    for port in ports:
        ph, pk = PortHandler(port), PacketHandler(0)
        if not ph.openPort() or not ph.setBaudRate(1_000_000):
            result[port], ok = "cannot open", False
            continue

        def read(i, addr):
            fn = pk.read1ByteTxRx if addr == ADDR_TORQUE else pk.read2ByteTxRx
            for _ in range(5):
                try:
                    v, r, _e = fn(ph, i, addr)
                    if r == COMM_SUCCESS:
                        return v
                except IndexError:  # a cut-off reply
                    pass
                time.sleep(0.02)
            return None

        def write(i, addr, v):
            fn = pk.write1ByteTxRx if addr == ADDR_TORQUE else pk.write2ByteTxRx
            for _ in range(5):
                try:
                    if fn(ph, i, addr, v)[0] == COMM_SUCCESS:
                        return
                except IndexError:
                    pass
                time.sleep(0.02)

        try:
            res = sync_motors(read, write)
        finally:
            ph.closePort()
        result[port] = res
        ok = ok and all(isinstance(v, int) for v in res.values())
    print(json.dumps({"ok": ok, "ports": result}), flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
