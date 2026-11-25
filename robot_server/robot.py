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

from .camera import Camera, find_cameras, repair_top
from .config import Config
from .leadermath import JOINTS, leader_to_target
from .jobs import Jobs, JobSpec
from .lidar_setup import LidarSetup, Result, bin_start, env_get, env_update, mask_str, parse_mask
from .recorder import Recorder
from .stopmeter import StopMeter, csv_lines
from .training import Training
from .colab import ColabLink

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
# The old guard's two 360-degree "stop everything" rules are off since 20 Nov 2026 (user's choice):
# "contact" (TOUCH_DIST, a point this close to the lidar) and "covered" (COVERED_RATIO, too few readings in a
# direction). Parts of this robot sit right next to the lidar and set them off all the time. The distance
# stop in the driving direction (STOP_DIST / SIDE_STOP, and the web server's own guard.py) stays on.
GUARD_PROFILES = {"close": {"STOP_DIST": "0.12", "SLOW_DIST": "0.12", "SIDE_STOP": "0.15", "TOUCH_DIST": "0", "COVERED_RATIO": "0"},
                  "safe": {"STOP_DIST": "0.20", "SLOW_DIST": "0.30", "SIDE_STOP": "0.32", "TOUCH_DIST": "0", "COVERED_RATIO": "0"}}
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
ENGINEER_JOBS = ("record", "pack", "wizard")  # their output is shown only on the Studio page
ARM_JOBS = ("relay", "skill", "demo", "bridge", "arm_right", "arm_left", "arm_both")  # programs that move a follower arm by themselves (relay feeds the bridge)
# Follower arms copying a leader held by a person; a recording (Studio) is teleop plus cameras
TELEOP_JOBS = ("teleop_right", "teleop_left", "teleop_both", "record")
BRIDGE_ADDR = ("127.0.0.1", 5558)  # twin_bridge listens here, only for this server
JOINTS_ADDR = ("127.0.0.1", 5005)  # twin_bridge sends the arms' measured joint angles here, 30 times a second
HF_CALIB = Path(os.environ.get("HF_LEROBOT_HOME", "~/.cache/huggingface/lerobot")).expanduser() / "calibration"


def cpu_percent(prev: dict[str, list[int]], cur: dict[str, list[int]]) -> dict[str, float]:
    """Busy % per CPU between two /proc/stat readings ({"cpu0": [user, nice, system, idle, iowait, ...]})."""
    out = {}
    for k, c in cur.items():
        p = prev.get(k)
        if not p:
            continue
        total = sum(c) - sum(p)
        idle = (c[3] + c[4]) - (p[3] + p[4])
        out[k] = round(100.0 * (total - idle) / total, 1) if total > 0 else 0.0
    return out


SERIAL_RE = re.compile(r"^/dev/(so101_[a-z_]+|ttyACM\d+|ttyUSB\d+)$")
VOICE_MODELS = {"ko": "vosk-model-small-ko-0.22", "en": "vosk-model-small-en-us-0.15"}
# Tagged lines of the voice node worth showing on the page (heard, executed, done, stopped, ignored...)
VOICE_TAG = re.compile(r"\[(들림|실행|완료|정지|중단|무시|취소|비슷한 문구|주의|오류|이어붙임)\].*")
# Voice listens all the time while it is on (tap on / tap off on the page, 23 Nov 2026: no holding a button).
# Each sentence is judged on its own and only an exact command phrase moves the robot.
VOICE_ARGS = ("-p ptt:=false -p grammar_mode:=phrases -p output_topic:=/cmd_vel_raw -p lin_speed:=0.10 -p min_conf:=0.6")
# Steps per voice command. "big" (default since 23 Nov 2026): "forward" drives slowly (0.10 m/s) until "stop",
# at most 1 m (10 s) when no "stop" is heard. "test" 0.2 m / 45 degrees is what the voice test form asks for (4.1).
VOICE_STEPS = {"big": "-p default_dist:=1.0 -p max_dist:=1.0 -p default_angle:=90.0",
               "test": "-p default_dist:=0.2 -p max_dist:=0.2 -p default_angle:=45.0"}
# The phone's or laptop's microphone: the page streams 16 kHz mono 16-bit sound over its WebSocket,
# the server passes it to the old voice node here (voice_cmd device:=udp:<port>)
VOICE_UDP = ("127.0.0.1", 5559)
VOICE_STOP_WORD = {"ko": "멈춰", "en": "stop"}


# Lines of a program that lost its USB serial board (LeRobot / Feetech / pyserial messages)
USB_LOST = re.compile(r"could not open port|No such file or directory: '/dev/(so101|tty)|Input/output error|SerialException"
                      r"|device reports readiness to read but returned no data|There is no status packet|Failed to (sync )?(read|write)"
                      r"|ConnectionError|응답 없는 서보", re.I)


def end_reason(info: dict, lines: list[str]) -> str | None:
    """Why a program ended, for the page: None (running, or ended normally), STOPPED (the server stopped it),
    USB_LOST (its board went away or stopped answering), KILLED (a signal from outside, e.g. SIGKILL:
    an arm keeps its torque and last pose) or ERROR (it failed; its last lines tell why)."""
    if info.get("running") or info.get("code") is None:
        return None
    if info.get("asked"):
        return "STOPPED"
    if any(USB_LOST.search(line) for line in lines[-30:]):
        return "USB_LOST"
    code = info["code"]
    if code < 0:
        return "KILLED"
    return "ERROR" if code > 0 else None


def leader_cal_problems(cal: dict) -> list[str]:
    """Joints of a leader calibration that cannot be right: a range of a few steps (the joint was not moved
    while calibrating; 24 Nov 2026: the left leader's gripper had 6 steps and snapped open), or a shoulder /
    elbow range across the whole 0..4095 circle (it crossed the sensor's wrap-around point)."""
    bad = []
    for j in JOINTS:
        lo, hi = cal[j]["range_min"], cal[j]["range_max"]
        if hi - lo < 300 or (j in ("shoulder_lift", "elbow_flex") and hi - lo > 4000):
            bad.append(j)
    return bad


def _sides(side: str) -> list[str]:
    return ["right", "left"] if side == "both" else [side]


