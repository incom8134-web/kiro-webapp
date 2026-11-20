"""Starts and stops the robot's existing programs (teleop, fold, skills, voice...).

Each job is one child process in its own process group, so stopping it also
stops everything it started. Jobs name the devices they open ("uses"). Only
one job may hold a device at a time: two programs on one serial port or one
camera fight and fail.
"""
from __future__ import annotations

import collections
import datetime
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    stdin: bool = False               # True = the server can send lines to the program (Job.send)
    on_line: Callable[[str], None] | None = None  # called with every output line, as it arrives


def _stamp(t: float | None = None) -> str:
    """Wall-clock time with milliseconds, for log files the test reports read response times from."""
    return datetime.datetime.fromtimestamp(time.time() if t is None else t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Job:
    def __init__(self, name: str, spec: JobSpec, on_exit: Callable[["Job"], None] | None = None,
                 log_dir: Path | None = None):
        self.name, self.spec = name, spec
        self.lines: collections.deque[str] = collections.deque(maxlen=200)
        self.started = time.time()
        self.ended: float | None = None
        self.code: int | None = None
        self.asked = False  # the server stopped it (button, E-STOP), so its end is no surprise
        self._on_exit = on_exit
        self.log_path: Path | None = None
        self._log = None
        if log_dir is not None:  # every line with its time, kept after the job ends (test records)
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                self.log_path = log_dir / f"web_{name}_{time.strftime('%Y%m%d_%H%M%S')}.log"
                self._log = open(self.log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - closed when the job ends
                self._log.write(f"{_stamp(self.started)} [server] start {spec.label or name}: {' '.join(spec.cmd)[:400]}\n")
            except OSError:
                self._log = None
        env = {**os.environ, "PYTHONUNBUFFERED": "1", **spec.env}
        env = {k: v for k, v in env.items() if v is not None}
        self.proc = subprocess.Popen(spec.cmd, cwd=spec.cwd, env=env, stdin=subprocess.PIPE if spec.stdin else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True, text=True, errors="replace", bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip()
            if line:
                self.lines.append(line[:300])
                self._write(line)
                if self.spec.on_line:
                    try:
                        self.spec.on_line(line)
                    except Exception:  # noqa: BLE001 - a parser bug must not stop reading the output
                        pass
        self.code = self.proc.wait()
        self.ended = time.time()
        self._write(f"[server] ended, exit code {self.code}")
        if self._log:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        if self._on_exit:
            self._on_exit(self)

    def _write(self, line: str) -> None:
        if self._log:
            try:
                self._log.write(f"{_stamp()} {line}\n")
            except (OSError, ValueError):
                pass

    def note(self, text: str) -> None:
        """A server line in this job's log file (signals sent, E-STOP), not shown as program output."""
        self._write(f"[server] {text}")

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def send(self, line: str) -> bool:
        """One line to the program's stdin (only for jobs started with stdin=True)."""
        if not self.running or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write(line.rstrip("\n") + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def _signal(self, sig: int) -> None:
        self.note(f"signal {signal.Signals(sig).name}")
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, first_sent: bool = False) -> None:
        """Ask nicely, then harder. Blocks up to about stop_wait + 5 seconds."""
        self.asked = True
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
        self.asked = True
        if self.running:
            self._signal(signal.SIGKILL)
            try:
                self.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass

    def info(self) -> dict:
        return {"name": self.name, "label": self.spec.label or self.name, "running": self.running,
                "code": self.code, "asked": self.asked, "started": self.started, "ended": self.ended,
                "uses": sorted(self.spec.uses), "tail": list(self.lines)[-12:]}

    def output(self) -> list[str]:
        """A copy of the captured lines (safe while the reader thread is still appending)."""
        return list(self.lines)


class Jobs:
    """All jobs started from the page. Thread-safe. log_dir: each job's output also goes to
    web_<job>_<time>.log there, every line with its time (None = no files)."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir
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
                self._jobs[name] = Job(name, spec, on_exit=self._ended, log_dir=self.log_dir)
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
            j.asked = True
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
