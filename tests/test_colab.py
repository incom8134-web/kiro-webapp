"""Train in Colab, the robot's side (robot_server/colab.py): only the engineer, only a Cloudflare quick tunnel
address, never follows a redirect, survives a server restart, shows a failed Colab. The whole round trip
with the real notebook cells is in tests/test_notebook.py."""
import http.server
import json
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robot_server import colab as colab_mod
from robot_server.base import MockBase
from robot_server.config import Config
from robot_server.main import create_app
from robot_server.robot import Robot

KEY = "k" * 32


@pytest.fixture
def cfg(tmp_path):
    return Config(pin="4321", engineer_pin="8765", so101_dir=str(tmp_path / "no_so101"), policies_dir=str(tmp_path / "policies"),
                  datasets_dir=str(tmp_path / "datasets"), backups_dir=str(tmp_path / "backups"),
                  packages_dir=str(tmp_path / "packages"), health_period=60)


def put_dataset(cfg, repo="tester/pick"):
    d = Path(cfg.datasets_dir) / repo
    (d / "meta").mkdir(parents=True)
    (d / "meta" / "info.json").write_text(json.dumps({"codebase_version": "v3.0", "robot_type": "so_follower", "total_episodes": 1,
                                                       "total_frames": 30, "fps": 30, "features": {}}))
    return d


