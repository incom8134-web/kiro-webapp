import time

from fastapi.testclient import TestClient

from robot_server.base import MockBase
from robot_server.config import Config
from robot_server.main import create_app


def read_until(ws, pred, tries=60):
    for _ in range(tries):
        m = ws.receive_json()
        if pred(m):
            return m
    raise AssertionError("message not received")


def test_page_and_health():
    with TestClient(create_app(Config(pin="4321"), MockBase())) as client:
        assert client.get("/api/health").json()["backend"] == "mock"
        assert "HAEBOT" in client.get("/").text


def test_take_drive_and_viewer_is_ignored():
    with TestClient(create_app(Config(pin="4321"), MockBase())) as client:
        with client.websocket_connect("/ws?name=Laptop") as op, client.websocket_connect("/ws?name=Phone") as viewer:
            assert read_until(op, lambda m: m["t"] == "state")["you"]["role"] == "viewer"
            op.send_json({"t": "take", "pin": "nope"})
            assert read_until(op, lambda m: m["t"] == "note")["ok"] is False
            op.send_json({"t": "take", "pin": "4321"})
            assert read_until(op, lambda m: m["t"] == "note")["ok"] is True
            op.send_json({"t": "drive", "x": 0, "y": 0, "z": 0, "en": False})

            viewer.send_json({"t": "take", "pin": "4321"})
            assert read_until(viewer, lambda m: m["t"] == "note")["ok"] is False

            end = time.time() + 1.0
            while time.time() < end:
                op.send_json({"t": "drive", "x": 1, "y": 0, "z": 0, "en": True, "src": "test"})
                viewer.send_json({"t": "drive", "x": -1, "y": 0, "z": 0, "en": True})
                time.sleep(0.05)
            s = read_until(op, lambda m: m["t"] == "state" and m["cmd"]["vx"] > 0.1)
            assert s["you"]["role"] == "operator" and s["operator"]["name"] == "Laptop" and s["odom"]["x"] > 0

            viewer.send_json({"t": "estop", "on": True})
            assert read_until(op, lambda m: m["t"] == "state" and m["estop"])["cmd"]["vx"] == 0


def test_operator_disconnect_frees_lock():
    with TestClient(create_app(Config(pin="4321"), MockBase())) as client:
        with client.websocket_connect("/ws?name=Phone") as other:
            with client.websocket_connect("/ws?name=Laptop") as op:
                op.send_json({"t": "take", "pin": "4321"})
                assert read_until(op, lambda m: m["t"] == "note")["ok"]
            other.send_json({"t": "take", "pin": "4321"})
            assert read_until(other, lambda m: m["t"] == "note")["ok"]


def test_pages_are_never_served_stale():
    with TestClient(create_app(Config(pin="4321"), MockBase())) as client:
        for path in ("/", "/app.js", "/style.css", "/dashboard.html"):
            assert client.get(path).headers["cache-control"] == "no-cache"


def test_dashboard_terminal_gets_the_server_log_without_pins():
    import logging
    kiro = logging.getLogger("kiro")
    old = kiro.level
    kiro.setLevel(logging.INFO)
    try:
        with TestClient(create_app(Config(pin="4321"), MockBase())) as client:
            logging.getLogger("kiro.test").warning("before the page   opened")
            with client.websocket_connect("/ws?name=Dashboard") as dash, client.websocket_connect("/ws?name=Laptop") as op:
                dash.send_json({"t": "logs"})
                first = read_until(dash, lambda m: m["t"] == "log")
                assert first["reset"]
                assert any(x["msg"] == "before the page opened" and x["lvl"] == "WARN" for x in first["lines"])
                op.send_json({"t": "take", "pin": "9876"})
                op.send_json({"t": "take", "pin": "4321"})
                m = read_until(dash, lambda m: m["t"] == "log" and any("took control" in x["msg"] for x in m["lines"]))
                assert m["lines"][-1]["n"] > first["lines"][-1]["n"]
                seen = " ".join(x["msg"] for x in first["lines"] + m["lines"])
                assert "4321" not in seen and "9876" not in seen
    finally:
        kiro.setLevel(old)


def test_obstacle_stop_blocks_driving_and_only_the_operator_can_switch_it():
    b = MockBase()
    b.x = 3.5 - 0.225 - 0.10           # the front edge is 10 cm from the front wall of the pretend room
    start = b.x
    with TestClient(create_app(Config(pin="4321"), b)) as client:
        with client.websocket_connect("/ws?name=Laptop") as op, client.websocket_connect("/ws?name=Phone") as viewer:
            op.send_json({"t": "take", "pin": "4321"})
            assert read_until(op, lambda m: m["t"] == "note")["ok"] is True
            op.send_json({"t": "drive", "x": 0, "y": 0, "z": 0, "en": False})

            def drive(seconds):
                end = time.time() + seconds
                while time.time() < end:
                    op.send_json({"t": "drive", "x": 1, "y": 0, "z": 0, "en": True, "src": "test"})
                    time.sleep(0.05)

            drive(1.0)
            s = read_until(op, lambda m: m["t"] == "state" and m["cmd"]["vx"] > 0.1)
            assert s["guard"]["on"] is True and s["guard"]["lidar"] is True and "front" in s["guard"]["block"]
            assert b.x == start        # the command asked for forward, the robot did not move

            viewer.send_json({"t": "guard", "on": False})   # a viewer cannot switch it off
            assert read_until(viewer, lambda m: m["t"] == "note")["ok"] is False
            assert read_until(op, lambda m: m["t"] == "state")["guard"]["on"] is True

            op.send_json({"t": "guard", "on": False})       # the operator can
            assert read_until(op, lambda m: m["t"] == "state" and not m["guard"]["on"])["guard"]["block"] == []
            drive(0.6)
            assert b.x > start         # with the obstacle stop off the command goes through


