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
