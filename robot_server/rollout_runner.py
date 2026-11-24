"""lerobot-rollout with the ACT queue shortcut (lerobot_patch.apply_rollout) and reversed / tuned joints.
Same arguments. Started by ~/so101/run_policy.sh when ROLLOUT_PY points here (the Play button)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lerobot_patch  # noqa: E402

lerobot_patch.apply()
lerobot_patch.apply_rollout()
try:
    lerobot_patch.apply_timing()  # log lines: where each step's time goes
except Exception as e:  # noqa: BLE001 - timing is only a help
    print("timing not available:", e, flush=True)
from lerobot.scripts.lerobot_rollout import main  # noqa: E402

if __name__ == "__main__":
    sys.argv[0] = "lerobot-rollout"
    sys.exit(main())
