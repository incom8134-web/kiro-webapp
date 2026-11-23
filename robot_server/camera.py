"""Small live camera preview for the page (MJPEG over HTTP).

Device lookup reuses the old code (`cams_config.load()` in the so101 folder),
which knows how to find the Gemini 335 colour node and the wrist webcams.

`repair_top()` brings back the Gemini's colour node after the Orbbec ROS node was used (the old
`gemini_uvc_fix.sh`, but through a root helper instead of a password window).

A camera is opened only while someone watches, and closed 3 s after the last
viewer leaves. Only one program can open a camera at a time, so a job that
needs the camera (skill, recording) makes the preview let go first.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("kiro.camera")

PREVIEW_W, PREVIEW_H = 480, 360
JPEG_QUALITY = 60
PREVIEW_FPS = 10.0
IDLE_CLOSE_S = 3.0
FPS_WINDOW_S = 1.0      # the real frame rate is counted over windows this long
FPS_STALE_S = 3.0       # a rate older than this is not shown

# Top camera repair (deploy/README.md step 6b installs the helper and the sudo rule)
SUDO = "sudo"
REBIND_HELPER = "/usr/local/sbin/kiro-gemini-rebind"
FIX_SCRIPT = "gemini_uvc_fix.sh"       # in the old code folder; --check needs no root
CHECK_TIMEOUT_S = 15.0
HELPER_TIMEOUT_S = 30.0
COLOUR_WAIT_S = 10.0                   # after the re-plug: how long the colour node may take to come back
COLOUR_POLL_S = 0.5
OK_ALREADY, REPAIRED, NOT_CONNECTED, ORBBEC_RUNNING, NO_HELPER, FAILED = (
    "OK_ALREADY", "REPAIRED", "NOT_CONNECTED", "ORBBEC_RUNNING", "NO_HELPER", "FAILED")
REPAIR_CODES = (OK_ALREADY, REPAIRED, NOT_CONNECTED, ORBBEC_RUNNING, NO_HELPER, FAILED)
_repair_lock = threading.Lock()


def assign_wrists(cams: list[dict], webcams: list[tuple[str, str]]) -> list[dict]:
    """Plug and play for the hand cameras (preview only): a hand camera whose configured USB port
    is empty gets the next webcam found on any other port, right hand first. `webcams` is
    [(usb port, /dev/videoN)] of plugged-in webcams. The skill still uses the configured ports."""
    used = {c["device"] for c in cams if c.get("present")}
    free = [(port, dev) for port, dev in webcams if dev not in used]
    for key in ("right_wrist", "left_wrist"):
        c = next((c for c in cams if c["key"] == key), None)
        if c is None or c.get("present") or not free:
            continue
        port, dev = free.pop(0)
        c.update(device=dev, present=True, auto=port)
    return cams


def find_cameras(so101_dir: Path) -> list[dict]:
    """[{key, label, device, present, reason, auto}] from the old camera config, plus webcams found on
    other ports for empty hand-camera slots. [] if the config cannot be read."""
    if not (so101_dir / "cams_config.py").exists():
        return []
    if str(so101_dir) not in sys.path:
        sys.path.insert(0, str(so101_dir))
    try:
        import cams_config  # from the old code
        cams = [{k: c.get(k) for k in ("key", "label", "device", "present", "reason", "width", "height", "fps", "fourcc")}
                for c in cams_config.load(enabled_only=True)]
        return assign_wrists(cams, cams_config._webcam_bypaths())
    except Exception as e:  # never let a camera problem stop the server
        log.warning("camera lookup failed: %s", e)
        return []


# ------------------------------------------------------------ top camera repair
def _orbbec_running() -> bool:
    """The Orbbec ROS node (camera viewer or driving stack) holds the Gemini: it cannot be re-plugged then."""
    try:
        return subprocess.run(["pgrep", "-x", "component_conta"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _colour_check(script: Path) -> tuple[int, str]:
    """`gemini_uvc_fix.sh --check` (needs no root): (exit code, its output). 0 = colour node there,
    1 = missing (also while the Orbbec ROS node runs), 2 = Gemini not on USB. -1 = it could not run."""
    try:
        r = subprocess.run(["bash", str(script), "--check"], capture_output=True, text=True, timeout=CHECK_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, f"{type(e).__name__}: {e}"
    return r.returncode, (r.stdout + r.stderr).strip()


def repair_top(so101_dir: Path | str) -> tuple[bool, str, str]:
    """Bring the top camera's colour node back. Returns (ok, code, detail); ok is True for OK_ALREADY and
    REPAIRED. The code (REPAIR_CODES) is what the page shows text for, detail is technical (a script line).

      OK_ALREADY      the colour node is there, nothing was done
      REPAIRED        the USB device was re-plugged in software and the colour node came back
      NOT_CONNECTED   the Gemini is not on USB (cable, power, port)
      ORBBEC_RUNNING  the Orbbec ROS node is running: stop it first
      NO_HELPER       the root helper or its sudo rule is not installed (deploy/README.md step 6b)
      FAILED          anything else (script missing, re-plug refused or failed, node did not come back)

    It never moves anything and only re-plugs the one USB device with vendor id 2bc5, through the
    root-owned helper (sudo -n, no password window). Blocks up to about HELPER_TIMEOUT_S + COLOUR_WAIT_S."""
    if not _repair_lock.acquire(blocking=False):
        return False, FAILED, "another repair is running"
    try:
        script = Path(so101_dir) / FIX_SCRIPT
        if not script.is_file():
            return False, FAILED, f"{script} not found"
        rc, out = _colour_check(script)
        last = (out.splitlines() or [""])[0][:200]
        if rc == 0:
            return True, OK_ALREADY, last
        if rc == 2:
            return False, NOT_CONNECTED, last
        if rc != 1:
            return False, FAILED, f"gemini_uvc_fix.sh --check gave {rc}: {last}"
        if _orbbec_running():
            return False, ORBBEC_RUNNING, last
        if not os.access(REBIND_HELPER, os.X_OK):
            return False, NO_HELPER, f"{REBIND_HELPER} is not installed"
        try:
            r = subprocess.run([SUDO, "-n", REBIND_HELPER], capture_output=True, text=True, timeout=HELPER_TIMEOUT_S)
        except FileNotFoundError:
            return False, NO_HELPER, f"{SUDO} not found"
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, FAILED, f"{type(e).__name__}: {e}"
        said = (r.stdout.strip().splitlines() or [""])[-1][:200]
        if r.stderr.lstrip().startswith("sudo:"):   # sudo itself said no (the helper writes to stdout only)
            return False, NO_HELPER, r.stderr.strip().splitlines()[0][:200]
        if r.returncode == 2:
            return False, NOT_CONNECTED, said
        if r.returncode == 3:
            return False, ORBBEC_RUNNING, said
        if r.returncode != 0:
            return False, FAILED, f"helper exit {r.returncode}: {said}"
        end = time.monotonic() + COLOUR_WAIT_S
        while True:
            if _colour_check(script)[0] == 0:
                log.info("top camera repaired: %s", said)
                return True, REPAIRED, said
            if time.monotonic() >= end:
                return False, FAILED, f"the colour node did not come back within {COLOUR_WAIT_S:.0f} s"
            time.sleep(COLOUR_POLL_S)
    finally:
        _repair_lock.release()


class _Pattern:
    """Pretend camera (device "mock:<name>", mock mode and tests): a moving test picture, no hardware."""

    def __init__(self, name: str):
        self.name, self.n = name, 0

    def isOpened(self) -> bool:  # noqa: N802 - same names as cv2.VideoCapture
        return True

    def set(self, *_a) -> bool:
        return True

    def read(self):
        import cv2
        import numpy as np
        time.sleep(1 / 30)
        self.n += 1
        img = np.full((480, 640, 3), 28, np.uint8)
        x = (self.n * 6) % 640
        cv2.rectangle(img, (x, 0), (x + 40, 480), (70, 70, 70), -1)
        cv2.putText(img, f"MOCK {self.name}", (24, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (235, 235, 235), 3)
        cv2.putText(img, time.strftime("%H:%M:%S"), (24, 440), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2)
        return True, img

    def release(self) -> None:
        pass


class Camera:
    """One preview stream. Thread-safe."""

    def __init__(self, key: str, device: str, fourcc: str | None = None,
                 size: tuple[int, int] = (PREVIEW_W, PREVIEW_H), fps: float = PREVIEW_FPS):
        self.key, self.device, self.fourcc = key, device, fourcc
        self.size, self.fps = size, fps
        self._lock = threading.Condition()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._viewers = 0
        self._last_view = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.paused_by: str | None = None  # a job holds the camera
        self.error: str | None = None
        self.fps_real: float | None = None  # frames really read per second (the camera's own rate), not the preview rate
        self._fps_at = 0.0

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

        cap = _Pattern(self.device[5:]) if self.device.startswith("mock:") else cv2.VideoCapture(self.device, cv2.CAP_V4L2)
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
        period, next_t, fails = 1.0 / self.fps, 0.0, 0
        n_read, t_read = 0, time.monotonic()
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
                n_read += 1
                if now - t_read >= FPS_WINDOW_S:
                    self.fps_real, self._fps_at = n_read / (now - t_read), now
                    n_read, t_read = 0, now
                with self._lock:
                    idle = self._viewers == 0 and now - self._last_view > IDLE_CLOSE_S
                if idle:
                    break
                if now < next_t:
                    continue
                next_t = now + period
                small = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    with self._lock:
                        self._jpeg, self._seq = buf.tobytes(), self._seq + 1
                        self._lock.notify_all()
        finally:
            cap.release()
            log.info("camera %s closed", self.key)
            self._stop.set()  # ends every open stream; the page reconnects and attach() reopens
            self.fps_real = None
            with self._lock:
                self._jpeg = None
                self._lock.notify_all()

    def info(self) -> dict:
        live = bool(self._thread and self._thread.is_alive())
        fps = self.fps_real
        fresh = live and fps is not None and time.monotonic() - self._fps_at < FPS_STALE_S
        return {"key": self.key, "device": self.device, "live": live, "viewers": self._viewers,
                "paused_by": self.paused_by, "error": self.error, "fps_real": round(fps, 1) if fresh else None}
