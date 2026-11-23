"""Studio page: engineer-only access, and recording datasets with the pretend recorder (mock mode)."""
import json
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from robot_server import camera as cam_mod
from robot_server import recorder as rec_mod
from robot_server import robot as robot_mod
from robot_server.base import MockBase
from robot_server.config import Config
from robot_server.control import Controller
from robot_server.main import create_app
from robot_server.record_runner import MockDataset, files_check
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
    {"t": "studio", "cmd": "cam_repair"},
    {"t": "guard_profile", "profile": "safe"}, {"t": "lidar_setup", "cmd": "start"},
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
            assert r.lidar_setup.step == "idle"


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
    assert res["info"]["cams"] == ["right_wrist", "top"] and res["info"]["robot_type"] == "so_follower"
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


def test_add_accepts_datasets_recorded_by_lerobot_0_6(robot, cfg):
    """LeRobot 0.6.1 writes the robot class name: so_follower (one arm) or bi_so_follower (two arms)."""
    def dataset(repo, robot_type, cams):
        meta = Path(cfg.datasets_dir) / repo / "meta"
        meta.mkdir(parents=True)
        feats = {"action": {"dtype": "float32", "shape": [6]}, "observation.state": {"dtype": "float32", "shape": [6]},
                 **{f"observation.images.{k}": {"dtype": "video", "shape": [480, 640, 3]} for k in cams}}
        (meta / "info.json").write_text(json.dumps({"codebase_version": "v3.0", "robot_type": robot_type, "fps": 30,
                                                    "total_episodes": 43, "total_frames": 19283, "features": feats}))
    dataset("tester/right_arm_task2", "so_follower", ["top", "right_wrist"])
    dataset("tester/two_arms", "bi_so_follower", ["top"])
    dataset("tester/old_name", "so101_follower", ["top", "right_wrist"])
    ds = lambda **kw: {x["id"]: x for x in robot.rec.check(settings(mode="add", **kw))["rows"]}["dataset"]
    assert ds(repo="tester/right_arm_task2")["st"] == "ok"
    assert ds(repo="tester/right_arm_task2", arm="both")["fix"] == "fix.dsrobot"
    assert ds(repo="tester/two_arms", arm="both", cams=["top"])["st"] == "ok"
    assert ds(repo="tester/two_arms", cams=["top"])["fa"] == {"have": "bi_so_follower", "want": "so_follower"}
    assert ds(repo="tester/old_name")["fix"] == "fix.dsrobot"      # lerobot-record 0.6.1 would refuse it too


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


# ------------------------------------------------ top camera repair (needs Robot.cam_repair, see the report)
needs_hook = pytest.mark.skipif(not hasattr(Robot, "cam_repair"), reason="Robot.cam_repair and the studio cmd cam_repair are not added yet")


def fake_repair(monkeypatch, result):
    """The real repair re-plugs a USB device: never in tests."""
    calls = []

    def repair(so101):
        calls.append(str(so101))
        return result
    monkeypatch.setattr(cam_mod, "repair_top", repair)
    monkeypatch.setattr(robot_mod, "repair_top", repair, raising=False)
    return calls


@needs_hook
@pytest.mark.parametrize("result, ok, msg", [
    ((True, "REPAIRED", "OK 2-1.1"), True, "REPAIRED"), ((True, "OK_ALREADY", ""), True, "OK_ALREADY"),
    ((False, "NO_HELPER", "not installed"), False, "NO_HELPER: not installed"), ((False, "ORBBEC_RUNNING", ""), False, "ORBBEC_RUNNING")])
def test_camera_repair_answers_with_the_code_and_looks_at_the_cameras_again(robot, monkeypatch, result, ok, msg):
    calls = fake_repair(monkeypatch, result)
    monkeypatch.setattr(robot, "mock", False)   # the pretend robot never re-plugs: test the real path with a fake repair
    looked = []
    robot.refresh_cameras = lambda: looked.append(1)
    assert robot.cam_repair("Laptop") == (ok, msg)
    assert calls == [str(robot.so101)] and bool(looked) == ok            # new devices are only looked for after a repair
    assert any("camera repair" in e["text"] and result[1] in e["text"] for e in robot.events)


@needs_hook
def test_camera_repair_over_the_websocket_for_the_engineer_only(cfg, monkeypatch):
    calls = fake_repair(monkeypatch, (True, "REPAIRED", "OK 2-1.1"))
    app = create_app(cfg, MockBase())
    with TestClient(app) as client, client.websocket_connect("/ws?name=Laptop") as op:
        app.state.data["robot"].mock = False    # as on the robot; the repair itself is the fake above
        take(op)
        unlock(op)
        op.send_json({"t": "studio", "cmd": "cam_repair"})
        m = note_any(op, "studio")
        assert (m["cmd"], m["ok"], m["msg"]) == ("cam_repair", True, "REPAIRED") and len(calls) == 1


