"""Lidar setup (Studio): front direction and the robot's own points, measured with the pretend lidar."""
import math

import pytest

from robot_server import lidar_setup as ls
from robot_server.base import MOCK_LIDAR_YAW, MOCK_SELF, MockBase, scan_points
from robot_server.config import Config
from robot_server.robot import Robot


def scans(step, n=10):
    return MockBase().grab_scans(step, n, 0.0)


def test_front_is_found_from_the_box_like_lidar_yaw_does():
    front, box = ls.find_front(scans("clear", 20), scans("box"))
    yaw = math.atan2(math.sin(-front), math.cos(-front))
    assert abs(yaw - MOCK_LIDAR_YAW) < math.radians(3)
    assert 0.40 <= box["dist"] <= 0.50 and box["points"] >= 5


def test_flickering_robot_parts_are_not_taken_for_the_box():
    clear = scans("clear", 20)
    with pytest.raises(ls.SetupError) as e:
        ls.find_front(clear, scans("clear"))          # nothing was put in front
    assert e.value.key == "ls.err.nobox"


def test_two_things_coming_closer_is_refused():
    clear = scans("clear", 20)
    box = scans("box")
    s0 = box[0]
    n = len(s0.ranges)
    for s in box:  # a second object of the same size behind the robot, as if someone walked past
        for i in range(n):
            a = math.degrees(s0.angle_min + i * s0.inc + MOCK_LIDAR_YAW)
            if abs(ls.wrap_deg(a - 180)) <= 8:
                s.ranges[i] = 0.5
    with pytest.raises(ls.SetupError) as e:
        ls.find_front(clear, box)
    assert e.value.key == "ls.err.two"


def test_the_mask_covers_the_robot_parts_and_not_the_front():
    mask = ls.self_mask(scans("clear", 20), MOCK_LIDAR_YAW)
    for lo, hi, d in MOCK_SELF:
        for a in range(int(lo) + 1, int(hi)):
            assert ls.is_self(a, d, mask), a
            assert not ls.is_self(a, d + 0.20, mask)   # something further out the same way is still an obstacle
    assert not any(ls.is_self(a, 0.3, mask) for a in range(-60, 61))
    assert ls.mask_warnings(mask) == []


def test_mask_text_round_trip_and_warnings():
    mask = {ls.bin_of(-2): 0.25, ls.bin_of(3): 0.30, ls.bin_of(170): 0.4}
    assert ls.parse_mask(ls.mask_str(mask)) == mask
    assert ls.mask_str(mask) == "-5:0.25,0:0.30,170:0.40"
    w = ls.mask_warnings(mask)
    assert w[0]["key"] == "ls.warn.front" and w[0]["cm"] == 30
    boxed = {b: 0.2 for b in range(ls.NBINS)}
    assert any(x["key"] == "ls.warn.boxed" for x in ls.mask_warnings(boxed))
    assert ls.parse_mask("") == {} and ls.parse_mask("junk,1:x") == {}


def test_env_update_keeps_the_other_lines():
    old = "# mounting\nOBSTACLE_STOP=0   # off for now\nLASER_YAW=1.17   # old\nBODY_FRONT=0.15\n"
    new = ls.env_update(old, {"LASER_YAW": "1.26", "LIDAR_MASK": "0:0.30", "OBSTACLE_STOP": "1"}, "test")
    assert "# mounting" in new and "BODY_FRONT=0.15" in new and "1.17" not in new
    assert ls.env_get(new, "LASER_YAW") == "1.26" and ls.env_get(new, "OBSTACLE_STOP") == "1"
    assert ls.env_get(new, "LIDAR_MASK") == "0:0.30"


def test_scan_points_leave_out_the_robot_itself():
    s = scans("clear", 1)[0]
    ranges = [r if r is not None else math.inf for r in s.ranges]
    mask = ls.self_mask(scans("clear", 20), MOCK_LIDAR_YAW)
    every = scan_points(ranges, s.angle_min, s.inc, 0.05, 12.0, MOCK_LIDAR_YAW, 0.0, 450)
    kept = scan_points(ranges, s.angle_min, s.inc, 0.05, 12.0, MOCK_LIDAR_YAW, 0.0, 450, mask)
    near = lambda xy: sum(1 for i in range(0, len(xy), 2) if math.hypot(xy[i], xy[i + 1]) < 35)  # noqa: E731
    assert near(every) > 10 and near(kept) == 0


