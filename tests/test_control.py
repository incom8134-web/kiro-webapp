import math

import pytest

from robot_server.config import Config
from robot_server.control import MAX_BAD_PINS, ZERO_TICKS, Controller


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def step(self, dt):
        self.t += dt


@pytest.fixture
def env():
    clock = Clock()
    c = Controller(Config(pin="4321"), clock)
    c.connect("a", "Laptop")
    c.connect("b", "Phone")
    return c, clock


def arm(c, cid="a"):
    """Take control and let go of the dead-man once, as a real operator does."""
    assert c.take(cid, "4321")[0]
    c.drive(cid, 0, 0, 0, en=False)


def run(c, clock, cid, seconds, **kw):
    cmd = None
    for _ in range(int(seconds * 20)):
        clock.step(0.05)
        c.drive(cid, **kw)
        cmd = c.tick()
    return cmd


def test_wrong_pin_is_refused(env):
    c, _ = env
    ok, msg = c.take("a", "0000")
    assert not ok and "Wrong" in msg and c.operator is None


def test_only_one_operator(env):
    c, _ = env
    assert c.take("a", "4321")[0]
    ok, msg = c.take("b", "4321")
    assert not ok and "Laptop" in msg and c.operator == "a"


def test_force_takeover_stops_robot(env):
    c, clock = env
    arm(c)
    assert run(c, clock, "a", 1, x=1, y=0, z=0, en=True).vx > 0
    assert c.take("b", "4321", force=True)[0]
    assert c.operator == "b"
    clock.step(0.05)
    assert c.tick().vx == 0


def test_pin_lockout(env):
    c, clock = env
    for _ in range(MAX_BAD_PINS):
        c.take("a", "bad")
    ok, msg = c.take("a", "4321")
    assert not ok and "Wait" in msg
    clock.step(31)
    assert c.take("a", "4321")[0]


def test_viewer_cannot_drive(env):
    c, clock = env
    arm(c)
    cmd = run(c, clock, "b", 2, x=1, y=0, z=0, en=True)
    assert (cmd.vx, cmd.vy, cmd.wz) == (0, 0, 0)
    assert not c.link_ok  # a viewer's messages do not count as the operator's heartbeat


def test_drive_ramps_up_and_respects_limits(env):
    c, clock = env
    arm(c)
    clock.step(0.05)
    c.drive("a", 1, 0, 0, en=True)
    c.tick()
    clock.step(0.05)
    c.drive("a", 1, 0, 0, en=True)
    first = c.tick()
    assert 0 < first.vx < c.cfg.max_vx  # not instant
    cmd = run(c, clock, "a", 2, x=5, y=0, z=-9, en=True)  # out-of-range input is clamped
    assert cmd.vx == pytest.approx(c.cfg.max_vx) and cmd.wz == pytest.approx(-c.cfg.max_wz)
    cmd = run(c, clock, "a", 2, x=1, y=0, z=0, en=True, turbo=True)
    assert cmd.vx == pytest.approx(c.cfg.turbo_vx)


def test_diagonal_is_not_faster(env):
    c, clock = env
    arm(c)
    cmd = run(c, clock, "a", 2, x=1, y=1, z=0, en=True)
    assert math.hypot(cmd.vx, cmd.vy) == pytest.approx(c.cfg.max_vx, rel=1e-6)


def test_deadman_release_stops_at_once(env):
    c, clock = env
    arm(c)
    run(c, clock, "a", 1, x=1, y=0, z=0, en=True)
    clock.step(0.05)
    c.drive("a", 1, 0, 0, en=False)
    assert c.tick().vx == 0


def test_watchdog_stops_when_messages_stop(env):
    c, clock = env
    arm(c)
    assert run(c, clock, "a", 1, x=1, y=0, z=0, en=True).vx > 0
    clock.step(c.cfg.heartbeat_timeout + 0.01)  # the laptop went silent
    cmd = c.tick()
    assert cmd.vx == 0 and cmd.publish  # zeros are sent first...
    for _ in range(ZERO_TICKS):
        clock.step(0.05)
        cmd = c.tick()
    assert not cmd.publish  # ...then the server goes silent
    assert not c.link_ok


