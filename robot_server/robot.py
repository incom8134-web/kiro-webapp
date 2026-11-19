"""Everything beyond the wheels: arms, teleop, skills, voice, health, shutdown.

Nothing here re-implements robot logic. Each action starts a program from the
old code folder (KIRO_SO101_DIR, default ~/so101) exactly as the old Tkinter
app did, with stdin closed and output captured for the page:

  base      run_base_teleop.sh --inner      wheel node + lidar guard + robot gamepad
  teleop    lerobot-teleoperate             leader arm -> follower arm
  fold/...  arm_tuck.py --tuck|--hold|--release
  skill     run_policy.sh run <model> <s>   trained ACT model on the right arm
  voice     ros2 run so101_base voice_cmd   push-to-talk voice driving (Korean or English)
  servo     so101_servo_tester.Bus          scan / torque / move / change ID (servo_tool.py)
  wizard    hw_test_wizard.Wizard.t_<step>  one step of the 13-step check (wizard_runner.py)
  bridge    twin_bridge.py                  follower arm driven by target angles: the leader arm on the
                                            laptop (Web Serial) and the wave. Limits, speed ramp, deadman

In mock mode the same buttons start harmless stand-in commands.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import socket
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
# Lidar guard distances (m), the two profiles of the old base panel. Passed to run_base_teleop.sh.
GUARD_PROFILES = {"close": {"STOP_DIST": "0.12", "SLOW_DIST": "0.12", "SIDE_STOP": "0.15"},
                  "safe": {"STOP_DIST": "0.20", "SLOW_DIST": "0.30", "SIDE_STOP": "0.32"}}
# Devices each check-wizard step opens (so it never shares a port or camera with a running job)
WIZARD_USES = {
    "devices": set(), "servos": {"/dev/so101_right_follower", "/dev/so101_left_follower", "/dev/so101_right_leader",
                                 "/dev/so101_left_leader", "/dev/so101_base"},
    "calib": {"/dev/so101_right_follower", "/dev/so101_left_follower", "/dev/so101_right_leader", "/dev/so101_left_leader"},
    "teleop_left": {"/dev/so101_left_follower", "/dev/so101_left_leader"},
    "teleop_right": {"/dev/so101_right_follower", "/dev/so101_right_leader"},
    "teleop_both": {"/dev/so101_right_follower", "/dev/so101_left_follower", "/dev/so101_right_leader", "/dev/so101_left_leader"},
    "camera_uvc": {"cam:top"}, "gamepad": {"/dev/input/js0"}, "wheels": {"/dev/so101_base"}, "mecanum_floor": {"/dev/so101_base"},
    "ros_base": {"/dev/so101_base"}, "ros_camera": {"cam:top"}, "lidar": {"/dev/rplidar"},
}
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]  # servo IDs 1..6
BRIDGE_ADDR = ("127.0.0.1", 5558)  # twin_bridge listens here, only for this server
HF_CALIB = Path(os.environ.get("HF_LEROBOT_HOME", "~/.cache/huggingface/lerobot")).expanduser() / "calibration"


def leader_to_target(cal: dict, raw: list) -> dict:
    """Leader servo positions (raw ticks, IDs 1..6) -> twin_bridge target, the same maths as LeRobot
    (motors_bus._normalize): joints in degrees, 0 = middle of the calibrated range; gripper 0..100 %."""
    out = {}
    for i, j in enumerate(JOINTS):
        c, v = cal[j], float(raw[i])
        lo, hi = c["range_min"], c["range_max"]
        if j == "gripper":
            n = (min(hi, max(lo, v)) - lo) / (hi - lo) * 100.0
            out[j] = round(100.0 - n if c.get("drive_mode") else n, 2)
        else:
            out[j] = round((v - (lo + hi) / 2.0) * 360.0 / 4095.0, 2)
    return out


SERIAL_RE = re.compile(r"^/dev/(so101_[a-z_]+|ttyACM\d+|ttyUSB\d+)$")
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
        self.servo_scan: dict | None = None
        self._wizard_steps: list[dict] | None = None
        self._wizard_running: str | None = None
        self.wizard_results: dict[str, dict] = {}
        self.bridge_side: str | None = None
        self.bridge_mode: str | None = None
        self.leader_cal: dict | None = None
        self.leader_rx, self.leader_last, self.leader_deg = 0, 0.0, None
        self._seq = 0
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._wave_stop = threading.Event()
        self._wave_thread: threading.Thread | None = None
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
                wrist = key != "top"  # hand cameras: smaller and slower, MJPG so two webcams fit on USB 2
                cam = Camera(key, dev, c.get("fourcc") or ("MJPG" if wrist else None),
                             size=(320, 240) if wrist else (480, 360), fps=8.0 if wrist else 10.0)
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
    def obstacle_stop_on(self) -> bool:
        """The old config/lidar.env can switch the obstacle stop off (OBSTACLE_STOP=0)."""
        try:
            for line in (self.so101 / "config" / "lidar.env").read_text().splitlines():
                k, _, v = line.partition("=")
                if k.strip() == "OBSTACLE_STOP":
                    return v.split("#")[0].strip() != "0"
        except OSError:
            pass
        return True

    def base(self, on: bool, who: str, profile: str = "close") -> tuple[bool, str]:
        if not on:
            return self._stop_job("base", who)
        if self._missing(["base"]):
            return False, "Wheel board /dev/so101_base not found. Check USB and 12 V power."
        uses = frozenset({PORT["base"], "/dev/rplidar"})
        if self.mock:
            cmd = self._mock_cmd("wheel node + guard")
        else:
            cmd = ["bash", "-c", ROS_SETUP + f'exec bash "{self.so101}/run_base_teleop.sh" --inner']
        prof = profile if profile in GUARD_PROFILES else "close"
        return self._start("base", JobSpec(cmd, uses, cwd=str(self.so101) if not self.mock else None,
                                           env=dict(GUARD_PROFILES[prof]), label=f"base ({prof})", stop_wait=8.0), who)

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

    def say(self, text: str, who: str) -> tuple[bool, str]:
        """Quick commands and typed commands go to the old voice node (/voice/say), which drives
        with odometry and the lidar guard exactly as for spoken commands."""
        text = " ".join(str(text).split())[:80]
        if not text:
            return False, "Empty command."
        if not self.jobs.running("voice"):
            return False, "Start voice first: quick commands use the voice program."
        if self.ros is not None and hasattr(self.ros, "publish_str"):
            self.ros.publish_str("/voice/say", text)
        self.event(who, f"command: {text}")
        return True, ""

    # ------------------------------------------------------- servo tester
    def serial_ports(self) -> list[dict]:
        from .servo_tool import ports
        return ports()

    def servo(self, cmd: str, port: str, who: str, sid: int = 0, value: int = 0,
              lo: int = 1, hi: int = 12, arm_ok: bool = False) -> tuple[bool, str]:
        if cmd not in ("scan", "torque", "move", "setid"):
            return False, "Unknown servo command."
        if not SERIAL_RE.match(port or ""):
            return False, "Unknown port."
        if self.mock:
            return False, "Mock mode: no servo boards."
        writes = cmd != "scan"
        is_arm = "so101_" in port and "base" not in port or any(
            p["arm"] and p["real"] == os.path.realpath(port) for p in self.serial_ports())
        if writes and is_arm and not arm_ok:
            return False, "Arm ports are read-only. Tick 'Allow control on arm ports' first."
        busy = self.jobs.busy(frozenset({port, os.path.realpath(port)}))
        if busy:
            return False, f"{busy} is using {port}. Stop it first."
        args = {"scan": [str(int(lo)), str(int(hi))], "torque": [str(int(sid)), "1" if value else "0"],
                "move": [str(int(sid)), str(int(value))], "setid": [str(int(sid)), str(int(value))]}[cmd]
        with self.bus_lock:
            try:
                r = subprocess.run([sys.executable, str(HERE / "servo_tool.py"), cmd, port, *args], capture_output=True,
                                   text=True, timeout=25, env={**os.environ, "KIRO_SO101_DIR": str(self.so101)})
            except subprocess.TimeoutExpired:
                return False, "The servo board did not answer in time."
        line = next((x for x in reversed(r.stdout.splitlines()) if x.startswith("@SERVO ")), None)
        if line is None:
            return False, (r.stderr.strip().splitlines() or ["servo tool failed"])[-1][:160]
        res = json.loads(line[7:])
        if cmd == "scan" and res.get("ok"):
            self.servo_scan = {**res, "t": time.time()}
        if writes:
            self.event(who, f"servo {cmd} {port} {' '.join(args)}: {res.get('msg', '')}")
        return bool(res.get("ok")), res.get("msg", f"{len(res.get('servos', []))} servos answered")

    # ------------------------------------------------------- check wizard
    def wizard_steps(self) -> list[dict]:
        if self._wizard_steps is None:
            try:
                r = subprocess.run([str(self.venv / "bin" / "python"), str(HERE / "wizard_runner.py"), "--list"],
                                   capture_output=True, text=True, timeout=30,
                                   env={**os.environ, "KIRO_SO101_DIR": str(self.so101), "PYTHONPATH": ""})
                self._wizard_steps = json.loads(r.stdout)
            except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                log.warning("wizard steps: %s", e)
                return []
        return self._wizard_steps

    def wizard(self, step: str, on: bool, who: str, seconds: int = 20) -> tuple[bool, str]:
        if not on:
            return self._stop_job("wizard", who)
        if step not in WIZARD_USES:
            return False, "Unknown step."
        if step in ("teleop_left", "teleop_both") and not self.cfg.left_arm_ok:
            return False, "LEFT_BLOCKED"
        seconds = max(5, min(120, int(seconds)))
        if self.mock:
            cmd = self._mock_cmd(f"wizard step {step}", seconds=3)
        else:
            cmd = [str(self.venv / "bin" / "python"), str(HERE / "wizard_runner.py"), step, str(seconds)]
        self.wizard_results[step] = {"status": "running", "t": time.time()}
        ok, msg = self._start("wizard", JobSpec(cmd, frozenset(WIZARD_USES[step]), cwd=str(self.so101) if not self.mock else None,
                                                env={**self._venv_env(), "KIRO_SO101_DIR": str(self.so101)},
                                                label=f"check {step}", stop_wait=12.0), who)
        if ok:
            self._wizard_running = step
        else:
            self.wizard_results.pop(step, None)
        return ok, msg

    def wizard_decide(self, step: str, status: str, who: str) -> tuple[bool, str]:
        if status not in ("pass", "fail", "skip") or step not in WIZARD_USES:
            return False, "Unknown decision."
        if self.jobs.running("wizard") and self._wizard_running == step:
            return False, "Wait until the step has finished."
        r = self.wizard_results.setdefault(step, {})
        r.update(status=status, decided_by=who, t=time.time())
        self.event(who, f"check {step}: {status}")
        return True, ""

    def _wizard_collect(self) -> None:
        """When a wizard step ends, read its @RESULT line (the old step's own verdict)."""
        step = self._wizard_running
        j = self.jobs.get("wizard")
        if not step or j is None or j.running:
            return
        self._wizard_running = None
        res = {"status": "stopped" if j.code else "fail", "auto": None}
        for line in reversed(j.lines):
            if line.startswith("@RESULT "):
                try:
                    res = json.loads(line[8:])
                except ValueError:
                    pass
                break
        if self.mock:
            res = {"status": "confirm", "auto": None}
        res["lines"] = [ANSI.sub("", x) for x in list(j.lines)[-60:] if not x.startswith("@RESULT ")]
        res["t"] = time.time()
        self.wizard_results[step] = res

    # ------------------------------------------- leader arm on the laptop, wave
    def _bridge_start(self, side: str, mode: str, who: str) -> tuple[bool, str]:
        if side not in ("right", "left"):
            return False, "Unknown arm."
        if self.jobs.running("bridge"):
            if self.bridge_side == side and self.bridge_mode == mode:
                return True, ""
            return False, "The arm is busy (leader arm or wave). Stop it first."
        why = self._arm_ok(side)
        if why:
            return False, why
        missing = self._missing([f"{side}_follower"])
        if missing:
            return False, "Not connected: " + ", ".join(missing)
        if self.mock:
            cmd = self._mock_cmd(f"twin_bridge {side} ({mode})")
        else:
            cmd = [str(self.venv / "bin" / "python"), str(self.so101 / "twin_bridge.py"), "--arms", side,
                   "--to", "127.0.0.1:5005", "--listen", f"{BRIDGE_ADDR[0]}:{BRIDGE_ADDR[1]}", "--allow-from", "127.0.0.1",
                   "--ramp", "90", "--deadman", "0.5", "--status-json", "--quiet"]
        ok, msg = self._start("bridge", JobSpec(cmd, frozenset({PORT[f"{side}_follower"]}), cwd=str(self.so101) if not self.mock else None,
                                                env=self._venv_env(), label=f"{mode} {side} arm", stop_wait=8.0), who)
        if ok:
            self.bridge_side, self.bridge_mode = side, mode
            self.leader_rx, self.leader_last = 0, 0.0
        return ok, msg

    def _bridge_send(self, side: str, target: dict) -> None:
        self._seq += 1
        pkt = {"cmd": "target", "t": time.time(), "seq": self._seq, "units": "deg", "arms": {side: target}}
        try:
            self._udp.sendto(json.dumps(pkt).encode(), BRIDGE_ADDR)
        except OSError:
            pass

    def _bridge_status(self) -> dict | None:
        """Last '@STATUS {json}' line of the bridge (current joint angles, state)."""
        j = self.jobs.get("bridge")
        if not j:
            return None
        for line in reversed(j.lines):
            if line.startswith("@STATUS "):
                try:
                    return json.loads(line[8:])
                except ValueError:
                    return None
        return None

    def leader(self, on: bool, side: str, who: str) -> tuple[bool, str]:
        if not on:
            if self.bridge_mode == "leader":
                return self._stop_job("bridge", who)
            return True, ""
        f = HF_CALIB / "teleoperators" / "so_leader" / f"{side}_leader.json"
        try:
            self.leader_cal = json.loads(f.read_text())
        except (OSError, ValueError):
            if not self.mock:
                return False, f"Leader calibration not found: {f}"
            self.leader_cal = None
        return self._bridge_start(side, "leader", who)

    def leader_pos(self, side: str, raw: object) -> None:
        """Called about 30 times per second while the leader arm streams. Never blocks."""
        if self.bridge_mode != "leader" or self.bridge_side != side or not self.jobs.running("bridge"):
            return
        if not isinstance(raw, list) or len(raw) != 6 or not all(isinstance(v, (int, float)) and 0 <= v <= 4095 for v in raw):
            return
        self.leader_rx += 1
        self.leader_last = time.monotonic()
        if self.leader_cal is None:  # mock mode without calibration
            return
        self.leader_deg = leader_to_target(self.leader_cal, raw)
        self._bridge_send(side, self.leader_deg)

    def wave(self, side: str, who: str) -> tuple[bool, str]:
        """A small hello from the arm's current pose: wrist waves, gripper opens and closes, back to start."""
        if self._wave_thread and self._wave_thread.is_alive():
            return False, "Already waving."
        ok, msg = self._bridge_start(side, "wave", who)
        if not ok:
            return ok, msg
        self._wave_stop.clear()
        self._wave_thread = threading.Thread(target=self._wave_run, args=(side, who), daemon=True)
        self._wave_thread.start()
        return True, "Waving."

    def _wave_run(self, side: str, who: str) -> None:
        try:
            start = None
            t_end = time.monotonic() + 20  # the bridge needs a few seconds to connect to the arm
            while time.monotonic() < t_end and not self._wave_stop.is_set():
                st = self._bridge_status()
                if self.mock:
                    start = {j: 0.0 for j in JOINTS}
                    break
                if st and st.get("arms", {}).get(side, {}).get("obs"):
                    start = dict(st["arms"][side]["obs"])
                    break
                time.sleep(0.2)
            if start is None:
                self.event(who, "wave: the arm did not answer")
                return
            period, cycles, rate = 1.6, 3, 30.0
            t0 = time.monotonic()
            while not self._wave_stop.is_set():
                t = time.monotonic() - t0
                if t > period * cycles + 1.2:
                    break
                a = math.sin(2 * math.pi * t / period) if t < period * cycles else 0.0
                tgt = dict(start)
                tgt["wrist_flex"] = start["wrist_flex"] + 20.0 * a
                tgt["gripper"] = max(0.0, min(100.0, start["gripper"] + 25.0 * a))
                if not self.mock:
                    self._bridge_send(side, tgt)
                time.sleep(1.0 / rate)
        finally:
            if self.bridge_mode == "wave":
                self.jobs.stop("bridge")  # torque stays on: the arm holds where it is

    # --------------------------------------------------------------- E-STOP
    def on_estop(self, who: str) -> None:
        """Stop everything that can move by itself. Teleop stays: a person holds the leader arm."""
        if self.ros is not None and hasattr(self.ros, "publish_str") and self.jobs.running("voice"):
            self.ros.publish_str("/voice/say", "멈춰")
        self._wave_stop.set()
        stopped = self.jobs.stop_all(lambda j: j.name in ("skill", "voice", "wizard", "bridge"))
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
        self._wizard_collect()
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
            "obstacle_stop": self.obstacle_stop_on(), "ports": [] if self.mock else self.serial_ports(),
            "servo_scan": self.servo_scan, "wizard": self.wizard_results,
            "bridge": {"running": self.jobs.running("bridge"), "side": self.bridge_side, "mode": self.bridge_mode,
                       "state": (self._bridge_status() or {}).get("state"), "rx": self.leader_rx,
                       "fresh": time.monotonic() - self.leader_last < 0.5 if self.leader_last else False},
        }

    def close(self) -> None:
        self._stop.set()
        self._wave_stop.set()
        stopped = self.jobs.stop_all()
        log.info("server stopping; stopped: %s", ", ".join(stopped) or "nothing running")
        for cam in self.cams.values():
            cam.pause("shutdown")
