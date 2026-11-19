"""Start with:  python -m robot_server

Serves the page twice from one app:
  http://<robot>:8080    plain, for phones and quick use
  https://<robot>:8443   needed by the browser for the leader arm on a laptop (Web Serial)
                         and the microphone. Self-made certificate: the browser warns once.
"""
import asyncio
import contextlib
import logging
import os
import subprocess
from pathlib import Path

import uvicorn

from .config import Config
from .main import create_app

log = logging.getLogger("kiro")
TLS_DIR = Path(os.environ.get("KIRO_TLS_DIR", "~/.config/kiro-webapp/tls")).expanduser()


def ensure_cert() -> tuple[str, str] | None:
    """A self-signed certificate for the robot, made once with openssl. Never in git."""
    key, crt = TLS_DIR / "key.pem", TLS_DIR / "cert.pem"
    if key.exists() and crt.exists():
        return str(key), str(crt)
    try:
        TLS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-keyout", str(key), "-out", str(crt), "-subj", "/CN=kiro-robot",
                        "-addext", "subjectAltName=IP:10.42.0.1,IP:127.0.0.1,DNS:localhost"],
                       check=True, capture_output=True, timeout=60)
        key.chmod(0o600)
        return str(key), str(crt)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("no https: could not make a certificate (%s)", e)
        return None


class _SecondServer(uvicorn.Server):
    """The https server. Ctrl+C is handled by the main server, which then stops this one."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def serve(cfg: Config) -> None:
    app = create_app(cfg)
    main = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning",
                                         timeout_graceful_shutdown=3))  # camera streams never end by themselves
    https_port = int(os.environ.get("KIRO_HTTPS_PORT", "8443"))
    cert = ensure_cert() if https_port else None
    second = None
    if cert:
        second = _SecondServer(uvicorn.Config(app, host=cfg.host, port=https_port, ssl_keyfile=cert[0], ssl_certfile=cert[1],
                                              lifespan="off", log_level="warning", timeout_graceful_shutdown=3))
        print(f"  Leader arm / microphone (https)  ->  https://localhost:{https_port}   (hotspot: https://10.42.0.1:{https_port})\n")
    task = asyncio.create_task(second.serve()) if second else None
    try:
        await main.serve()
    finally:
        if second:
            second.should_exit = True
            with contextlib.suppress(Exception):
                await task


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    print(f"\n  KIRO Command Center  ->  http://localhost:{cfg.port}   (on the robot hotspot: http://10.42.0.1:{cfg.port})")
    if cfg.pin_is_default:
        print("  Operator PIN is the mock default 0000. Set KIRO_OPERATOR_PIN before using the real robot.")
    with contextlib.suppress(KeyboardInterrupt):  # uvicorn re-raises Ctrl+C after its clean shutdown
        asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
