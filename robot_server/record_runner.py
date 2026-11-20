#!/usr/bin/env python3
"""Runs the old data recorder for the Studio page, without a screen.

The recording is LeRobot's own `lerobot-record`, unchanged, set up exactly the way the old
"SO-101 데이터 기록" window (~/so101/record_gui.py) sets it up: its arm table (ARMS), its camera list
(cams_config), its camera tuning (cam_tune) and its options (H.264, streaming encoding, no name stamp,
--dataset.root, --resume). The old window pressed → / ← / Esc on the robot's desktop (pynput) for
"next episode / record it again / stop"; a server has no desktop, so this file gives LeRobot a listener
that reads the same three commands from stdin, one per line: next, redo, stop. When stdin closes (the
server went away) that counts as stop: the current episode is saved and the dataset is finished.

  record_runner.py check  '<json>'   facts for the page's checks, from the old helpers (one JSON line)
  record_runner.py record '<json>'   tune the cameras, then lerobot-record (needs the LeRobot venv)
  record_runner.py mock   '<json>'   pretend recorder for mock mode and tests: same lines, same commands

Progress lines: @REC {json}. "say" is LeRobot's own announcement ("Recording episode 3",
"Reset the environment", "Re-record episode", "Stop recording", "Exiting").
"""
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

SO101 = Path(os.environ.get("KIRO_SO101_DIR", "~/so101")).expanduser()
KEYS = {"next": "right", "redo": "left", "stop": "esc"}   # the old window's buttons -> LeRobot's keys


def mark(**kv) -> None:
    print("@REC " + json.dumps({**kv, "t": round(time.time(), 3)}), flush=True)


def apply_key(key: str, events: dict) -> None:
    """The same flags LeRobot's apply_recording_control sets (used by the pretend recorder)."""
    events["exit_early"] = True
    if key == "left":
        events["rerecord_episode"] = True
    elif key == "esc":
        events["stop_recording"] = True


def stdin_listener(apply) -> tuple[object, dict]:
    """(listener, events) like LeRobot's init_keyboard_listener, fed by stdin instead of the keyboard."""
    events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}

    def read():
        for line in sys.stdin:
            key = KEYS.get(line.strip())
            if key:
                mark(key=line.strip())
                apply(key, events)
        mark(key="stop", why="stdin closed")
        apply("esc", events)

    threading.Thread(target=read, name="commands", daemon=True).start()

    class Listener:
        def stop(self):
            pass
    return Listener(), events


# ------------------------------------------------------------------ the old code
def old_gui():
    sys.path.insert(0, str(SO101))
    import record_gui  # noqa: E402  (old code: ARMS, cams_config, cam_tune, MIN_FOLLOWER_VOLT ...)
    return record_gui


def check(cfg: dict) -> None:
    rg = old_gui()
    tool = rg.record_tool()
    calib = sorted({rel for a in rg.ARMS.values() for rel in a["calib"]})
    users = {}
    for key, dev in rg.ARM_LINKS.items():
        if os.path.exists(dev):
            u = rg.ss.users_of(dev)
            if u:
                users[key] = u[0][1]
    cams = [{k: c.get(k) for k in ("key", "label", "source", "device", "present", "reason", "width", "height", "fps", "fourcc")}
            for c in rg.cams_config.load()]
    print(json.dumps({
        "tool": tool, "tool_ok": Path(tool).is_file(),
        "arms": {k: {"ports": a["ports"], "followers": a["followers"], "calib": a["calib"]} for k, a in rg.ARMS.items()},
        "calib_dir": str(rg.CALIB), "calib_ok": {rel: (rg.CALIB / rel).is_file() for rel in calib},
        "min_volt": rg.MIN_FOLLOWER_VOLT, "users": users, "cams": cams,
        "orbbec_ros": subprocess.run(["pgrep", "-x", "component_conta"], capture_output=True).returncode == 0,
    }, ensure_ascii=False))


