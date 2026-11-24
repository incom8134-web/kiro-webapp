"""lerobot-teleoperate with reversed joints honoured in degrees (lerobot_patch.py). Same arguments."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lerobot_patch  # noqa: E402

lerobot_patch.apply()
from lerobot.scripts.lerobot_teleoperate import main  # noqa: E402

if __name__ == "__main__":
    sys.argv[0] = "lerobot-teleoperate"
    sys.exit(main())