def test_estop_anyone_can_press_only_operator_releases(env):
    c, clock = env
    arm(c)
    run(c, clock, "a", 1, x=1, y=0, z=0, en=True)
    assert c.set_estop("b", True)[0]  # a viewer presses it
    clock.step(0.05)
    c.drive("a", 1, 0, 0, en=True)
    assert c.tick().vx == 0
    assert not c.set_estop("b", False)[0]
    assert c.estop
    assert c.set_estop("a", False)[0]


def test_no_motion_after_estop_until_deadman_is_let_go(env):
    c, clock = env
    arm(c)
    c.set_estop("a", True)
    c.set_estop("a", False)
    cmd = run(c, clock, "a", 1, x=1, y=0, z=0, en=True)  # still holding the stick
    assert cmd.vx == 0
    c.drive("a", 0, 0, 0, en=False)  # let go once
    assert run(c, clock, "a", 1, x=1, y=0, z=0, en=True).vx > 0


def test_no_motion_right_after_take_while_holding(env):
    c, clock = env
    assert c.take("a", "4321")[0]
    assert run(c, clock, "a", 1, x=1, y=0, z=0, en=True).vx == 0


def test_disconnect_frees_the_lock(env):
    c, clock = env
    arm(c)
    c.disconnect("a")
    assert c.operator is None
    assert c.take("b", "4321")[0]


def test_silent_operator_loses_the_lock(env):
    c, clock = env
    arm(c)
    clock.step(c.cfg.operator_stale_s + 1)
    c.tick()
    assert c.operator is None


def test_garbage_input_is_safe(env):
    c, clock = env
    arm(c)
    cmd = run(c, clock, "a", 1, x="abc", y=None, z=float("nan"), en=True)
    assert (cmd.vx, cmd.vy, cmd.wz) == (0, 0, 0)


def test_engineer_tools_need_operator_and_their_own_pin():
    cfg = Config(pin="1234", engineer_pin="98765")
    c = Controller(cfg, clock=lambda: 0.0)
    c.connect("a", "Laptop")
    c.connect("b", "Phone")
    assert c.unlock_engineer("a", "98765") == (False, "Take control first.")
    assert c.take("a", "1234")[0]
    assert c.unlock_engineer("a", "1234")[0] is False          # the operator PIN is not enough
    assert c.unlock_engineer("a", "98765")[0] and c.is_engineer("a")
    assert c.snapshot("a")["you"]["engineer"] is True
    assert c.snapshot("b")["you"]["engineer"] is False
    assert c.take("b", "1234", force=True)[0]                   # a new operator clears the unlock
    assert not c.is_engineer("a") and not c.is_engineer("b")


def test_engineer_tools_off_without_pin_and_wrong_pins_lock_out():
    t = [0.0]
    c = Controller(Config(pin="1234"), clock=lambda: t[0])
    c.connect("a", "Laptop")
    assert c.take("a", "1234")[0]
    assert "Engineer tools are off" in c.unlock_engineer("a", "")[1]
    c.engineer_pin = "5555"
    for _ in range(5):
        assert c.unlock_engineer("a", "0000")[0] is False
    assert "Too many wrong PINs" in c.unlock_engineer("a", "5555")[1]
    t[0] = 31.0
    assert c.unlock_engineer("a", "5555")[0]


def test_wrong_pins_count_per_device_address_not_per_connection():
    t = [0.0]
    c = Controller(Config(pin="1234"), clock=lambda: t[0])
    for i in range(MAX_BAD_PINS):          # a new connection for every guess, same laptop
        c.connect(f"s{i}", "Laptop", ip="10.42.0.50")
        assert c.take(f"s{i}", "0000")[0] is False
        c.disconnect(f"s{i}")
    c.connect("fresh", "Laptop", ip="10.42.0.50")
    ok, msg = c.take("fresh", "1234")
    assert not ok and "Too many wrong PINs" in msg   # even the right PIN waits out the lockout
    c.connect("other", "Phone", ip="10.42.0.51")     # another device is not punished
    assert c.take("other", "1234")[0]


def test_a_typed_pin_is_accepted_even_0000_but_no_pin_is_not(monkeypatch):
    from robot_server.config import Config
    monkeypatch.delenv("KIRO_OPERATOR_PIN", raising=False)
    assert Config.from_env().pin_is_default                    # nothing typed: the real robot refuses
    monkeypatch.setenv("KIRO_OPERATOR_PIN", "0000")
    c = Config.from_env()
    assert c.pin == "0000" and not c.pin_is_default            # typed on purpose: accepted
    assert not Config(pin="4321").pin_is_default
