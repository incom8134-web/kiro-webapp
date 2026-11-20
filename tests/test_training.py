"""Studio training files: model zips from Colab (checks, install), downloads, deletes, two-arm models."""
import io
import json
import struct
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robot_server import training as tr_mod
from robot_server.base import MockBase
from robot_server.config import Config
from robot_server.main import create_app
from robot_server.robot import Robot
from robot_server.training import Refused, arms_of

@pytest.fixture
def cfg(tmp_path):   # no old code folder: no cameras, nothing on sys.path (the Studio tests use the pretend cameras)
    return Config(pin="4321", engineer_pin="8765", so101_dir=str(tmp_path / "no_so101"), policies_dir=str(tmp_path / "policies"),
                  datasets_dir=str(tmp_path / "datasets"), backups_dir=str(tmp_path / "backups"),
                  packages_dir=str(tmp_path / "packages"), health_period=60, skill_seconds=30)


@pytest.fixture
def robot(cfg):
    r = Robot(cfg, MockBase())
    yield r
    r.close()


def weights() -> bytes:
    head = json.dumps({"w": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]}}).encode()
    return struct.pack("<Q", len(head)) + head + b"\0" * 8


def config(dim=6, cams=("top", "right_wrist")) -> dict:
    ins = {"observation.state": {"type": "STATE", "shape": [dim]},
           **{f"observation.images.{c}": {"type": "VISUAL", "shape": [3, 480, 640]} for c in cams}}
    return {"type": "act", "input_features": ins, "output_features": {"action": {"type": "ACTION", "shape": [dim]}}}


PRE = "policy_preprocessor_step_3_normalizer_processor.safetensors"
POST = "policy_postprocessor_step_0_unnormalizer_processor.safetensors"


def model_files(dim=6, cams=("top", "right_wrist"), steps=20000) -> dict[str, bytes]:
    """The files of a pretrained_model folder as lerobot-train 0.6.1 writes them."""
    pre = {"name": "policy_preprocessor", "steps": [{"registry_name": "to_batch_processor", "config": {}},
                                                    {"registry_name": "normalizer_processor", "config": {}, "state_file": PRE}]}
    post = {"name": "policy_postprocessor", "steps": [{"registry_name": "unnormalizer_processor", "config": {}, "state_file": POST}]}
    return {"config.json": json.dumps(config(dim, cams)).encode(), "model.safetensors": weights(),
            "train_config.json": json.dumps({"steps": steps, "batch_size": 8, "dataset": {"repo_id": "local/pick"}}).encode(),
            "policy_preprocessor.json": json.dumps(pre).encode(), PRE: weights(),
            "policy_postprocessor.json": json.dumps(post).encode(), POST: weights()}


