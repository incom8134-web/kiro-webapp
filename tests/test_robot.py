"""Robot actions in mock mode: who may start what, and what E-STOP stops."""
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from robot_server.base import MockBase
from robot_server.config import Config
from robot_server.jobs import Jobs, JobSpec
from robot_server.main import create_app

SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.fixture
def cfg(tmp_path):
    model = tmp_path / "policies" / "demo_act" / "pretrained_model"
    model.mkdir(parents=True)
    (model / "config.json").write_text(json.dumps({"type": "act", "input_features": {"observation.images.top": {}}}))
    (model / "model.safetensors").write_text("")
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
        op.send_json({"t": "arm", "side": "left", "action": "release"})  # letting go never moves the arm
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
