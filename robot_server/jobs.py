"""Starts and stops the robot's existing programs (teleop, fold, skills, voice...).

Each job is one child process in its own process group, so stopping it also
stops everything it started. Jobs name the devices they open ("uses"). Only
one job may hold a device at a time: two programs on one serial port or one
camera fight and fail.
"""
from __future__ import annotations

import collections
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class JobSpec:
    cmd: list[str]
    uses: frozenset[str] = frozenset()
    cwd: str | None = None
    env: dict[str, str | None] = field(default_factory=dict)  # None removes the variable
    stop_signal: int = signal.SIGINT  # LeRobot and ros2 launch shut down cleanly on Ctrl+C
    stop_wait: float = 6.0            # seconds before the next, harder signal
    label: str = ""


class Job:
    def __init__(self, name: str, spec: JobSpec, on_exit: Callable[["Job"], None] | None = None):
        self.name, self.spec = name, spec
        self.lines: collections.deque[str] = collections.deque(maxlen=200)
        self.started = time.time()
        self.ended: float | None = None
        self.code: int | None = None
        self._on_exit = on_exit
        env = {**os.environ, "PYTHONUNBUFFERED": "1", **spec.env}
        env = {k: v for k, v in env.items() if v is not None}
        self.proc = subprocess.Popen(spec.cmd, cwd=spec.cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True, text=True, errors="replace", bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip()
            if line:
                self.lines.append(line[:300])
        self.code = self.proc.wait()
        self.ended = time.time()
        if self._on_exit:
            self._on_exit(self)

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, first_sent: bool = False) -> None:
        """Ask nicely, then harder. Blocks up to about stop_wait + 5 seconds."""
        for i, (sig, wait) in enumerate(((self.spec.stop_signal, self.spec.stop_wait), (signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0))):
            if not self.running:
                return
            if not (i == 0 and first_sent):
                self._signal(sig)
            try:
                self.proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                continue

    def kill(self) -> None:
        """Stop at once, with no clean-up code in the program (E-STOP for arm programs:
        their clean-up moves the arm). The servos keep their torque and last goal."""
        if self.running:
            self._signal(signal.SIGKILL)
            try:
                self.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass

    def info(self) -> dict:
        return {"name": self.name, "label": self.spec.label or self.name, "running": self.running,
                "code": self.code, "started": self.started, "ended": self.ended,
                "uses": sorted(self.spec.uses), "tail": list(self.lines)[-12:]}

    def output(self) -> list[str]:
        """A copy of the captured lines (safe while the reader thread is still appending)."""
        return list(self.lines)


class Jobs:
    """All jobs started from the page. Thread-safe."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # device -> (let go, take back) callbacks. The camera preview lets go while a job uses the camera.
        self.holders: dict[str, tuple[Callable[[], None], Callable[[], None]]] = {}

    def busy(self, uses: frozenset[str]) -> str | None:
        """Name of a running job that holds one of these devices, or None."""
        for j in list(self._jobs.values()):
            if j.running and j.spec.uses & uses:
                return j.name
        return None

    def start(self, name: str, spec: JobSpec) -> tuple[bool, str]:
        with self._lock:
            old = self._jobs.get(name)
            if old and old.running:
                return False, f"{name} is already running."
            other = self.busy(spec.uses)
            if other:
                return False, f"{other} is using the same device. Stop it first."
            for dev in spec.uses:
                if dev in self.holders:
                    self.holders[dev][0]()
            try:
                self._jobs[name] = Job(name, spec, on_exit=self._ended)
            except OSError as e:
                self._ended_uses(spec.uses)
                return False, f"Could not start {name}: {e}"
        return True, f"{spec.label or name} started."

    def _ended(self, job: Job) -> None:
        self._ended_uses(job.spec.uses)

    def _ended_uses(self, uses: frozenset[str]) -> None:
        for dev in uses:
            if dev in self.holders:
                self.holders[dev][1]()

    def stop(self, name: str) -> tuple[bool, str]:
        j = self._jobs.get(name)
        if not j or not j.running:
            return False, f"{name} is not running."
        j.stop()
        return True, f"{j.spec.label or name} stopped."

    def stop_all(self, only: Callable[[Job], bool] = lambda j: True) -> list[str]:
        """Signal every matching job at once, then wait for each (one slow job does not delay the rest)."""
        todo = [j for j in list(self._jobs.values()) if j.running and only(j)]
        for j in todo:
            j._signal(j.spec.stop_signal)
        threads = [threading.Thread(target=j.stop, kwargs={"first_sent": True}) for j in todo]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return [j.name for j in todo]

    def kill(self, names: list[str]) -> list[str]:
        killed = []
        for n in names:
            j = self._jobs.get(n)
            if j and j.running:
                j.kill()
                killed.append(n)
        return killed

    def running(self, name: str) -> bool:
        j = self._jobs.get(name)
        return bool(j and j.running)

    def get(self, name: str) -> Job | None:
        return self._jobs.get(name)

    def info(self) -> dict[str, dict]:
        return {n: j.info() for n, j in list(self._jobs.items())}
