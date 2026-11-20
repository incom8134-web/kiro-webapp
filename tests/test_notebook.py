"""The Colab training notebook: it is what make_notebook.py writes, every cell is valid Python, and its
cells 1, 2, 4 and 5 (everything except installing LeRobot and the GPU training itself) run here and make
a zip that the Studio page installs."""
import ast
import io
import json
import re
import sys
import tarfile
import types
from pathlib import Path

import pytest

from robot_server.base import MockBase
from robot_server.config import Config
from robot_server.robot import Robot

ROOT = Path(__file__).resolve().parent.parent
NB = ROOT / "colab" / "so101_train_colab.ipynb"
from test_training import model_files  # noqa: E402  (pytest puts tests/ on the path)


def cells():
    nb = json.loads(NB.read_text(encoding="utf-8"))
    return nb, ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def test_committed_notebook_is_the_generated_one():
    sys.path.insert(0, str(ROOT / "colab"))
    try:
        import make_notebook
        assert NB.read_text(encoding="utf-8") == make_notebook.render(), "run: python3 colab/make_notebook.py"
    finally:
        sys.path.pop(0)
        sys.modules.pop("make_notebook", None)


def test_every_cell_is_python_and_the_notebook_asks_for_a_t4():
    nb, code = cells()
    assert len(code) == 5
    for src in code:
        ast.parse(src)                      # the old notebook's cell 3 was a SyntaxError
    assert nb["metadata"]["colab"]["gpuType"] == "T4"
    assert 'STEPS = 20000' in code[0] and "assert STEPS >= 20000" in code[0] and 'SOURCE = "robot"' in code[0]
    assert "2026.10.7" in code[0] and re.search(r'"[0-9a-f]{64}"\)', code[0])     # cloudflared pinned with its SHA-256
    text = NB.read_text(encoding="utf-8")
    for secret in ("leeyoum", "AIza", "hf_", "API_KEY", "password"):
        assert secret not in text


def fake_package(where: Path, tag="pick_20261120_1405", episodes=3) -> Path:
    src = where / "src" / "pick"
    (src / "meta").mkdir(parents=True)
    (src / "meta" / "info.json").write_text(json.dumps({
        "codebase_version": "v3.0", "robot_type": "so_follower", "total_episodes": episodes, "total_frames": 90 * episodes, "fps": 30,
        "features": {"observation.images.top": {"dtype": "video"}, "observation.images.right_wrist": {"dtype": "video"},
                     "action": {"dtype": "float32"}}}))
    tar = where / f"{tag}.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        t.add(src, arcname="pick")
    return tar


def run(code, g):
    exec(compile(code, "<cell>", "exec"), g)


def cell1(code0: str, **kw) -> str:
    """Cell 1 with other settings (as if changed in Colab's form)."""
    for k, v in kw.items():
        code0, n = re.subn(rf"^{k} = .*$", f"{k} = {v!r}", code0, count=1, flags=re.M)
        assert n == 1, k
    return code0


def drive_mode(code, tmp_path, g):
    run(cell1(code[0], SOURCE="drive", LOCAL_ROOT=str(tmp_path / "so101"), USE_DRIVE=False), g)


def test_cells_run_without_colab_and_make_a_zip_the_studio_installs(tmp_path):
    _, code = cells()
    up = tmp_path / "content"
    up.mkdir()
    fake_package(up)
    g = {"__name__": "__main__"}
    drive_mode(code, tmp_path, g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "out"))
    run(code[2], g)                                         # cell 2: find, check, unpack
    assert g["NAME"] == "pick" and g["TAG"] == "pick_20261120_1405" and g["info"]["total_episodes"] == 3
    # what lerobot-train leaves behind after 20000 steps
    ck = tmp_path / "so101" / "outputs" / "pick_20261120_1405_act" / "checkpoints" / "020000"
    (ck / "pretrained_model").mkdir(parents=True)
    (ck / "training_state").mkdir()
    (ck / "training_state" / "training_step.json").write_text('{"step": 20000}')
    for n, b in model_files().items():
        (ck / "pretrained_model" / n).write_bytes(b)
    g["lerobot"] = types.SimpleNamespace(__version__="0.6.1")
    run(code[3], g)                                         # cell 4: finished, nothing to train
    assert g["cmd"] is None
    run(code[4], g)                                         # cell 5: the zip
    z = tmp_path / "out" / "pick_act_pretrained_model.zip"
    assert z.exists()
    cfg = Config(so101_dir=str(tmp_path / "no_so101"), policies_dir=str(tmp_path / "policies"),
                 datasets_dir=str(tmp_path / "d"), health_period=60)
    r = Robot(cfg, MockBase())
    try:
        info = r.train.install(z, "pick_act", "test")
        assert info["steps"] == 20000 and info["playable"]
        about = json.loads((tmp_path / "policies" / "pick_act" / "pretrained_model" / "kiro_training.json").read_text())
        assert about["package"] == "pick_20261120_1405" and about["episodes"] == 3
    finally:
        r.close()


