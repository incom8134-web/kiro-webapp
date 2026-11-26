"""Robot actions in mock mode: who may start what, and what E-STOP stops."""
import dataclasses
import json
import re
import sys
import time

import pytest
from fastapi.testclient import TestClient

from robot_server.base import MockBase
from robot_server.config import Config
from robot_server.jobs import Jobs, JobSpec
from robot_server.main import create_app
from robot_server.robot import Robot

SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.fixture
def cfg(tmp_path):
    model = tmp_path / "policies" / "demo_act" / "pretrained_model"
    model.mkdir(parents=True)
    (model / "config.json").write_text(json.dumps({"type": "act", "input_features": {"observation.images.top": {}}}))
    (model / "model.safetensors").write_text("")
    (model / "policy_preprocessor.json").write_text('{"steps": []}')
    (model / "policy_postprocessor.json").write_text('{"steps": []}')
    return Config(pin="4321", so101_dir=str(tmp_path / "so101"), policies_dir=str(tmp_path / "policies"),
                  health_period=60, skill_seconds=30)


def note(ws, about, tries=200):
    for _ in range(tries):
        m = ws.receive_json()
        if m["t"] == "note" and m.get("about") == about:
            return m
    raise AssertionError("no note")


def robot_until(ws, pred, tries=400):
    for _ in range(tries):
        m = ws.receive_json()
        if m["t"] == "robot" and pred(m):
            return m
    raise AssertionError("robot state not reached")


def take(ws):
    ws.send_json({"t": "take", "pin": "4321"})
    assert note(ws, "take")["ok"]


