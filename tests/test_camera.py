"""Camera preview: the real frame rate, and the top camera repair (fake scripts only, never the real ones)."""
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from robot_server import camera as cam_mod
from robot_server.camera import Camera

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A pretend old code folder (gemini_uvc_fix.sh), a pretend sudo and a pretend root helper. The state is
    a few files: colour (the colour node is there), nodev (the Gemini is not on USB), helper_ran."""
    so101 = tmp_path / "so101"
    so101.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    script(so101 / "gemini_uvc_fix.sh", f'''
[ "$1" = "--check" ] || exit 9
[ -e {state}/nodev ] && {{ echo "Gemini 335(2bc5) 가 USB 에 없음"; exit 2; }}
echo "Gemini: 2-1.1, 5000 Mbps"
[ -e {state}/colour ] && exit 0
exit 1
''')
    helper = script(tmp_path / "kiro-gemini-rebind", f'''
touch {state}/helper_ran
echo "rebinding 2-1.1"
[ -e {state}/helper_rc ] && {{ echo "helper said no"; exit "$(cat {state}/helper_rc)"; }}
[ -e {state}/helper_keeps_colour_away ] || touch {state}/colour
echo "OK 2-1.1 interfaces=7"
''')
    sudo = script(tmp_path / "sudo", '''
[ "$1" = "-n" ] || exit 99
shift
exec "$@"
''')
    monkeypatch.setattr(cam_mod, "SUDO", str(sudo))
    monkeypatch.setattr(cam_mod, "REBIND_HELPER", str(helper))
    monkeypatch.setattr(cam_mod, "_orbbec_running", lambda: False)   # the real robot may be running the ROS node
    monkeypatch.setattr(cam_mod, "COLOUR_WAIT_S", 0.8)
    monkeypatch.setattr(cam_mod, "COLOUR_POLL_S", 0.1)
    return type("Fake", (), {"so101": so101, "state": state, "helper": helper, "sudo": sudo})


def touch(fake, name, text=""):
    (fake.state / name).write_text(text)


def ran(fake):
    return (fake.state / "helper_ran").exists()


def test_nothing_is_done_when_the_colour_node_is_there(fake):
    touch(fake, "colour")
    ok, code, detail = cam_mod.repair_top(fake.so101)
    assert (ok, code) == (True, "OK_ALREADY") and "Gemini" in detail
    assert not ran(fake)


def test_camera_not_on_usb(fake):
    touch(fake, "nodev")
    assert cam_mod.repair_top(fake.so101)[:2] == (False, "NOT_CONNECTED")
    assert not ran(fake)


def test_the_orbbec_ros_node_blocks_the_repair(fake, monkeypatch):
    monkeypatch.setattr(cam_mod, "_orbbec_running", lambda: True)
    assert cam_mod.repair_top(fake.so101)[:2] == (False, "ORBBEC_RUNNING")
    assert not ran(fake)


def test_missing_colour_node_is_repaired_through_the_helper(fake):
    ok, code, detail = cam_mod.repair_top(fake.so101)
    assert (ok, code) == (True, "REPAIRED") and "OK 2-1.1" in detail
    assert ran(fake)
    assert cam_mod.repair_top(fake.so101)[:2] == (True, "OK_ALREADY")   # and now nothing more to do


def test_helper_or_sudo_rule_not_installed(fake, monkeypatch):
    monkeypatch.setattr(cam_mod, "REBIND_HELPER", str(fake.helper) + ".missing")
    ok, code, detail = cam_mod.repair_top(fake.so101)
    assert (ok, code) == (False, "NO_HELPER") and "not installed" in detail
    monkeypatch.setattr(cam_mod, "REBIND_HELPER", str(fake.helper))
    monkeypatch.setattr(cam_mod, "SUDO", str(fake.sudo.parent / "no-such-sudo"))
    assert cam_mod.repair_top(fake.so101)[:2] == (False, "NO_HELPER")
    # sudo is there but has no rule for the helper: it asks for a password (sudo -n refuses) and says so on stderr
    refusing = script(fake.sudo.parent / "sudo-refuses", 'echo "sudo: a password is required" >&2\nexit 1\n')
    monkeypatch.setattr(cam_mod, "SUDO", str(refusing))
    ok, code, detail = cam_mod.repair_top(fake.so101)
    assert (ok, code) == (False, "NO_HELPER") and "password" in detail
    assert not ran(fake)


@pytest.mark.parametrize("rc, want", [(2, "NOT_CONNECTED"), (3, "ORBBEC_RUNNING"), (4, "FAILED"), (1, "FAILED")])
def test_helper_exit_codes_become_result_codes(fake, rc, want):
    touch(fake, "helper_rc", str(rc))
    ok, code, detail = cam_mod.repair_top(fake.so101)
    assert (ok, code) == (False, want) and "helper said no" in detail
    assert not (fake.state / "colour").exists()


def test_failed_when_the_colour_node_does_not_come_back(fake):
    touch(fake, "helper_keeps_colour_away")
    t0 = time.monotonic()
    ok, code, detail = cam_mod.repair_top(fake.so101)
    assert (ok, code) == (False, "FAILED") and "did not come back" in detail
    assert 0.7 < time.monotonic() - t0 < 5            # waited, but only the limit


def test_old_script_missing_and_a_second_repair_at_the_same_time(fake):
    assert cam_mod.repair_top(fake.so101 / "nowhere")[:2] == (False, "FAILED")
    with cam_mod._repair_lock:                           # a repair is running: the second click does nothing
        ok, code, detail = cam_mod.repair_top(fake.so101)
    assert (ok, code) == (False, "FAILED") and "another repair" in detail
    assert not ran(fake)


def test_every_result_code_is_listed():
    assert set(cam_mod.REPAIR_CODES) == {"OK_ALREADY", "REPAIRED", "NOT_CONNECTED", "ORBBEC_RUNNING", "NO_HELPER", "FAILED"}


# ------------------------------------------------- the real files in deploy/
def test_the_root_helper_takes_no_arguments_and_needs_root():
    helper = DEPLOY / "kiro-gemini-rebind"
    assert os.access(helper, os.X_OK)
    r = subprocess.run(["bash", str(helper), "2-1.1"], capture_output=True, text=True, timeout=10)   # refused before anything else
    assert r.returncode == 4 and "no arguments" in r.stdout
    if os.geteuid() != 0:                                # as root it would really re-plug the camera: never run it there
        r = subprocess.run(["bash", str(helper)], capture_output=True, text=True, timeout=10)
        assert r.returncode == 4 and "root" in r.stdout
    text = helper.read_text()
    assert "2bc5" in text and "component_conta" in text and "unbind" in text and "sleep 2" in text
    assert "$1" not in text and "$@" not in text.replace('"$#"', "")      # never uses what it was given


def test_the_sudo_rule_names_exactly_the_helper_and_no_arguments():
    lines = [x for x in (DEPLOY / "kiro-camera.sudoers").read_text().splitlines() if x.strip() and not x.startswith("#")]
    assert lines == ['YOUR_USER ALL=(root) NOPASSWD: /usr/local/sbin/kiro-gemini-rebind ""']
    assert cam_mod.REBIND_HELPER == "/usr/local/sbin/kiro-gemini-rebind"


# ------------------------------------------------------- the real frame rate
def test_camera_info_has_the_real_frame_rate():
    cam = Camera("t", "mock:t", fps=10.0)
    try:
        assert cam.info()["fps_real"] is None                 # not open
        cam.attach()
        end = time.time() + 10
        while time.time() < end and cam.info()["fps_real"] is None:
            time.sleep(0.1)
        fps = cam.info()["fps_real"]
        assert fps is not None and 8 < fps < 40, fps         # the pretend camera runs at about 30 frames a second
        assert cam.info()["live"]
        cam.pause("test")
        assert cam.info()["fps_real"] is None and not cam.info()["live"]
    finally:
        cam.pause("end")
