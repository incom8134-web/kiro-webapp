"""Demo motion: replay one recorded picking demonstration on a follower arm, smoothly, several times.

For showing the robot (24 Nov 2026, user's choice): it does not need to grab anything, it must look like a
smooth pick. A recorded human demonstration is naturally smooth and needs no model (starts in seconds).
Each run: a soft 2 s glide (minimum-jerk) from the arm's pose to the first frame, the recorded pick at its
own 30 fps (lightly smoothed), a soft glide back to where the arm started, then a pause.
Runs in LeRobot's Python. Stop = Ctrl+C / SIGTERM: the arm holds where it is (torque stays on).

  python demo_replay.py --port /dev/so101_right_follower --id right_follower --dataset <root> --episode 9
"""
import argparse
import glob
import signal
import sys
import time

import numpy as np
import pandas as pd
from lerobot.robots.so_follower import SOFollower
from lerobot.robots.so_follower.config_so_follower import SOFollowerRobotConfig

KEYS = ["shoulder_pan.pos", "shoulder_lift.pos", "elbow_flex.pos", "wrist_flex.pos", "wrist_roll.pos", "gripper.pos"]
stop = False


def on_signal(*_):
    global stop
    stop = True


def min_jerk(a: np.ndarray, b: np.ndarray, seconds: float, fps: float):
    n = max(2, int(seconds * fps))
    for i in range(1, n + 1):
        t = i / n
        s = 10 * t ** 3 - 15 * t ** 4 + 6 * t ** 5
        yield a + (b - a) * s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--episode", type=int, default=9)
    ap.add_argument("--episodes", default="", help="several episodes, played in turn: 0,1,2 (overrides --episode)")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--pause", type=float, default=3.0)
    ap.add_argument("--glide", type=float, default=2.0)
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    files = sorted(glob.glob(a.dataset + "/data/*/*.parquet"))
    df = pd.concat([pd.read_parquet(f) for f in files])
    wanted = [int(x) for x in a.episodes.split(",") if x.strip()] or [a.episode]
    clips = []
    for e in wanted:
        ep = df[df["episode_index"] == e].sort_values("frame_index")
        if ep.empty:
            print(f"Episode {e} not found in {a.dataset}", flush=True)
            return 2
        f = np.stack(ep["action"].values).astype(float)
        padded = np.pad(f, ((2, 2), (0, 0)), mode="edge")  # light smoothing of hand jitter, same length
        clips.append((e, np.stack([np.convolve(padded[:, j], np.ones(5) / 5, mode="valid") for j in range(f.shape[1])], axis=1)))
    fps = 30.0 * max(0.5, min(2.0, a.speed))
    print(f"episodes {wanted}, {a.repeat} run(s), speed x{fps / 30:.2f}", flush=True)

    robot = SOFollower(SOFollowerRobotConfig(port=a.port, id=a.id, disable_torque_on_disconnect=False))
    robot.connect(calibrate=False)
    try:
        obs = robot.get_observation()
        home = np.array([obs[k] for k in KEYS], dtype=float)

        def send(pose):
            robot.send_action({k: float(v) for k, v in zip(KEYS, pose)})

        def play(poses, rate):
            next_t = time.perf_counter()
            last = None
            for pose in poses:
                if stop:
                    return last
                send(pose)
                last = pose
                next_t += 1 / rate
                time.sleep(max(0.0, next_t - time.perf_counter()))
            return last

        pose = home
        for run in range(1, a.repeat + 1):
            e, frames = clips[(run - 1) % len(clips)]
            print(f"run {run} of {a.repeat} (episode {e}, {len(frames) / 30:.1f} s)", flush=True)
            for part in ("in", "pick", "out"):  # glide to the start, the recorded pick, glide home
                poses = (min_jerk(pose, frames[0], a.glide, 30) if part == "in" else frames if part == "pick"
                         else min_jerk(pose, home, a.glide, 30))
                last = play(poses, fps if part == "pick" else 30)
                pose = pose if last is None else last
                if stop:
                    break
            if stop:
                break
            if run < a.repeat:
                end = time.time() + a.pause
                while time.time() < end and not stop:
                    time.sleep(0.05)
        print("stopped: the arm holds where it is" if stop else "done", flush=True)
    finally:
        robot.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
