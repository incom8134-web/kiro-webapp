"""Small live camera preview for the page (MJPEG over HTTP).

Device lookup reuses the old code (`cams_config.load()` in the so101 folder),
which knows how to find the Gemini 335 colour node and the wrist webcams.

A camera is opened only while someone watches, and closed 3 s after the last
viewer leaves. Only one program can open a camera at a time, so a job that
needs the camera (skill, recording) makes the preview let go first.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("kiro.camera")

PREVIEW_W, PREVIEW_H = 480, 360
JPEG_QUALITY = 60
PREVIEW_FPS = 10.0
IDLE_CLOSE_S = 3.0


def find_cameras(so101_dir: Path) -> list[dict]:
    """[{key, label, device, present, reason}] from the old camera config. [] if it cannot be read."""
    if not (so101_dir / "cams_config.py").exists():
        return []
    if str(so101_dir) not in sys.path:
        sys.path.insert(0, str(so101_dir))
    try:
        import cams_config  # from the old code
        return [{k: c.get(k) for k in ("key", "label", "device", "present", "reason", "width", "height", "fps", "fourcc")}
                for c in cams_config.load(enabled_only=True)]
    except Exception as e:  # never let a camera problem stop the server
        log.warning("camera lookup failed: %s", e)
        return []


class Camera:
    """One preview stream. Thread-safe."""

    def __init__(self, key: str, device: str, fourcc: str | None = None):
        self.key, self.device, self.fourcc = key, device, fourcc
        self._lock = threading.Condition()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._viewers = 0
        self._last_view = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.paused_by: str | None = None  # a job holds the camera
        self.error: str | None = None

    # -- viewers ------------------------------------------------------------
    def attach(self) -> None:
        with self._lock:
            self._viewers += 1
            self._last_view = time.monotonic()
            if self.paused_by is None and (self._thread is None or not self._thread.is_alive()):
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name=f"cam-{self.key}", daemon=True)
                self._thread.start()

    def detach(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            self._last_view = time.monotonic()

    def frames(self):
        """Yields JPEG bytes as new frames arrive. Ends when the camera is paused or closed."""
        seq = -1
        while True:
            with self._lock:
                self._lock.wait_for(lambda: self._seq != seq or self._stop.is_set(), timeout=2.0)
                if self._stop.is_set() or self.paused_by:
                    return
                if self._seq == seq or self._jpeg is None:
                    continue
                seq, jpg = self._seq, self._jpeg
            yield jpg

    # -- jobs ---------------------------------------------------------------
    def pause(self, by: str) -> None:
        """Close the device now (a job needs it). Waits until it is really closed."""
        self.paused_by = by
        self._stop.set()
        with self._lock:
            self._lock.notify_all()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)

    def resume(self) -> None:
        self.paused_by = None

    # -- worker -------------------------------------------------------------
    def _run(self) -> None:
        import cv2

        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if self.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            self.error = f"cannot open {self.device}"
            log.warning("camera %s: %s", self.key, self.error)
            self._stop.set()
            with self._lock:
                self._lock.notify_all()
            return
        self.error = None
        log.info("camera %s opened (%s)", self.key, self.device)
        period, next_t, fails = 1.0 / PREVIEW_FPS, 0.0, 0
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()  # keep reading at full rate so the picture is never old
                if not ok:
                    fails += 1
                    if fails > 30:
                        self.error = "no frames"
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                now = time.monotonic()
                with self._lock:
                    idle = self._viewers == 0 and now - self._last_view > IDLE_CLOSE_S
                if idle:
                    break
                if now < next_t:
                    continue
                next_t = now + period
                small = cv2.resize(frame, (PREVIEW_W, PREVIEW_H), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    with self._lock:
                        self._jpeg, self._seq = buf.tobytes(), self._seq + 1
                        self._lock.notify_all()
        finally:
            cap.release()
            log.info("camera %s closed", self.key)
            self._stop.set()  # ends every open stream; the page reconnects and attach() reopens
            with self._lock:
                self._jpeg = None
                self._lock.notify_all()

    def info(self) -> dict:
        live = bool(self._thread and self._thread.is_alive())
        return {"key": self.key, "device": self.device, "live": live, "viewers": self._viewers,
                "paused_by": self.paused_by, "error": self.error}