class Robot:
    def __init__(self, cfg: Config, ros=None):
        self.cfg = cfg
        self.mock = cfg.backend == "mock" or ros is None or getattr(ros, "name", "") != "ros"
        self.ros = ros
        self.so101 = Path(cfg.so101_dir).expanduser()
        self.venv = Path(cfg.lerobot_venv).expanduser()
        log_dir = Path(cfg.log_dir).expanduser() if cfg.log_dir else None if self.mock else self.so101 / "logs"
        self.jobs = Jobs(log_dir=log_dir)
        self.events: deque[dict] = deque(maxlen=60)
        self.health: dict = {"jetson": {}, "boards": {}, "t": 0}
        self.bus_lock = threading.Lock()  # the servo probe and arm jobs never open a port at the same time
        self.cams: dict[str, Camera] = {}
        self.cam_info: list[dict] = []
        self._stop = threading.Event()
        self.voice_lang = "ko"
        self.voice_step = "big"  # VOICE_STEPS: "big" (default) or "test" (0.2 m / 45 degrees, the voice test form)
        self.voice_mic = "device"  # "device" = the operator's phone / laptop (sound from the page), "robot" = USB mic
        self.voice_rx_t = 0.0  # when the last piece of sound from the page arrived
        self._skill_stop = threading.Event()  # set = the series of skill runs ends (Stop, E-STOP)
        self._skill_thread: threading.Thread | None = None
        self.skill_series: dict | None = None  # {"name", "run", "of", "pause", "waiting"} while a series plays
        self.voice_log: deque[dict] = deque(maxlen=12)  # the voice node's last tagged lines, with their time
        self.servo_scan: dict | None = None
        self._wizard_steps: list[dict] | None = None
        self._wizard_running: str | None = None
        self.wizard_results: dict[str, dict] = {}
        self.bridge_side: str | None = None
        self.bridge_mode: str | None = None
        self.leader_cals: dict = {}  # leader calibration per side ("right", "left") of the laptop leader arms
        self.leader_rx, self.leader_last, self.leader_deg = 0, 0.0, None
        self._seq = 0
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._wave_stop = threading.Event()
        self._wave_thread: threading.Thread | None = None
        self._estop_lock = threading.Lock()
        self._fold_cancel = threading.Event()  # set = the pending fold-home after an E-STOP is cancelled
        self._fold_thread: threading.Thread | None = None
        self._fold_jobs: set[str] = set()
        self._cpu_prev: dict = {}
        self._net_prev: tuple | None = None
        self.guard_profile = "close"  # lidar guard distance of the base program; set by the engineer (Studio page)
        # Stop meter for the test reports: the base (cm) and the arms driven through the bridge (degrees)
        self.base_meter = StopMeter("base")
        self.arm_meter = StopMeter("arm", unit_scale=1.0, still_speed=2.0, move_speed=5.0, norm="max")
        self.joints: tuple[float, tuple] | None = None  # (time, measured joint angles) from the bridge
        self._joints_csv = None
        self._link_flowing = False
        threading.Thread(target=self._joints_loop, name="joints", daemon=True).start()
        self.lidar_setup = LidarSetup(self._lidar_grab, self._lidar_save)
        self._lidar_restart_t = 0.0  # last time the wheel program was restarted because the lidar went quiet
        self.refresh_cameras()
        self.rec = Recorder(self)
        self.train = Training(self)
        self.colab = ColabLink(self)
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

    def cam_repair(self, who: str) -> tuple[bool, str]:
        """Studio "Repair top camera" (test R6): the top camera's colour node is re-plugged in software through
        the root helper (deploy/README.md 6b). Reply "<CODE>: <detail>"; the page shows a text per code."""
        if self.mock:  # the pretend robot never re-plugs a real USB device
            return True, "OK_ALREADY"
        ok, code, detail = repair_top(self.so101)
        self.event(who, f"camera repair: {code} {detail}".strip())
        if ok:
            self.refresh_cameras()  # the camera's video devices are new after a re-plug
        return ok, code if ok or not detail else f"{code}: {detail}"

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
            if self._stop.is_set():  # the server is shutting down: nothing new may start
                return False, "Server is stopping."
            ok, msg = self.jobs.start(name, spec)
        finally:
            if serial:
                self.bus_lock.release()
        if ok:
            self.event(who, f"started {spec.label or name}")
        return ok, msg

    def _timed_stop(self, name: str, who: str) -> tuple[bool, str]:
        """Stop a program and note how long it took to end (tests E3 and E6: within 2 s)."""
        running = self.jobs.running(name)
        t0 = time.monotonic()
        ok, msg = self._stop_job(name, who)
        if running and ok:
            meter = self.base_meter if name == "base" else self.arm_meter
            meter.add_done("base_stop" if name == "base" else "teleop_stop", f"{name} ended after Stop", time.monotonic() - t0)
        return ok, msg

    def _stop_job(self, name: str, who: str) -> tuple[bool, str]:
        ok, msg = self.jobs.stop(name)
        if ok:
            self.event(who, f"stopped {name}")
        return ok, msg

    # ----------------------------------------------------------------- base
    def base_wheels(self) -> str | None:
        """From the base log: 'ok' when the wheel node is ready, 'off' when the wheels did not answer
        (12 V off: the wheel node dies but the rest of the launch keeps running), None if unknown."""
        j = self.jobs.get("base")
        if not j or not j.running:
            return None
        state = None
        for line in j.output():
            if "휠 서보 응답 0/4" in line or "응답 없는 서보" in line or ("mecanum_base_node" in line and "process has died" in line):
                state = "off"
            elif "베이스 준비" in line:
                state = "ok"
        return state

    def base_guard(self) -> str | None:
        """From the base log: 'on' = the old lidar guard runs with the lidar, 'pass' = it started without the
        lidar (no obstacle stop at all: OBSTACLE_STOP=0 or the lidar was missing), None if unknown."""
        j = self.jobs.get("base")
        if not j or not j.running:
            return None
        state = None
        for line in j.output():
            if "라이다 감지" in line:
                state = "on"
            elif "라이다 없음" in line:
                state = "pass"
        return state

    @staticmethod
    def _wait_for(path: str, seconds: float) -> bool:
        """A USB device that is being re-plugged comes back after a moment."""
        end = time.monotonic() + seconds
        while not os.path.exists(path):
            if time.monotonic() > end:
                return False
            time.sleep(0.2)
        return True

    @staticmethod
    def usb_mic(cards: str = "/proc/asound/cards") -> bool:
        """A USB microphone shows up as a USB-Audio sound card (the Jetson's own cards are HDA and APE)."""
        try:
            return "USB-Audio" in Path(cards).read_text()
        except OSError:
            return False

    @property
    def lidar_env(self) -> Path:
        """~/so101/config/lidar.env on the robot; a file of its own in mock mode (never the robot's)."""
        return self.rec.root.parent / "mock_lidar.env" if self.mock else self.cfg.lidar_env

    def _lidar_env_text(self) -> str:
        try:
            return self.lidar_env.read_text(encoding="utf-8")
        except OSError:
            return ""

    def set_obstacle_stop(self, on: bool, who: str) -> tuple[bool, str]:
        """The Drive tab's obstacle stop switch, for the robot's own lidar guard (gamepad and voice too):
        OBSTACLE_STOP in lidar.env. The wheel program reads it when it starts, so it is restarted if it runs
        (nothing moves meanwhile)."""
        text = self._lidar_env_text()
        if self.obstacle_stop_on() == on:
            return True, ""
        try:
            self.lidar_env.parent.mkdir(parents=True, exist_ok=True)
            self.lidar_env.write_text(env_update(text, {"OBSTACLE_STOP": "1" if on else "0"},
                                                 f"{time.strftime('%Y-%m-%d %H:%M')} Drive tab switch"), encoding="utf-8")
        except OSError as e:
            return False, f"Could not write {self.lidar_env}: {e}"
        self.event(who, "robot lidar obstacle stop " + ("on" if on else "OFF"))
        if self.jobs.running("base"):
            self._stop_job("base", who)
            ok, msg = self.base(True, who, self.guard_profile)
            return ok, ("" if ok else f"Start the wheels again: {msg}")
        return True, ""

    def obstacle_stop_on(self) -> bool:
        """The old config/lidar.env can switch the obstacle stop off (OBSTACLE_STOP=0)."""
        return env_get(self._lidar_env_text(), "OBSTACLE_STOP") != "0"

    # ---------------------------------------------------------- lidar setup
    # Studio wizard (robot_server/lidar_setup.py): measures where the robot's front is and which close
    # points are the robot itself, then switches the obstacle stop on. Nothing moves.
    def lidar_info(self) -> dict:
        text = self._lidar_env_text()
        age = self.ros.scan_age() if self.ros is not None and hasattr(self.ros, "scan_age") else None
        try:
            yaw = float(env_get(text, "LASER_YAW") or "nan")
        except ValueError:
            yaw = float("nan")
        mask = parse_mask(env_get(text, "LIDAR_MASK"))
        return {"stop_on": env_get(text, "OBSTACLE_STOP") != "0", "yaw_deg": None if math.isnan(yaw) else round(math.degrees(yaw)),
                "mask_bins": len(mask), "mask": [[bin_start(b), r] for b, r in sorted(mask.items())],
                "plugged": self.mock or os.path.exists("/dev/rplidar"),
                "scan_age": None if age is None or math.isinf(age) else round(age, 1), "driver": self.jobs.running("lidar")}

    def lidar(self, cmd: str, who: str) -> tuple[bool, str]:
        ls = self.lidar_setup
        if cmd == "start":
            ok, msg = self._lidar_feed(who)
            if not ok:
                return ok, msg
            self.event(who, "lidar setup started")
            return ls.start()
        if cmd in ("clear", "box"):
            return ls.measure(cmd)
        if cmd == "apply":
            ok, msg = ls.apply()
            if ok:
                self._lidar_stop_feed(who)
            return ok, msg
        if cmd == "cancel":
            self._lidar_stop_feed(who)
            return ls.cancel()
        return False, "Unknown lidar setup step."

    def _lidar_feed(self, who: str) -> tuple[bool, str]:
        """Scans for the setup: from the wheel program if it runs with the lidar, else from the lidar driver alone."""
        if self.mock:
            return True, ""
        if not os.path.exists("/dev/rplidar"):
            return False, "Lidar /dev/rplidar not found. Check its USB cable."
        if self.ros is not None and self.ros.scan_age() < 2.0:
            return True, ""
        if self.jobs.running("base"):
            return False, "The wheel program runs but no lidar scans come in. Stop the wheels, then start the lidar setup again."
        cmd = ["bash", "-c", ROS_SETUP + "exec ros2 run sllidar_ros2 sllidar_node --ros-args -p channel_type:=serial "
               "-p serial_port:=/dev/rplidar -p serial_baudrate:=460800 -p frame_id:=laser -p angle_compensate:=true "
               "-p scan_mode:=Standard"]
        ok, msg = self._start("lidar", JobSpec(cmd, frozenset({"/dev/rplidar"}), label="lidar driver (setup)"), who)
        if not ok:
            return ok, msg
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:  # the C1 spins up in 2-3 s
            if self.ros.scan_age() < 1.0:
                return True, ""
            if not self.jobs.running("lidar"):
                break
            time.sleep(0.2)
        self._lidar_stop_feed(who)
        return False, "The lidar driver did not send scans. Unplug the lidar USB, plug it back in, then try again."

    def _lidar_stop_feed(self, who: str) -> None:
        if self.jobs.running("lidar"):
            self._stop_job("lidar", who)

    def _lidar_grab(self, step: str, n: int, timeout: float):
        if self.ros is None or not hasattr(self.ros, "grab_scans"):
            return None
        return self.ros.grab_scans(step, n, timeout)

    def _lidar_save(self, res: Result) -> tuple[bool, str]:
        """lidar.env: LASER_YAW, LIDAR_MASK and OBSTACLE_STOP=1, after a backup. The web server uses it at once,
        the wheel program from its next start (restarted here if it runs)."""
        path = self.lidar_env
        old = self._lidar_env_text()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if old:
                (path.parent / f"{path.name}.bak_{stamp}").write_text(old, encoding="utf-8")
            path.write_text(env_update(old, {"LASER_YAW": f"{res.yaw:.2f}", "LIDAR_MASK": mask_str(res.mask), "OBSTACLE_STOP": "1"},
                                       f"{time.strftime('%Y-%m-%d %H:%M')} web lidar setup"), encoding="utf-8")
        except OSError as e:
            return False, f"Could not write {path}: {e}"
        if self.ros is not None and hasattr(self.ros, "set_lidar"):
            self.ros.set_lidar(res.yaw, res.mask)
        self.event("lidar setup", f"saved: front = lidar {-math.degrees(res.yaw):+.0f} deg, robot seen in {len(res.mask)} of 72 directions, obstacle stop ON")
        if self.jobs.running("base"):
            self._stop_job("base", "lidar setup")
            ok, msg = self.base(True, "lidar setup", self.guard_profile)
            return True, "Saved. The wheel program restarted with the new values." if ok else f"Saved. Start the wheels again: {msg}"
        return True, "Saved. The obstacle stop is on from the next wheel start."

    def _lidar_watch(self) -> None:
        """The lidar USB sometimes drops and comes back: the driver then stays silent and the old guard stops
        everything ('no scans'). Restart the wheel program, at most once a minute. Nothing moves meanwhile."""
        j = self.jobs.get("base")
        if self.mock or not j or not j.running or self.ros is None or not hasattr(self.ros, "scan_age"):
            return
        if not self.obstacle_stop_on() or not os.path.exists("/dev/rplidar"):
            return
        now = time.time()
        if now - j.started < 20 or now - self._lidar_restart_t < 60:
            return
        age = self.ros.scan_age()
        if age < 5:
            return
        self._lidar_restart_t = now
        self.event("lidar", f"no lidar scans for {'a while' if math.isinf(age) else f'{age:.0f} s'}: restarting the wheel program")
        self._stop_job("base", "lidar watch")
        self.base(True, "lidar watch", self.guard_profile)

    def base(self, on: bool, who: str, profile: str = "close") -> tuple[bool, str]:
        if not on:
            self.base_meter.trigger(time.monotonic(), "base_stop", f"base stopped by {who}")  # test E3
            return self._timed_stop("base", who)
        if self._missing(["base"]):
            return False, "Wheel board /dev/so101_base not found. Check USB and 12 V power."
        if not self.mock and self.obstacle_stop_on() and not self._wait_for("/dev/rplidar", 5.0):
            # Without it the launcher silently starts the guard in pass mode: no obstacle stop at all (test R3)
            return False, "LIDAR_MISSING"
        uses = frozenset({PORT["base"], "/dev/rplidar"})
        if self.mock:
            cmd = self._mock_cmd("wheel node + guard")
        else:
            cmd = ["bash", "-c", ROS_SETUP + f'exec bash "{self.so101}/run_base_teleop.sh" --inner']
        prof = profile if profile in GUARD_PROFILES else "close"
        return self._start("base", JobSpec(cmd, uses, cwd=str(self.so101) if not self.mock else None,
                                           env=self.guard_env(prof), label=f"base ({prof})", stop_wait=8.0), who)

    def guard_env(self, prof: str) -> dict[str, str]:
        """Settings for the old lidar guard. Its 'covered' and 'contact' stops stay off (user's choice, 21 Nov 2026:
        they made the robot refuse to move). With the robot's own parts measured (LIDAR_MASK, Studio lidar setup)
        they can be switched on by hand with STRICT_RULES=1 in lidar.env, after a real test (test E4: a hand
        over the lidar stops everything within 2 scans)."""
        env = dict(GUARD_PROFILES[prof if prof in GUARD_PROFILES else "close"])
        text = self._lidar_env_text()
        if env_get(text, "STRICT_RULES") == "1" and parse_mask(env_get(text, "LIDAR_MASK")):
            env.update(TOUCH_DIST="0.10", COVERED_RATIO="0.4")
        return env

    # --------------------------------------------------------------- teleop
    def teleop(self, side: str, on: bool, who: str) -> tuple[bool, str]:
        if side not in SIDES:
            return False, "Unknown arm."
        name = f"teleop_{side}"
        if not on:
            return self._timed_stop(name, who)
        why = self._arm_ok(side)
        if why:
            return False, why
        keys = [f"{s}_{r}" for s in _sides(side) for r in ("follower", "leader")]
        missing = self._missing(keys)
        if missing:
            return False, "Not connected: " + ", ".join(missing)
        if side == "both":
            args = ["--robot.type=bi_so_follower", "--robot.id=so101",
                    f"--robot.left_arm_config.port={PORT['left_follower']}", f"--robot.right_arm_config.port={PORT['right_follower']}",
                    "--teleop.type=bi_so_leader", "--teleop.id=so101",
                    f"--teleop.left_arm_config.port={PORT['left_leader']}", f"--teleop.right_arm_config.port={PORT['right_leader']}"]
        else:
            args = ["--robot.type=so101_follower", f"--robot.port={PORT[side + '_follower']}", f"--robot.id={side}_follower",
                    "--teleop.type=so101_leader", f"--teleop.port={PORT[side + '_leader']}", f"--teleop.id={side}_leader"]
        # through teleop_runner.py: LeRobot plus reversed joints in degrees (the yellow leader's shoulder)
        cmd = self._mock_cmd(f"teleop {side}") if self.mock else [str(self.venv / "bin" / "python"), str(HERE / "teleop_runner.py"), *args]
        uses = frozenset(PORT[k] for k in keys)
        busy = self.jobs.busy(uses)
        if busy:
            return False, f"{busy} is using the same device. Stop it first."
        # LeRobot switches torque on with whatever target the motor still holds: after a killed program or a
        # Release that is the old pose, and the arm would snap back to it at full speed (tests R1 and R8)
        why = self._sync_goals([PORT[f"{s}_follower"] for s in _sides(side)])
        if why:
            log.warning("%s: %s", who, why)
            return False, why
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
        """Trained models for the Autonomous skills card. arms: "right", "left", "both" or "?" (training.py)."""
        return [{k: m[k] for k in ("name", "path", "type", "cams", "arms", "playable", "block")} for m in self.train.models()]

    def skill(self, name: str, on: bool, who: str, repeat: int = 1, pause: float = 10.0,
              seconds: float | None = None) -> tuple[bool, str]:
        """Play a trained skill: one run of skill_seconds, or `repeat` runs with `pause` seconds between them
        (time to put the object back). Stop, E-STOP or a run that fails ends the whole series."""
        if not on:
            self._skill_stop.set()
            return self._stop_job("skill", who)
        repeat, pause = max(1, min(50, int(repeat))), max(0.0, min(120.0, float(pause)))
        self._skill_secs = int(max(5, min(120, float(seconds)))) if seconds else int(self.cfg.skill_seconds)
        if self._skill_thread and self._skill_thread.is_alive():
            return False, "The skill is already running."
        ok, msg = self._skill_run(name, who)
        if not ok:
            return ok, msg
        self._skill_stop.clear()
        self.skill_series = {"name": name, "run": 1, "of": repeat, "pause": pause, "waiting": False}
        self._skill_thread = threading.Thread(target=self._skill_series, args=(name, who, repeat, pause), daemon=True)
        self._skill_thread.start()
        return True, ""

    def _skill_series(self, name: str, who: str, repeat: int, pause: float) -> None:
        try:
            for run in range(1, repeat + 1):
                if run > 1:
                    self.skill_series.update(run=run, waiting=True)
                    if self._skill_stop.wait(pause):
                        return
                    self.skill_series["waiting"] = False
                    ok, msg = self._skill_run(name, who)
                    if not ok:
                        self.event(who, f"skill series stopped before run {run}: {msg}")
                        return
                while self.jobs.running("skill"):
                    if self._skill_stop.wait(0.3):
                        return
                j = self.jobs.get("skill")
                if j is not None and j.code not in (0, None):
                    self.event(who, f"skill series stopped: run {run} ended with code {j.code}")
                    return
            self.event(who, f"skill {name}: {repeat} run(s) done")
        finally:
            self.skill_series = None

    def _skill_run(self, name: str, who: str) -> tuple[bool, str]:
        m = next((m for m in self.models() if m["name"] == name), None)
        if m is None:
            return False, "Model not found."
        if not m["playable"]:  # run_policy.sh drives the right arm only, and only cameras it knows by name
            if m.get("block") == "cams":
                return False, "CAM_NAMES:" + ", ".join(m["cams"])
            return False, {"both": "TWO_ARM", "left": "LEFT_ARM"}.get(m["arms"], "NO_FIT")
        lacking = Training._missing(Path(m["path"]))
        if lacking:  # lerobot-rollout exits at once without the processor files; say so before the arm moves
            return False, "MISSING:" + ", ".join(lacking)
        missing = self._missing(["right_follower"])
        if missing:
            return False, "Not connected: right follower"
        cams = m["cams"] or ["top", "right_wrist"]
        stuck = self._cams_let_go(cams)
        if stuck:  # the preview still holds the device: the rollout would fail with "device busy"
            self._cams_resume(cams)
            return False, f"CAM_BUSY:{stuck}"
        uses = frozenset({PORT["right_follower"], *(f"cam:{c}" for c in cams)})
        secs = str(getattr(self, "_skill_secs", int(self.cfg.skill_seconds)))
        cmd = (self._mock_cmd(f"skill {name}", seconds=int(secs)) if self.mock
               else ["bash", str(self.so101 / "run_policy.sh"), "run", m["path"], secs])
        # ROLLOUT_PY: the rollout with the ACT queue shortcut (30 Hz instead of ~10 on the Jetson).
        # If that shortcut cannot load, rollout_runner falls back to stock lerobot-rollout.
        env = {**self._venv_env(), "ROLLOUT_PY": str(HERE / "rollout_runner.py"),
               "KIRO_POLICY_CAMS": ",".join(cams)}
        ok, msg = self._start("skill", JobSpec(cmd, uses, cwd=str(self.so101) if not self.mock else None,
                                               env=env, label=f"skill {name}", stop_wait=8.0), who)
        if not ok:
            self._cams_resume(cams)
        return ok, msg

    def _cams_let_go(self, keys: list[str]) -> str | None:
        """Close the preview for these cameras so a skill can open them. A key whose device is still open."""
        stuck = None
        for k in keys:
            cam = self.cams.get(k)
            if cam is None:
                continue
            cam.pause("skill")
            t = cam._thread
            if t is not None and t.is_alive():
                stuck = k
        return stuck

    def _cams_resume(self, keys: list[str]) -> None:
        for k in keys:
            cam = self.cams.get(k)
            if cam is not None:
                cam.resume()

    # ---------------------------------------------------------- demo motion
    # Replays one recorded picking demonstration on the black arm, smoothly, several times (24 Nov 2026: to
    # show the robot it only has to look like a pick; no model, starts in seconds). demo_replay.py.
    DEMO_DATASET = Path("~/.cache/kiro-webapp/demo_motion/right_arm_task2").expanduser()
    DEMO_EPISODE = 9  # the smoothest clean pick of the 43 recorded (15 s)

    def demo(self, on: bool, who: str, repeat: int = 5, pause: float = 3.0) -> tuple[bool, str]:
        if not on:
            return self._stop_job("demo", who)
        if self.jobs.running("demo"):
            return True, ""
        if not self.mock and not (self.DEMO_DATASET / "data").is_dir():
            return False, f"Demo motion data not found: {self.DEMO_DATASET}"
        if self._missing(["right_follower"]):
            return False, "Not connected: right follower"
        port = PORT["right_follower"]
        busy = self.jobs.busy(frozenset({port}))
        if busy:
            return False, f"{busy} is using the arm. Stop it first."
        why = self._sync_goals([port])  # targets = where the motors are: no jump when torque comes on
        if why:
            return False, why
        repeat = max(1, min(50, int(repeat)))
        cmd = (self._mock_cmd(f"demo motion x{repeat}", seconds=5) if self.mock else
               [str(self.venv / "bin" / "python"), str(HERE / "demo_replay.py"), "--port", port, "--id", "right_follower",
                "--dataset", str(self.DEMO_DATASET), "--episode", str(self.DEMO_EPISODE), "--repeat", str(repeat),
                "--pause", str(float(pause))])
        return self._start("demo", JobSpec(cmd, frozenset({port}), env=self._venv_env(), label=f"demo motion x{repeat}",
                                           stop_wait=6.0), who)

    # ---------------------------------------------------------------- voice
    def voice(self, on: bool, who: str, lang: str = "ko", step: str = "big", mic: str = "device") -> tuple[bool, str]:
        if not on:
            self._voice_halt()
            return self._stop_job("voice", who)
        lang = "en" if lang == "en" else "ko"
        step = step if step in VOICE_STEPS else "big"
        mic = "robot" if mic == "robot" else "device"
        if self.jobs.running("voice"):
            if self.voice_lang == lang and self.voice_step == step and self.voice_mic == mic:
                return True, ""
            self._voice_halt()
            self._stop_job("voice", who)       # switching language, step or microphone: restart with them
        model = Path("~/models/vosk").expanduser() / VOICE_MODELS[lang]
        if not self.mock and not model.is_dir():
            return False, f"Speech model not installed: {model}"
        extra = ""
        if mic == "device":
            device = f"udp:{VOICE_UDP[1]}"
            extra = " -p min_rms:=150.0"  # the browser's noise suppression makes speech quieter than the USB mic
        else:  # no USB microphone: start without one, so typed and quick commands still work (they use the node too)
            device = "auto" if self.mock or self.usb_mic() else "none"
        if self.mock:
            cmd = self._mock_cmd(f"voice ({lang}, {mic} microphone, listening)")
        else:
            cmd = ["bash", "-c", ROS_SETUP + f"exec ros2 run so101_base voice_cmd --ros-args {VOICE_ARGS} {VOICE_STEPS[step]} "
                                             f"-p lang:={lang} -p model_path:={model} -p device:={device}{extra}"]
        self.voice_lang, self.voice_step, self.voice_mic = lang, step, mic
        self.voice_log.clear()
        ok, msg = self._start("voice", JobSpec(cmd, frozenset({"mic"}), label=f"voice {lang}", on_line=self._voice_line), who)
        return ok, ("NO_MIC" if ok and device == "none" else msg)

    def voice_audio(self, data: bytes) -> None:
        """Sound from the operator's phone or laptop, about 8 pieces a second. Never blocks."""
        if self.voice_mic != "device" or not data or not self.jobs.running("voice"):
            return
        self.voice_rx_t = time.monotonic()
        try:
            self._udp.sendto(data[:16000], VOICE_UDP)
        except OSError:
            pass

    def _voice_halt(self) -> None:
        """Voice going off while the robot drives a voice command: stop that drive first."""
        if self.jobs.running("voice") and self.ros is not None and hasattr(self.ros, "publish_str"):
            self.ros.publish_str("/voice/say", VOICE_STOP_WORD.get(self.voice_lang, "멈춰"))
            time.sleep(0.3)

    def _voice_line(self, line: str) -> None:
        m = VOICE_TAG.search(ANSI.sub("", line))
        if m:
            self.voice_log.append({"ts": time.time(), "text": m.group(0)[:160]})
            if m.group(1) == "정지" or (m.group(1) == "들림" and m.group(0).rstrip().endswith("-> 정지")):
                self.base_meter.trigger(time.monotonic(), "voice", m.group(0))  # test E2 / voice stop

    def talk(self, on: bool) -> tuple[bool, str]:
        if not self.jobs.running("voice"):
            return False, "Voice is off."
        if self.ros is not None and hasattr(self.ros, "publish_str"):
            self.ros.publish_str("/voice/listen", "start:4" if on else "stop")
        return True, ""

    def voice_text(self) -> dict:
        if self.ros is not None and hasattr(self.ros, "latest_str"):
            return {"text": self.ros.latest_str("/voice/text"), "status": self.ros.latest_str("/voice/status"),
                    "guard": self.ros.latest_str("/obstacle_guard/status"), "log": list(self.voice_log)}
        return {"log": list(self.voice_log)}

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
        if not step or j is None or j.code is None:  # code is set only after all output was read
            return
        self._wizard_running = None
        res = {"status": "stopped" if (j.code < 0 or j.code == 130) else "fail", "auto": None}
        lines = j.output()
        for line in reversed(lines):
            if line.startswith("@RESULT "):
                try:
                    res = json.loads(line[8:])
                except ValueError:
                    pass
                break
        if self.mock:
            res = {"status": "confirm", "auto": None}
        res["lines"] = [ANSI.sub("", x) for x in lines[-60:] if not x.startswith("@RESULT ")]
        res["t"] = time.time()
        self.wizard_results[step] = res

    # ------------------------------------------- leader arm on the laptop, wave
    def _bridge_start(self, side: str, mode: str, who: str) -> tuple[bool, str]:
        if side not in ("right", "left", "both"):
            return False, "Unknown arm."
        if self.jobs.running("bridge"):
            if self.bridge_side == side and self.bridge_mode == mode:
                return True, ""
            return False, "The arm is busy (leader arm or wave). Stop it first."
        why = self._arm_ok(side)
        if why:
            return False, why
        missing = self._missing([f"{s}_follower" for s in _sides(side)])
        if missing:
            return False, "Not connected: " + ", ".join(missing)
        why = self._sync_goals([PORT[f"{s}_follower"] for s in _sides(side)])
        if why:
            log.warning("%s: %s", who, why)
            return False, why
        if self.mock:
            cmd = self._mock_cmd(f"twin_bridge {side} ({mode})")
        else:
            cmd = [str(self.venv / "bin" / "python"), str(self.so101 / "twin_bridge.py"), "--arms", ",".join(_sides(side)),
                   "--to", "127.0.0.1:5005", "--listen", f"{BRIDGE_ADDR[0]}:{BRIDGE_ADDR[1]}", "--allow-from", "127.0.0.1",
                   "--ramp", "40", "--deadman", "0.5", "--status-json", "--quiet"]
        uses = frozenset(PORT[f"{s}_follower"] for s in _sides(side))
        ok, msg = self._start("bridge", JobSpec(cmd, uses, cwd=str(self.so101) if not self.mock else None, env=self._venv_env(),
                                                label=f"{mode} both arms" if side == "both" else f"{mode} {side} arm", stop_wait=8.0), who)
        if ok:
            self.bridge_side, self.bridge_mode = side, mode
            self.leader_rx, self.leader_last = 0, 0.0
        return ok, msg

    def _sync_goals(self, ports: list[str]) -> str | None:
        """Targets = current positions before the bridge switches the motors on (servo_sync.py): after a
        power cut or a recalibration a stale target makes the arm flip. None = done, else why not to start."""
        if self.mock:
            return None
        if not self.bus_lock.acquire(timeout=10):
            return "Servo check is busy. Try again."
        try:
            r = subprocess.run([sys.executable, str(HERE / "servo_sync.py"), *ports], capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return "The arm did not answer. Check its power and cables."
        finally:
            self.bus_lock.release()
        try:
            res = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return "Arm check failed: " + ((r.stderr.strip().splitlines() or ["no output"])[-1])[:160]
        if res.get("ok"):
            return None
        bad = []
        for port, motors in res.get("ports", {}).items():
            arm = Path(port).name.replace("so101_", "").replace("_", " ")
            if not isinstance(motors, dict):
                bad.append(f"{arm}: {motors}")
                continue
            bad += [f"{arm} motor {i}: {why}" for i, why in motors.items() if not isinstance(why, int)]
        return "Arm not ready (" + "; ".join(bad) + "). Check its power and cables."

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
        for line in reversed(j.output()):
            if line.startswith("@STATUS "):
                try:
                    return json.loads(line[8:])
                except ValueError:
                    return None
        return None

    def leader(self, on: bool, side: str, who: str) -> tuple[bool, str]:
        """Leader arms on the operator's laptop: each moves the follower arm of its own side (right leader ->
        right arm, left leader -> left arm). side = right, left or both (two leaders). Readings: leader_pos()."""
        if not on:
            if self.bridge_mode == "leader":
                return self._stop_job("bridge", who)
            return True, ""
        if side not in SIDES:
            return False, "Unknown arm."
        why = self._arm_ok(side)
        if why:
            return False, why
        cals: dict = {}
        for s in _sides(side):
            f = HF_CALIB / "teleoperators" / "so_leader" / f"{s}_leader.json"
            try:
                cal = json.loads(f.read_text())
                bad = leader_cal_problems(cal)
            except (OSError, ValueError, KeyError, TypeError):
                if not self.mock:
                    return False, f"Leader calibration not found or unreadable: {f}"
                continue
            if bad and not self.mock:  # mock mode reads this computer's files but never moves an arm
                return False, f"{s} leader calibration is broken (" + ", ".join(bad) + "). Recalibrate that leader arm."
            cals[s] = cal
        self.leader_cals = cals
        return self._bridge_start(side, "leader", who)

    def follow(self, on: bool, who: str) -> tuple[bool, str]:
        """Copy mode: ONE leader arm on the Jetson moves BOTH follower arms the same way (back on 24 Nov 2026,
        next to one leader per arm). The grey leader (right) is used when plugged in: the per-arm offsets in
        follow_offsets.json were measured with it. The relay reads the leader; the twin bridge moves the arms
        with its safety limits and holds them on stop."""
        if not on:
            ok, msg = self._stop_job("relay", who) if self.jobs.running("relay") else (True, "")
            if self.bridge_mode == "follow" and self.jobs.running("bridge"):
                return self._stop_job("bridge", who)
            return ok, msg
        leader = next((s for s in ("right", "left") if os.path.exists(PORT[f"{s}_leader"])), None)
        if leader is None and not self.mock:
            return False, "No leader arm is plugged into the Jetson."
        leader = leader or "right"
        f = HF_CALIB / "teleoperators" / "so_leader" / f"{leader}_leader.json"
        if not self.mock:
            try:
                cal = json.loads(f.read_text())
                bad = leader_cal_problems(cal)
            except (OSError, ValueError, KeyError, TypeError):
                return False, f"Leader calibration not found or unreadable: {f}"
            if bad:
                return False, "Leader calibration is broken (" + ", ".join(bad) + "). Recalibrate the leader arm."
        ok, msg = self._bridge_start("both", "follow", who)
        if not ok:
            return ok, msg
        lport = PORT[f"{leader}_leader"]
        if self.mock:
            cmd = self._mock_cmd(f"leader_relay {leader} -> both arms")
        else:
            cmd = [sys.executable, str(HERE / "leader_relay.py"), "--port", lport, "--calib", str(f), "--arms", "right,left",
                   "--to", f"{BRIDGE_ADDR[0]}:{BRIDGE_ADDR[1]}"]
        ok, msg = self._start("relay", JobSpec(cmd, frozenset({lport}), label=f"{leader} leader -> both arms", stop_wait=3.0), who)
        if not ok:
            self._stop_job("bridge", who)
            return ok, msg
        return True, "Both arms follow the leader arm."

    def _follow_watch(self) -> None:
        """The relay only feeds the bridge: when the bridge ends (a motor stopped answering, E-STOP...)
        the relay is stopped too, so the page shows the copy as off and the bridge's error stays visible."""
        if self.jobs.running("relay") and not self.jobs.running("bridge"):
            self.jobs.stop("relay")
            self.event("server", "both-arm copy stopped: the arm program ended (see the Arms tab)")

    def leader_pos(self, side: str, raw: object) -> None:
        """Called about 30 times per second per leader arm while it streams. Never blocks."""
        if side not in ("right", "left") or self.bridge_mode != "leader" or side not in _sides(self.bridge_side or "") \
                or not self.jobs.running("bridge"):
            return
        if not isinstance(raw, list) or len(raw) != 6 or not all(isinstance(v, (int, float)) and 0 <= v <= 4095 for v in raw):
            return
        self.leader_rx += 1
        self.leader_last = time.monotonic()
        cal = self.leader_cals.get(side)
        if cal is None:  # mock mode without calibration
            return
        try:
            self.leader_deg = leader_to_target(cal, raw)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return
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

    # ----------------------------------------------------------- stop meter
    def _joints_loop(self) -> None:
        """The bridge's 30 Hz packets of measured joint angles: for the arm stop meter, and saved as
        web_joints_<time>.csv in the log folder while the bridge runs (test E5: the joint-angle log)."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(JOINTS_ADDR)
            sock.settimeout(1.0)
        except OSError as e:
            log.warning("joint angles: cannot listen on %s:%s (%s)", *JOINTS_ADDR, e)
            return
        names: list[str] = []
        try:
            self._joints_read(sock, names)
        finally:
            sock.close()
            self._close_joints_csv()

    def _joints_read(self, sock: socket.socket, names: list[str]) -> None:
        while not self._stop.is_set():
            try:
                data = sock.recv(65536)
            except socket.timeout:
                self._close_joints_csv()  # the bridge stopped: the next run gets its own file
                continue
            except OSError:
                return
            try:
                pkt = json.loads(data)
                joints = (pkt.get("payload") or {}).get("joints") or {}
                keys = sorted(k for k in joints if "_" in k.split(".")[0] and k.split("_")[0] in ("right", "left"))
                vals = tuple(float(joints[k]) for k in keys)
            except (ValueError, TypeError, AttributeError):
                continue
            now = time.monotonic()
            self.joints = (now, vals)
            if keys != names or self._joints_csv is None:
                names = keys
                self._open_joints_csv(names)
            if self._joints_csv:
                try:
                    gap = f"{now - self.leader_last:.3f}" if self.leader_last else ""
                    self._joints_csv.write(f"{time.time():.3f},{gap},"
                                           + ",".join(f"{v:.2f}" for v in vals) + "\n")
                except (OSError, ValueError):
                    self._joints_csv = None

    def _open_joints_csv(self, names: list[str]) -> None:
        self._close_joints_csv()
        if self.jobs.log_dir is None:
            return
        try:
            self.jobs.log_dir.mkdir(parents=True, exist_ok=True)
            f = open(self.jobs.log_dir / f"web_joints_{time.strftime('%Y%m%d_%H%M%S')}.csv", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
            f.write("unix_time,s_since_last_leader_packet," + ",".join(n.replace(".pos", "") for n in names) + "\n")
            self._joints_csv = f
        except OSError:
            self._joints_csv = None

    def _close_joints_csv(self) -> None:
        f, self._joints_csv = self._joints_csv, None
        if f:
            try:
                f.close()
            except OSError:
                pass

    def meter_tick(self, now: float) -> None:
        """20 times a second from the server loop: the arm's joint angles and its leader link."""
        j = self.joints
        pos = j[1] if j and now - j[0] < 0.3 else None
        flowing = self.bridge_mode == "leader" and self.jobs.running("bridge") and bool(self.leader_last) \
            and now - self.leader_last < 0.15
        if self._link_flowing and not flowing and self.leader_last:  # test E5: the laptop stopped sending
            self.arm_meter.trigger(now, "arm_link", "leader arm packets stopped", at=self.leader_last)
        self._link_flowing = flowing
        self.arm_meter.sample(now, pos, flowing)

    def stops(self) -> list[dict]:
        rows = [{"meter": "base", **r} for r in self.base_meter.rows()] + [{"meter": "arm", **r} for r in self.arm_meter.rows()]
        return sorted(rows, key=lambda r: r["ts"])[-12:]

    def stops_csv(self) -> str:
        return "\n".join(csv_lines([self.base_meter, self.arm_meter])) + "\n"

    def stops_clear(self, who: str) -> tuple[bool, str]:
        self.base_meter.clear()
        self.arm_meter.clear()
        self.event(who, "stop measurements cleared")
        return True, ""

    # --------------------------------------------------------------- E-STOP
    def on_estop(self, who: str) -> None:
        """Stop everything that can move. Runs on every E-STOP press; safe to repeat.

        Arm programs, teleop included, are KILLED at once: their own clean-up would move the arm
        (the skill swings back, teleop lets the arm drop). Each of those arms is then held where it
        is (arm_tuck --hold). Voice, wizard and base get Ctrl+C at the same time, so the wheels stop
        and nothing can keep driving; then every wheel's speed is checked (wheel_stop.py).
        The user's choice (20 Nov 2026): a moment later both arms fold home by themselves, slowly.
        Pressing E-STOP again while they are about to fold or folding cancels it: they stay frozen."""
        self._skill_stop.set()  # a series of skill runs ends too, not only the current run
        now = time.monotonic()
        self.base_meter.trigger(now, "estop", f"E-STOP by {who}")
        self.arm_meter.trigger(now, "estop", f"E-STOP by {who}")
        with self._estop_lock:
            self._wave_stop.set()
            refold = self._folding()
            self._fold_cancel.set()
            if self.ros is not None and hasattr(self.ros, "publish_str") and self.jobs.running("voice"):
                self.ros.publish_str("/voice/say", "stop" if self.voice_lang == "en" else "멈춰")
            names = list(ARM_JOBS) + list(TELEOP_JOBS)
            sides = set()
            for name in names:
                j = self.jobs.get(name)
                if j and j.running:
                    sides |= {s for s in ("right", "left") if PORT[f"{s}_follower"] in j.spec.uses}
            killed = self.jobs.kill(names)
            stopped: list[str] = []
            t = threading.Thread(target=lambda: stopped.extend(
                self.jobs.stop_all(lambda j: j.name in ("voice", "wizard", "base"))), daemon=True)
            t.start()                       # wheels stop while the arms are being held
            held = self._hold_arms(sorted(sides)) if sides and not self.mock else ""
            t.join()
            wheels = self._check_wheels()
            fold = ""
            if refold:
                fold = "arm fold cancelled: arms stay still"
            elif self.cfg.estop_fold_s > 0:
                fold_sides = [s for s in ("right", "left") if not self._arm_ok(s) and not self._missing([f"{s}_follower"])]
                if fold_sides:
                    self._fold_cancel = threading.Event()
                    self._fold_thread = threading.Thread(target=self._autofold, daemon=True,
                                                         args=(fold_sides, who, self._fold_cancel, self.cfg.estop_fold_s))
                    self._fold_thread.start()
                    fold = f"arms fold home in {self.cfg.estop_fold_s:g} s (press E-STOP again to keep them still)"
            parts = [f"killed {k}" for k in killed] + [f"stopped {x}" for x in stopped] + [p for p in (held, wheels, fold) if p]
            if parts:
                self.event(who, "E-STOP: " + "; ".join(parts))

    def _folding(self) -> bool:
        """A fold-home after an E-STOP is waiting or running."""
        return bool((self._fold_thread and self._fold_thread.is_alive())
                    or any(self.jobs.running(n) for n in self._fold_jobs))

    def estop_fold_state(self) -> str | None:
        if self._fold_thread and self._fold_thread.is_alive():
            return "pending"
        return "running" if any(self.jobs.running(n) for n in self._fold_jobs) else None

    def _autofold(self, sides: list[str], who: str, cancel: threading.Event, delay: float) -> None:
        if cancel.wait(delay):
            return
        with self._estop_lock:          # never while another E-STOP is being handled
            if cancel.is_set():
                return
            arm = "both" if len(sides) == 2 else sides[0]
            ok, msg = self.arm(arm, "fold", who)
            if ok:
                self._fold_jobs = {f"arm_{arm}"}
        self.event(who, "E-STOP: folding the arms home" if ok else f"E-STOP: could not fold the arms ({msg})")

    def _check_wheels(self) -> str:
        """After the wheel program stopped: no wheel may still be turning (wheel_stop.py)."""
        if self.mock or not os.path.exists(PORT["base"]):
            return ""
        if self.jobs.running("base"):
            return "wheel check skipped: the wheel program is still stopping"
        with self.bus_lock:
            try:
                r = subprocess.run([sys.executable, str(HERE / "wheel_stop.py"), PORT["base"]],
                                   capture_output=True, text=True, timeout=10)
            except subprocess.TimeoutExpired:
                return "wheel check: the wheel board did not answer"
        try:
            res = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return "wheel check failed"
        if res.get("turning"):
            return "wheels " + ", ".join(map(str, res["turning"])) + " were still turning: switched off"
        return "wheels checked: stopped" if res.get("ok") else "wheel check: " + res.get("msg", "some wheels did not answer")

    def _hold_arms(self, sides: list[str]) -> str:
        """Freeze follower arms where they are, with the old arm_tuck.py --hold."""
        arm = "both" if len(sides) == 2 else sides[0]
        cmd = [str(self.venv / "bin" / "python"), str(self.so101 / "arm_tuck.py"), "--hold", "--arm", arm, "--yes"]
        with self.bus_lock:
            try:
                env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
                env["PATH"] = f"{self.venv / 'bin'}:{env.get('PATH', '')}"
                r = subprocess.run(cmd, cwd=str(self.so101), capture_output=True, text=True, timeout=20, env=env)
                return f"held {arm} arm" if r.returncode == 0 else f"hold {arm} arm failed (exit {r.returncode})"
            except subprocess.TimeoutExpired:
                return f"hold {arm} arm timed out"

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
        now = time.monotonic()
        # CPU busy % per core (between two health readings)
        try:
            cur = {ln.split()[0]: [int(x) for x in ln.split()[1:8]]
                   for ln in Path("/proc/stat").read_text().splitlines() if ln.startswith("cpu")}
            pct = cpu_percent(self._cpu_prev, cur)
            self._cpu_prev = cur
            if pct:
                out["cpu"] = pct.get("cpu")
                out["cpu_cores"] = [pct[k] for k in sorted((k for k in pct if k != "cpu"), key=lambda k: int(k[3:]))]
        except (OSError, ValueError, IndexError):
            pass
        # GPU load (Jetson: per mille)
        for g in ("/sys/devices/platform/bus@0/17000000.gpu/load", "/sys/devices/platform/17000000.gpu/load", "/sys/devices/gpu.0/load"):
            try:
                out["gpu"] = round(int(Path(g).read_text()) / 10.0, 1)
                break
            except (OSError, ValueError):
                continue
        # Power from the board's INA3221 sensors: watts per rail
        power = {}
        for h in Path("/sys/bus/i2c/drivers/ina3221").glob("*/hwmon/hwmon*"):
            for i in (1, 2, 3):
                try:
                    label = (h / f"in{i}_label").read_text().strip()
                    mv, ma = int((h / f"in{i}_input").read_text()), int((h / f"curr{i}_input").read_text())
                    power[label] = round(mv * ma / 1e6, 2)
                except (OSError, ValueError):
                    pass
        if power:
            out["power"] = power
        # Wi-Fi traffic (KB/s) between two readings
        try:
            for ln in Path("/proc/net/dev").read_text().splitlines():
                name, _, rest = ln.partition(":")
                if name.strip().startswith(("wl", "wlan")):
                    f = rest.split()
                    rx, tx = int(f[0]), int(f[8])
                    if self._net_prev:
                        dt = max(0.1, now - self._net_prev[2])
                        out["net"] = {"rx": round((rx - self._net_prev[0]) / dt / 1024, 1),
                                      "tx": round((tx - self._net_prev[1]) / dt / 1024, 1)}
                    self._net_prev = (rx, tx, now)
                    break
        except (OSError, ValueError, IndexError):
            pass
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
            out["temps"] = {k.replace("-thermal", ""): round(v, 1) for k, v in temps}
        try:
            out["load"] = round(os.getloadavg()[0], 2)
            out["cores"] = os.cpu_count()
        except OSError:
            pass
        try:
            mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
            total = int(mem["MemTotal"].split()[0]); avail = int(mem["MemAvailable"].split()[0])
            out["mem"] = round(100 * (total - avail) / total)
            out["mem_mb"] = [round((total - avail) / 1024), round(total / 1024)]
        except (OSError, KeyError, ValueError):
            pass
        du = shutil.disk_usage(Path.home())
        out["disk"] = round(100 * du.used / du.total)
        out["disk_free_gb"] = round(du.free / 1e9, 1)
        return out

    def _boards(self) -> dict:
        if self.mock:
            return {}
        with self.bus_lock:  # no job can start while the probe reads
            skip = [k for k, dev in PORT.items() if self.jobs.busy(frozenset({dev}))]
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
                self._follow_watch()
                self._lidar_watch()
                self.rec.poll()
                h["boards"] = self._boards() if n % 2 == 0 else self.health.get("boards", {})
                self.health = h
                if n % 2 == 0:  # a camera plugged in shows up within a few seconds
                    self.refresh_cameras()
            except Exception as e:  # noqa: BLE001 - health must never kill the server
                log.warning("health: %s", e)
            n += 1
            self._stop.wait(self.cfg.health_period)

    # ---------------------------------------------------------------- state
    def _jobs_view(self, n: int) -> dict:
        jobs = {}
        for name, info in self.jobs.info().items():
            j = self.jobs.get(name)
            info["why"] = end_reason(info, j.output() if j else [])
            info["tail"] = [ANSI.sub("", s).strip() for s in info["tail"] if ANSI.sub("", s).strip()][-n:]
            jobs[name] = info
        return jobs

    def battery(self) -> dict:
        """The battery icon. Not wired to hardware yet.

        percent is 0-100 or None, charging is True/False or None, health is good, fair, poor or unknown.
        source is "mock" (a sample, so the icon can be checked) or "none" (no reading)."""
        if self.mock:
            return {"percent": 76, "charging": False, "health": "good", "source": "mock"}
        return {"percent": None, "charging": None, "health": "unknown", "source": "none"}

    def snapshot(self) -> dict:
        """Sent to every page, viewers too. Engineer-only values (serial ports, servo scan, check wizard,
        device paths, recording and packaging output) are in studio_snapshot() instead."""
        self._wizard_collect()
        jobs = self._jobs_view(8)
        for name, info in jobs.items():
            info.pop("uses", None)
            if name in ENGINEER_JOBS:
                info["tail"] = []
        cams = []
        for c in self.cam_info:
            cam = self.cams.get(c["key"])
            cams.append({"key": c["key"], "present": bool(c.get("present")), "reason": c.get("reason"), "auto": c.get("auto"),
                         **({"live": cam.info()["live"], "paused_by": cam.paused_by, "error": cam.error} if cam else {})})
        return {
            "t": "robot", "mock": self.mock, "jobs": jobs, "health": self.health, "cams": cams,
            "models": self.models(), "voice": self.voice_text(), "voice_models": self.voice_models(), "voice_lang": self.voice_lang, "voice_step": self.voice_step, "voice_mic": self.voice_mic, "voice_rx": {"age": round(time.monotonic() - self.voice_rx_t, 1) if self.voice_rx_t else None},
            "left_arm_ok": self.cfg.left_arm_ok, "lidar": os.path.exists("/dev/rplidar"),
            "estop_fold": self.estop_fold_state(), "skill_s": getattr(self, "_skill_secs", self.cfg.skill_seconds), "skill_series": self.skill_series,
            "gamepad": os.path.exists("/dev/input/js0"), "mic": self.usb_mic(),
            "battery": self.battery(),
            "events": list(self.events)[-20:],
            "obstacle_stop": self.obstacle_stop_on(), "lidar_measured": bool(parse_mask(env_get(self._lidar_env_text(), "LIDAR_MASK"))), "wheels": self.base_wheels(), "base_guard": self.base_guard(),
            "stops": self.stops(),
            "bridge": {"running": self.jobs.running("bridge"), "side": self.bridge_side, "mode": self.bridge_mode,
                       "state": (self._bridge_status() or {}).get("state"), "rx": self.leader_rx,
                       "fresh": time.monotonic() - self.leader_last < 0.5 if self.leader_last else False},
        }

    def studio_snapshot(self) -> dict:
        """Only for the unlocked engineer (Studio page): recording, engineer tools and the technical values."""
        self._wizard_collect()
        out = self.rec.snapshot()
        cams = []
        for c in self.cam_info:
            cam = self.cams.get(c["key"])
            cams.append({**{k: c.get(k) for k in ("key", "label", "device", "present", "reason", "auto", "width", "height", "fps")},
                         **({k: v for k, v in cam.info().items() if k in ("live", "viewers", "paused_by", "error", "fps_real")} if cam else {})})
        out.update(
            cams=cams, guard_profile=self.guard_profile, servo_scan=self.servo_scan, wizard=self.wizard_results,
            lidar_setup={**self.lidar_setup.view(), **self.lidar_info()},
            ports=[] if self.mock else self.serial_ports(),
            info={"mock": self.mock, "jobs": self._jobs_view(12), "boards": self.health.get("boards", {}),
                  "jetson": self.health.get("jetson", {}), "left_arm_ok": self.cfg.left_arm_ok,
                  "obstacle_stop_env": self.obstacle_stop_on(),
                  "paths": {"so101": str(self.so101), "venv": str(self.venv), "datasets": str(self.rec.root),
                            "backups": str(self.rec.backups), "calibration": str(HF_CALIB),
                            "policies": str(self.train.root), "packages": str(self.train.pkg_dir)}},
            train=self.train.snapshot(), colab=self.colab.snapshot())
        return out

    def set_guard_profile(self, profile: str, who: str) -> tuple[bool, str]:
        if profile not in GUARD_PROFILES:
            return False, "Unknown guard distance."
        self.guard_profile = profile
        self.event(who, f"guard distance: {profile} (used when the base starts)")
        return True, ""

    def close(self) -> None:
        self._stop.set()
        self._wave_stop.set()
        self.colab.close()
        stopped = self.jobs.stop_all()
        log.info("server stopping; stopped: %s", ", ".join(stopped) or "nothing running")
        for cam in self.cams.values():
            cam.pause("shutdown")
