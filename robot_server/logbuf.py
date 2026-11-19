"""The server log kept in memory for the dashboard's terminal: the last lines of Python logging
(the same lines the server prints in its own terminal). Every line has a running number, so a
page only gets what is new. Log calls never contain PINs (see the tests)."""
from __future__ import annotations

import collections
import logging

MAX_LINES = 300
MAX_CHARS = 400
LEVELS = {logging.INFO: "INFO", logging.WARNING: "WARN", logging.ERROR: "ERROR", logging.CRITICAL: "ERROR"}


class LogBuffer(logging.Handler):
    def __init__(self, size: int = MAX_LINES) -> None:
        super().__init__(logging.INFO)
        self.lines: collections.deque = collections.deque(maxlen=size)
        self.n = 0

    def emit(self, record: logging.LogRecord) -> None:  # called with self.lock held
        try:
            msg = record.getMessage()
            err = record.exc_info[1] if record.exc_info else None
            if err is not None:
                msg += f" ({type(err).__name__}: {err})"
        except Exception:  # noqa: BLE001 - a bad log call must not break logging
            msg = str(record.msg)
        self.n += 1
        self.lines.append({"n": self.n, "ts": round(record.created, 3), "lvl": LEVELS.get(record.levelno, record.levelname),
                           "src": record.name, "msg": " ".join(msg.split())[:MAX_CHARS]})

    def since(self, n: int, limit: int = 200) -> list[dict]:
        """Lines newer than number n (oldest first), at most `limit` of the newest."""
        with self.lock:
            out = [x for x in self.lines if x["n"] > n]
        return out[-limit:]


LOGS = LogBuffer()


def install() -> LogBuffer:
    """Adds the buffer to the root logger once (create_app runs many times in the tests)."""
    root = logging.getLogger()
    if LOGS not in root.handlers:
        root.addHandler(LOGS)
    return LOGS