def test_a_newer_package_of_the_same_dataset_is_unpacked_and_trained_apart(tmp_path):
    _, code = cells()
    up = tmp_path / "content"
    up.mkdir()
    fake_package(up, "pick_20261120_1405", episodes=3)
    g = {"__name__": "__main__"}
    drive_mode(code, tmp_path, g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "out"))
    run(code[2], g)
    import os
    import time
    time.sleep(0.01)
    newer = fake_package(tmp_path / "b", "pick_20261121_0900", episodes=5)
    os.replace(newer, up / newer.name)
    run(code[2], g)
    assert g["TAG"] == "pick_20261121_0900" and g["info"]["total_episodes"] == 5


def test_cell_2_refuses_a_damaged_package(tmp_path):
    _, code = cells()
    up = tmp_path / "content"
    up.mkdir()
    tar = fake_package(up)
    (up / (tar.name[:-7] + ".sha256")).write_text("0" * 64 + "  " + tar.name + "\n")
    g = {"__name__": "__main__"}
    drive_mode(code, tmp_path, g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "out"))
    with pytest.raises(SystemExit, match="SHA-256"):
        run(code[2], g)


def checkpoint(base: Path, n: int):
    ck = base / f"{n:06d}"
    (ck / "pretrained_model").mkdir(parents=True)
    (ck / "training_state").mkdir()
    (ck / "training_state" / "training_step.json").write_text(json.dumps({"step": n}))
    for name, b in model_files().items():
        (ck / "pretrained_model" / name).write_bytes(b)
    return ck


def test_drive_keeps_whole_checkpoint_copies_and_resume_skips_half_ones(tmp_path, monkeypatch):
    """Cell 4 with a stand-in lerobot-train: finished checkpoints go to Drive whole (size list, rename), the
    newest two stay, and after a cut session the resume ignores a half-copied checkpoint."""
    import os
    _, code = cells()
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "lerobot-train").write_text("#!/bin/sh\nexit 0\n")
    (fake / "lerobot-train").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    up = tmp_path / "content"
    up.mkdir()
    fake_package(up)
    g = {"__name__": "__main__"}
    drive_mode(code, tmp_path, g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "drive"))
    run(code[2], g)
    local = tmp_path / "so101" / "outputs" / "pick_20261120_1405_act" / "checkpoints"
    for n in (2000, 4000, 6000):
        checkpoint(local, n)
    os.symlink("006000", local / "last")
    run(code[3], g)                                         # "trains" (the stand-in), then copies to Drive
    drive = tmp_path / "drive" / "pick_20261120_1405_act" / "checkpoints"
    assert sorted(p.name for p in drive.iterdir()) == ["004000", "006000"]
    assert json.loads((drive / "006000" / "kiro_copy.json").read_text())["pretrained_model/model.safetensors"] > 0
    # the session is cut: local disk gone, and Drive holds a half copy of 008000 that looks newer
    import shutil
    shutil.rmtree(tmp_path / "so101" / "outputs")
    half = checkpoint(drive, 8000)
    (half / "pretrained_model" / "model.safetensors").unlink()
    g2 = {"__name__": "__main__"}
    drive_mode(code, tmp_path, g2)
    g2.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "drive"))
    run(code[2], g2)
    run(code[3], g2)
    assert any("--config_path=" in c and "006000" in c for c in g2["cmd"])   # resumed from the whole one
    assert (local / "006000").is_dir() and not (local / "008000").exists()   # (the half copy goes at the next save)


def test_cell_2_takes_robot_packages_from_the_drive_folder_first(tmp_path):
    _, code = cells()
    folder, top = tmp_path / "SO101", tmp_path / "MyDrive"
    folder.mkdir()
    top.mkdir()
    fake_package(folder, "pick_20261120_1405")
    import os
    import time
    time.sleep(0.01)
    fake_package(top, "other_20261121_0900")              # newer, but not in the package folder
    (top / "backup.tar.gz").write_bytes(b"not a package")
    g = {"__name__": "__main__"}
    drive_mode(code, tmp_path, g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(folder), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "out"))
    run(code[2], g)
    assert g["TAG"] == "pick_20261120_1405"
    assert os.path.basename(g["SRC"]) == "pick_20261120_1405.tar.gz"



FAKE_TRAIN = """#!{python}
import json, os, shutil, sys
args = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
out, steps = args["output_dir"], int(args["steps"])
ck = os.path.join(out, "checkpoints", f"{{steps:06d}}")
shutil.copytree(os.environ["FAKE_MODEL"], os.path.join(ck, "pretrained_model"))
os.makedirs(os.path.join(ck, "training_state"))
json.dump({{"step": steps}}, open(os.path.join(ck, "training_state", "training_step.json"), "w"))
os.symlink(f"{{steps:06d}}", os.path.join(out, "checkpoints", "last"))
print("INFO step:20K smpl:160K loss:0.050", flush=True)
"""