def test_viewer_cannot_start_anything(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Phone") as v:
        for msg in ({"t": "base", "on": True}, {"t": "arm", "side": "right", "action": "fold"},
                    {"t": "skill", "name": "demo_act", "on": True}, {"t": "system", "action": "shutdown"}):
            v.send_json(msg)
            assert note(v, msg["t"])["ok"] is False


def test_operator_starts_and_stops_base(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        op.send_json({"t": "base", "on": True})
        assert note(op, "base")["ok"]
        robot_until(op, lambda m: m["jobs"].get("base", {}).get("running"))
        op.send_json({"t": "base", "on": False})
        assert note(op, "base")["ok"]
        r = robot_until(op, lambda m: not m["jobs"]["base"]["running"])
        assert any("started base" in e["text"] for e in r["events"])


def test_left_arm_blocked_until_recalibrated(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        for msg in ({"t": "teleop", "side": "left", "on": True}, {"t": "teleop", "side": "both", "on": True},
                    {"t": "arm", "side": "left", "action": "fold"}):
            op.send_json(msg)
            n = note(op, msg["t"])
            assert n["ok"] is False and n["msg"] == "LEFT_BLOCKED"
        op.send_json({"t": "confirm", "action": "arm:release"})  # letting go never moves the arm, but it is confirmed
        tok = next(m["token"] for _ in range(40) if (m := op.receive_json())["t"] == "confirm")
        op.send_json({"t": "arm", "side": "left", "action": "release", "confirm": tok})
        assert note(op, "arm")["ok"]


def test_estop_stops_skill_and_blocks_new_motion(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        op.send_json({"t": "skill", "name": "demo_act", "on": True})
        assert note(op, "skill")["ok"]
        robot_until(op, lambda m: m["jobs"].get("skill", {}).get("running"))
        op.send_json({"t": "estop", "on": True})
        assert note(op, "estop")["ok"]
        robot_until(op, lambda m: not m["jobs"]["skill"]["running"])
        op.send_json({"t": "arm", "side": "right", "action": "fold"})
        n = note(op, "arm")
        assert n["ok"] is False and "E-STOP" in n["msg"]


def test_two_jobs_cannot_share_a_device():
    jobs = Jobs()
    ok, _ = jobs.start("a", JobSpec(SLEEP, frozenset({"/dev/so101_right_follower"})))
    assert ok
    ok, msg = jobs.start("b", JobSpec(SLEEP, frozenset({"/dev/so101_right_follower", "cam:top"})))
    assert not ok and "a is using" in msg
    ok, _ = jobs.start("c", JobSpec(SLEEP, frozenset({"/dev/so101_left_follower"})))
    assert ok
    t0 = time.time()
    assert sorted(jobs.stop_all()) == ["a", "c"]
    assert time.time() - t0 < 5 and not jobs.running("a")


def test_job_gives_camera_back_when_it_ends():
    jobs = Jobs()
    calls = []
    jobs.holders["cam:top"] = (lambda: calls.append("pause"), lambda: calls.append("resume"))
    ok, _ = jobs.start("skill", JobSpec([sys.executable, "-c", "print('hi')"], frozenset({"cam:top"})))
    assert ok
    for _ in range(50):
        if calls == ["pause", "resume"]:
            break
        time.sleep(0.05)
    assert calls == ["pause", "resume"]
    assert jobs.get("skill").lines[-1] == "hi"


def test_scan_points_geometry():
    import math

    from robot_server.base import scan_points
    # one beam straight ahead of the lidar at 1 m; lidar sits 0.2 m in front of the robot centre
    pts = scan_points([1.0], 0.0, 0.0, 0.05, 12.0, 0.0, 0.2, 360)
    assert pts == [120, 0]
    # same beam with the lidar turned 90 degrees left: the point is on the robot's left
    pts = scan_points([1.0], 0.0, 0.0, 0.05, 12.0, math.pi / 2, 0.0, 360)
    assert pts == [0, 100]
    # invalid ranges are dropped; the nearest point of each bin is kept
    assert scan_points([float("inf"), 0.01, 2.0, 1.0], 0.0, 0.0, 0.05, 12.0, 0.0, 0.0, 1) == [100, 0]


def test_mock_sends_scan_and_odometer(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        for _ in range(200):
            m = op.receive_json()
            if m["t"] == "scan":
                break
        assert m["src"] == "mock" and len(m["xy"]) == 2 * cfg.scan_points
        s = next(m for m in iter(op.receive_json, None) if m["t"] == "state")
        assert "odo_m" in s and "up_s" in s


def test_leader_to_target_matches_lerobot_maths():
    from robot_server.robot import JOINTS, leader_to_target
    cal = {j: {"range_min": 1000, "range_max": 3000, "drive_mode": 0} for j in JOINTS}
    mid = leader_to_target(cal, [2000] * 6)
    assert all(abs(mid[j]) < 0.01 for j in JOINTS[:5]) and mid["gripper"] == 50.0
    t = leader_to_target(cal, [2000 + 1024, 2000, 2000, 2000 - 1024, 2000, 3500])
    assert abs(t["shoulder_pan"] - 90.0) < 0.1 and abs(t["wrist_flex"] + 90.0) < 0.1
    assert t["gripper"] == 100.0                     # gripper is clamped to its range
    cal["gripper"]["drive_mode"] = 1
    assert leader_to_target(cal, [2000] * 5 + [1500])["gripper"] == 75.0


def test_leader_and_wave_share_the_arm_and_estop_stops_them(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        op.send_json({"t": "leader", "on": True, "side": "left"})
        assert note(op, "leader")["msg"] == "LEFT_BLOCKED"
        op.send_json({"t": "leader", "on": True, "side": "right"})
        assert note(op, "leader")["ok"]
        for _ in range(5):
            op.send_json({"t": "leader_pos", "side": "right", "raw": [2048] * 6})
        op.send_json({"t": "leader_pos", "side": "right", "raw": [9999] * 6})   # out of range: ignored
        r = robot_until(op, lambda m: m["bridge"]["running"] and m["bridge"]["rx"] == 5)
        assert r["bridge"]["mode"] == "leader"
        op.send_json({"t": "wave", "side": "right"})
        assert "busy" in note(op, "wave")["msg"]
        op.send_json({"t": "estop", "on": True})
        robot_until(op, lambda m: not m["bridge"]["running"])
        op.send_json({"t": "estop", "on": False})
        op.send_json({"t": "drive", "en": False})
        op.send_json({"t": "wave", "side": "right"})
        assert note(op, "wave")["ok"]
        r = robot_until(op, lambda m: m["bridge"]["running"] and m["bridge"]["mode"] == "wave")
        robot_until(op, lambda m: not m["bridge"]["running"], tries=2000)   # the wave ends by itself


def test_base_reports_wheels_without_power(cfg):
    from types import SimpleNamespace

    from robot_server.robot import Robot
    r = Robot(cfg, MockBase())
    try:
        fake = SimpleNamespace(running=True, lines=[])
        fake.output = lambda: list(fake.lines)
        r.jobs.get = lambda name: fake if name == "base" else None
        assert r.base_wheels() is None                       # nothing logged yet: starting
        fake.lines = ["휠 서보 응답 0/4  !! 12 V 전원·서보 케이블 확인", "[ERROR] [mecanum_base_node-1]: process has died"]
        assert r.base_wheels() == "off"                      # 12 V off: launch still runs, wheels do not
        fake.lines.append("[mecanum_base_node-1] 베이스 준비: 포트 /dev/so101_base, 전압 12.1 V")
        assert r.base_wheels() == "ok"                       # power on and restarted
        fake.running = False
        assert r.base_wheels() is None
    finally:
        r.close()


VIEWER_MESSAGES = [
    {"t": "base", "on": True}, {"t": "teleop", "side": "right", "on": True}, {"t": "arm", "side": "right", "action": "fold"},
    {"t": "skill", "name": "demo_act", "on": True}, {"t": "voice", "on": True}, {"t": "system", "action": "shutdown"},
    {"t": "say", "text": "go forward"}, {"t": "servo", "cmd": "scan", "port": "/dev/so101_base"},
    {"t": "wizard", "step": "devices", "on": True}, {"t": "wizard_decide", "step": "devices", "status": "pass"},
    {"t": "leader", "on": True, "side": "right"}, {"t": "wave", "side": "right"},
    {"t": "leader_pos", "side": "right", "raw": [2048] * 6}, {"t": "talk", "on": True},
    {"t": "drive", "x": 1, "y": 0, "z": 0, "en": True}, {"t": "engineer", "pin": "1111"},
]


@pytest.mark.parametrize("msg", VIEWER_MESSAGES, ids=[m["t"] for m in VIEWER_MESSAGES])
def test_a_viewer_can_start_nothing(cfg, msg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Phone") as v:
        v.send_json(msg)
        time.sleep(0.4)
        r = robot_until(v, lambda m: True)
        assert not any(j["running"] for j in r["jobs"].values())
        assert r["bridge"]["rx"] == 0
        assert client.app.state.ctrl.operator is None
        assert not client.app.state.ctrl.engineers


def test_websocket_from_another_website_is_refused(cfg):
    from starlette.websockets import WebSocketDisconnect
    with TestClient(create_app(cfg, MockBase())) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as ws:
                ws.receive_json()
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:  # the page itself
            assert ws.receive_json()["t"] == "state"


def test_estop_while_a_program_is_starting_still_stops_it(cfg):
    from robot_server.robot import Robot
    base = MockBase()
    robot = Robot(cfg, base)
    real = robot.skill

    def slow_skill(*a, **k):  # like waiting for the servo-bus lock
        time.sleep(0.6)
        return real(*a, **k)
    robot.skill = slow_skill
    with TestClient(create_app(cfg, base, robot)) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        op.send_json({"t": "skill", "name": "demo_act", "on": True})
        time.sleep(0.1)
        op.send_json({"t": "estop", "on": True})
        assert note(op, "skill")["ok"]                      # it did start...
        r = robot_until(op, lambda m: "skill" in m["jobs"] and not m["jobs"]["skill"]["running"], tries=600)
        assert r["jobs"]["skill"]["code"] is not None       # ...and E-STOP stopped it right after


def test_estop_kills_a_fold_and_stops_the_base_at_once(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        op.send_json({"t": "base", "on": True})
        assert note(op, "base")["ok"]
        op.send_json({"t": "arm", "side": "right", "action": "fold"})
        assert note(op, "arm")["ok"]
        t0 = time.time()
        op.send_json({"t": "estop", "on": True})
        robot_until(op, lambda m: not m["jobs"]["arm_right"]["running"] and not m["jobs"]["base"]["running"], tries=600)
        assert time.time() - t0 < 3.0
        r = robot_until(op, lambda m: any("E-STOP:" in e["text"] for e in m["events"]))
        assert any("killed arm_right" in e["text"] and "stopped base" in e["text"] for e in r["events"])


def test_hand_cameras_plug_and_play():
    from robot_server.camera import assign_wrists

    def cams():
        return [{"key": "top", "device": "/dev/video6", "present": True},
                {"key": "right_wrist", "device": None, "present": False},
                {"key": "left_wrist", "device": None, "present": False}]
    # two webcams on unexpected ports: right hand first, then left
    out = {c["key"]: c for c in assign_wrists(cams(), [("2.1", "/dev/video8"), ("2.4", "/dev/video10")])}
    assert out["right_wrist"]["device"] == "/dev/video8" and out["right_wrist"]["auto"] == "2.1"
    assert out["left_wrist"]["device"] == "/dev/video10"
    # a camera already in its configured port keeps its place; the other slot takes the spare webcam
    c = cams()
    c[2].update(device="/dev/video8", present=True)
    out = {x["key"]: x for x in assign_wrists(c, [("2.3.2", "/dev/video8"), ("2.1", "/dev/video10")])}
    assert out["left_wrist"]["device"] == "/dev/video8" and "auto" not in out["left_wrist"]
    assert out["right_wrist"]["device"] == "/dev/video10"
    # no webcams: nothing changes
    assert not any(x.get("present") for x in assign_wrists(cams(), [])[1:])


def test_cpu_percent_from_proc_stat():
    from robot_server.robot import cpu_percent
    prev = {"cpu": [100, 0, 100, 800, 0, 0, 0], "cpu0": [50, 0, 50, 400, 0, 0, 0]}
    cur = {"cpu": [150, 0, 150, 900, 0, 0, 0], "cpu0": [50, 0, 50, 500, 0, 0, 0]}
    assert cpu_percent(prev, cur) == {"cpu": 50.0, "cpu0": 0.0}   # 100 busy of 200; core 0 idle
    assert cpu_percent({}, cur) == {}                             # first reading: nothing yet


def test_system_check_sees_gamepad_and_usb_mic(cfg, tmp_path):
    from robot_server.robot import Robot
    cards = tmp_path / "cards"
    cards.write_text(" 0 [HDA ]: tegra-hda - NVIDIA Jetson Orin Nano HDA\n 2 [APE ]: tegra-ape - NVIDIA APE\n")
    assert Robot.usb_mic(str(cards)) is False                # the Jetson's own sound cards are not a microphone
    cards.write_text(cards.read_text() + " 1 [Device ]: USB-Audio - USB PnP Sound Device\n")
    assert Robot.usb_mic(str(cards)) is True
    assert Robot.usb_mic(str(tmp_path / "missing")) is False
    r = Robot(cfg, MockBase())
    try:
        snap = r.snapshot()
        assert isinstance(snap["gamepad"], bool) and isinstance(snap["mic"], bool)
    finally:
        r.close()


def test_servo_sync_sets_targets_to_positions_and_refuses_bad_readings():
    from robot_server.servo_sync import ADDR_GOAL, ADDR_POS, ADDR_TORQUE, sync_motors
    regs = {1: {ADDR_POS: 2020, ADDR_GOAL: 0, ADDR_TORQUE: 0},      # power-up: target 0, torque off
            2: {ADDR_POS: 770, ADDR_GOAL: 770, ADDR_TORQUE: 1},     # already fine: untouched
            3: None}                                                 # motor not answering
    writes = []
    def read(i, a):
        return None if regs[i] is None else regs[i][a]
    def write(i, a, v):
        writes.append((i, a, v))
        regs[i][a] = v
    out = sync_motors(read, write, ids=[1, 2, 3])
    assert out == {1: 2020, 2: 770, 3: "no answer"}
    assert regs[1][ADDR_GOAL] == 2020 and regs[1][ADDR_TORQUE] == 0   # target = here, torque left off
    assert all(i == 1 for i, _, _ in writes)                          # motor 2 was not written
    noisy = iter([100, 100, 300, 100, 100])
    assert sync_motors(lambda i, a: next(noisy) if a == ADDR_POS else 0, lambda *a: None, ids=[4]) == {4: "readings disagree"}


def test_wheel_check_switches_off_a_wheel_that_still_turns():
    from robot_server.wheel_stop import ADDR_GOAL_SPEED, ADDR_SPEED, ADDR_TORQUE, stop_wheels
    regs = {7: {ADDR_SPEED: 0}, 8: {ADDR_SPEED: 0x8000 | 600}, 9: {ADDR_SPEED: 5}, 10: None}   # 8 turns backwards
    writes = []

    def read(i, a):
        return None if regs[i] is None else regs[i].get(a, 0)

    def write(i, a, v):
        writes.append((i, a, v))
        regs[i][a] = v
        if a == ADDR_TORQUE and v == 0:
            regs[i][ADDR_SPEED] = 0                  # torque off: the wheel stops
    wheels, turning = stop_wheels(read, write)
    assert turning == [8] and wheels == {7: 0, 8: 0, 9: 0, 10: "no answer"}
    assert (8, ADDR_GOAL_SPEED, 0) in writes and (8, ADDR_TORQUE, 0) in writes
    assert all(i == 8 for i, _, _ in writes)        # wheels standing still are not touched


def test_estop_freezes_then_folds_home_and_a_second_press_cancels(cfg):
    import dataclasses
    from robot_server.robot import Robot
    r = Robot(dataclasses.replace(cfg, estop_fold_s=0.3), MockBase())
    try:
        assert r.teleop("right", True, "t")[0]
        r.on_estop("t")                                   # 1st press: teleop killed, fold pending
        assert not r.jobs.running("teleop_right")
        assert r.estop_fold_state() == "pending"
        assert any("killed teleop_right" in e["text"] and "fold home" in e["text"] for e in r.events)
        for _ in range(50):
            if r.jobs.running("arm_right"):
                break
            time.sleep(0.05)
        assert r.jobs.running("arm_right") and r.estop_fold_state() == "running"   # the left arm is locked: right only
        r.on_estop("t")                                   # 2nd press while folding: fold killed, arms stay still
        assert not r.jobs.running("arm_right")
        assert any("fold cancelled" in e["text"] for e in r.events)
        time.sleep(0.5)
        assert not r.jobs.running("arm_right") and r.estop_fold_state() is None
        r.on_estop("t")                                   # a later, new emergency folds again
        assert r.estop_fold_state() == "pending"
        r._fold_cancel.set()
    finally:
        r.close()


def test_estop_fold_can_be_switched_off(cfg):
    import dataclasses
    from robot_server.robot import Robot
    r = Robot(dataclasses.replace(cfg, estop_fold_s=0.0), MockBase())
    try:
        r.on_estop("t")
        assert r.estop_fold_state() is None
    finally:
        r.close()


def test_base_passes_the_contact_distance_to_the_old_guard(cfg):
    from robot_server.robot import Robot
    r = Robot(cfg, MockBase())
    env_text = "OBSTACLE_STOP=1\n"
    r._lidar_env_text = lambda: env_text   # never the shared mock lidar.env
    try:
        assert r.base(True, "t")[0]
        env = r.jobs.get("base").spec.env
        assert env["TOUCH_DIST"] == "0" and env["COVERED_RATIO"] == "0"     # the two strict rules are off
        assert float(env["STOP_DIST"]) > 0                                   # the distance stop is not
        env_text = "OBSTACLE_STOP=1\nLIDAR_MASK=-35:0.06,-30:0.06\n"
        env = r.guard_env("close")                                           # a mask alone does not switch them on
        assert env["TOUCH_DIST"] == "0" and env["COVERED_RATIO"] == "0"
        env_text = "OBSTACLE_STOP=1\nLIDAR_MASK=-35:0.06,-30:0.06\nSTRICT_RULES=1\n"
        env = r.guard_env("close")                                           # switched on by hand: test E4 rules on
        assert env["TOUCH_DIST"] == "0.10" and env["COVERED_RATIO"] == "0.4" and env["STOP_DIST"] == "0.12"
        env_text = "OBSTACLE_STOP=1\nSTRICT_RULES=1\n"                     # never without the mask
        assert r.guard_env("close")["TOUCH_DIST"] == "0"
    finally:
        r.close()


def test_job_log_file_has_timed_lines(tmp_path):
    """Test records: every line of a job, with its time, is kept in web_<job>_<time>.log."""
    from robot_server.jobs import Jobs, JobSpec
    jobs = Jobs(log_dir=tmp_path)
    ok, _ = jobs.start("demo", JobSpec([sys.executable, "-c", "print('hello'); print('[들림] 정지')"], label="demo"))
    assert ok
    for _ in range(50):
        if not jobs.running("demo") and jobs.get("demo").ended:
            break
        time.sleep(0.05)
    files = list(tmp_path.glob("web_demo_*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "[server] start demo" in text and "[들림] 정지" in text and "exit code 0" in text
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} ", text.splitlines()[1])


def test_rate_hz_counts_recent_messages():
    from robot_server.base import rate_hz
    stamps = [10.0 + i * 0.05 for i in range(60)]   # 20 Hz for 3 s
    assert rate_hz(stamps, 12.95) == 20.0
    assert rate_hz(stamps, 20.0) == 0.0               # nothing in the last 2 s


def test_end_reason_tells_why_a_program_ended():
    from robot_server.robot import end_reason
    assert end_reason({"running": True, "code": None}, []) is None
    assert end_reason({"running": False, "code": 0, "asked": False}, []) is None
    assert end_reason({"running": False, "code": -9, "asked": True}, []) == "STOPPED"     # E-STOP or a button
    assert end_reason({"running": False, "code": -9, "asked": False}, []) == "KILLED"     # test R8: SIGKILL from outside
    lines = ["Traceback ...", "serial.serialutil.SerialException: [Errno 5] Input/output error"]
    assert end_reason({"running": False, "code": 1, "asked": False}, lines) == "USB_LOST"  # test R1: board unplugged
    assert end_reason({"running": False, "code": 1, "asked": False}, ["ValueError: bad"]) == "ERROR"


def test_job_stopped_by_the_server_is_marked_asked():
    jobs = Jobs()
    assert jobs.start("s", JobSpec(SLEEP, stop_wait=1.0))[0]
    jobs.stop("s")
    assert jobs.get("s").info()["asked"] is True


def test_voice_record_keeps_tagged_lines(cfg):
    """The page shows what the voice node heard and did, with the time (voice test records)."""
    r = Robot(cfg, MockBase())
    try:
        r._voice_line("[INFO] [1763.1] [voice_cmd]:   ... 앞으로  (음량 900, 소음 400)")
        r._voice_line("[INFO] [1763.2] [voice_cmd]: [들림] '앞으로 가' (신뢰도 0.98) -> 전진 0.20 m")
        r._voice_line("\x1b[0m[INFO] [1763.3] [voice_cmd]: [실행] 전진 0.20 m (속도 x1.0)")
        log = r.voice_text()["log"]
        assert [e["text"] for e in log] == ["[들림] '앞으로 가' (신뢰도 0.98) -> 전진 0.20 m", "[실행] 전진 0.20 m (속도 x1.0)"]
        assert all(e["ts"] > 0 for e in log)
    finally:
        r.close()


def test_voice_switches_language_and_takes_bigger_steps(cfg):
    from robot_server.robot import VOICE_STEPS, Robot
    assert "default_dist:=1.0" in VOICE_STEPS["big"] and "default_angle:=90.0" in VOICE_STEPS["big"]   # until "stop", at most 1 m
    assert "default_dist:=0.2" in VOICE_STEPS["test"] and "default_angle:=45.0" in VOICE_STEPS["test"]  # voice test form 4.1
    r = Robot(cfg, MockBase())
    try:
        assert r.voice(True, "t", "ko")[0] and r.voice_lang == "ko"
        first = r.jobs.get("voice")
        assert r.voice(True, "t", "ko") == (True, "")              # same language: nothing restarts
        assert r.jobs.get("voice") is first
        assert r.voice(True, "t", "en")[0] and r.voice_lang == "en"  # other language: restarted with it
        assert r.jobs.get("voice") is not first and r.jobs.running("voice")
        second = r.jobs.get("voice")
        assert r.voice(True, "t", "en", "test")[0] and r.voice_step == "test"  # test steps: restarted with them
        assert r.jobs.get("voice") is not second and r.jobs.running("voice")
    finally:
        r.close()


def test_voice_sound_from_the_phone_goes_to_the_voice_node(cfg):
    import socket
    from robot_server import robot as robot_mod
    from robot_server.robot import Robot
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(1.0)
    r = Robot(cfg, MockBase())
    old = robot_mod.VOICE_UDP
    robot_mod.VOICE_UDP = rx.getsockname()
    try:
        r.voice_audio(b"\x01\x00" * 100)                  # voice off: nothing is sent
        assert r.voice(True, "t", "ko", "big", "device")[0] and r.voice_mic == "device"
        r.voice_audio(b"\x01\x00" * 100)
        assert rx.recv(4096) == b"\x01\x00" * 100
        first = r.jobs.get("voice")
        assert r.voice(True, "t", "ko", "big", "robot")[0] and r.voice_mic == "robot"   # other microphone: restarted
        assert r.jobs.get("voice") is not first
        r.voice_audio(b"\x02\x00" * 100)                  # the robot's own mic now: the phone's sound is dropped
        with pytest.raises(socket.timeout):
            rx.recv(4096)
    finally:
        robot_mod.VOICE_UDP = old
        rx.close()
        r.close()


def test_obstacle_stop_switch_writes_lidar_env_and_restarts_the_wheels(cfg, tmp_path):
    from robot_server.lidar_setup import env_get
    from robot_server.robot import Robot
    r = Robot(dataclasses.replace(cfg, datasets_dir=str(tmp_path / "d")), MockBase())   # mock lidar.env in tmp
    try:
        r.lidar_env.parent.mkdir(parents=True, exist_ok=True)
        r.lidar_env.write_text("LASER_YAW=1.26\nOBSTACLE_STOP=0\n")
        assert r.base(True, "t")[0]
        first = r.jobs.get("base")
        assert r.set_obstacle_stop(True, "t")[0] and r.obstacle_stop_on()
        assert env_get(r.lidar_env.read_text(), "LASER_YAW") == "1.26"          # the other values stay
        assert r.jobs.get("base") is not first and r.jobs.running("base")        # the wheel program reads it at start
        second = r.jobs.get("base")
        assert r.set_obstacle_stop(True, "t") == (True, "") and r.jobs.get("base") is second   # no change: no restart
        assert r.set_obstacle_stop(False, "t")[0] and not r.obstacle_stop_on()
    finally:
        r.close()

def test_each_arm_follows_its_own_leader_separately_or_both_at_once(cfg):
    """Right leader -> right arm, left leader -> left arm: two separate teleop programs, one or both running."""
    from robot_server.robot import PORT, Robot
    r = Robot(dataclasses.replace(cfg, left_arm_ok=True), MockBase())
    try:
        assert r.teleop("right", True, "t")[0] and r.jobs.running("teleop_right")
        assert r.teleop("left", True, "t")[0] and r.jobs.running("teleop_left")      # at the same time
        assert r.jobs.get("teleop_right").spec.uses == {PORT["right_follower"], PORT["right_leader"]}
        assert r.jobs.get("teleop_left").spec.uses == {PORT["left_follower"], PORT["left_leader"]}
        assert r.teleop("right", False, "t")[0] and not r.jobs.running("teleop_right")
        assert r.jobs.running("teleop_left")                                          # the other arm goes on
    finally:
        r.close()


def test_two_laptop_leaders_move_each_arm_of_their_own_side(cfg):
    from robot_server.robot import JOINTS, Robot
    r = Robot(dataclasses.replace(cfg, left_arm_ok=True), MockBase())
    sent = []
    r._bridge_send = lambda side, target: sent.append((side, target["shoulder_pan"]))
    try:
        assert r.leader(True, "both", "t")[0] and r.bridge_side == "both" and r.bridge_mode == "leader"
        r.leader_cals = {"right": {j: {"range_min": 1000, "range_max": 3000, "drive_mode": 0} for j in JOINTS},
                         "left": {j: {"range_min": 0, "range_max": 2000, "drive_mode": 0} for j in JOINTS}}
        r.leader_pos("right", [2000] * 6)
        r.leader_pos("left", [2000] * 6)
        assert sent[0] == ("right", 0.0)                       # each leader with its own calibration
        assert sent[1][0] == "left" and sent[1][1] > 80        # 2000 is the top of the left leader's range
        r.leader_pos("both", [2000] * 6)                       # not a leader's side: ignored
        assert len(sent) == 2
        assert r.leader(False, "both", "t")[0] and not r.jobs.running("bridge")
        assert r.leader(True, "left", "t")[0] and r.bridge_side == "left"
        r.leader_cals = {"left": {j: {"range_min": 1000, "range_max": 3000, "drive_mode": 0} for j in JOINTS}}
        r.leader_pos("right", [2000] * 6)                      # only the left arm runs now
        r.leader_pos("left", [2000] * 6)
        assert sent[-1] == ("left", 0.0) and len(sent) == 3
    finally:
        r.close()


def test_a_broken_leader_calibration_is_refused():
    """24 Nov 2026: the left leader's file had a 6-step gripper range and a 0..4095 elbow."""
    from robot_server.robot import JOINTS, leader_cal_problems
    good = {j: {"range_min": 800, "range_max": 3200} for j in JOINTS}
    good["wrist_roll"] = {"range_min": 0, "range_max": 4095}          # a full turn is normal for the wrist roll
    assert leader_cal_problems(good) == []
    bad = {**good, "gripper": {"range_min": 2045, "range_max": 2051}, "elbow_flex": {"range_min": 0, "range_max": 4095}}
    assert leader_cal_problems(bad) == ["elbow_flex", "gripper"]


def test_one_leader_moves_both_arms_only_when_the_left_arm_is_allowed(cfg):
    import dataclasses
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        op.send_json({"t": "follow", "on": True})
        assert note(op, "follow")["msg"] == "LEFT_BLOCKED"        # broken left calibration: no both-arm copy
    ok = dataclasses.replace(cfg, left_arm_ok=True)
    with TestClient(create_app(ok, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        op.send_json({"t": "follow", "on": True})
        assert note(op, "follow")["ok"]
        r = robot_until(op, lambda m: m["bridge"]["running"] and "relay" in m["jobs"] and m["jobs"]["relay"]["running"])
        assert r["bridge"]["mode"] == "follow" and r["bridge"]["side"] == "both"
        op.send_json({"t": "wave", "side": "right"})
        assert "busy" in note(op, "wave")["msg"]                  # one program per arm
        op.send_json({"t": "estop", "on": True})                   # E-STOP kills the relay and the bridge
        robot_until(op, lambda m: not m["bridge"]["running"] and not m["jobs"]["relay"]["running"])
        op.send_json({"t": "estop", "on": False})
        op.send_json({"t": "drive", "en": False})
        op.send_json({"t": "follow", "on": True})
        assert note(op, "follow")["ok"]
        robot_until(op, lambda m: m["bridge"]["running"] and m["jobs"]["relay"]["running"])
        op.send_json({"t": "follow", "on": False})
        assert note(op, "follow")["ok"]
        robot_until(op, lambda m: not m["bridge"]["running"] and not m["jobs"]["relay"]["running"])


def test_leader_relay_sends_one_target_for_both_arms():
    from robot_server.leadermath import leader_to_target
    cal = {j: {"range_min": 1000, "range_max": 3000, "drive_mode": 0} for j in
           ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]}
    tgt = leader_to_target(cal, [2000, 2000, 2000, 2000, 3000, 1500])
    assert tgt["shoulder_pan"] == 0.0 and tgt["wrist_roll"] == round(1000 * 360 / 4095, 2) and tgt["gripper"] == 25.0


def test_relay_stops_when_the_arm_program_ends(cfg):
    import dataclasses
    from robot_server.robot import Robot
    r = Robot(dataclasses.replace(cfg, left_arm_ok=True), MockBase())
    try:
        assert r.follow(True, "t")[0]
        assert r.jobs.running("relay") and r.jobs.running("bridge")
        r.jobs.kill(["bridge"])                      # the bridge dies (a motor stopped answering)
        r._follow_watch()
        assert not r.jobs.running("relay")
        assert any("copy stopped" in e["text"] for e in r.events)
    finally:
        r.close()


def test_follow_offsets_shift_one_arm_without_a_jump_at_the_seam():
    from robot_server.leadermath import apply_offsets
    tgt = {"wrist_flex": -30.0, "elbow_flex": 96.0, "gripper": 5.0}
    out = apply_offsets(tgt, {"wrist_flex": 123, "gripper": 50})
    assert out == {"wrist_flex": 93.0, "elbow_flex": 96.0, "gripper": 5.0}      # gripper never shifted
    assert apply_offsets({"wrist_flex": 179.0}, {"wrist_flex": 123})["wrist_flex"] == -58.0   # 302 -> -58, continuous
    assert apply_offsets(tgt, {}) == tgt


def test_a_reversed_leader_joint_is_turned_round():
    """24 Nov 2026: the yellow leader's shoulder counts the other way round (drive_mode 1 in its file)."""
    from robot_server.robot import JOINTS, leader_to_target
    cal = {j: {"range_min": 1000, "range_max": 3000, "drive_mode": 0} for j in JOINTS}
    normal = leader_to_target(cal, [2500] * 6)
    cal["shoulder_lift"]["drive_mode"] = 1
    turned = leader_to_target(cal, [2500] * 6)
    assert turned["shoulder_lift"] == -normal["shoulder_lift"] and normal["shoulder_lift"] > 40
    assert turned["elbow_flex"] == normal["elbow_flex"] and turned["gripper"] == normal["gripper"]


def test_leader_tune_file_is_read_and_bad_files_are_ignored(tmp_path):
    from robot_server.lerobot_patch import load_tune
    f = tmp_path / "leader_tune.json"
    f.write_text('{"/dev/so101_left_leader": {"shoulder_lift": 1.091}}')
    assert load_tune(f) == {"/dev/so101_left_leader": {"shoulder_lift": (1.091, 0.0)}}
    f.write_text('{"/dev/so101_left_leader": {"shoulder_lift": {"scale": 1.2, "offset": -14.5}}}')
    assert load_tune(f) == {"/dev/so101_left_leader": {"shoulder_lift": (1.2, -14.5)}}
    f.write_text("not json")
    assert load_tune(f) == {} and load_tune(tmp_path / "missing.json") == {}


def test_a_missing_queue_shortcut_does_not_stop_playback(monkeypatch):
    """The Play button sets ROLLOUT_PY. If that shortcut cannot load, stock lerobot-rollout still starts.
    The motor patch is not optional: wrong units would move the arm the wrong way."""
    from robot_server import rollout_runner

    class Optional:
        def apply(self):
            return None

        def apply_rollout(self):
            raise ImportError("no SyncInferenceEngine")

        def apply_timing(self):
            raise RuntimeError("no timing")

    class Motors:
        def apply(self):
            raise RuntimeError("no motors")

    monkeypatch.setattr(rollout_runner, "_patch", lambda: Optional())
    rollout_runner.prepare()
    monkeypatch.setattr(rollout_runner, "_patch", lambda: Motors())
    with pytest.raises(RuntimeError, match="no motors"):
        rollout_runner.prepare()


def test_battery_is_a_sample_in_mock_and_unknown_until_wired(cfg):
    r = Robot(cfg, MockBase())
    try:
        assert r.snapshot()["battery"] == {"percent": 76, "charging": False, "health": "good", "source": "mock"}
        r.mock = False
        assert r.battery() == {"percent": None, "charging": None, "health": "unknown", "source": "unavailable"}
    finally:
        r.close()


def test_skill_repeats_with_a_pause_and_stop_or_estop_ends_the_series(cfg):
    from robot_server.robot import Robot
    r = Robot(dataclasses.replace(cfg, skill_seconds=1), MockBase())
    try:
        assert r.skill("demo_act", True, "t", repeat=2, pause=0.3)[0]
        assert r.skill_series and r.skill_series["of"] == 2
        assert not r.skill("demo_act", True, "t")[0]                     # one series at a time
        end = time.time() + 15
        runs = set()
        while r.skill_series and time.time() < end:
            runs.add(r.skill_series["run"])
            time.sleep(0.05)
        assert r.skill_series is None and runs == {1, 2}                  # both runs played, then it ended
        assert r.skill("demo_act", True, "t", repeat=5, pause=5)[0]
        assert r.skill("demo_act", False, "t")[0]                         # Stop ends the whole series
        time.sleep(0.5)
        assert r.skill_series is None and not r.jobs.running("skill")
        assert r.skill("demo_act", True, "t", repeat=5, pause=5, seconds=45)[0]
        assert r._skill_secs == 45 and r.jobs.get("skill").spec.env["ROLLOUT_PY"].endswith("rollout_runner.py")
        r.on_estop("t")                                                   # E-STOP ends it too
        time.sleep(0.8)
        assert r.skill_series is None and not r.jobs.running("skill")
    finally:
        r.close()
