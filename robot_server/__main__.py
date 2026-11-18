"""Start with:  python -m robot_server"""
import logging

import uvicorn

from .config import Config
from .main import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    print(f"\n  KIRO Command Center  ->  http://localhost:{cfg.port}   (on the robot hotspot: http://10.42.0.1:{cfg.port})")
    if cfg.pin_is_default:
        print("  Operator PIN is the mock default 0000. Set KIRO_OPERATOR_PIN before using the real robot.\n")
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
