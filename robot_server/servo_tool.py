#!/usr/bin/env python3
"""Servo tester for the web page. Reuses the old tester's Bus class
(so101_servo_tester.Bus: Feetech registers, retries, the lerobot ID-change order).

Runs as its own short process for each command, so a stuck USB board can never
hang the server. Prints one line: @SERVO {json}.

  servo_tool.py scan   PORT [LO HI]     ping IDs, read info + telemetry
  servo_tool.py torque PORT ID 0|1
  servo_tool.py move   PORT ID POS      position mode, torque on, go to POS (0-4095)
  servo_tool.py setid  PORT OLD NEW     only when OLD is the only servo answering on the port
  servo_tool.py ports                   serial ports with their /dev/so101_* names
"""
import glob
import json
import os
import sys
from pathlib import Path

SO101 = Path(os.environ.get("KIRO_SO101_DIR", "~/so101")).expanduser()
sys.path.insert(0, str(SO101))
BAUD = 1_000_000


def out(**kw):
    print("@SERVO " + json.dumps(kw), flush=True)


def ports():
    links = {os.path.realpath(p): p for p in glob.glob("/dev/so101_*") + glob.glob("/dev/rplidar")}
    found = []
    for dev in sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")):
        name = links.get(dev)
        found.append({"dev": name or dev, "real": dev, "arm": bool(name and "base" not in name and "rplidar" not in name),
                      "lidar": bool(name and "rplidar" in name)})
    return found


def main(argv):
    if not argv:
        out(ok=False, msg="no command")
        return 2
    cmd = argv[0]
    if cmd == "ports":
        out(ok=True, ports=ports())
        return 0
    port = argv[1]
    if not os.path.exists(port):
        out(ok=False, msg=f"{port} not found")
        return 1
    import scan_servos as ss  # old code: who has the port open
    users = ss.users_of(port)
    if users:
        out(ok=False, msg=f"{port} is in use by PID {users[0][0]} ({users[0][1][:40]})")
        return 1
    from so101_servo_tester import Bus  # old code
    try:
        bus = Bus(port, BAUD)
    except Exception as e:  # noqa: BLE001
        out(ok=False, msg=f"cannot open {port}: {e}")
        return 1
    try:
        if cmd == "scan":
            lo, hi = (int(argv[2]), int(argv[3])) if len(argv) > 3 else (1, 12)
            servos = []
            for sid in range(lo, hi + 1):
                model = bus.ping(sid)
                if model is None:
                    continue
                row = {"id": sid}
                try:
                    row.update(bus.info(sid, model))
                    row.update(bus.telemetry(sid))
                except Exception as e:  # noqa: BLE001 - one bad servo must not hide the others
                    row["error"] = str(e)[:80]
                servos.append(row)
            out(ok=True, port=port, lo=lo, hi=hi, servos=servos)
        elif cmd == "torque":
            sid, on = int(argv[2]), argv[3] == "1"
            bus.set_torque(sid, on)
            out(ok=True, msg=f"ID {sid} torque {'on' if on else 'off'}")
        elif cmd == "move":
            sid, pos = int(argv[2]), int(argv[3])
            if bus.read(sid, "MODE") != 0:
                bus.set_mode(sid, 0)  # position mode (turns torque off first)
            bus.set_torque(sid, True)
            bus.set_position(sid, pos)
            out(ok=True, msg=f"ID {sid} -> {max(0, min(4095, pos))}")
        elif cmd == "setid":
            old, new = int(argv[2]), int(argv[3])
            if not 1 <= new <= 253:
                out(ok=False, msg="New ID must be 1 to 253.")
                return 1
            answering = [sid for sid in range(0, 254) if bus.ping(sid) is not None]
            if answering != [old]:
                out(ok=False, msg=f"Connect exactly one servo (ID {old}) to this port. Answering now: {answering}")
                return 1
            ok = bus.change_id(old, new)
            out(ok=ok is not None, msg=f"ID {old} -> {new}" + ("" if ok is not None else " (no answer on the new ID)"))
        else:
            out(ok=False, msg=f"unknown command {cmd}")
            return 2
    except Exception as e:  # noqa: BLE001
        out(ok=False, msg=str(e)[:160])
        return 1
    finally:
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
