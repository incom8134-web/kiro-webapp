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
