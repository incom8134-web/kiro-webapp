"""LeRobot 0.6.1 ignores a calibration's drive_mode (a joint whose motor counts the other way round) when it
works in degrees, which our arms do. The yellow leader's shoulder is such a joint (24 Nov 2026): it rests at
the top of its range while every other arm rests at the bottom, so the green arm was told "shoulder up" and
overloaded. apply() makes degrees honour drive_mode, for the leader (reading) and the follower (writing).
It changes nothing for joints with drive_mode 0, i.e. every other arm.

Used by teleop_runner.py and record_runner.py, inside LeRobot's own Python."""
from __future__ import annotations

import json
from pathlib import Path


TUNE = Path("~/.config/kiro-webapp/leader_tune.json").expanduser()


def load_tune(path: Path = TUNE) -> dict:
    """{port: {joint: (scale, offset)}} from {port: {joint: scale | {"scale": s, "offset": deg}}}: map a leader
    joint onto its follower when the two travel differently (24 Nov 2026: yellow leader's shoulder vs the green
    arm, measured in two poses, rest and straight up, by matching both arms)."""
    try:
        out = {}
        for port, joints in json.loads(path.read_text()).items():
            out[port] = {}
            for j, v in joints.items():
                out[port][j] = (float(v["scale"]), float(v.get("offset", 0.0))) if isinstance(v, dict) else (float(v), 0.0)
        return out
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return {}


def apply() -> None:
    from lerobot.motors.motors_bus import MotorNormMode, SerialMotorsBus

    tune = load_tune()

    if getattr(SerialMotorsBus, "_kiro_drive_mode", False):
        return
    orig_norm, orig_unnorm = SerialMotorsBus._normalize, SerialMotorsBus._unnormalize

    def reversed_deg(bus, id_) -> bool:
        motor = bus._id_to_name(id_)
        cal = (bus.calibration or {}).get(motor)
        return bool(cal and cal.drive_mode and bus.motors[motor].norm_mode is MotorNormMode.DEGREES)

    def tuned(bus, id_) -> tuple[float, float]:
        motor = bus._id_to_name(id_)
        if bus.motors[motor].norm_mode is not MotorNormMode.DEGREES:
            return 1.0, 0.0
        return tune.get(str(getattr(bus, "port", "")), {}).get(motor, (1.0, 0.0))

    def _normalize(self, ids_values):
        out = {}
        for i, v in orig_norm(self, ids_values).items():
            sc, off = tuned(self, i)
            out[i] = (-v if reversed_deg(self, i) else v) * sc + off
        return out

    def _unnormalize(self, ids_values):
        back = {}
        for i, v in ids_values.items():
            sc, off = tuned(self, i)
            v = (v - off) / sc
            back[i] = -v if reversed_deg(self, i) else v
        return orig_unnorm(self, back)

    SerialMotorsBus._normalize, SerialMotorsBus._unnormalize = _normalize, _unnormalize
    SerialMotorsBus._kiro_drive_mode = True


def apply_rollout() -> None:
    """lerobot-rollout runs the whole picture pipeline (both cameras to the GPU, normalised) on EVERY step,
    but ACT looks at it only once per chunk (100 steps) and otherwise pops its planned queue. On the Jetson
    that wasted work held the loop at ~10 Hz instead of 30 (24 Nov 2026), so the arm moved 3x slower than
    it was trained. While the queue still has actions, pop it directly: the same actions, in the same order."""
    import torch
    from contextlib import nullcontext
    from lerobot.rollout.inference.sync import SyncInferenceEngine

    if getattr(SyncInferenceEngine, "_kiro_queue_skip", False):
        return
    orig = SyncInferenceEngine.get_action

    def get_action(self, obs_frame):
        policy = getattr(self, "_policy", None)
        queue = getattr(policy, "_action_queue", None)
        ensemble = getattr(getattr(policy, "config", None), "temporal_ensemble_coeff", None)
        if obs_frame is None or not queue or ensemble is not None:
            return orig(self, obs_frame)  # empty queue: the model plans the next chunk as usual
        try:
            amp = self._device.type == "cuda" and self._policy.config.use_amp
            with torch.inference_mode(), (torch.autocast(device_type=self._device.type) if amp else nullcontext()):
                item = queue[0]
                action = self._postprocessor(item.unsqueeze(0) if item.dim() == 1 else item)
            from lerobot.rollout.inference.sync import make_robot_action
            action_dict = make_robot_action(action.squeeze(0).cpu(), self._dataset_features)
            out = torch.tensor([action_dict[k] for k in self._ordered_action_keys])
            queue.popleft()  # only after the action was built: a failure leaves the queue for the normal step
            return out
        except Exception as e:  # noqa: BLE001 - a LeRobot build we do not recognise must still play
            if not getattr(self, "_kiro_skip_warned", False):
                print(f"@REC queue skip failed, normal step: {e}", flush=True)
                self._kiro_skip_warned = True
            return orig(self, obs_frame)

    SyncInferenceEngine.get_action = get_action
    SyncInferenceEngine._kiro_queue_skip = True


def apply_timing(every: float = 3.0) -> None:
    """Print, every few seconds, how long each part of a rollout step takes (cameras, motors, model):
    finds what holds the loop below 30 Hz. Log lines only; nothing else changes."""
    import time
    from collections import defaultdict
    from lerobot.cameras.opencv.camera_opencv import OpenCVCamera
    from lerobot.rollout.inference.sync import SyncInferenceEngine
    from lerobot.motors.motors_bus import SerialMotorsBus

    acc, n, t_last = defaultdict(float), defaultdict(int), [time.monotonic()]

    def timed(key, fn):
        def wrap(*a, **kw):
            t0 = time.monotonic()
            try:
                return fn(*a, **kw)
            finally:
                k = key(a[0]) if callable(key) else key
                acc[k] += time.monotonic() - t0
                n[k] += 1
                if time.monotonic() - t_last[0] > every:
                    t_last[0] = time.monotonic()
                    print("[timing] " + "  ".join(f"{k} {1000 * acc[k] / max(1, n[k]):.0f} ms x{n[k]}" for k in sorted(acc)), flush=True)
                    acc.clear(); n.clear()
        return wrap

    OpenCVCamera.async_read = timed(lambda cam: f"cam {cam.config.index_or_path}", OpenCVCamera.async_read)
    SerialMotorsBus.sync_read = timed("motors read", SerialMotorsBus.sync_read)
    SerialMotorsBus.sync_write = timed("motors write", SerialMotorsBus.sync_write)
    SyncInferenceEngine.get_action = timed("policy", SyncInferenceEngine.get_action)