def wait_for(pred, timeout=30.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def test_train_in_colab_end_to_end_with_the_robot(tmp_path, monkeypatch):
    """The robot (robot_server/colab.py) and the notebook's cells, talking for real on this computer:
    package -> sent in pieces -> unpacked -> "trained" (a stand-in for lerobot-train) -> zipped -> fetched
    by the robot -> installed as a skill. Only the Cloudflare tunnel and the GPU are left out."""
    import os
    import sys
    from robot_server import colab as colab_mod
    monkeypatch.setenv("KIRO_NO_TUNNEL", "1")
    monkeypatch.setattr(colab_mod, "URL_RE", re.compile(r"http://127\.0\.0\.1:\d+"))
    monkeypatch.setattr(colab_mod, "CHUNK", 200)                       # several pieces even for a tiny package
    fake_model = tmp_path / "fake_model"
    fake_model.mkdir()
    for n, b in model_files().items():
        (fake_model / n).write_bytes(b)
    monkeypatch.setenv("FAKE_MODEL", str(fake_model))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "lerobot-train").write_text(FAKE_TRAIN.format(python=sys.executable))
    (bin_ / "lerobot-train").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")

    _, code = cells()
    g = {"__name__": "__main__"}
    run(cell1(code[0], LOCAL_ROOT=str(tmp_path / "colab"), USE_DRIVE=False), g)
    g.update(OUT_ROOT=str(tmp_path / "drive"))
    link = g["LINK"]
    assert link.url.startswith("http://127.0.0.1:") and len(link.key) >= 20

    ds = tmp_path / "datasets" / "tester" / "pick"
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text(json.dumps({
        "codebase_version": "v3.0", "robot_type": "so_follower", "total_episodes": 2, "total_frames": 60, "fps": 30,
        "features": {"observation.images.top": {"dtype": "video"}, "action": {"dtype": "float32"}}}))
    (ds / "data").mkdir()
    (ds / "data" / "file-000.parquet").write_bytes(os.urandom(3000))
    cfg = Config(so101_dir=str(tmp_path / "no_so101"), policies_dir=str(tmp_path / "policies"),
                 datasets_dir=str(tmp_path / "datasets"), backups_dir=str(tmp_path / "backups"),
                 packages_dir=str(tmp_path / "packages"), health_period=60)
    r = Robot(cfg, MockBase())
    try:
        r.colab.poll_s = 0.1
        assert r.colab.start("tester/pick", "Laptop") == (True, "")
        assert r.colab.start("tester/pick", "Laptop") == (False, "COLAB_BUSY")
        wait_for(lambda: r.colab.run["state"] == "waiting")              # packaged
        assert r.colab.connect("https://evil.example.com", link.key, "Laptop") == (False, "COLAB_ADDR")
        assert r.colab.connect(link.url, link.key, "Laptop") == (True, "")
        wait_for(lambda: link.st["state"] == "received")
        assert r.colab.snapshot()["host"] == link.url.split("//")[1] and "key" not in r.colab.snapshot()
        g["lerobot"] = types.SimpleNamespace(__version__="0.6.1")        # cell 2 (installing LeRobot) needs Colab
        run(code[2], g)                                                    # cell 3: the dataset from the robot
        assert g["TAG"] == r.colab.run["pkg"][:-7] and g["info"]["total_episodes"] == 2
        run(code[3], g)                                                    # cell 4: "train"
        wait_for(lambda: r.colab.run["step"] == 20000 and r.colab.run["state"] == "training")
        run(code[4], g)                                                    # cell 5: zip, the robot fetches it
        assert g["fetched"] is True
        run_ = wait_for(lambda: r.colab.run["state"] == "installed" and r.colab.run)
        name = run_["model"]
        assert name == g["TAG"] + "_act"
        m = {x["name"]: x for x in r.models()}[name]
        assert m["playable"] and m["arms"] == "right"
        assert (tmp_path / "drive" / "pick_act_pretrained_model.zip").exists()          # the Drive copy too
        assert any("trained in Colab and installed" in e["text"] for e in r.events)
    finally:
        r.close()


def test_a_wrong_key_gets_nothing_from_the_notebook(tmp_path, monkeypatch):
    import urllib.error
    import urllib.request
    monkeypatch.setenv("KIRO_NO_TUNNEL", "1")
    _, code = cells()
    g = {"__name__": "__main__"}
    run(cell1(code[0], LOCAL_ROOT=str(tmp_path / "colab"), USE_DRIVE=False), g)
    link = g["LINK"]
    for path, method in (("/status", "GET"), ("/model", "GET"), ("/fetched", "POST")):
        req = urllib.request.Request(link.url + path, method=method, data=b"" if method == "POST" else None,
                                     headers={"X-Kiro-Key": "x" * 32})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 403
    assert link.st["state"] == "waiting"