def make_zip(files: dict[str, bytes], prefix="pretrained_model/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if prefix:
            zf.writestr(prefix, b"")
        for n, b in files.items():
            zf.writestr(prefix + n, b)
    return buf.getvalue()


def put_model(root: Path, name: str, **kw) -> Path:
    d = root / name / "pretrained_model"
    d.mkdir(parents=True)
    for n, b in model_files(**kw).items():
        (d / n).write_bytes(b)
    return d


def install(robot, tmp_path, data: bytes, name="pick_act"):
    z = tmp_path / "up.zip"
    z.write_bytes(data)
    return robot.train.install(z, name, "Laptop")


# ------------------------------------------------------------ which arm
def test_arms_from_the_model_config():
    assert arms_of(config(6, ("top", "right_wrist"))) == "right"
    assert arms_of(config(6, ("top", "left_wrist"))) == "left"
    assert arms_of(config(12, ("top", "right_wrist", "left_wrist"))) == "both"
    assert arms_of(config(12, ("top",))) == "both"
    assert arms_of({"type": "act", "input_features": {"observation.images.top": {}}}) == "?"   # says nothing: played as before
    assert arms_of({}) == "?" and arms_of({"input_features": [], "output_features": None}) == "?"
    assert arms_of(config(7)) == "other"                                                        # fits no arm of this robot
    mixed = config(6)
    mixed["input_features"]["observation.state"]["shape"] = [12]
    assert arms_of(mixed) == "other"


def test_two_arm_and_left_arm_models_cannot_be_played_yet(robot, cfg):
    root = Path(cfg.policies_dir)
    put_model(root, "right_act")
    put_model(root, "both_act", dim=12, cams=("top", "right_wrist", "left_wrist"))
    put_model(root, "left_act", cams=("top", "left_wrist"))
    ms = {m["name"]: m for m in robot.models()}
    assert ms["right_act"]["playable"] and ms["right_act"]["arms"] == "right"
    assert not ms["both_act"]["playable"] and ms["both_act"]["arms"] == "both"
    assert not ms["left_act"]["playable"] and ms["left_act"]["arms"] == "left"
    assert robot.skill("both_act", True, "Laptop") == (False, "TWO_ARM")
    assert robot.skill("left_act", True, "Laptop") == (False, "LEFT_ARM")
    assert not robot.jobs.running("skill")
    assert robot.skill("right_act", True, "Laptop")[0]                      # one-arm playback unchanged
    assert robot.jobs.running("skill")
    robot.skill("", False, "Laptop")


def test_model_details_for_the_file_list(robot, cfg):
    put_model(Path(cfg.policies_dir), "pick_act", steps=20000)
    (Path(cfg.policies_dir) / ".incoming" / "x" / "pretrained_model").mkdir(parents=True)   # an upload in progress
    m = robot.train.models()
    assert [x["name"] for x in m] == ["pick_act"]
    assert m[0]["steps"] == 20000 and m[0]["dataset"] == "local/pick" and m[0]["batch"] == 8 and m[0]["type"] == "act"
    assert m[0]["cams"] == ["right_wrist", "top"] and m[0]["files"] == 7 and m[0]["mtime"] > 0


def test_mock_mode_never_uses_the_real_policies_folder(tmp_path):
    r = Robot(Config(so101_dir=str(tmp_path / "no_so101"), datasets_dir=str(tmp_path / "d"), health_period=60), MockBase())
    try:
        assert r.train.root == Path("~/.cache/kiro-webapp/mock_policies").expanduser()
        assert r.train.pkg_dir == tmp_path / "mock_uploads"
    finally:
        r.close()


# --------------------------------------------------------------- install
@pytest.mark.parametrize("prefix", ["pretrained_model/", "", "right_arm_task2_act/pretrained_model/"])
def test_a_colab_model_zip_is_installed_and_becomes_a_skill(robot, cfg, tmp_path, prefix):
    info = install(robot, tmp_path, make_zip(model_files(), prefix))
    d = Path(cfg.policies_dir) / "pick_act" / "pretrained_model"
    assert info["name"] == "pick_act" and info["playable"] and info["steps"] == 20000
    assert sorted(p.name for p in d.iterdir()) == sorted(model_files())
    assert (d / "model.safetensors").read_bytes() == weights()
    assert [m["name"] for m in robot.snapshot()["models"]] == ["pick_act"]
    assert not any((Path(cfg.policies_dir) / ".incoming").iterdir())      # nothing left behind


def test_mac_zips_are_accepted_without_their_extra_files(robot, cfg, tmp_path):
    files = {**model_files(), ".DS_Store": b"x"}
    data = make_zip(files)
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf, "a") as zf:
        zf.writestr("__MACOSX/pretrained_model/._config.json", b"x")
    install(robot, tmp_path, buf.getvalue())
    assert not (Path(cfg.policies_dir) / "pick_act" / "pretrained_model" / ".DS_Store").exists()


def link_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, b in model_files().items():
            zf.writestr("pretrained_model/" + n, b)
        zi = zipfile.ZipInfo("pretrained_model/README.md")
        zi.external_attr = (0o120777 << 16)                              # a symbolic link entry
        zf.writestr(zi, "/etc/passwd")
    return buf.getvalue()


def encrypted_zip() -> bytes:
    data = bytearray(make_zip(model_files()))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        offs = [i.header_offset for i in zf.infolist()]
    for off in offs:                                                     # set the "encrypted" flag in every local header
        data[off + 6] |= 1
    cd = bytes(data).find(b"PK\x01\x02")
    while cd != -1:
        data[cd + 8] |= 1
        cd = bytes(data).find(b"PK\x01\x02", cd + 4)
    return bytes(data)


