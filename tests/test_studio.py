"""Studio page: engineer-only access, and recording datasets with the pretend recorder (mock mode)."""
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robot_server import recorder as rec_mod
from robot_server.base import MockBase
from robot_server.config import Config
from robot_server.control import Controller
from robot_server.main import create_app
from robot_server.robot import Robot

FAKE_SO101 = str(Path(__file__).parent / "fake_so101")


@pytest.fixture(autouse=True)
def pretend_cameras():
    """The camera list module is imported once per process; other tests load the real one from ~/so101."""
    sys.modules.pop("cams_config", None)
    yield
    sys.modules.pop("cams_config", None)


@pytest.fixture
def cfg(tmp_path):
    return Config(pin="4321", engineer_pin="8765", so101_dir=FAKE_SO101, policies_dir=str(tmp_path / "policies"),
                  datasets_dir=str(tmp_path / "datasets"), backups_dir=str(tmp_path / "backups"), health_period=60)


@pytest.fixture
def fast(monkeypatch):
    """Short episodes for tests (the page allows 5 s and up)."""
    monkeypatch.setattr(rec_mod, "LIMITS", {"episodes": (1, 500), "episode_s": (1, 600), "reset_s": (0, 300), "fps": (5, 60)})
    monkeypatch.setattr(rec_mod, "MIN_PHASE_S", 0.2)


def settings(**kw):
    s = {"arm": "right", "mode": "new", "repo": "tester/pick_cube", "task": "pick up the cube and place it in the box",
         "episodes": 2, "episode_s": 2, "reset_s": 1, "fps": 30, "cams": ["top", "right_wrist"]}
    s.update(kw)
    return s