def wait(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not reached")


class Fake(http.server.BaseHTTPRequestHandler):
    """A pretend notebook: answers /status with whatever the test sets."""
    status: dict = {}
    redirect = False
    calls: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Fake.calls.append(self.path)
        if Fake.redirect:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        b = json.dumps(Fake.status).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(colab_mod, "URL_RE", re.compile(r"http://127\.0\.0\.1:\d+"))
    Fake.status, Fake.redirect, Fake.calls = {"kiro": 1, "state": "training", "have": None, "step": 4000, "steps": 20000}, False, []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def started(cfg) -> Robot:
    put_dataset(cfg)
    r = Robot(cfg, MockBase())
    r.colab.poll_s = 0.05
    assert r.colab.start("tester/pick", "Laptop") == (True, "")
    wait(lambda: r.colab.run["state"] == "waiting")
    return r


def test_only_cloudflare_quick_tunnels_and_a_real_key(cfg):
    r = Robot(cfg, MockBase())
    try:
        assert r.colab.connect("https://abc-def.trycloudflare.com", KEY, "Laptop") == (False, "COLAB_NORUN")   # nothing started
        assert r.colab.start("tester/none", "Laptop") == (False, "NO_DATASET")
        put_dataset(cfg)
        assert r.colab.start("tester/pick", "Laptop")[0]
        for url, key in (("http://abc.trycloudflare.com", KEY), ("https://abc.trycloudflare.com.evil.io", KEY),
                         ("https://10.42.0.1:8080", KEY), ("https://abc.trycloudflare.com/x?y", KEY),
                         ("https://abc.trycloudflare.com", "short"), ("https://abc.trycloudflare.com", "k" * 30 + "\n")):
            assert r.colab.connect(url, key, "Laptop") == (False, "COLAB_ADDR"), url
        assert colab_mod.URL_RE.fullmatch("https://quiet-river-1234.trycloudflare.com")
    finally:
        r.close()


def test_progress_from_colab_and_a_stop_on_the_robot(cfg, fake):
    r = started(cfg)
    try:
        Fake.status.update(state="training", have=r.colab.run["pkg"])
        assert r.colab.connect(fake, KEY, "Laptop")[0]
        s = wait(lambda: r.colab.snapshot()["step"] == 4000 and r.colab.snapshot())
        assert s["state"] == "training" and s["steps"] == 20000 and s["remote"] == "training"
        assert r.colab.stop("Laptop") == (True, "")
        assert r.colab.run["state"] == "stopped"
        n = len(Fake.calls)
        time.sleep(0.3)
        assert len(Fake.calls) <= n + 1                                   # it stopped asking
        assert r.colab.stop("Laptop") == (True, "") and r.colab.run is None   # a second press clears it
    finally:
        r.close()


def test_a_failed_colab_is_shown_and_a_new_session_can_be_connected(cfg, fake):
    r = started(cfg)
    try:
        Fake.status.update(state="failed", error="RuntimeError: CUDA out of memory")
        assert r.colab.connect(fake, KEY, "Laptop")[0]
        s = wait(lambda: r.colab.run["state"] == "failed" and r.colab.snapshot())
        assert s["error"] == "COLAB_FAILED" and "out of memory" in r.colab.run["detail"]
        Fake.status.update(state="training", error=None, have=r.colab.run["pkg"])
        assert r.colab.connect(fake, KEY, "Laptop")[0]                     # Run all again in Colab, Connect again
        wait(lambda: r.colab.run["state"] == "training")
    finally:
        r.close()


def test_a_redirect_is_never_followed(cfg, fake):
    r = started(cfg)
    try:
        Fake.redirect = True
        assert r.colab.connect(fake, KEY, "Laptop")[0]
        wait(lambda: len(Fake.calls) >= 3)
        assert all(c == "/status" for c in Fake.calls) and r.colab.run["state"] == "waiting"
    finally:
        r.close()


def test_not_the_notebook_is_refused(cfg, fake):
    r = started(cfg)
    try:
        Fake.status = {"hello": "world"}
        assert r.colab.connect(fake, KEY, "Laptop")[0]
        wait(lambda: r.colab.run["state"] == "failed")
        assert r.colab.run["error"] == "COLAB_NOTKIRO"
    finally:
        r.close()


def test_a_server_restart_goes_on_with_the_training(cfg, fake):
    r = started(cfg)
    Fake.status.update(state="training", have=r.colab.run["pkg"], step=6000)
    assert r.colab.connect(fake, KEY, "Laptop")[0]
    wait(lambda: r.colab.run["step"] == 6000)
    r.close()
    f = Path(cfg.backups_dir).parent / "mock_colab_run.json"                # mock mode keeps its own file
    assert f.stat().st_mode & 0o077 == 0                                   # the session key is kept private
    saved = json.loads(f.read_text())
    saved["state"] = "sending"                                             # as if stopped in the middle of sending
    f.write_text(json.dumps(saved))
    Fake.status.update(step=8000)
    r2 = Robot(cfg, MockBase())
    r2.colab.poll_s = 0.05
    try:
        wait(lambda: r2.colab.run["step"] == 8000)
        assert r2.colab.run["state"] == "training"
    finally:
        r2.close()


COLAB_MESSAGES = [{"t": "studio", "cmd": "colab", "action": "start", "repo": "tester/pick"},
                  {"t": "studio", "cmd": "colab", "action": "connect", "url": "https://a-b.trycloudflare.com", "key": KEY},
                  {"t": "studio", "cmd": "colab", "action": "stop"}]


@pytest.mark.parametrize("msg", COLAB_MESSAGES, ids=[m["action"] for m in COLAB_MESSAGES])
def test_train_in_colab_is_only_for_the_engineer(cfg, msg):
    put_dataset(cfg)

    def note(ws, about):
        for _ in range(300):
            m = ws.receive_json()
            if m["t"] == "note" and m.get("about") == about:
                return m
        raise AssertionError("no note")
    with TestClient(create_app(cfg, MockBase())) as client:
        with client.websocket_connect("/ws?name=Phone") as viewer, client.websocket_connect("/ws?name=Laptop") as op:
            viewer.send_json(msg)
            assert note(viewer, "studio")["msg"] == "Take control first."
            op.send_json({"t": "take", "pin": "4321"})
            assert note(op, "take")["ok"]
            op.send_json(msg)
            assert note(op, "studio")["msg"] == "Unlock the engineer tools first."
            assert client.app.state.data["robot"].colab.run is None
            op.send_json({"t": "engineer", "pin": "8765"})
            assert note(op, "engineer")["ok"]
            op.send_json(COLAB_MESSAGES[0])
            assert note(op, "studio")["ok"]
            st = None
            for _ in range(300):
                m = op.receive_json()
                if m["t"] == "studio" and m.get("colab"):
                    st = m["colab"]
                    break
            assert st and st["repo"] == "tester/pick" and "key" not in st


def test_a_local_address_only_in_mock_mode(cfg, monkeypatch):
    put_dataset(cfg)
    r = Robot(cfg, MockBase())
    try:
        assert r.colab.start("tester/pick", "Laptop")[0]
        assert r.colab.connect("http://127.0.0.1:8123", KEY, "Laptop")[0]          # the pretend robot: allowed
        r.mock = False                                                               # the real robot: never
        assert r.colab.connect("http://127.0.0.1:8123", KEY, "Laptop") == (False, "COLAB_ADDR")
    finally:
        r.close()