def test_steps_must_come_in_order():
    saved = []
    w = ls.LidarSetup(lambda step, n, t: MockBase().grab_scans(step, n, 0.0), lambda r: (saved.append(r) or True, "ok"))
    assert not w.measure("clear")[0]                   # not started
    assert not w.apply()[0]
    assert w.start()[0] and w.step == "clear"
    assert not w.measure("box")[0]                     # clear first
    assert w.measure("clear")[0] and w.step == "box"
    assert w.measure("box")[0] and w.step == "result"
    v = w.view()["result"]
    assert abs(v["yaw"] - MOCK_LIDAR_YAW) < 0.06 and v["mask"]
    assert w.apply()[0] and saved and w.step == "idle" and w.view()["saved"]["yaw_deg"] == round(math.degrees(saved[0].yaw))


def test_no_scans_is_a_clear_message():
    w = ls.LidarSetup(lambda step, n, t: None, lambda r: (True, ""))
    w.start()
    ok, msg = w.measure("clear")
    assert not ok and "No lidar scans" in msg and w.view()["error"]["key"] == "ls.err.noscan"


def test_robot_saves_with_a_backup_and_never_touches_the_robots_file_in_mock(tmp_path):
    so101 = tmp_path / "so101"
    (so101 / "config").mkdir(parents=True)
    real = so101 / "config" / "lidar.env"
    real.write_text("OBSTACLE_STOP=0\nLASER_YAW=1.17\n")
    base = MockBase()
    r = Robot(Config(so101_dir=str(so101), datasets_dir=str(tmp_path / "d"), health_period=60), base)
    try:
        assert r.lidar("start", "t")[0]
        assert r.lidar("clear", "t")[0] and r.lidar("box", "t")[0]
        assert r.studio_snapshot()["lidar_setup"]["step"] == "result"
        ok, msg = r.lidar("apply", "t")
        assert ok, msg
        assert real.read_text() == "OBSTACLE_STOP=0\nLASER_YAW=1.17\n"     # the robot's file: untouched in mock mode
        text = r.lidar_env.read_text()
        assert ls.env_get(text, "OBSTACLE_STOP") == "1" and abs(float(ls.env_get(text, "LASER_YAW")) - MOCK_LIDAR_YAW) < 0.06
        assert ls.parse_mask(ls.env_get(text, "LIDAR_MASK")) == base.lidar[1]
        assert r.obstacle_stop_on() and r.lidar_info()["mask_bins"] > 0
        assert r.lidar("start", "t")[0] and r.lidar("clear", "t")[0] and r.lidar("box", "t")[0] and r.lidar("apply", "t")[0]
        assert list(r.lidar_env.parent.glob("mock_lidar.env.bak_*"))      # the first save's file, kept
    finally:
        r.close()


def test_lidar_watch_restarts_the_wheels_once_when_scans_stop(tmp_path, monkeypatch):
    """Real-robot path with fakes: no program is started for real."""
    import os
    import time as _t
    r = Robot(Config(so101_dir=str(tmp_path / "x"), datasets_dir=str(tmp_path / "d"), health_period=60), MockBase())
    try:
        calls = []

        class Job:
            running, started = True, _t.time() - 100

        class Ros:
            age = 9.0

            def scan_age(self):
                return self.age

        r.mock, r.ros = False, Ros()
        monkeypatch.setattr(r.jobs, "get", lambda name: Job() if name == "base" else None)
        monkeypatch.setattr(r, "_stop_job", lambda name, who: calls.append(("stop", name)) or (True, ""))
        monkeypatch.setattr(r, "base", lambda on, who, prof="close": calls.append(("start", prof)) or (True, ""))
        real_exists = os.path.exists
        monkeypatch.setattr(os.path, "exists", lambda p: True if p == "/dev/rplidar" else real_exists(p))
        r._lidar_watch()
        assert calls == [("stop", "base"), ("start", "close")]
        r._lidar_watch()                                   # at most once a minute
        assert len(calls) == 2
        r._lidar_restart_t, r.ros.age = 0.0, 1.0           # scans flowing: nothing to do
        r._lidar_watch()
        assert len(calls) == 2
        r.lidar_env.parent.mkdir(parents=True, exist_ok=True)
        r.lidar_env.write_text("OBSTACLE_STOP=0\n")       # stop switched off: no lidar driver expected
        r.ros.age = 99.0
        r._lidar_watch()
        assert len(calls) == 2
    finally:
        r.mock = True
        r.close()


def test_lidar_watch_leaves_mock_mode_alone(tmp_path):
    r = Robot(Config(so101_dir=str(tmp_path / "x"), datasets_dir=str(tmp_path / "d"), health_period=60), MockBase())
    try:
        r._lidar_watch()
        assert not r.jobs.running("base") and r._lidar_restart_t == 0.0
    finally:
        r.close()