def one_leader_args(rg, arm: str) -> list:
    """No left leader: the silver arm (the right leader) drives the green arm, or both arms."""
    L = rg.ARM_LINKS
    if arm == "both":
        robot = ["--robot.type=bi_so_follower", "--robot.id=so101",
                 f"--robot.left_arm_config.port={L['left_follower']}", f"--robot.right_arm_config.port={L['right_follower']}"]
    else:
        robot = ["--robot.type=so101_follower", f"--robot.port={L['left_follower']}", "--robot.id=left_follower"]
    return robot + ["--teleop.type=so101_leader", f"--teleop.port={L['right_leader']}", "--teleop.id=right_leader"]


def sync_two_arm_calibration(calib: Path) -> None:
    """LeRobot's two-arm robot reads so101_left.json / so101_right.json; they must be today's arm
    calibrations (the motors hold those), or LeRobot would stop and ask to calibrate. Old files are kept."""
    import shutil
    for side in ("left", "right"):
        src, dst = calib / "robots/so_follower" / f"{side}_follower.json", calib / "robots/so_follower" / f"so101_{side}.json"
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            continue
        if dst.exists():
            shutil.copy(dst, dst.with_name(f"so101_{side}_before_{time.strftime('%Y%m%d_%H%M%S')}.json"))
        shutil.copy(src, dst)
        print(f"two-arm calibration {dst.name} = {src.name}", flush=True)


def install_one_leader(lr, rg, cfg: dict) -> None:
    """Make LeRobot's leader give the action of every follower arm (see leadermath.one_leader_action)."""
    from leadermath import one_leader_action
    sides = ["left", "right"] if cfg["arm"] == "both" else ["left"]
    off_file = Path.home() / ".config/kiro-webapp/follow_offsets.json"
    offsets = json.loads(off_file.read_text()) if off_file.exists() else {}
    leader_cal = json.loads((rg.CALIB / "teleoperators/so_leader/right_leader.json").read_text())
    follower_cals = {s: json.loads((rg.CALIB / f"robots/so_follower/{s}_follower.json").read_text()) for s in sides}
    print(f"one leader (silver arm) -> {' + '.join(sides)} arm(s), offsets {offsets}", flush=True)
    real = lr.make_teleoperator_from_config

    def make(tcfg):
        t = real(tcfg)
        get = t.get_action
        deg = bool(getattr(tcfg, "use_degrees", True))
        print(f"leader values in {'degrees' if deg else 'percent'}", flush=True)
        t.get_action = lambda: one_leader_action(get(), sides, offsets, leader_cal, follower_cals, degrees=deg)
        return t
    lr.make_teleoperator_from_config = make


def lerobot_args(rg, cfg: dict, cams: list) -> list:
    """The command line of the old window's build_cmd()."""
    args = one_leader_args(rg, cfg["arm"]) if cfg.get("one_leader") else [*rg.ARMS[cfg["arm"]]["args"]]
    if cams:
        args.append(f"--robot.cameras={rg.cams_config.lerobot_arg(cams)}")
    args += [f"--dataset.repo_id={cfg['repo']}", f"--dataset.root={cfg['root']}", f"--dataset.single_task={cfg['task']}",
             f"--dataset.num_episodes={int(cfg['episodes'])}", f"--dataset.episode_time_s={int(cfg['episode_s'])}",
             f"--dataset.reset_time_s={int(cfg['reset_s'])}", f"--dataset.fps={int(cfg['fps'])}",
             "--dataset.push_to_hub=false", "--dataset.no_stamp=true", "--display_data=false", "--play_sounds=false"]
    if cfg.get("vcodec", "h264") != "libsvtav1":
        args.append(f"--dataset.rgb_encoder.vcodec={cfg.get('vcodec', 'h264')}")
    if cfg.get("streaming", True):
        args += ["--dataset.streaming_encoding=true", "--dataset.encoder_threads=2"]
    if cfg.get("resume"):
        args.append("--resume=true")
    return args


