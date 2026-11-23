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
  record_runner.py verify '<json>'   reads a finished dataset back with LeRobot (needs the LeRobot venv), one JSON line

files_check() (pure Python, used by recorder.py) looks at a dataset folder the way LeRobot v3 leaves it. A
parquet file only gets its end marker, and an mp4 only its index (moov), when LeRobot finalizes the dataset:
after Ctrl+C that happens, after a kill -9 (E-STOP) it does not, and the dataset is broken. The pretend
recorder keeps the same habits, so a force stop and an E-STOP can be tried without a robot.

Progress lines: @REC {json}. "say" is LeRobot's own announcement ("Recording episode 3",
"Reset the environment", "Re-record episode", "Stop recording", "Exiting").
"""
import json
import os
import shlex
import struct
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


def lerobot_args(rg, cfg: dict, cams: list) -> list:
    """The command line of the old window's build_cmd()."""
    args = [*rg.ARMS[cfg["arm"]]["args"]]  # each follower arm with the leader of its own side
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
    sys.argv = ["lerobot-record", *args]
    mark(phase="connecting")
    try:
        lr.main()
    except KeyboardInterrupt:   # force stop: LeRobot already dropped the unfinished episode and finished the rest
        mark(phase="forced")
        return 130
    return 0


# ------------------------------------------------------ dataset files: layout and read check
PAR1 = b"PAR1"   # a parquet file starts and ends with it; LeRobot writes the end only when it finalizes


def fake_mp4(payload: int, finished: bool) -> bytes:
    """Boxes of an mp4 for the pretend recorder: ftyp, mdat, and the moov index only once finished."""
    ftyp = struct.pack(">I4s", 24, b"ftyp") + b"isom" + b"\0\0\2\0" + b"isom" + b"mp41"
    mdat = struct.pack(">I4s", 8 + payload, b"mdat") + b"\0" * payload
    return ftyp + mdat + (struct.pack(">I4s", 8, b"moov") if finished else b"")


