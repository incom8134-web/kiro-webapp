#!/usr/bin/env python3
"""Read-only servo check: voltage, temperature, current and alarm bits of every
servo board. Prints one JSON line. No motion, no torque change.

Same registers and rules as power_check.py in the old code. Runs as its own
short process (the server kills it after a few seconds) because the Feetech
SDK can hang forever on a stuck USB board. Ports that another program has
open are skipped, never shared.

  python3 servo_probe.py [--skip key,key]    all boards except the skipped ones
"""
import glob
import json
import os
import sys
import time

ADDR_VOLT, ADDR_TEMP, ADDR_ERR, ADDR_CURR = 62, 63, 65, 69
BOARDS = [  # key, device link, servo ids
    ("right_follower", "/dev/so101_right_follower", range(1, 7)),
    ("left_follower", "/dev/so101_left_follower", range(1, 7)),
    ("right_leader", "/dev/so101_right_leader", range(1, 7)),
    ("left_leader", "/dev/so101_left_leader", range(1, 7)),
    ("base", "/dev/so101_base", range(7, 11)),
]


def users(dev):
    """PIDs that have this device open (same method as the old power_check.busy)."""
    real = os.path.realpath(dev)
    me = os.getpid()
    out = []
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(fd) == real:
                pid = int(fd.split("/")[2])
                if pid != me:
                    out.append(pid)
        except (OSError, ValueError):
            pass
    return sorted(set(out))


def comm(pid):
    try:
        return open(f"/proc/{pid}/comm").read().strip()
    except OSError:
        return "?"


def probe(skip):
    from scservo_sdk import PacketHandler, PortHandler

    out = {}
    for key, dev, ids in BOARDS:
        if not os.path.exists(dev):
            out[key] = {"state": "missing"}
            continue
        if key in skip:
            out[key] = {"state": "busy", "by": "job"}
            continue
        pids = users(dev)
        if pids:
            out[key] = {"state": "busy", "by": comm(pids[0])}
            continue
        port, ph = PortHandler(dev), PacketHandler(0)
        try:
            if not port.openPort() or not port.setBaudRate(1_000_000):
                out[key] = {"state": "error", "msg": "cannot open port"}
                continue
            servos = {}
            for sid in ids:
                _, res, _ = ph.ping(port, sid)
                if res != 0:
                    continue
                s = {}
                v, r, _ = ph.read1ByteTxRx(port, sid, ADDR_VOLT)
                if r == 0:
                    s["v"] = round(v / 10.0, 1)
                t, r, _ = ph.read1ByteTxRx(port, sid, ADDR_TEMP)
                if r == 0:
                    s["t"] = t
                e, r, _ = ph.read1ByteTxRx(port, sid, ADDR_ERR)
                if r == 0:
                    s["err"] = e
                c, r, _ = ph.read2ByteTxRx(port, sid, ADDR_CURR)
                if r == 0:
                    s["ma"] = round((c & 0x7FFF) * 6.5)
                servos[str(sid)] = s
                time.sleep(0.002)
            volts = [s["v"] for s in servos.values() if "v" in s]
            temps = [s["t"] for s in servos.values() if "t" in s]
            out[key] = {"state": "ok" if servos else "silent", "n": len(servos), "of": len(ids), "servos": servos,
                        "vmin": min(volts, default=None), "tmax": max(temps, default=None),
                        "alarm": any(s.get("err") for s in servos.values())}
        except Exception as e:  # noqa: BLE001 - report, never crash the server
            out[key] = {"state": "error", "msg": str(e)[:120]}
        finally:
            try:
                port.closePort()
            except Exception:  # noqa: BLE001
                pass
    return out


if __name__ == "__main__":
    skip = set()
    if "--skip" in sys.argv:
        skip = set(sys.argv[sys.argv.index("--skip") + 1].split(","))
    print(json.dumps({"t": time.time(), "boards": probe(skip)}))