REFUSED = {
    "path": (lambda: make_zip({**model_files(), "../evil.json": b"{}"}), "BAD_PATH"),
    "absolute": (lambda: make_zip({**model_files(), "/etc/x.json": b"{}"}, prefix=""), "BAD_PATH"),
    "backslash": (lambda: make_zip({**model_files(), "..\\evil.json": b"{}"}), "BAD_PATH"),
    "link": (link_zip, "BAD_PATH"),
    "pickle": (lambda: make_zip({**model_files(), "model.bin": b"x"}), "BAD_FILE"),
    "script": (lambda: make_zip({**model_files(), "run.sh": b"rm -rf ~"}), "BAD_FILE"),
    "checkpoint": (lambda: make_zip({"pretrained_model/" + k: v for k, v in model_files().items()}
                                    | {"training_state/optimizer_state.safetensors": weights()}, prefix="020000/"), "EXTRA_FILES"),
    "no_weights": (lambda: make_zip({k: v for k, v in model_files().items() if k != "model.safetensors"}), "NO_MODEL"),
    "two_models": (lambda: make_zip({**{"a/" + k: v for k, v in model_files().items()},
                                     **{"b/" + k: v for k, v in model_files().items()}}, prefix=""), "MANY_MODELS"),
    "bad_weights": (lambda: make_zip({**model_files(), "model.safetensors": b"not a tensor file"}), "BAD_WEIGHTS"),
    "bad_config": (lambda: make_zip({**model_files(), "config.json": b'{"type": "act"}'}), "BAD_CONFIG"),
    "bad_json": (lambda: make_zip({**model_files(), "policy_preprocessor.json": b"{oops"}), "BAD_CONFIG"),
    "no_processor": (lambda: make_zip({k: v for k, v in model_files().items() if k != "policy_postprocessor.json"}), "MISSING"),
    "no_state_file": (lambda: make_zip({k: v for k, v in model_files().items() if k != PRE}), "MISSING"),
    "not_zip": (lambda: b"this is not a zip file", "NOT_ZIP"),
    "encrypted": (encrypted_zip, "ENCRYPTED"),
    "deep": (lambda: make_zip(model_files(), prefix="a/b/c/d/"), "BAD_PATH"),
}


@pytest.mark.parametrize("case", REFUSED, ids=list(REFUSED))
def test_bad_model_zips_are_refused_and_leave_nothing(robot, cfg, tmp_path, case):
    make, code = REFUSED[case]
    with pytest.raises(Refused) as e:
        install(robot, tmp_path, make())
    assert e.value.code == code
    root = Path(cfg.policies_dir)
    assert not (root / "pick_act").exists() and robot.models() == []
    assert not (tmp_path / "evil.json").exists() and not (root / "evil.json").exists()
    assert not any((root / ".incoming").iterdir()) if (root / ".incoming").exists() else True


def test_too_big_unpacked_is_refused(robot, tmp_path, monkeypatch):
    monkeypatch.setattr(tr_mod, "UNPACKED_MAX", 1000)
    with pytest.raises(Refused) as e:
        install(robot, tmp_path, make_zip({**model_files(), "model.safetensors": weights() + b"\0" * 5000}))
    assert e.value.code == "TOO_BIG"


@pytest.mark.parametrize("name", ["", "../x", ".hidden", "a/b", "x" * 81, "이름", "a b"])
def test_model_names_are_plain(robot, tmp_path, name):
    with pytest.raises(Refused) as e:
        install(robot, tmp_path, make_zip(model_files()), name=name)
    assert e.value.code == "NAME"


def test_an_installed_model_is_never_overwritten(robot, cfg, tmp_path):
    put_model(Path(cfg.policies_dir), "pick_act", steps=100)
    with pytest.raises(Refused) as e:
        install(robot, tmp_path, make_zip(model_files()))
    assert e.value.code == "EXISTS"
    assert robot.train.models()[0]["steps"] == 100
    with pytest.raises(Refused) as e:
        robot.train.begin_upload("pick_act", 10, "Laptop")
    assert e.value.code == "EXISTS"


def test_one_upload_at_a_time_and_size_limit(robot):
    tmp = robot.train.begin_upload("a_act", 1000, "Laptop")
    with pytest.raises(Refused) as e:
        robot.train.begin_upload("b_act", 1000, "Phone")
    assert e.value.code == "BUSY" and robot.train.snapshot()["uploading"]["name"] == "a_act"
    robot.train.end_upload(tmp)
    with pytest.raises(Refused) as e:
        robot.train.begin_upload("b_act", tr_mod.ZIP_MAX + 1, "Laptop")
    assert e.value.code == "TOO_BIG" and robot.train.uploading is None