def record(cfg: dict) -> int:
    rg = old_gui()
    cams = [c for c in rg.cams_config.load() if c["key"] in cfg["cams"]]
    gone = [c["key"] for c in cams if not c["present"]] + [k for k in cfg["cams"] if k not in {c["key"] for c in cams}]
    if gone:
        print("Error: camera not connected: " + ", ".join(gone), flush=True)
        return 2
    mark(phase="tuning")
    for c in cams:   # the old window's tune_cameras(): Gemini flicker-safe, webcams default + 30 fps
        try:
            if c["source"] == "gemini" or cfg.get("flicker"):
                rg.cam_tune.flicker_safe(c["device"], log=lambda m, k=c["key"]: print(f"{k}: {m}", flush=True))
            else:
                rg.cam_tune.apply_defaults(c["device"])
                print(f"{c['key']}: defaults, exposure priority off (30 fps), 60 Hz", flush=True)
        except Exception as e:  # noqa: BLE001 - the old window also goes on
            print(f"{c['key']}: camera tuning failed: {e}", flush=True)

    from lerobot.scripts import lerobot_record as lr
    from lerobot.utils.keyboard_input import apply_recording_control
    args = lerobot_args(rg, cfg, cams)
    mark(cmd=shlex.join(["lerobot-record", *args]))
    say = lr.log_say

    def log_say(text, play_sounds=True, blocking=False):
        mark(say=text)
        return say(text, play_sounds, blocking)
    lr.log_say = log_say
    lr.init_keyboard_listener = lambda: stdin_listener(apply_recording_control)
    if cfg.get("one_leader"):
        if cfg["arm"] == "both":
            sync_two_arm_calibration(rg.CALIB)
        install_one_leader(lr, rg, cfg)
    sys.argv = ["lerobot-record", *args]
    mark(phase="connecting")
    try:
        lr.main()
    except KeyboardInterrupt:   # force stop: LeRobot already dropped the unfinished episode and finished the rest
        mark(phase="forced")
        return 130
    return 0


# ----------------------------------------------------------- pretend recorder
def mock(cfg: dict) -> int:
    """Mock mode: the control flow of lerobot_record.record() with a clock instead of a robot."""
    root, fps = Path(cfg["root"]), int(cfg["fps"])
    meta = root / "meta" / "info.json"
    print(f"MOCK lerobot-record {cfg['repo']} ({cfg['arm']} arm, cameras {', '.join(cfg['cams']) or 'none'})", flush=True)
    if cfg.get("resume"):
        info = json.loads(meta.read_text())
    else:
        if root.exists():
            print(f"Error: FileExistsError: {root} already exists", flush=True)
            return 1
        info = {"codebase_version": "v3.0", "robot_type": "bi_so_follower" if cfg["arm"] == "both" else "so_follower",
                "total_episodes": 0, "total_frames": 0, "fps": fps,
                "features": {"action": {"dtype": "float32"}, "observation.state": {"dtype": "float32"},
                             **{f"observation.images.{k}": {"dtype": "video"} for k in cfg["cams"]}}}
    mark(cmd=f"lerobot-record (mock) --dataset.repo_id={cfg['repo']} --dataset.root={root}")

    def save():
        (root / "meta").mkdir(parents=True, exist_ok=True)
        (root / "data").mkdir(exist_ok=True)
        (root / "data" / "mock.txt").write_text("x" * (1000 * info["total_episodes"]))
        meta.write_text(json.dumps(info))

    events = {"stop_recording": False}

    def loop(seconds: float) -> int:
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            if events["exit_early"]:
                events["exit_early"] = False
                break
            time.sleep(0.02)
        return int((time.monotonic() - t0) * fps)

    n = int(cfg["episodes"])
    try:
        mark(phase="connecting")
        time.sleep(float(cfg.get("mock_connect_s", 1.5)))
        _, events = stdin_listener(apply_key)
        recorded = 0
        while recorded < n and not events["stop_recording"]:
            mark(say=f"Recording episode {info['total_episodes']}")
            frames = loop(float(cfg["episode_s"]))
            if not events["stop_recording"] and (recorded < n - 1 or events["rerecord_episode"]):
                mark(say="Reset the environment")
                loop(float(cfg["reset_s"]))
            if events["rerecord_episode"]:
                mark(say="Re-record episode")
                events["rerecord_episode"] = events["exit_early"] = False
                continue
            info["total_episodes"] += 1
            info["total_frames"] += frames
            save()
            recorded += 1
    except KeyboardInterrupt:
        mark(phase="forced")
        return 130
    finally:
        mark(say="Stop recording")
        save()
        mark(say="Exiting")
    return 0


if __name__ == "__main__":
    what, cfg = sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    if what == "check":
        check(cfg)
    else:
        sys.exit(record(cfg) if what == "record" else mock(cfg))
