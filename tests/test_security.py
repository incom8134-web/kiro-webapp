"""Login, headers, confirm tokens, E-STOP torque block, lockout backoff, origin and host."""
import pytest
from fastapi.testclient import TestClient

from robot_server.config import Config
from robot_server.control import Controller
from robot_server.base import MockBase
from robot_server.main import create_app, response_headers


def login(client, pin="4321"):
    r = client.post("/api/login", json={"pin": pin}, headers={"origin": "http://testserver"})
    assert r.status_code == 200, r.text
    return client.cookies.get("kiro_session")


def note(ws, about, tries=80):
    for _ in range(tries):
        m = ws.receive_json()
        if m.get("t") == "note" and m.get("about") == about:
            return m
    raise AssertionError(about)


def confirm(ws, action):
    ws.send_json({"t": "confirm", "action": action})
    for _ in range(40):
        m = ws.receive_json()
        if m.get("t") == "confirm" and m.get("action") == action:
            assert m["ok"], m
            return m["token"]
    raise AssertionError(action)


def test_security_headers_on_pages_errors_and_https_only_hsts():
    with TestClient(create_app(Config(pin="4321"), MockBase())) as client:
        denied = client.get("/api/health")
        assert denied.status_code == 401
        for r in (denied, client.get("/")):
            h = r.headers
            assert h["x-content-type-options"] == "nosniff"
            assert h["referrer-policy"] == "no-referrer"
            assert h["x-frame-options"] == "DENY"
            csp = h["content-security-policy"]
            assert "script-src 'self'" in csp and "object-src 'none'" in csp
            assert "base-uri 'none'" in csp and "frame-ancestors 'none'" in csp
            assert "strict-transport-security" not in h
        assert client.get("/camera/top.mjpg").status_code == 401
        assert client.get("/camera/top.mjpg").headers["x-frame-options"] == "DENY"
    https = {k.decode(): v.decode() for k, v in response_headers([], "https", "/")}
    assert https["strict-transport-security"].startswith("max-age=")
    cam = {k.decode(): v.decode() for k, v in response_headers([(b"cache-control", b"no-store")], "http", "/camera/top.mjpg")}
    assert cam["cache-control"] == "no-store" and "script-src 'self'" in cam["content-security-policy"]


def test_one_pin_session_unlocks_engineer_tools_and_hides_the_default():
    cfg = Config(pin="4321", engineer_pin="9999")
    with TestClient(create_app(cfg, MockBase())) as client:
        assert client.post("/api/login", json={"pin": "4321"}).status_code == 403   # no Origin
        bad = client.post("/api/login", json={"pin": "nope"}, headers={"origin": "http://testserver"})
        assert bad.status_code == 401 and "0000" not in bad.text
        r = client.post("/api/login", json={"pin": "4321"}, headers={"origin": "http://testserver"})
        c = r.headers["set-cookie"].lower()
        assert "httponly" in c and "samesite=lax" in c and "secure" not in c
        sid = client.cookies.get("kiro_session")
        assert sid
        assert client.get("/api/session").status_code == 200
        assert client.get("/api/health").json()["ok"] is True
        with client.websocket_connect("/ws?name=Laptop", headers={"cookie": f"kiro_session={sid}"}) as op:
            first = op.receive_json()
            while first.get("t") != "state":
                first = op.receive_json()
            assert "default_pin" not in first and "mock_engineer_pin" not in first
            op.send_json({"t": "take"})                       # no PIN: the cookie is the login
            assert note(op, "take")["ok"]
            st = op.receive_json()
            while st.get("t") != "state":
                st = op.receive_json()
            assert st["you"]["role"] == "operator" and st["you"]["engineer"] is True


def test_pin_only_take_does_not_open_engineer_tools():
    with TestClient(create_app(Config(pin="4321", engineer_pin="9999"), MockBase())) as client:
        with client.websocket_connect("/ws?name=Laptop") as op:
            op.send_json({"t": "take", "pin": "4321"})
            assert note(op, "take")["ok"]
            st = op.receive_json()
            while st.get("t") != "state":
                st = op.receive_json()
            assert st["you"]["engineer"] is False


def test_confirm_token_is_required_once_and_torque_off_is_blocked_during_estop():
    cfg = Config(pin="4321", engineer_pin="9999")
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        op.send_json({"t": "take", "pin": "4321"})
        assert note(op, "take")["ok"]
        op.send_json({"t": "engineer", "pin": "9999"})
        assert note(op, "engineer")["ok"]
        op.send_json({"t": "system", "action": "shutdown"})
        assert "Confirmation required" in note(op, "system")["msg"]
        tok = confirm(op, "system:shutdown")
        op.send_json({"t": "system", "action": "shutdown", "confirm": tok})
        assert "Mock mode" in note(op, "system")["msg"]          # mock refuses to power off, after the token
        op.send_json({"t": "system", "action": "shutdown", "confirm": tok})
        assert "Confirmation required" in note(op, "system")["msg"]
        op.send_json({"t": "estop", "on": True})
        assert note(op, "estop")["ok"]
        op.send_json({"t": "servo", "cmd": "torque", "value": 0, "port": "/dev/ttyUSB0", "id": 1})
        assert "E-STOP" in note(op, "servo")["msg"]
        tok = confirm(op, "servo:torque_off")
        op.send_json({"t": "servo", "cmd": "torque", "value": 0, "port": "/dev/ttyUSB0", "id": 1, "confirm": tok})
        assert "E-STOP" in note(op, "servo")["msg"]
        op.send_json({"t": "arm", "side": "right", "action": "release"})
        assert "E-STOP" in note(op, "arm")["msg"]


def test_logs_and_stops_need_a_login_and_a_missing_origin_is_refused():
    from starlette.websockets import WebSocketDisconnect
    with TestClient(create_app(Config(pin="4321"), MockBase())) as client:
        assert client.get("/api/stops.csv").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": ""}) as ws:
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": "http://evil.example", "host": "evil.example"}) as ws:
                ws.receive_json()
        with client.websocket_connect("/ws?name=Dash") as dash:
            dash.send_json({"t": "logs"})
            m = dash.receive_json()
            while not (m.get("t") == "note" and m.get("about") == "logs"):
                m = dash.receive_json()
            assert m["ok"] is False


def test_later_pin_lockouts_get_longer():
    t = [0.0]
    c = Controller(Config(pin="1234"), clock=lambda: t[0])
    c.connect("a", "Laptop", ip="10.1.1.1")
    for _ in range(5):
        assert c.take("a", "nope")[0] is False
    assert "Wait" in c.take("a", "1234")[1]          # first lockout is still 30 s
    t[0] = 31
    for _ in range(5):
        assert c.take("a", "nope")[0] is False       # no success in between, so the next lockout doubles
    assert "Wait" in c.take("a", "1234")[1]
    t[0] = 62                                        # 30 s into the 60 s lockout
    assert "Wait" in c.take("a", "1234")[1]
    t[0] = 93
    assert c.take("a", "1234")[0]


def test_colab_url_must_be_https(monkeypatch):
    from robot_server.config import Config as C
    monkeypatch.setenv("KIRO_COLAB_URL", "http://evil.example/x")
    assert C.from_env().colab_url.startswith("https://")
    monkeypatch.setenv("KIRO_COLAB_URL", "javascript:alert(1)")
    assert C.from_env().colab_url.startswith("https://colab.research.google.com/")
