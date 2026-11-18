"""Everything beyond the wheels: arms, teleop, skills, voice, health, shutdown.

Nothing here re-implements robot logic. Each action starts a program from the
old code folder (KIRO_SO101_DIR, default ~/so101) exactly as the old Tkinter
app did, with stdin closed and output captured for the page:

  base      run_base_teleop.sh --inner      wheel node + lidar guard + robot gamepad
  teleop    lerobot-teleoperate             leader arm -> follower arm
  fold/...  arm_tuck.py --tuck|--hold|--release
  skill     run_policy.sh run <model> <s>   trained ACT model on the right arm
  voice     ros2 run so101_base voice_cmd   push-to-talk Korean voice driving

In mock mode the same buttons start harmless stand-in commands.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .camera import Camera, find_cameras
from .config import Config
from .jobs import Jobs, JobSpec

log = logging.getLogger("kiro.robot")
HERE = Path(__file__).resolve().parent
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

PORT = {
    "right_follower": "/dev/so101_right_follower", "left_follower": "/dev/so101_left_follower",
    "right_leader": "/dev/so101_right_leader", "left_leader": "/dev/so101_left_leader",
    "base": "/dev/so101_base",
}
SIDES = ("right", "left", "both")
ARM_ACTIONS = {"fold": "--tuck", "hold": "--hold", "release": "--release"}
ROS_SETUP = "unset PYTHONPATH; source /opt/ros/jazzy/setup.bash; source \"$HOME/ros2_ws/install/setup.bash\"; "
VOICE_MODELS = {"ko": "vosk-model-small-ko-0.22", "en": "vosk-model-small-en-us-0.15"}
VOICE_ARGS = ("-p ptt:=true -p grammar_mode:=phrases -p device:=auto -p output_topic:=/cmd_vel_raw "
              "-p lin_speed:=0.10 -p min_conf:=0.6 -p default_dist:=0.2 -p max_dist:=0.2 -p default_angle:=45.0")


def _sides(side: str) -> list[str]:
    return ["right", "left"] if side == "both" else [side]


class Robot:
    def __init__(self, cfg: Config, ros=None):
        self.cfg = cfg
        self.mock = cfg.backend == "mock" or ros is None or getattr(ros, "name", "") != "ros"
        self.ros = ros
        self.so101 = Path(cfg.so101_dir).expanduser()
        self.venv = Path(cfg.lerobot_venv).expanduser()
        self.jobs = Jobs()
        self.events: deque[dict] = deque(maxlen=60)
        self.health: dict = {"jetson": {}, "boards": {}, "t": 0}
        self.bus_lock = threading.Lock()  # the servo probe and arm jobs never open a port at the same time
        self.cams: dict[str, Camera] = {}
        self.cam_info: list[dict] = []
        self._stop = threading.Event()
        self.voice_lang = "ko"
        self.refresh_cameras()
        threading.Thread(target=self._health_loop, name="health", daemon=True).start()

    # ------------------------------------------------------------- events
    def event(self, who: str, text: str) -> None:
        self.events.append({"ts": time.time(), "who": who, "text": text})
        log.info("%s: %s", who, text)

    # ------------------------------------------------------------ cameras
    def refresh_cameras(self) -> None:
        self.cam_info = find_cameras(self.so101)
        for c in self.cam_info:
            key, dev = c["key"], c.get("device")
            if not c.get("present") or not dev:
                continue
            cam = self.cams.get(key)
            if cam is None or cam.device != dev:
                if cam:
                    cam.pause("refresh")
                cam = Camera(key, dev, c.get("fourcc"))
                self.cams[key] = cam
            self.jobs.holders[f"cam:{key}"] = (lambda k=key: self.cams[k].pause("job"),
                                               lambda k=key: self.cams[k].resume())

    # -------------------------------------------------------------- helpers
    def _venv_env(self) -> dict[str, str]:
        return {"PYTHONPATH": None, "PATH": f"{self.venv / 'bin'}:{os.environ.get('PATH', '')}"}

    def _mock_cmd(self, what: str, seconds: float = 3600) -> list[str]:
        return [sys.executable, "-u", "-c",
                f"import time,signal,sys\nsignal.signal(signal.SIGINT, lambda *a: (print('stopped'), sys.exit(0)))\n"
                f"print('MOCK: {what}')\nfor i in range(int({seconds})):\n    time.sleep(1)\n    if i % 5 == 4: print('MOCK: still running', i + 1, 's')\n"
                f"print('MOCK: done')"]

    def _arm_ok(self, side: str) -> str | None:
        """None if this arm may move, otherwise the reason (in English, the page translates known ones)."""
        if "left" in _sides(side) and not self.cfg.left_arm_ok:
            return "LEFT_BLOCKED"
        return None

    def _missing(self, keys: list[str]) -> list[str]:
        if self.mock:
            return []
        return [k for k in keys if not os.path.exists(PORT[k])]

    def _start(self, name: str, spec: JobSpec, who: str) -> tuple[bool, str]:
        serial = any(u.startswith("/dev/so101") for u in spec.uses)
        if serial and not self.bus_lock.acquire(timeout=10):
            return False, "Servo check is busy. Try again."
        try:
            ok, msg = self.jobs.start(name, spec)
        finally:
            if serial:
                self.bus_lock.release()
        if ok:
            self.event(who, f"started {spec.label or name}")
        return ok, msg

    def _stop_job(self, name: str, who: str) -> tuple[bool, str]:
        ok, msg = self.jobs.stop(name)
        if ok:
            self.event(who, f"stopped {name}")
        return ok, msg

    # ----------------------------------------------------------------- base
    def base(self, on: bool, who: str) -> tuple[bool, str]:
        if not on:
            return self._stop_job("base", who)
        if self._missing(["base"]):
            return False, "Wheel board /dev/so101_base not found. Check USB and 12 V power."
        uses = frozenset({PORT["base"], "/dev/rplidar"})
        if self.mock:
            cmd = self._mock_cmd("wheel node + guard")
        else:
            cmd = ["bash", "-c", ROS_SETUP + f'exec bash "{self.so101}/run_base_teleop.sh" --inner']
        return self._start("base", JobSpec(cmd, uses, cwd=str(self.so101) if not self.mock else None,
                                           label="base", stop_wait=8.0), who)

    # --------------------------------------------------------------- teleop
    def teleop(self, side: str, on: bool, who: str) -> tuple[bool, str]:
        if side not in SIDES:
            return False, "Unknown arm."
        name = f"teleop_{side}"
        if not on:
            return self._stop_job(name, who)
        why = self._arm_ok(side)
        if why:
            return False, why
        keys = [f"{s}_{r}" for s in _sides(side) for r in ("follower", "leader")]
        missing = self._missing(keys)
        if missing:
            return False, "Not connected: " + ", ".join(missing)
        bin_ = str(self.venv / "bin" / "lerobot-teleoperate")
        if side == "both":
            args = ["--robot.type=bi_so_follower", "--robot.id=so101",
                    f"--robot.left_arm_config.port={PORT['left_follower']}", f"--robot.right_arm_config.port={PORT['right_follower']}",
                    "--teleop.type=bi_so_leader", "--teleop.id=so101",
                    f"--teleop.left_arm_config.port={PORT['left_leader']}", f"--teleop.right_arm_config.port={PORT['right_leader']}"]
        else:
            args = ["--robot.type=so101_follower", f"--robot.port={PORT[side + '_follower']}", f"--robot.id={side}_follower",
                    "--teleop.type=so101_leader", f"--teleop.port={PORT[side + '_leader']}", f"--teleop.id={side}_leader"]
        cmd = self._mock_cmd(f"teleop {side}") if self.mock else [bin_, *args]
        uses = frozenset(PORT[k] for k in keys)
        return self._start(name, JobSpec(cmd, uses, cwd=str(self.so101) if not self.mock else None,
                                         env=self._venv_env(), label=f"teleop {side}", stop_wait=8.0), who)

    # ------------------------------------------------------------ arm poses
    def arm(self, side: str, action: str, who: str) -> tuple[bool, str]:
        if side not in SIDES or action not in ARM_ACTIONS:
            return False, "Unknown arm action."
        if action != "release":  # releasing torque never moves anything by itself
            why = self._arm_ok(side)
            if why:
                return False, why
        keys = [f"{s}_follower" for s in _sides(side)]
        missing = self._missing(keys)
        if missing:
            return False, "Not connected: " + ", ".join(missing)
        if self.mock:
            cmd = self._mock_cmd(f"{action} {side} arm", seconds=3)
        else:
            cmd = [str(self.venv / "bin" / "python"), str(self.so101 / "arm_tuck.py"), ARM_ACTIONS[action], "--arm", side, "--yes"]
        uses = frozenset(PORT[k] for k in keys)
        return self._start(f"arm_{side}", JobSpec(cmd, uses, cwd=str(self.so101) if not self.mock else None,
                                                  env=self._venv_env(), label=f"{action} {side} arm"), who)

    # --------------------------------------------------------------- skills
    def models(self) -> list[dict]:
        root = Path(self.cfg.policies_dir).expanduser()
        out = []
        for cfgf in sorted(root.glob("*/pretrained_model/config.json")):
            d = cfgf.parent
            if not (d / "model.safetensors").exists():
                continue
            try:
                c = json.loads(cfgf.read_text())
                cams = sorted(k.split(".")[-1] for k in c.get("input_features", {}) if k.startswith("observation.images."))
                kind = c.get("type", "?")
            except (OSError, ValueError):
                cams, kind = [], "?"
            out.append({"name": d.parent.name, "path": str(d), "type": kind, "cams": cams})
        return out

    def skill(self, name: str, on: bool, who: str) -> tuple[bool, str]:
        if not on:
            return self._stop_job("skill", who)
        m = next((m for m in self.models() if m["name"] == name), None)
        if m is None:
            return False, "Model not found."
        missing = self._missing(["right_follower"])
        if missing:
            return False, "Not connected: right follower"
        cams = m["cams"] or ["top", "right_wrist"]
        uses = frozenset({PORT["right_follower"], *(f"cam:{c}" for c in cams)})
        secs = str(int(self.cfg.skill_seconds))
        cmd = (self._mock_cmd(f"skill {name}", seconds=int(secs)) if self.mock
               else ["bash", str(self.so101 / "run_policy.sh"), "run", m["path"], secs])
        return self._start("skill", JobSpec(cmd, uses, cwd=str(self.so101) if not self.mock else None,
                                            env=self._venv_env(), label=f"skill {name}", stop_wait=8.0), who)

    # ---------------------------------------------------------------- voice
    def voice(self, on: bool, who: str, lang: str = "ko") -> tuple[bool, str]:
        if not on:
            return self._stop_job("voice", who)
        lang = "en" if lang == "en" else "ko"
        model = Path("~/models/vosk").expanduser() / VOICE_MODELS[lang]
        if not self.mock and not model.is_dir():
            return False, f"Speech model not installed: {model}"
        if self.mock:
            cmd = self._mock_cmd(f"voice ({lang}, push-to-talk)")
        else:
            cmd = ["bash", "-c", ROS_SETUP + f"exec ros2 run so101_base voice_cmd --ros-args {VOICE_ARGS} "
                                             f"-p lang:={lang} -p model_path:={model}"]
        self.voice_lang = lang
        return self._start("voice", JobSpec(cmd, frozenset({"mic"}), label=f"voice {lang}"), who)

    def talk(self, on: bool) -> tuple[bool, str]:
        if not self.jobs.running("voice"):
            return False, "Voice is off."
        if self.ros is not None and hasattr(self.ros, "publish_str"):
            self.ros.publish_str("/voice/listen", "start:4" if on else "stop")
        return True, ""

    def voice_text(self) -> dict:
        if self.ros is not None and hasattr(self.ros, "latest_str"):
            return {"text": self.ros.latest_str("/voice/text"), "status": self.ros.latest_str("/voice/status"),
                    "guard": self.ros.latest_str("/obstacle_guard/status")}
        return {}

    def voice_models(self) -> list[str]:
        root = Path("~/models/vosk").expanduser()
        return sorted(p.name for p in root.glob("vosk-model*")) if root.exists() else []

    # --------------------------------------------------------------- E-STOP
    def on_estop(self, who: str) -> None:
        """Stop everything that can move by itself. Teleop stays: a person holds the leader arm."""
        if self.ros is not None and hasattr(self.ros, "publish_str") and self.jobs.running("voice"):
            self.ros.publish_str("/voice/say", "멈춰")
        stopped = self.jobs.stop_all(lambda j: j.name in ("skill", "voice"))
        if stopped:
            self.event(who, "E-STOP stopped " + ", ".join(stopped))

    # --------------------------------------------------------------- system
    def system(self, action: str, who: str) -> tuple[bool, str]:
        if action not in ("shutdown", "reboot"):
            return False, "Unknown action."
        if self.mock:
            return False, "Mock mode: the Jetson is not shut down."
        verb = "poweroff" if action == "shutdown" else "reboot"
        self.jobs.stop_all()
        self.event(who, action)
        for cmd in (["sudo", "-n", "/usr/bin/systemctl", verb], ["systemctl", verb]):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return True, f"{action} started."
        return False, "Not allowed. See deploy/README.md (shutdown permission)."

    # --------------------------------------------------------------- health
    def _jetson(self) -> dict:
        out: dict = {}
        temps = []
        for z in Path("/sys/class/thermal").glob("thermal_zone*"):
            try:
                kind = (z / "type").read_text().strip()
                v = int((z / "temp").read_text()) / 1000.0
                if -40 < v < 150:
                    temps.append((kind, v))
            except (OSError, ValueError):
                pass
        if temps:
            cpu = [v for k, v in temps if "cpu" in k.lower()]
            out["temp"] = round(max(cpu) if cpu else max(v for _, v in temps), 1)
        try:
            out["load"] = round(os.getloadavg()[0], 2)
            out["cores"] = os.cpu_count()
        except OSError:
            pass
        try:
            mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
            total = int(mem["MemTotal"].split()[0]); avail = int(mem["MemAvailable"].split()[0])
            out["mem"] = round(100 * (total - avail) / total)
        except (OSError, KeyError, ValueError):
            pass
        du = shutil.disk_usage(Path.home())
        out["disk"] = round(100 * du.used / du.total)
        out["disk_free_gb"] = round(du.free / 1e9, 1)
        return out

    def _boards(self) -> dict:
        if self.mock:
            return {}
        skip = [k for k, dev in PORT.items() if self.jobs.busy(frozenset({dev}))]
        with self.bus_lock:
            try:
                r = subprocess.run([sys.executable, str(HERE / "servo_probe.py"), "--skip", ",".join(skip) or "-"],
                                   capture_output=True, text=True, timeout=8)
                return json.loads(r.stdout or "{}").get("boards", {})
            except (subprocess.TimeoutExpired, ValueError) as e:
                return {"_error": str(e)[:80]}

    def _health_loop(self) -> None:
        n = 0
        while not self._stop.is_set():
            try:
                h = {"jetson": self._jetson(), "t": time.time()}
                h["boards"] = self._boards() if n % 2 == 0 else self.health.get("boards", {})
                self.health = h
                if n % 10 == 0:
                    self.refresh_cameras()
            except Exception as e:  # noqa: BLE001 - health must never kill the server
                log.warning("health: %s", e)
            n += 1
            self._stop.wait(self.cfg.health_period)

    # ---------------------------------------------------------------- state
    def snapshot(self) -> dict:
        jobs = {}
        for name, info in self.jobs.info().items():
            info["tail"] = [ANSI.sub("", s).strip() for s in info["tail"] if ANSI.sub("", s).strip()][-8:]
            jobs[name] = info
        cams = []
        for c in self.cam_info:
            cam = self.cams.get(c["key"])
            cams.append({"key": c["key"], "present": bool(c.get("present")), "reason": c.get("reason"),
                         **({"live": cam.info()["live"], "paused_by": cam.paused_by, "error": cam.error} if cam else {})})
        return {
            "t": "robot", "mock": self.mock, "jobs": jobs, "health": self.health, "cams": cams,
            "models": self.models(), "voice": self.voice_text(), "voice_models": self.voice_models(), "voice_lang": self.voice_lang,
            "left_arm_ok": self.cfg.left_arm_ok, "lidar": os.path.exists("/dev/rplidar"),
            "events": list(self.events)[-20:],
        }

    def close(self) -> None:
        self._stop.set()
        self.jobs.stop_all()
        for cam in self.cams.values():
            cam.pause("shutdown")