# ------------------------------------------- R7: continue after a force stop, and the read check
def checked(robot, timeout=15.0):
    """The finished session once its dataset read check has an answer."""
    def ok():
        s = robot.rec.snapshot()["session"]
        v = s and s["result"] and s["result"]["verify"]
        return s if v and v["state"] != "checking" else None
    return wait(ok, timeout)


def test_continue_recording_after_a_force_stop_keeps_the_episodes_and_adds_one(robot, fast, cfg):
    """R7: force stop, then 'continue recording' (add to the existing dataset): the episodes of before stay,
    one new episode is added, the dataset reads back OK."""
    assert robot.rec.start(settings(episodes=3, episode_s=1, reset_s=0), "Laptop")[0]
    phase(robot, "rec", 1)                                                # episode 0 is saved, episode 1 is being recorded
    assert robot.rec.control("kill", None, None, "Laptop")[0]
    first = checked(robot)
    assert first["result"]["state"] == "stopped" and first["result"]["info"]["episodes"] == 1
    v = first["result"]["verify"]
    assert (v["state"], v["episodes"], v["how"]) == ("ok", 1, "files") and v["frames"] > 0 and v["problems"] == []
    assert first["backup"] is None                                        # a new dataset has no copy

    assert robot.rec.start(settings(mode="add", episodes=1, episode_s=1), "Laptop")[0]
    second = checked(robot)
    res = second["result"]
    assert res["state"] == "done" and res["added"] == 1                    # exactly the new episode
    assert res["info"]["episodes"] == 2 and res["info"]["frames"] > v["frames"]
    assert res["verify"]["state"] == "ok" and res["verify"]["episodes"] == 2 and res["verify"]["frames"] == res["info"]["frames"]
    assert [d["episodes"] for d in robot.rec.snapshot()["datasets"]] == [2]
    info = json.loads((Path(cfg.datasets_dir) / "tester/pick_cube/meta/info.json").read_text())
    assert (info["total_episodes"], info["total_frames"]) == (2, res["info"]["frames"])
    backup = Path(second["backup"])                                        # the copy from before adding is the 1-episode dataset
    assert json.loads((backup / "meta" / "info.json").read_text())["total_episodes"] == 1 and files_check(backup)["ok"]


def test_force_stop_before_the_first_episode_leaves_a_readable_empty_dataset(robot, fast):
    assert robot.rec.start(settings(episode_s=5), "Laptop")[0]
    phase(robot, "rec", 0)
    assert robot.rec.control("kill", None, None, "Laptop")[0]
    s = checked(robot)
    assert s["result"]["state"] == "stopped" and s["result"]["added"] == 0
    assert (s["result"]["verify"]["state"], s["result"]["verify"]["episodes"]) == ("ok", 0)


def test_estop_cuts_the_dataset_off_and_the_page_is_told_where_the_copy_is(robot, fast):
    """kill -9 never reaches LeRobot's finalize: the saved episodes are in meta/info.json but the files are cut off."""
    assert robot.rec.start(settings(episodes=1, episode_s=1), "Laptop")[0]
    phase(robot, "done")
    assert checked(robot)["result"]["verify"]["state"] == "ok"
    assert robot.rec.start(settings(mode="add", episodes=3, episode_s=1, reset_s=0), "Laptop")[0]
    phase(robot, "rec", 2)                                                 # one more episode is saved, the next one runs
    robot.on_estop("Phone")
    s = checked(robot)
    assert s["result"]["state"] == "estop"
    v = s["result"]["verify"]
    assert v["state"] == "broken" and any("cut off" in x for x in v["problems"])
    assert s["result"]["info"]["episodes"] == 2                            # info.json says 2: it does not know the files are cut off
    backup = Path(s["backup"])
    assert files_check(backup)["ok"] and files_check(backup)["episodes"] == 1    # the way back
    assert any("BROKEN" in e["text"] for e in robot.events)


def test_estop_in_a_new_dataset_before_any_episode_is_saved_cuts_nothing(robot, fast):
    assert robot.rec.start(settings(episode_s=5), "Laptop")[0]
    phase(robot, "rec", 0)
    robot.on_estop("Phone")
    s = checked(robot)
    assert s["result"]["state"] == "estop" and s["result"]["verify"]["state"] == "ok" and s["result"]["verify"]["episodes"] == 0


def test_a_new_recording_waits_for_the_read_check_of_the_last_one(robot, fast, monkeypatch):
    monkeypatch.setattr(rec_mod, "VERIFY_WAIT_S", 0.1)
    release = threading.Event()
    slow = threading.Thread(target=release.wait, daemon=True)
    slow.start()
    robot.rec._verify_thread = slow
    assert robot.rec.start(settings(episode_s=5), "Laptop") == (False, "VERIFYING")
    release.set()
    slow.join()
    assert robot.rec.start(settings(episode_s=5), "Laptop")[0]
    phase(robot, "rec", 0)
    robot.rec.control("kill", None, None, "Laptop")
    checked(robot)


