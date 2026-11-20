import pytest

from robot_server.base import MockBase
from robot_server import guard
from robot_server.guard import GuardCfg, apply, blocked, limits

G = GuardCfg()  # robot 0.45 x 0.35 m, stop at 0.20 m, slow from 0.45 m


def pt(x_m, y_m):
    return [round(x_m * 100), round(y_m * 100)]


def test_no_points_means_free():
    assert limits([], G) == {"front": 1.0, "back": 1.0, "left": 1.0, "right": 1.0}
    assert limits(None, G) == {"front": 1.0, "back": 1.0, "left": 1.0, "right": 1.0}


def test_far_obstacle_does_not_slow():
    assert limits(pt(G.half_len + 1.0, 0), G)["front"] == 1.0


def test_close_obstacle_in_front_stops_forward_only():
    lim = limits(pt(G.half_len + 0.15, 0.05), G)   # 15 cm ahead of the front edge
    assert lim["front"] == 0.0
    assert lim["back"] == lim["left"] == lim["right"] == 1.0
    assert apply(0.3, 0.0, lim) == (0.0, 0.0)       # forward is stopped
    assert apply(-0.3, 0.0, lim) == (-0.3, 0.0)     # backing away is always allowed
    assert apply(0.0, 0.2, lim) == (0.0, 0.2)       # sideways is free
    assert blocked(0.3, 0.0, lim) == ["front"]
    assert blocked(-0.3, 0.0, lim) == []


def test_slows_down_between_slow_and_stop_distance():
    half = limits(pt(G.half_len + (G.stop + G.slow) / 2, 0), G)["front"]
    assert half == pytest.approx(0.5, abs=0.03)
    vx, _ = apply(0.3, 0.0, {"front": half, "back": 1, "left": 1, "right": 1})
    assert 0 < vx < 0.3


def test_inside_the_stop_distance_is_stopped():
    # the lidar points arrive in whole centimetres, so test 1 cm inside and 3 cm outside the line
    assert limits(pt(G.half_len + G.stop - 0.01, 0), G)["front"] == 0.0
    assert limits(pt(G.half_len + G.stop + 0.03, 0), G)["front"] > 0.0


@pytest.mark.parametrize("p, side", [((-(G.half_len + 0.1), 0), "back"), ((0, G.half_wid + 0.1), "left"), ((0, -(G.half_wid + 0.1)), "right")])
def test_each_side(p, side):
    lim = limits(pt(*p), G)
    assert lim[side] == 0.0
    assert all(v == 1.0 for k, v in lim.items() if k != side)


def test_obstacle_beside_the_path_does_not_stop_forward():
    # 1 m to the left and a little ahead: not in the way of driving forward
    lim = limits(pt(G.half_len + 0.1, 1.0), G)
    assert lim["front"] == 1.0


def test_obstacle_off_a_corner_stops_both_directions():
    lim = limits(pt(G.half_len + 0.08, G.half_wid + 0.08), G)   # 11 cm off the front-left corner
    assert lim["front"] == 0.0 and lim["left"] == 0.0
    assert lim["back"] == 1.0 and lim["right"] == 1.0


def test_points_inside_the_robot_are_ignored():
    # the robot's own parts (a mast, an arm) must never block it
    assert limits(pt(0.1, 0.05) + pt(-0.2, -0.1), G) == {"front": 1.0, "back": 1.0, "left": 1.0, "right": 1.0}


def test_turning_is_never_scaled():
    lim = limits(pt(G.half_len + 0.05, 0), G)
    assert apply(0.0, 0.0, lim) == (0.0, 0.0)   # apply() only touches vx and vy; wz is not passed in at all


def test_mock_robot_stops_before_the_wall():
    """Drive the pretend robot at the front wall with the guard in the loop, as the server does."""
    b = MockBase()
    dt = 0.05
    for _ in range(int(60 / dt)):                # one minute of full speed forward
        lim = limits(b.scan(360)["xy"], G)
        vx, vy = apply(0.3, 0.0, lim)
        b.apply(vx, vy, 0.0, True, dt)
    wall_x = 3.5                                  # MOCK_ROOM front wall
    gap = wall_x - (b.x + G.half_len)
    assert G.stop - 0.02 <= gap <= G.stop + 0.05  # stopped about 20 cm in front of it, not in it


def test_mock_robot_without_guard_would_hit_the_wall():
    b = MockBase()
    for _ in range(int(60 / 0.05)):
        b.apply(0.3, 0.0, 0.0, True, 0.05)
    assert b.x > 3.5                              # this is the behaviour the guard prevents


def test_lidar_lost_only_after_it_worked():
    assert not guard.lidar_lost(True, None, 100.0)            # no lidar since the base started: drive without it
    assert not guard.lidar_lost(True, 99.6, 100.0)            # last scan 0.4 s ago
    assert guard.lidar_lost(True, 99.4, 100.0)                # 0.6 s without a scan: stop
    assert not guard.lidar_lost(False, 90.0, 100.0)           # the operator switched the obstacle stop off
