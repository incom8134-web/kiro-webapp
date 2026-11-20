#!/usr/bin/env python3
"""Leader arm on the Jetson -> follower arms. Reads the leader's six servo positions about 30 times
a second (read only: the leader's torque is never touched) and sends them as joint angles to the old
twin_bridge.py (127.0.0.1:5558). The bridge moves the arms with its own safety: joint limits from
each arm's calibration, a speed limit and ramp, and it holds still when packets stop for 0.5 s.

  python3 leader_relay.py --port /dev/so101_right_leader --calib right_leader.json --arms right,left
"""
from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from robot_server.leadermath import leader_to_target  # noqa: E402

ADDR_POS = 56  # Present_Position, 2 bytes (Feetech STS3215)
IDS = range(1, 7)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--calib", required=True, help="the leader arm's LeRobot calibration .json")
    ap.add_argument("--arms", default="right,left", help="follower arms that copy the leader")
    ap.add_argument("--to", default="127.0.0.1:5558")
    ap.add_argument("--hz", type=float, default=30.0)
    a = ap.parse_args()
    cal = json.loads(Path(a.calib).read_text())
    arms = [s for s in a.arms.split(",") if s in ("right", "left")]
    host, _, port = a.to.rpartition(":")
    dest = (host, int(port))

    from scservo_sdk import COMM_SUCCESS, GroupSyncRead, PacketHandler, PortHandler
    ph, pk = PortHandler(a.port), PacketHandler(0)
    if not ph.openPort() or not ph.setBaudRate(1_000_000):
        print(f"cannot open {a.port}", flush=True)
        return 1
    sync = GroupSyncRead(ph, pk, ADDR_POS, 2)
    for i in IDS:
        sync.addParam(i)

    def read_sync():
        if sync.txRxPacket() != COMM_SUCCESS or not all(sync.isAvailable(i, ADDR_POS, 2) for i in IDS):
            return None
        return [sync.getData(i, ADDR_POS, 2) for i in IDS]

    def read_each():  # boards that do not answer the group read
        out = []
        for i in IDS:
            v, res, _ = pk.read2ByteTxRx(ph, i, ADDR_POS)
            if res != COMM_SUCCESS:
                return None
            out.append(v)
        return out

    stop = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    read, bad_sync = read_sync, 0
    seq = sent = missed = 0
    t_log = time.monotonic()
    print(f"leader {a.port} -> {'+'.join(arms)} arm(s) via {a.to}", flush=True)
    while not stop:
        t0 = time.monotonic()
        raw = read()
        if raw is None and read is read_sync:
            bad_sync += 1
            if bad_sync >= 5:
                read = read_each
                print("group read not answered: reading the motors one by one", flush=True)
        if raw is not None and all(0 <= v <= 4095 for v in raw):
            seq += 1
            tgt = leader_to_target(cal, raw)
            pkt = {"cmd": "target", "t": time.time(), "seq": seq, "units": "deg", "arms": {s: tgt for s in arms}}
            udp.sendto(json.dumps(pkt).encode(), dest)
            sent += 1
        else:
            missed += 1
        if t0 - t_log >= 5:
            print(f"sent {sent}, missed reads {missed}", flush=True)
            t_log = t0
        time.sleep(max(0.0, 1.0 / a.hz - (time.monotonic() - t0)))
    ph.closePort()
    print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