def make_dataset(root, episodes=2, cams=("top",), finish=True):
    info = {"codebase_version": "v3.0", "robot_type": "so_follower", "total_episodes": 0, "total_frames": 0, "fps": 30,
            "features": {"action": {"dtype": "float32"}, **{f"observation.images.{k}": {"dtype": "video"} for k in cams}}}
    ds = MockDataset(Path(root), info)
    ds.write_info()
    for _ in range(episodes):
        ds.save_episode(30)
    if finish:
        ds.finalize()
    return Path(root)


def test_files_check_sees_what_a_cut_off_recorder_leaves(tmp_path):
    ok = files_check(make_dataset(tmp_path / "ok"))
    assert ok == {"ok": True, "episodes": 2, "frames": 60, "problems": []}
    assert files_check(make_dataset(tmp_path / "empty", episodes=0))["ok"]          # nothing saved: nothing to read
    cut = files_check(make_dataset(tmp_path / "cut", finish=False))
    assert not cut["ok"] and cut["episodes"] == 2
    assert any("data/chunk-000/file-000.parquet" in x and "cut off" in x for x in cut["problems"])
    assert any("episodes" in x and "cut off" in x for x in cut["problems"]) and any(".mp4" in x and "no index" in x for x in cut["problems"])
    # one thing at a time
    base = make_dataset(tmp_path / "base", cams=("top", "right_wrist"))
    (base / "videos/observation.images.right_wrist/chunk-000/file-000.mp4").write_bytes(b"\0" * 8)
    assert [x.split(":")[0] for x in files_check(base)["problems"]] == ["videos/observation.images.right_wrist/chunk-000/file-000.mp4"]
    for gone, text in (("data/chunk-000/file-000.parquet", "no data files"), ("meta/stats.json", "stats.json"),
                       ("videos/observation.images.top/chunk-000/file-000.mp4", "no video files for top")):
        d = make_dataset(tmp_path / gone.replace("/", "_"))
        (d / gone).unlink()
        assert any(text in x for x in files_check(d)["problems"]), gone
    broken = make_dataset(tmp_path / "info")
    (broken / "meta" / "info.json").write_text("{not json")
    assert files_check(broken)["problems"] == ["meta/info.json is not valid JSON"]
    (broken / "meta" / "info.json").unlink()
    assert files_check(broken)["problems"] == ["meta/info.json is missing"]
    assert files_check(tmp_path / "nothing")["ok"] is False


def test_the_lerobot_read_check_and_what_it_may_not_claim(tmp_path, monkeypatch):
    """On the robot LeRobot loads the dataset in the LeRobot venv. Here a pretend python answers."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    answer, called = tmp_path / "answer", tmp_path / "called"
    py.write_text(f"#!/bin/bash\ntouch {called}\ncat {answer}\n")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    root = tmp_path / "datasets"
    make_dataset(root / "tester/good")
    rec = object.__new__(rec_mod.Recorder)
    rec.r, rec.root = SimpleNamespace(mock=False, venv=venv), root
    verify = rec.verify_dataset

    answer.write_text('{"ok": true, "episodes": 2, "frames": 60}\n')
    v = verify("tester/good")
    assert (v["state"], v["how"], v["episodes"], v["frames"]) == ("ok", "lerobot", 2, 60) and called.exists()
    answer.write_text('some LeRobot noise\n{"ok": false, "error": "ArrowInvalid: magic bytes"}\n')
    v = verify("tester/good")
    assert v["state"] == "broken" and v["problems"] == ["ArrowInvalid: magic bytes"]
    answer.write_text('{"ok": null, "error": "LeRobot could not be loaded: ImportError"}\n')
    v = verify("tester/good")
    assert v["state"] == "unchecked" and "ImportError" in v["problems"][0]          # the files look fine: it does not say OK
    answer.write_text("Traceback (most recent call last)\n")
    assert verify("tester/good")["state"] == "unchecked"
    py.write_text("#!/bin/bash\nexec sleep 20\n")
    monkeypatch.setattr(rec_mod, "VERIFY_TIMEOUT_S", 0.5)
    v = verify("tester/good")
    assert v["state"] == "unchecked" and "no answer within" in v["problems"][0]
    rec.r = SimpleNamespace(mock=False, venv=tmp_path / "no-venv")
    assert verify("tester/good")["state"] == "unchecked"
    # a cut-off dataset or an empty one never starts LeRobot
    called.unlink()
    answer.write_text('{"ok": true, "episodes": 2, "frames": 60}\n')
    rec.r = SimpleNamespace(mock=False, venv=venv)
    py.write_text(f"#!/bin/bash\ntouch {called}\ncat {answer}\n")
    make_dataset(root / "tester/cut", finish=False)
    make_dataset(root / "tester/empty", episodes=0)
    assert verify("tester/cut")["state"] == "broken" and verify("tester/empty")["episodes"] == 0
    assert verify("../x")["state"] == "broken" and not called.exists()


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

