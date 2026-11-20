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
    """A pretend notebook: answers /status with whatever the test sets, takes the package like the real one
    (then says "training" with it), or refuses the end of every upload (done_ok False)."""
    status: dict = {}
    redirect = False
    raw: bytes | None = None        # an odd answer instead of JSON
    done_ok = True
    calls: list = []

    def log_message(self, *a):
        pass

    def reply(self, code, body: bytes):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Fake.calls.append(self.path)
        if Fake.redirect:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        self.reply(200, Fake.raw if Fake.raw is not None else json.dumps(Fake.status).encode())

    def do_PUT(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        Fake.calls.append("PUT")
        self.reply(200, b'{"ok": true}')

    def do_POST(self):
        Fake.calls.append(self.path)
        m = re.fullmatch(r"/dataset/([^/]+)/done\?sha256=[0-9a-f]{64}", self.path)
        if m and Fake.done_ok:
            Fake.status.update(state="training", have=m.group(1))
            return self.reply(200, b'{"ok": true}')
        self.reply(400 if m else 200, b'{"error": "SHA-256 does not match"}' if m else b'{"ok": true}')


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(colab_mod, "URL_RE", re.compile(r"http://127\.0\.0\.1:\d+"))
    Fake.status, Fake.redirect, Fake.raw, Fake.done_ok, Fake.calls = (
        {"kiro": 1, "state": "waiting", "have": None, "step": 4000, "steps": 20000}, False, None, True, [])
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def started(cfg) -> Robot:
    put_dataset(cfg)
    (Path(cfg.datasets_dir) / "tester" / "pick" / "data.bin").write_bytes(b"x" * 1000)
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
        assert r.colab.connect(fake, KEY, "Laptop")[0]
        s = wait(lambda: r.colab.snapshot()["remote"] == "training" and r.colab.run["state"] == "training" and r.colab.snapshot())
        assert "PUT" in Fake.calls and any("/done?sha256=" in c for c in Fake.calls)       # sent by the robot itself
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
        Fake.status.update(state="waiting", error=None)
        assert r.colab.connect(fake, KEY, "Laptop")[0]                     # Run all again in Colab, Connect again
        wait(lambda: r.colab.run["state"] == "training" and r.colab.run["sent_to"] == fake)
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
    Fake.status.update(step=6000)
    assert r.colab.connect(fake, KEY, "Laptop")[0]
    wait(lambda: r.colab.run["step"] == 6000 and r.colab.run["state"] == "training")
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



def test_a_model_from_a_session_the_robot_did_not_send_to_is_never_installed(cfg, fake):
    """A crafted link to someone else's session that says "done, here is a model": refused, nothing installed."""
    r = started(cfg)
    try:
        Fake.status.update(state="done", have=r.colab.run["pkg"],
                           model={"file": "x.zip", "bytes": 100, "sha256": "0" * 64, "policy": "act"})
        assert r.colab.connect(fake, KEY, "Laptop")[0]
        wait(lambda: r.colab.run["state"] == "failed")
        assert r.colab.run["error"] == "COLAB_NOTOURS" and r.models() == []
        assert "/model" not in Fake.calls and "PUT" not in Fake.calls
    finally:
        r.close()


def test_odd_answers_never_stop_the_robot_from_asking(cfg, fake, monkeypatch):
    monkeypatch.setattr(colab_mod, "LOST_AFTER", 3)
    r = started(cfg)
    try:
        for raw in (b"not json", b"[1, 2]", b'{"kiro": 1, "state": "training", "have": 5, "step": "x", "model": "y"}'):
            Fake.raw = raw if raw.startswith(b"{") is False else None
            if raw.startswith(b"{"):
                Fake.status = json.loads(raw)
            assert r.colab.connect(fake, KEY, "Laptop")[0] or r.colab.run["state"] == "failed"
            time.sleep(0.4)
            assert r.colab._thread.is_alive() or r.colab.run["state"] == "failed"
        assert r.colab.run["state"] in ("lost", "failed")
        Fake.raw, Fake.status = None, {"kiro": 1, "state": "waiting", "have": None, "step": None, "steps": 20000}
        if r.colab.run["state"] == "failed":
            r.colab.run["state"] = "lost"
        assert r.colab.connect(fake, KEY, "Laptop")[0]
        wait(lambda: r.colab.run["state"] == "training" and r.colab.run["sent_to"] == fake)   # back on track
    finally:
        r.close()


def test_a_package_colab_keeps_refusing_is_sent_three_times_only(cfg, fake, monkeypatch):
    monkeypatch.setattr(colab_mod, "TRIES", 1)
    Fake.done_ok = False
    r = started(cfg)
    try:
        assert r.colab.connect(fake, KEY, "Laptop")[0]
        wait(lambda: r.colab.run["state"] in ("failed", "lost") and r.colab.run.get("rounds", 0) >= 3, timeout=30)
        wait(lambda: r.colab.run["state"] == "failed", timeout=30)
        assert r.colab.run["error"] == "COLAB_SEND"
        assert sum(1 for c in Fake.calls if "/done?" in c) == 3
    finally:
        r.close()


def test_a_stop_is_never_undone_by_a_late_answer(cfg):
    put_dataset(cfg)
    r = Robot(cfg, MockBase())
    try:
        assert r.colab.start("tester/pick", "Laptop")[0]
        run = r.colab.run
        assert r.colab.stop("Laptop")[0] and run["state"] == "stopped"
        assert r.colab._set(run, state="sending") is False and run["state"] == "stopped"
        assert r.colab.start("tester/pick", "Laptop")[0]
        assert r.colab._set(run, state="training") is False and r.colab.run is not run   # the old run cannot touch the new one
        r.colab.run["state"] = "sending"
        assert r.colab.connect("https://a-b.trycloudflare.com", KEY, "Laptop") == (False, "COLAB_MIDWAY")
    finally:
        r.close()


def test_no_other_site_may_frame_the_pages(cfg):
    with TestClient(create_app(cfg, MockBase())) as client:
        for page in ("/studio.html", "/", "/api/health"):
            r = client.get(page)
            assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
