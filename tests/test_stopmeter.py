"""Stop meter: response time, stop distance and creep, measured from the robot's own data."""
from robot_server.stopmeter import StopMeter, csv_lines


def drive(m, t, x, v, seconds, commanding, dt=0.05):
    for _ in range(int(round(seconds / dt))):
        t += dt
        x += v * dt
        m.sample(t, (x, 0.0), commanding)
    return t, x


def test_measures_a_dead_man_stop():
    m = StopMeter("base")
    t, x = drive(m, 0.0, 0.0, 0.2, 1.0, True)                 # 0.2 m/s forward (test E1)
    assert m.trigger(t, "gamepad", "LB released")
    t, x = drive(m, t, x, 0.2, 0.10, True)                   # the command is still non-zero for 0.1 s
    t, x = drive(m, t, x, 0.1, 0.10, False)                  # command zero, the wheels slow down
    t, x = drive(m, t, x, 0.0, 6.0, False)                   # still, and nothing moves for 5 s
    r = m.rows()[-1]
    assert r["test"] == "E1" and r["state"] == "done" and r["ok"] is True
    assert abs(r["cmd_s"] - 0.15) < 0.051 and abs(r["dist"] - 3.0) < 0.6 and r["creep"] == 0.0
    assert r["still_s"] <= 0.3


def test_creep_after_the_stop_is_caught():
    m = StopMeter("base")
    t, x = drive(m, 0.0, 0.0, 0.1, 1.0, True)
    m.trigger(t, "voice", "[정지]")
    t, x = drive(m, t, x, 0.0, 1.0, False)
    t, x = drive(m, t, x, 0.02, 1.0, False)                   # creeps 2 cm with no command: unintended motion
    t, x = drive(m, t, x, 0.0, 4.0, False)
    r = m.rows()[-1]
    assert r["creep"] >= 1.9 and r["test"] == "E2"


def test_no_measurement_when_standing_still_and_one_per_stop():
    m = StopMeter("base")
    t, x = drive(m, 0.0, 0.0, 0.0, 1.0, False)
    assert not m.trigger(t, "deadman")                        # nothing moved
    t, x = drive(m, t, x, 0.2, 1.0, True)
    assert m.trigger(t, "deadman")
    assert not m.trigger(t + 0.05, "base_stop")               # the same stop, not a second one
    t, x = drive(m, t, x, 0.2, 20.0, True)                     # it never stops: timeout, a fail
    assert m.rows()[-1]["state"] == "timeout" and m.rows()[-1]["ok"] is False


def test_arm_meter_uses_the_largest_joint_change():
    m = StopMeter("arm", unit_scale=1.0, still_speed=2.0, move_speed=5.0, norm="max")
    t = 0.0
    for i in range(20):
        t += 0.05
        m.sample(t, (i * 1.5, 0.0, 0.0), True)                # 30 deg/s on one joint
    assert m.trigger(t, "arm_link", "leader packets stopped")
    for i in range(10):                                       # HOLD: 3 more degrees, then still
        t += 0.05
        m.sample(t, (28.5 + min(i, 2) * 1.5, 0.0, 0.0), True)
    for _ in range(120):
        t += 0.05
        m.sample(t, (31.5, 0.0, 0.0), True)
    r = m.rows()[-1]
    assert r["test"] == "E5" and r["ok"] is True and abs(r["dist"] - 3.0) < 0.01 and r["still_s"] < 0.2
    lines = csv_lines([m])
    assert lines[0].startswith("time,meter,test") and ",arm,E5,arm_link," in lines[1]