def parquet_problem(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size < 12:
            return "too small to be a parquet file"
        with open(path, "rb") as fh:
            head = fh.read(4)
            fh.seek(-4, 2)
            tail = fh.read(4)
    except OSError as e:
        return f"cannot be read ({e.strerror})"
    if head != PAR1:
        return "is not a parquet file"
    if tail != PAR1:
        return "cut off: no end marker (the recorder was stopped before it finished the file)"
    return ""


def mp4_problem(path: Path) -> str:
    """Walks the top-level boxes: a video needs ftyp and moov, and no box may run past the end of the file."""
    seen = set()
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            pos = 0
            while pos < size:
                fh.seek(pos)
                head = fh.read(8)
                if len(head) < 8:
                    return "cut off in the middle of a box"
                n, kind = struct.unpack(">I4s", head)
                if n == 1:
                    n = struct.unpack(">Q", fh.read(8))[0]
                elif n == 0:
                    n = size - pos
                if n < 8 or pos + n > size:
                    return "cut off: the video data ends early"
                seen.add(kind)
                pos += n
    except (OSError, struct.error) as e:
        return f"cannot be read ({e})"
    if b"ftyp" not in seen:
        return "is not an mp4 video"
    if b"moov" not in seen:
        return "cut off: no index (the encoder was stopped before it finished the file)"
    return ""


def files_check(root: Path | str) -> dict:
    """Pure-Python read check of a LeRobot v3 dataset folder: {ok, episodes, frames, problems}. Looks at
    meta/info.json, the data and episode tables (parquet start and end), the tasks and stats files and one
    mp4 per camera (the end of each). Not a full read: the LeRobot read (verify) does that."""
    root = Path(root)
    out = {"ok": False, "episodes": 0, "frames": 0, "problems": []}
    bad = out["problems"]
    try:
        info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
    except OSError:
        bad.append("meta/info.json is missing")
        return out
    except ValueError:
        bad.append("meta/info.json is not valid JSON")
        return out
    n, f = (info.get("total_episodes") if isinstance(info, dict) else None), (info.get("total_frames") if isinstance(info, dict) else None)
    if not (isinstance(n, int) and isinstance(f, int) and n >= 0 and f >= 0):
        bad.append("meta/info.json has no episode and frame count")
        return out
    out.update(episodes=n, frames=f)
    if n > 0:
        if f == 0:
            bad.append("meta/info.json counts episodes but no frames")
        for what, pattern in (("data", "data/**/*.parquet"), ("episode table", "meta/episodes/**/*.parquet")):
            files = sorted(root.glob(pattern))
            if not files:
                bad.append(f"no {what} files ({pattern})")
            for p in files:
                why = parquet_problem(p)
                if why:
                    bad.append(f"{p.relative_to(root)}: {why}")
        tasks = root / "meta" / "tasks.parquet"
        why = parquet_problem(tasks) if tasks.exists() else "is missing"
        if why:
            bad.append(f"meta/tasks.parquet: {why}")
        try:
            json.loads((root / "meta" / "stats.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bad.append("meta/stats.json is missing or not valid JSON")
        for key, v in (info.get("features") or {}).items():
            if not (key.startswith("observation.images.") and isinstance(v, dict) and v.get("dtype") == "video"):
                continue
            videos = sorted((root / "videos" / key).glob("**/*.mp4"))
            if not videos:
                bad.append(f"no video files for {key.split('.')[-1]}")
            for p in videos:
                why = mp4_problem(p)
                if why:
                    bad.append(f"{p.relative_to(root)}: {why}")
    out["ok"] = not bad
    return out


class MockDataset:
    """The pretend recorder's dataset, with LeRobot v3's habits: meta/info.json is written when the dataset is
    created and after every saved episode; the data file, the episode table and the videos only get their end
    in finalize(). LeRobot runs that after Ctrl+C (Force stop) but never after a kill -9 (E-STOP)."""

    def __init__(self, root: Path, info: dict):
        self.root, self.info, self.opened = root, info, False

    def write_info(self) -> None:
        (self.root / "meta").mkdir(parents=True, exist_ok=True)
        (self.root / "meta" / "info.json").write_text(json.dumps(self.info))

    def _files(self, finished: bool) -> None:
        n, end = self.info["total_episodes"], PAR1 if finished else b""
        parts = {"data/chunk-000/file-000.parquet": PAR1 + b"d" * 1000 * n + end,
                 "meta/episodes/chunk-000/file-000.parquet": PAR1 + b"e" * 200 * n + end,
                 "meta/tasks.parquet": PAR1 + b"t" * 50 + PAR1, "meta/stats.json": b"{}"}
        for key, v in self.info["features"].items():
            if key.startswith("observation.images.") and v.get("dtype") == "video":
                parts[f"videos/{key}/chunk-000/file-000.mp4"] = fake_mp4(2000 * n, finished)
        for rel, data in parts.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_bytes(data)

    def save_episode(self, frames: int) -> None:
        self.info["total_episodes"] += 1
        self.info["total_frames"] += frames
        self._files(finished=False)    # the files are open for writing from the first saved episode on
        self.opened = True
        self.write_info()

    def finalize(self) -> None:
        if self.opened:
            self._files(finished=True)
            self.opened = False


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
    ds = MockDataset(root, info)
    if not cfg.get("resume"):
        ds.write_info()   # LeRobotDataset.create(): the folder exists before the robot connects
    mark(cmd=f"lerobot-record (mock) --dataset.repo_id={cfg['repo']} --dataset.root={root}")

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
            ds.save_episode(frames)
            recorded += 1
    except KeyboardInterrupt:   # the episode being recorded is dropped, the saved ones are finished (finally)
        mark(phase="forced")
        return 130
    finally:
        mark(say="Stop recording")
        ds.finalize()
        mark(say="Exiting")
    return 0


# ----------------------------------------------------------- read check (LeRobot venv)
def verify(cfg: dict) -> None:
    """Loads the dataset the way training does (like pack_dataset.sh: first and last frame decoded) and prints
    one JSON line: {"ok": true, episodes, frames}, {"ok": false, "error"} when the dataset cannot be read, or
    {"ok": null, "error"} when LeRobot itself could not be loaded (not a problem of the dataset)."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": None, "error": f"LeRobot could not be loaded: {type(e).__name__}: {e}"[:300]}))
        return
    try:
        info = json.loads((Path(cfg["root"]) / "meta" / "info.json").read_text())
        ds = LeRobotDataset(cfg["repo"], root=cfg["root"], video_backend="pyav")
        rows, episodes = len(ds.hf_dataset), ds.meta.total_episodes      # what the data files really hold
        in_data = len(set(ds.hf_dataset.unique("episode_index")))
        if rows != info.get("total_frames") or in_data != info.get("total_episodes"):
            raise ValueError(f"data files hold {in_data} episodes and {rows} frames, meta/info.json says "
                             f"{info.get('total_episodes')} and {info.get('total_frames')}")
        if rows:
            ds[0], ds[rows - 1]   # decodes a video frame at each end
        frames = rows
        print(json.dumps({"ok": True, "episodes": episodes, "frames": frames}))
    except Exception as e:  # noqa: BLE001 - whatever is wrong, the dataset cannot be read
        why = f"{type(e).__name__}: {e}"
        if e.__cause__ is not None:   # LeRobot wraps the real reason ("error while generating the dataset")
            why += f" ({type(e.__cause__).__name__}: {e.__cause__})"
        print(json.dumps({"ok": False, "error": why[:300]}))


if __name__ == "__main__":
    what, cfg = sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    if what == "check":
        check(cfg)
    elif what == "verify":
        verify(cfg)
    else:
        sys.exit(record(cfg) if what == "record" else mock(cfg))
