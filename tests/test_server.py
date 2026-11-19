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
        assert "KIRO" in client.get("/").text


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