def wait(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not reached")


@pytest.fixture
def robot(cfg):
    r = Robot(cfg, MockBase())
    r.rec.mock_connect_s = 0.2
    yield r
    r.close()


def phase(robot, want, ep=None):
    def ok():
        s = robot.rec.snapshot()["session"]
        return s if s and s["phase"] == want and (ep is None or s["ep"] == ep) else None
    return wait(ok)


# ----------------------------------------------------------- authorization
def test_engineer_token_lives_only_as_long_as_the_unlock():
    c = Controller(Config(pin="1234", engineer_pin="9999"), clock=lambda: 0.0)
    c.connect("a", "Laptop")
    c.connect("b", "Phone")
    assert c.engineer_token("a") is None                    # not even the operator yet
    assert c.take("a", "1234")[0]
    assert c.engineer_token("a") is None                    # operator, but not unlocked
    assert c.unlock_engineer("a", "9999")[0]
    tok = c.engineer_token("a")
    assert tok and c.token_ok(tok) and c.engineer_token("a") == tok
    assert not c.token_ok("") and not c.token_ok("guess") and not c.token_ok(None)
    c.lock_engineer("a")
    assert not c.token_ok(tok)                              # locking ends it
    assert c.unlock_engineer("a", "9999")[0]
    tok2 = c.engineer_token("a")
    assert tok2 != tok and not c.token_ok(tok)
    assert c.take("b", "1234", force=True)[0]               # a new operator ends it
    assert not c.token_ok(tok2)
    assert c.unlock_engineer("b", "9999")[0]
    tok3 = c.engineer_token("b")
    c.disconnect("b")                                       # leaving ends it
    assert not c.token_ok(tok3)


def note(ws, about, tries=300):
    for _ in range(tries):
        m = ws.receive_json()
        assert m["t"] != "studio", "studio state sent to a connection that is not the engineer"
        if m["t"] == "note" and m.get("about") == about:
            return m
    raise AssertionError("no note")


def take(ws, pin="4321"):
    ws.send_json({"t": "take", "pin": pin})
    assert note(ws, "take")["ok"]


def unlock(ws):
    ws.send_json({"t": "engineer", "pin": "8765"})
    assert note(ws, "engineer")["ok"]
    for _ in range(300):
        m = ws.receive_json()
        if m["t"] == "studio":
            return m
    raise AssertionError("no studio state after unlocking")


STUDIO_MESSAGES = [
    {"t": "studio", "cmd": "hello"}, {"t": "studio", "cmd": "check", "settings": settings()},
    {"t": "studio", "cmd": "start", "settings": settings()}, {"t": "studio", "cmd": "stop"},
    {"t": "studio", "cmd": "kill"}, {"t": "studio", "cmd": "pack", "repo": "tester/pick_cube"},
    {"t": "guard_profile", "profile": "safe"},
]


@pytest.mark.parametrize("msg", STUDIO_MESSAGES, ids=[m.get("cmd", m["t"]) for m in STUDIO_MESSAGES])
def test_studio_refuses_viewers_and_operators_without_the_engineer_pin(cfg, msg):
    with TestClient(create_app(cfg, MockBase())) as client:
        with client.websocket_connect("/ws?name=Phone") as viewer, client.websocket_connect("/ws?name=Laptop") as op:
            viewer.send_json(msg)
            assert note(viewer, msg["t"])["msg"] == "Take control first."
            take(op)
            op.send_json(msg)
            assert note(op, msg["t"])["msg"] == "Unlock the engineer tools first."
            time.sleep(1.2)                                  # a 1 Hz round: still no studio state for either
            for ws in (viewer, op):
                ws.send_json({"t": "ping", "ts": 1})
                for _ in range(100):
                    m = ws.receive_json()
                    assert m["t"] != "studio"
                    if m["t"] == "pong":
                        break
            r = client.app.state.data["robot"]
            assert not r.jobs.running("record") and r.guard_profile == "close" and r.rec.last_check is None


def test_studio_camera_previews_need_the_engineer_token(cfg):
    with TestClient(create_app(cfg, MockBase())) as client:
        assert client.get("/api/studio/camera/top.mjpg").status_code == 403
        assert client.get("/api/studio/camera/top.mjpg?token=guess").status_code == 403
        with client.websocket_connect("/ws?name=Laptop") as op:
            take(op)
            st = unlock(op)
            tok = st["token"]
            assert client.get(f"/api/studio/camera/nope.mjpg?token={tok}").status_code == 404   # past the lock
            op.send_json({"t": "engineer", "lock": True})
            time.sleep(0.3)
            assert client.get(f"/api/studio/camera/nope.mjpg?token={tok}").status_code == 403
        assert client.get(f"/api/studio/camera/top.mjpg?token={tok}").status_code == 403


def test_viewers_get_no_engineer_values(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Phone") as v:
        for _ in range(50):
            m = v.receive_json()
            if m["t"] == "robot":
                break
        assert not {"ports", "servo_scan", "wizard"} & set(m)
        assert all("uses" not in j for j in m["jobs"].values())


def test_engineer_gets_studio_state_with_datasets_and_info(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        st = unlock(op)
        assert st["session"] is None and st["datasets"] == [] and st["root"].endswith("datasets")
        assert {c["key"] for c in st["cams"]} == {"top", "right_wrist", "left_wrist"}
        assert "paths" in st["info"] and "jobs" in st["info"] and st["guard_profile"] == "close"


def test_guard_distance_is_the_engineers_choice_and_the_base_uses_it(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        take(op)
        unlock(op)
        op.send_json({"t": "guard_profile", "profile": "nonsense"})
        assert note_any(op, "guard_profile")["ok"] is False
        op.send_json({"t": "guard_profile", "profile": "safe"})
        assert note_any(op, "guard_profile")["ok"]
        op.send_json({"t": "base", "on": True, "profile": "close"})   # what the page sends is ignored
        assert note_any(op, "base")["ok"]
        r = client.app.state.data["robot"]
        assert r.jobs.get("base").spec.env["STOP_DIST"] == "0.20"


def note_any(ws, about, tries=300):
    for _ in range(tries):
        m = ws.receive_json()
        if m["t"] == "note" and m.get("about") == about:
            return m
    raise AssertionError("no note")


# ----------------------------------------------------------------- checks
def test_settings_are_checked(robot):
    rows = {r["id"]: r for r in robot.rec.check(settings(repo="no-slash", task="", episodes=0))["rows"]}
    assert rows["name"]["fix"] == "fix.name.format" and rows["task"]["fix"] == "fix.task.empty"
    assert rows["num_episodes"]["st"] == "fail"
    rows = {r["id"]: r for r in robot.rec.check(settings(repo="me/eval_x", task="컵을 집어"))["rows"]}
    assert rows["name"]["fix"] == "fix.name.eval" and rows["task"]["fix"] == "fix.task.english"
    assert robot.rec.check(settings(repo="../../etc"))["ok"] is False
    assert robot.rec.path_of("a/../../x") is None and robot.rec.path_of("../a") is None


def test_check_passes_and_explains_what_blocks(robot, fast):
    res = robot.rec.check(settings())
    assert res["ok"], [(r["id"], r["fix"], r["d"], r["fa"]) for r in res["rows"] if r["st"] == "fail"]
    rows = {r["id"]: r for r in robot.rec.check(settings(arm="left", cams=["top", "left_wrist"]), estop=True)["rows"]}
    assert rows["arm"]["fix"] == "fix.leftlock"               # left arm locked until recalibrated
    assert rows["estop"]["st"] == "fail"
    assert rows["cam_left_wrist"]["fix"] == "fix.cam"          # the pretend left wrist camera is unplugged
    assert rows["dataset"]["st"] == "ok"
    rows = {r["id"]: r for r in robot.rec.check(settings(mode="add"))["rows"]}
    assert rows["dataset"]["fix"] == "fix.dsmissing"          # nothing to add to yet


# -------------------------------------------------------------- recording
def test_record_next_again_and_stop_like_lerobot(robot, fast):
    r = robot.rec
    ok, msg = r.start(settings(), "Laptop")
    assert ok, msg
    assert r.start(settings(), "Laptop") == (False, "A recording is already running.")   # double click
    s = phase(robot, "rec", 0)
    assert r.control("next", 1, "rec", "Laptop") == (False, "STALE")      # the page saw another episode
    time.sleep(0.25)
    assert r.control("next", 0, "rec", "Laptop") == (True, "")
    assert r.control("next", 0, "rec", "Laptop") == (False, "STALE")      # a second click in the same phase
    phase(robot, "reset")
    s = phase(robot, "rec", 1)                                            # after the reset time
    assert s["saved"] == 1
    time.sleep(0.25)
    assert r.control("redo", 1, "rec", "Laptop")[0]                      # record episode 1 again
    phase(robot, "reset")
    s = phase(robot, "rec", 1)
    assert s["redo"] == 1
    time.sleep(0.25)
    assert r.control("stop", None, None, "Laptop")[0]                     # stop and save the current one
    s = phase(robot, "done")
    res = s["result"]
    assert res["state"] == "done" and res["added"] == 2 and res["info"]["episodes"] == 2 and res["info"]["frames"] > 0
    assert res["info"]["cams"] == ["right_wrist", "top"] and res["info"]["robot_type"] == "so101_follower"
    assert [d["repo"] for d in r.snapshot()["datasets"]] == ["tester/pick_cube"]


def test_commands_wait_for_the_phase_to_run_a_moment(robot, fast, monkeypatch):
    monkeypatch.setattr(rec_mod, "MIN_PHASE_S", 30)
    assert robot.rec.start(settings(episode_s=5), "Laptop")[0]
    phase(robot, "rec", 0)
    assert robot.rec.control("next", 0, "rec", "Laptop") == (False, "TOO_SOON")   # no empty episode
    assert robot.rec.control("kill", None, None, "Laptop")[0]
    phase(robot, "stopped")


def test_force_stop_keeps_the_saved_episodes(robot, fast):
    assert robot.rec.start(settings(episodes=3, episode_s=1, reset_s=0), "Laptop")[0]
    phase(robot, "rec", 1)                                                # episode 0 is saved
    assert robot.rec.control("kill", None, None, "Laptop")[0]
    s = phase(robot, "stopped")
    assert s["result"]["info"]["episodes"] == 1 and s["result"]["added"] == 1


def test_estop_kills_a_recording_like_teleop(robot, fast):
    assert robot.rec.start(settings(episode_s=5), "Laptop")[0]
    phase(robot, "rec", 0)
    robot.on_estop("Phone")                                               # anyone may press it
    s = phase(robot, "estop")
    assert s["result"]["state"] == "estop" and not robot.jobs.running("record")
    assert any("killed record" in e["text"] for e in robot.events)


def test_new_never_overwrites_and_add_keeps_a_copy(robot, fast, cfg):
    assert robot.rec.start(settings(episodes=1, episode_s=1), "Laptop")[0]
    phase(robot, "done")
    rows = {x["id"]: x for x in robot.rec.check(settings())["rows"]}
    assert rows["dataset"]["fix"] == "fix.dsexists"                     # new: the name is taken
    rows = {x["id"]: x for x in robot.rec.check(settings(mode="add", fps=15))["rows"]}
    assert rows["dataset"]["fix"] == "fix.dsfps"                        # add: must match the dataset
    rows = {x["id"]: x for x in robot.rec.check(settings(mode="add", cams=["top"]))["rows"]}
    assert rows["dataset"]["fix"] == "fix.dscams"
    assert robot.rec.start(settings(mode="add", episodes=1, episode_s=1), "Laptop")[0]
    s = phase(robot, "done")
    assert s["result"]["added"] == 1 and s["result"]["info"]["episodes"] == 2
    backup = Path(s["backup"])
    assert backup.parent == Path(cfg.backups_dir) and json.loads((backup / "meta" / "info.json").read_text())["total_episodes"] == 1


def test_recording_holds_the_arm_ports_and_cameras(robot, fast):
    assert robot.teleop("right", True, "Laptop")[0]
    res = robot.rec.check(settings())
    assert {x["id"]: x for x in res["rows"]}["port_right_follower"]["fa"] == {"job": "teleop_right"}
    assert robot.rec.start(settings(), "Laptop") == (False, "CHECKS_FAILED")
    robot.teleop("right", False, "Laptop")
    assert robot.rec.start(settings(episode_s=5), "Laptop")[0]
    phase(robot, "rec", 0)
    assert robot.teleop("right", True, "Laptop")[0] is False             # the arm is busy recording
    assert robot.cams["top"].paused_by == "job"                           # the preview let go of the camera
    robot.rec.control("kill", None, None, "Laptop")
    phase(robot, "stopped")
    wait(lambda: robot.cams["top"].paused_by is None)


def test_package_for_upload(robot, fast):
    assert robot.rec.pack("tester/none", "Laptop") == (False, "NO_DATASET")
    assert robot.rec.start(settings(episodes=1, episode_s=1), "Laptop")[0]
    phase(robot, "done")
    assert robot.rec.pack("tester/pick_cube", "Laptop")[0]
    p = wait(lambda: (robot.rec.pack_state() or {}).get("ok") and robot.rec.pack_state())
    assert p["file"].endswith(".tar.gz") and Path(p["file"]).exists()


def test_recording_over_the_websocket_and_viewers_see_no_training_data(cfg, fast):
    with TestClient(create_app(cfg, MockBase())) as client:
        client.app.state.data["robot"].rec.mock_connect_s = 0.2
        with client.websocket_connect("/ws?name=Laptop") as op, client.websocket_connect("/ws?name=Phone") as viewer:
            take(op)
            unlock(op)
            op.send_json({"t": "studio", "cmd": "start", "settings": settings(episodes=1, episode_s=1)})
            assert note_any(op, "studio")["ok"]
            for _ in range(600):
                m = op.receive_json()
                if m["t"] == "studio" and m["session"] and m["session"]["phase"] == "done":
                    break
            else:
                raise AssertionError("recording did not finish")
            assert m["session"]["result"]["info"]["episodes"] == 1
            for _ in range(60):
                v = viewer.receive_json()
                assert v["t"] != "studio"
                if v["t"] == "robot" and "record" in v["jobs"]:
                    assert v["jobs"]["record"]["tail"] == [] and v["jobs"]["record"]["label"] == "recording"
                    assert "tester" not in json.dumps(v)               # no dataset name for viewers
                    break
