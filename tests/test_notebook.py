"""The Colab training notebook: it is what make_notebook.py writes, every cell is valid Python, and its
cells 1, 2, 4 and 5 (everything except installing LeRobot and the GPU training itself) run here and make
a zip that the Studio page installs."""
import ast
import io
import json
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
    assert 'STEPS = 20000' in code[0] and "assert STEPS >= 20000" in code[0]
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


def test_cells_run_without_colab_and_make_a_zip_the_studio_installs(tmp_path):
    _, code = cells()
    up = tmp_path / "content"
    up.mkdir()
    fake_package(up)
    g = {"__name__": "__main__"}
    run(code[0], g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "out"))
    run(code[1], g)                                         # cell 2: find, check, unpack
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
    run(code[0], g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "out"))
    run(code[1], g)
    import os
    import time
    time.sleep(0.01)
    newer = fake_package(tmp_path / "b", "pick_20261121_0900", episodes=5)
    os.replace(newer, up / newer.name)
    run(code[1], g)
    assert g["TAG"] == "pick_20261121_0900" and g["info"]["total_episodes"] == 5


def test_cell_2_refuses_a_damaged_package(tmp_path):
    _, code = cells()
    up = tmp_path / "content"
    up.mkdir()
    tar = fake_package(up)
    (up / (tar.name[:-7] + ".sha256")).write_text("0" * 64 + "  " + tar.name + "\n")
    g = {"__name__": "__main__"}
    run(code[0], g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "out"))
    with pytest.raises(SystemExit, match="SHA-256"):
        run(code[1], g)


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
    run(code[0], g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "drive"))
    run(code[1], g)
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
    run(code[0], g2)
    g2.update(USE_DRIVE=False, DRIVE_DIR=str(up), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "drive"))
    run(code[1], g2)
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
    run(code[0], g)
    g.update(USE_DRIVE=False, DRIVE_DIR=str(folder), LOCAL_ROOT=str(tmp_path / "so101"), OUT_ROOT=str(tmp_path / "out"))
    run(code[1], g)
    assert g["TAG"] == "pick_20261120_1405"
    assert os.path.basename(g["SRC"]) == "pick_20261120_1405.tar.gz"