def test_model_download_is_a_zip_that_installs_again(robot, cfg, tmp_path):
    put_model(Path(cfg.policies_dir), "pick_act")
    data = b"".join(robot.train.zip_stream("pick_act"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == sorted("pretrained_model/" + n for n in model_files())
        assert zf.testzip() is None
    install(robot, tmp_path, data, name="copy_act")
    assert {m["name"] for m in robot.train.models()} == {"pick_act", "copy_act"}
    assert b"".join(robot.train.zip_stream("../pick_act")) == b""


# ---------------------------------------------------------------- delete
def put_dataset(root: Path, repo: str) -> Path:
    d = root / repo
    (d / "meta").mkdir(parents=True)
    (d / "meta" / "info.json").write_text(json.dumps({"total_episodes": 3, "total_frames": 90, "fps": 30, "robot_type": "so101_follower",
                                                      "features": {"observation.images.top": {"dtype": "video"}}}))
    (d / "data").mkdir()
    (d / "data" / "file-000.parquet").write_bytes(b"x" * 10)
    return d


def test_delete_a_model_only_when_it_is_not_playing(robot, cfg):
    root = Path(cfg.policies_dir)
    put_model(root, "pick_act")
    (root / "notes.txt").write_text("keep")
    assert robot.train.delete("model", "../policies", "Laptop") == (False, "NOT_FOUND")
    assert robot.train.delete("model", "notes.txt", "Laptop") == (False, "NOT_FOUND")
    assert robot.skill("pick_act", True, "Laptop")[0]
    assert robot.train.delete("model", "pick_act", "Laptop") == (False, "IN_USE")
    robot.skill("", False, "Laptop")
    assert robot.train.delete("model", "pick_act", "Laptop") == (True, "")
    assert not (root / "pick_act").exists() and (root / "notes.txt").exists()
    assert any("deleted model pick_act" in e["text"] for e in robot.events)


def test_delete_a_dataset_never_touches_calibration_or_other_files(robot, cfg):
    root = Path(cfg.datasets_dir)
    d = put_dataset(root, "tester/pick")
    put_dataset(root, "tester/keep")
    calib = root / "calibration" / "robots" / "so_follower"
    calib.mkdir(parents=True)
    (calib / "right_follower.json").write_text("{}")
    put_dataset(root, "calibration/robots")                    # even if it looked like a dataset
    for bad in ("calibration/robots", "tester/none", "../datasets", "tester/../tester/pick", "tester"):
        assert robot.train.delete("dataset", bad, "Laptop") == (False, "NOT_FOUND"), bad
    assert (calib / "right_follower.json").exists()
    assert robot.train.delete("dataset", "tester/pick", "Laptop") == (True, "")
    assert not d.exists() and (root / "tester" / "keep").exists()
    assert [x["repo"] for x in robot.rec.datasets()] == ["calibration/robots", "tester/keep"] or \
        sorted(x["repo"] for x in robot.rec.datasets()) == ["calibration/robots", "tester/keep"]


def test_delete_a_dataset_waits_for_the_recorder(robot, cfg):
    put_dataset(Path(cfg.datasets_dir), "tester/pick")
    assert robot.rec.pack("tester/pick", "Laptop")[0]
    assert robot.train.delete("dataset", "tester/pick", "Laptop") == (False, "IN_USE")


def test_package_lists_downloads_and_deletes(robot, cfg):
    put_dataset(Path(cfg.datasets_dir), "tester/pick")
    assert robot.rec.pack("tester/pick", "Laptop")[0]
    end = time.time() + 15
    while time.time() < end and not (robot.rec.pack_state() or {}).get("ok"):
        time.sleep(0.05)
    pk = robot.train.packages()
    assert len(pk) == 1 and pk[0]["name"] == "pick" and pk[0]["sha"] and pk[0]["file"].startswith("pick_")
    pkg = Path(cfg.packages_dir)
    (pkg / "server_train.sh").write_text("shared")
    (pkg / (pk[0]["file"][:-7] + "_README.md")).write_text("readme")
    assert robot.train.package_path(pk[0]["file"]) == pkg / pk[0]["file"]
    assert robot.train.package_path(pk[0]["sha"]) == pkg / pk[0]["sha"]
    for bad in ("server_train.sh", "../datasets/tester/pick/meta/info.json", pk[0]["file"][:-7] + "_README.md"):
        assert robot.train.package_path(bad) is None
    assert robot.train.delete("package", "server_train.sh", "Laptop") == (False, "NOT_FOUND")
    assert robot.train.delete("package", pk[0]["file"], "Laptop") == (True, "")
    assert sorted(p.name for p in pkg.iterdir()) == ["server_train.sh"]


# ----------------------------------------------------- over the network
def take_and_unlock(ws):
    ws.send_json({"t": "take", "pin": "4321"})
    ws.send_json({"t": "engineer", "pin": "8765"})
    for _ in range(400):
        m = ws.receive_json()
        if m["t"] == "studio":
            return m
    raise AssertionError("no studio state")


def note(ws, about, tries=400):
    for _ in range(tries):
        m = ws.receive_json()
        if m["t"] == "note" and m.get("about") == about:
            return m
    raise AssertionError("no note")


def test_every_training_endpoint_needs_the_engineer(cfg):
    put_model(Path(cfg.policies_dir), "pick_act")
    with TestClient(create_app(cfg, MockBase())) as client:
        urls = ["/api/studio/models/pick_act.zip", "/api/studio/packages/x_20261120_1200.tar.gz", "/api/studio/notebook.ipynb"]
        for u in urls:
            assert client.get(u).status_code == 403
            assert client.get(u + "?token=guess").status_code == 403
        assert client.post("/api/studio/models?name=a_act", content=make_zip(model_files())).status_code == 403
        assert client.post("/api/studio/models?name=a_act", content=b"x", headers={"X-Studio-Token": "guess"}).status_code == 403
        with client.websocket_connect("/ws?name=Laptop") as op:
            op.send_json({"t": "take", "pin": "4321"})
            note(op, "take")
            for u in urls:                                          # the operator without the engineer PIN: still no
                assert client.get(u).status_code == 403
            tok = take_and_unlock(op)["token"]
            assert client.get(f"/api/studio/models/pick_act.zip?token={tok}").status_code == 200
            assert client.get(f"/api/studio/models/nope.zip?token={tok}").status_code == 404
            assert client.get(f"/api/studio/packages/x_20261120_1200.tar.gz?token={tok}").status_code == 404
            assert client.post("/api/studio/models?name=a_act", content=make_zip(model_files()),
                               headers={"X-Studio-Token": tok, "Origin": "http://evil.example"}).status_code == 403
            op.send_json({"t": "engineer", "lock": True})
            time.sleep(0.3)
            assert client.get(f"/api/studio/models/pick_act.zip?token={tok}").status_code == 403
            assert client.post("/api/studio/models?name=a_act", content=make_zip(model_files()),
                               headers={"X-Studio-Token": tok}).status_code == 403
        assert not (Path(cfg.policies_dir) / "a_act").exists()


def test_upload_download_and_delete_over_http_and_websocket(cfg):
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Laptop") as op:
        st = take_and_unlock(op)
        tok = st["token"]
        assert st["train"]["models"] == [] and "colab.research.google.com/github/" in st["train"]["colab"]
        h = {"X-Studio-Token": tok}
        r = client.post("/api/studio/models?name=pick_act", content=make_zip(model_files()), headers=h)
        assert r.status_code == 200 and r.json()["model"]["name"] == "pick_act", r.text
        r = client.post("/api/studio/models?name=pick_act", content=make_zip(model_files()), headers=h)
        assert r.status_code == 409 and r.json()["code"] == "EXISTS"
        r = client.post("/api/studio/models?name=bad_act", content=make_zip({**model_files(), "x.pt": b"x"}), headers=h)
        assert r.status_code == 400 and r.json()["code"] == "BAD_FILE"
        r = client.get(f"/api/studio/models/pick_act.zip?token={tok}")
        assert r.status_code == 200 and 'filename="pick_act_pretrained_model.zip"' in r.headers["content-disposition"]
        assert zipfile.ZipFile(io.BytesIO(r.content)).read("pretrained_model/model.safetensors") == weights()
        nb = client.get(f"/api/studio/notebook.ipynb?token={tok}")
        assert nb.status_code == 200 and json.loads(nb.content)["nbformat"] == 4
        for _ in range(100):                                      # the model is now an Autonomous skill
            m = op.receive_json()
            if m["t"] == "robot":
                assert [x["name"] for x in m["models"]] == ["pick_act"]
                break
        op.send_json({"t": "studio", "cmd": "delete", "kind": "model", "name": "pick_act"})
        n = note(op, "studio")
        assert n["ok"] and n["cmd"] == "delete"
        assert not (Path(cfg.policies_dir) / "pick_act").exists()
        op.send_json({"t": "studio", "cmd": "delete", "kind": "dataset", "name": "calibration/robots"})
        assert note(op, "studio")["msg"] == "NOT_FOUND"


def test_viewers_see_two_arm_flags_but_no_training_files(cfg):
    put_model(Path(cfg.policies_dir), "both_act", dim=12, cams=("top", "right_wrist", "left_wrist"))
    with TestClient(create_app(cfg, MockBase())) as client, client.websocket_connect("/ws?name=Phone") as v:
        for _ in range(50):
            m = v.receive_json()
            if m["t"] == "robot":
                break
        assert m["models"][0]["arms"] == "both" and m["models"][0]["playable"] is False
        assert "train" not in m and "steps" not in m["models"][0]
