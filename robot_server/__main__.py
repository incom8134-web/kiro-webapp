"""Start with:  python -m robot_server

Serves the page over HTTPS only, on KIRO_PORT (8080):
  https://<robot>:8080
The certificate is made once with openssl (or taken from KIRO_TLS_CERT and KIRO_TLS_KEY).
The browser warns once about that certificate. There is no cleartext fallback.
"""
import asyncio
import contextlib
import logging
import os
import signal
import subprocess
from pathlib import Path

import uvicorn

from .config import Config
from .main import create_app

log = logging.getLogger("kiro")
TLS_DIR = Path(os.environ.get("KIRO_TLS_DIR", "~/.config/kiro-webapp/tls")).expanduser()


def ensure_cert() -> tuple[str, str] | None:
    """A self-signed certificate for the robot, made once with openssl. Never in git.
    KIRO_TLS_DIR chooses the folder (default ~/.config/kiro-webapp/tls)."""
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
        log.error("could not make a certificate (%s)", e)
        return None


def tls_files() -> tuple[str, str]:
    """Key and certificate. Set both KIRO_TLS_CERT and KIRO_TLS_KEY to use your own.
    Otherwise a certificate is written under KIRO_TLS_DIR. The server does not start without one."""
    cert = os.environ.get("KIRO_TLS_CERT", "").strip()
    key = os.environ.get("KIRO_TLS_KEY", "").strip()
    if cert or key:
        if not (cert and key and Path(cert).is_file() and Path(key).is_file()):
            raise SystemExit("KIRO_TLS_CERT and KIRO_TLS_KEY must both be set, and both files must exist.")
        return key, cert
    made = ensure_cert()
    if not made:
        raise SystemExit("Refusing to serve without HTTPS. Install openssl, or set KIRO_TLS_CERT and KIRO_TLS_KEY.")
    return made


async def serve(cfg: Config) -> None:
    app = create_app(cfg)
    small = 64 * 1024  # the page never sends more; a 16 MB message would stall the control loop
    key, cert = tls_files()
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, ssl_keyfile=key, ssl_certfile=cert,
                                           log_level="warning", timeout_graceful_shutdown=3, ws_max_size=small))
    await server.serve()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    print(f"\n  HAEBOT Robot  ->  https://localhost:{cfg.port}   (on the robot hotspot: https://10.42.0.1:{cfg.port})")
    print("  The certificate is self-signed. The browser warns once; continue past that warning.")
    if cfg.pin_is_default:
        print("  Mock mode is using the built-in PIN. Set KIRO_OPERATOR_PIN before using the real robot.")
    # Closing the terminal sends SIGHUP, which would end Python at once and leave the base or an
    # arm program running without the server. Treat it like a normal stop (uvicorn handles SIGTERM).
    signal.signal(signal.SIGHUP, lambda *_: os.kill(os.getpid(), signal.SIGTERM))
    with contextlib.suppress(KeyboardInterrupt):  # uvicorn re-raises Ctrl+C after its clean shutdown
        asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