def test_obstacle_stop_can_start_switched_off():
    with TestClient(create_app(Config(pin="4321", guard=False), MockBase())) as client:
        with client.websocket_connect("/ws?name=Laptop") as op:
            assert read_until(op, lambda m: m["t"] == "state")["guard"]["on"] is False


class LosingLidar(MockBase):
    """A pretend robot whose lidar stops sending scans when told to (USB pulled, driver died)."""
    lost = False

    def scan(self, n_out=360):
        return None if self.lost else super().scan(n_out)


def test_lidar_lost_stops_everything_turning_included():
    b = LosingLidar()
    with TestClient(create_app(Config(pin="4321"), b)) as client:
        with client.websocket_connect("/ws?name=Laptop") as op:
            op.send_json({"t": "take", "pin": "4321"})
            assert read_until(op, lambda m: m["t"] == "note")["ok"] is True
            op.send_json({"t": "drive", "x": 0, "y": 0, "z": 0, "en": False})

            def drive(seconds):
                end = time.time() + seconds
                while time.time() < end:
                    op.send_json({"t": "drive", "x": 0.5, "y": 0, "z": 1, "en": True, "src": "test"})
                    time.sleep(0.05)

            drive(0.6)
            assert b.v[0] > 0 and b.v[2] > 0
            b.lost = True
            t0 = time.time()
            while time.time() - t0 < 2.0 and (b.v[0] or b.v[2]):
                drive(0.05)
            assert b.v == (0.0, 0.0, 0.0) and time.time() - t0 < 1.0      # test E4': stopped within 1 s
            s = read_until(op, lambda m: m["t"] == "state" and m["guard"]["lost"])
            assert "lidar" in s["guard"]["block"] and "seen" not in s["guard"]
            b.lost = False                                                # scans back: driving works again
            drive(0.6)
            assert b.v[0] > 0


def test_stop_meter_measures_a_web_dead_man_stop_and_serves_csv():
    """Test E1 (web path): letting go of the dead-man while driving is measured: time to command zero,
    stop distance, no creep; the measurement is on the page and in /api/stops.csv."""
    b = MockBase()
    with TestClient(create_app(Config(pin="4321"), b)) as client:
        with client.websocket_connect("/ws?name=Laptop") as op:
            op.send_json({"t": "take", "pin": "4321"})
            assert read_until(op, lambda m: m["t"] == "note")["ok"] is True
            op.send_json({"t": "drive", "x": 0, "y": 0, "z": 0, "en": False})
            end = time.time() + 1.2
            while time.time() < end:
                op.send_json({"t": "drive", "x": 1, "y": 0, "z": 0, "en": True, "src": "test"})
                time.sleep(0.05)
            end = time.time() + 1.0
            while time.time() < end:                 # dead-man let go; the page keeps its heartbeat
                op.send_json({"t": "drive", "x": 0, "y": 0, "z": 0, "en": False})
                time.sleep(0.05)
            r = read_until(op, lambda m: m["t"] == "robot" and any(s["kind"] == "deadman" and s["state"] != "measuring"
                                                                   for s in m["stops"]), tries=200)
            s = [s for s in r["stops"] if s["kind"] == "deadman"][-1]
            assert s["test"] == "E1" and s["cmd_s"] is not None and s["cmd_s"] <= 0.1 and s["dist"] <= 1.0
        csv = client.get("/api/stops.csv")
        assert csv.status_code == 200 and "deadman" in csv.text and csv.text.startswith("time,meter,test")


def test_guard_stop_kind_reads_the_old_guard_status():
    from robot_server.main import guard_stop_kind
    assert guard_stop_kind("scan 없음(0점) | 앞 - 좌 - 우 - 뒤 무시 m | 최근접 - | 가림 없음 | 정지 앞 0.12") == "no_scan"
    assert guard_stop_kind("scan OK(300점) | 앞 가림 좌 2.0 우 2.0 뒤 무시 m | 최근접 0.00 | 가림 +0°가림 +30°가림 | 정지") == "covered"
    assert guard_stop_kind("scan OK(432점) | 앞 1.0 좌 2.0 우 2.0 뒤 무시 m | 최근접 0.5 | 가림 없음 | 정지") is None
    assert guard_stop_kind(None) is None


def test_microphone_sound_only_from_the_operator_and_never_during_estop():
    with TestClient(create_app(Config(pin="4321"), MockBase())) as client:
        got = []
        client.app.state.data["robot"].voice_audio = got.append
        with client.websocket_connect("/ws?name=Laptop") as op, client.websocket_connect("/ws?name=Phone") as viewer:
            viewer.send_bytes(b"\x01\x00" * 10)                 # a viewer's microphone: dropped
            op.send_json({"t": "take", "pin": "4321"})
            assert read_until(op, lambda m: m["t"] == "note")["ok"] is True
            op.send_bytes(b"\x02\x00" * 10)
            op.send_json({"t": "ping", "ts": 1})                 # text still works between sound pieces
            read_until(op, lambda m: m["t"] == "pong")
            op.send_json({"t": "estop", "on": True})
            read_until(op, lambda m: m["t"] == "note" and m.get("about") == "estop")
            op.send_bytes(b"\x03\x00" * 10)                      # E-STOP: dropped
            op.send_json({"t": "ping", "ts": 2})
            read_until(op, lambda m: m["t"] == "pong")
        assert got == [b"\x02\x00" * 10]
