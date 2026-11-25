"""lerobot-rollout with the ACT queue shortcut (lerobot_patch.apply_rollout) and reversed / tuned joints.
Same arguments. Started by ~/so101/run_policy.sh when ROLLOUT_PY points here (the Play button).

The motor patch is required: without it the arm moves in the wrong units. The queue shortcut is not.
If this LeRobot build has no SyncInferenceEngine (or the shortcut throws), playback still starts
with stock lerobot-rollout. That is slower on the Jetson, and it is the same path a hand-run
`run_policy.sh` uses when ROLLOUT_PY is unset.
"""
import sys
from pathlib import Path


def _patch():
    try:
        from robot_server import lerobot_patch
        return lerobot_patch
    except ImportError:  # launched as a script: this folder is on sys.path, the package may not be
        import lerobot_patch
        return lerobot_patch


def prepare() -> None:
    """Apply the motor patch, then the optional queue shortcut and timing log."""
    patch = _patch()
    patch.apply()  # do not catch: wrong units would move the arm the wrong way
    try:
        patch.apply_rollout()
    except Exception as e:  # noqa: BLE001 - stock rollout still plays the model
        print(f"@REC queue skip not applied: {e}", flush=True)
    try:
        patch.apply_timing()  # log lines: where each step's time goes
    except Exception as e:  # noqa: BLE001 - timing is only a help
        print("timing not available:", e, flush=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    prepare()
    from lerobot.scripts.lerobot_rollout import main

    sys.argv[0] = "lerobot-rollout"
    sys.exit(main())
