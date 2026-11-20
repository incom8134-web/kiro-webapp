#!/usr/bin/env python3
"""Writes colab/so101_train_colab.ipynb, the training notebook the Studio page opens in Google Colab.

Based on the old ~/so101/colab/make_notebook.py (21 Oct 2026, used for the first model right_arm_task2_act),
with its cell 3 SyntaxError fixed, English + Korean instructions for the Studio flow, and a model zip that
the Studio page can install. Cells are plain Python (no ! magics), so they also run in a local venv.

Run:  python3 colab/make_notebook.py      (tests/test_notebook.py checks the committed file matches)
"""
import json
from pathlib import Path

CELLS = []


def md(text):
    CELLS.append({"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)})


def code(text):
    CELLS.append({"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                  "source": text.strip("\n").splitlines(keepends=True)})


md(r"""
# HAEBOT · Train an AI skill in Google Colab (ACT, LeRobot 0.6.1)

You need the dataset package from the robot: **Studio > Training > Download** gives `<name>_<date>.tar.gz` and `<name>_<date>.sha256`.

1. **Put both files in Google Drive**: drive.google.com > My Drive > folder `SO101` > folder `데이터셋_업로드` (make the folders if they are missing; putting the files straight into My Drive also works).
2. **GPU**: menu **Runtime > Change runtime type > T4 GPU > Save**.
3. **Run**: menu **Runtime > Run all** (Ctrl+F9). If Colab warns that the notebook is not from Google, press **Run anyway**. When Google asks to connect Google Drive, allow it.
4. **Wait**: 20,000 steps take about 1 to 3 hours on a free T4. Keep this tab open. If Colab disconnects, press **Run all** again: training continues from the last checkpoint saved in Drive.
5. **Model**: at the end the zip is in Drive: My Drive > `SO101` > `outputs` > `<name>_act_pretrained_model.zip`. Download it from drive.google.com (right click > Download).
6. **Install**: connect to the robot's Wi-Fi again, open **Studio > Training > Upload a trained model**, and choose the zip. The skill then appears under **Autonomous skills** on the Arms tab.

**Without Google Drive**: untick `USE_DRIVE` in cell 1, open the **Files** panel (folder icon on the left), upload the `.tar.gz` with its upload button, wait until it has fully arrived, then **Run all**. If Colab disconnects, everything is gone and you start again. The zip download starts by itself at the end.

---

## 한국어 안내

로봇의 **스튜디오 > 학습 > 내려받기** 로 받은 `<이름>_<날짜>.tar.gz` 와 `.sha256` 파일이 필요합니다.

1. **Google Drive 에 두 파일 올리기**: drive.google.com > 내 드라이브 > `SO101` 폴더 > `데이터셋_업로드` 폴더 (없으면 만드세요. 내 드라이브 바로 아래에 두어도 됩니다).
2. **GPU**: 메뉴 **런타임 > 런타임 유형 변경 > T4 GPU > 저장**.
3. **실행**: 메뉴 **런타임 > 모두 실행** (Ctrl+F9). Google 이 만든 노트북이 아니라는 경고가 나오면 **무시하고 실행**. Google Drive 연결을 물으면 허용하세요.
4. **기다리기**: 무료 T4 에서 20,000 스텝은 약 1~3 시간. 이 탭을 열어 두세요. 연결이 끊기면 **모두 실행** 을 다시 누르면 Drive 에 저장된 마지막 체크포인트부터 이어서 학습합니다.
5. **모델**: 끝나면 zip 이 Drive 의 내 드라이브 > `SO101` > `outputs` > `<이름>_act_pretrained_model.zip` 에 있습니다. drive.google.com 에서 내려받으세요 (오른쪽 클릭 > 다운로드).
6. **설치**: 로봇 Wi-Fi 에 다시 연결하고 **스튜디오 > 학습 > 학습된 모델 올리기** 에서 zip 을 고르세요. 팔 탭의 **자율 동작** 에 스킬이 나타납니다.

**Google Drive 없이**: 1번 셀의 `USE_DRIVE` 를 끄고, 왼쪽 **파일** 패널(폴더 아이콘)의 업로드 버튼으로 `.tar.gz` 를 올린 뒤 다 올라갈 때까지 기다렸다가 **모두 실행**. 연결이 끊기면 모두 사라지므로 처음부터 다시 해야 합니다. 끝나면 zip 내려받기가 자동으로 시작됩니다.
""")

code(r"""
#@title 1. Settings / 설정
USE_DRIVE = True       #@param {type:"boolean"}
DRIVE_DIR = "/content/drive/MyDrive/SO101/데이터셋_업로드"  #@param {type:"string"}
TAR_NAME = ""          #@param {type:"string"}
STEPS = 20000          #@param {type:"integer"}
BATCH = 8              #@param {type:"integer"}
SAVE_FREQ = 2000       #@param {type:"integer"}
POLICY = "act"         #@param ["act", "diffusion"]
OUT_ROOT = "/content/drive/MyDrive/SO101/outputs"  #@param {type:"string"}
LOCAL_ROOT = "/content/so101"   # local work folder for the dataset and the training (gone when the session ends)
# USE_DRIVE=True : the .tar.gz waits in Google Drive; checkpoints and the model zip are kept in Drive, so a cut session continues.
# USE_DRIVE=False: upload the .tar.gz with the Files panel (it lands in /content); everything is gone when the session ends.
# TAR_NAME empty = the newest .tar.gz found (DRIVE_DIR, then My Drive, then /content).
if not USE_DRIVE:
    DRIVE_DIR = "/content"
    OUT_ROOT = "/content/so101/outputs_keep"
assert STEPS >= 20000, "STEPS must be 20000 or more (the first robot skill used 20000) / STEPS 는 20000 이상이어야 합니다"
print(f"policy {POLICY}, {STEPS} steps, batch {BATCH}, checkpoint every {SAVE_FREQ} steps, Drive {USE_DRIVE}")
""")

code(r"""
#@title 2. Google Drive + dataset: find, check SHA-256, unpack / 데이터셋 찾기·확인·풀기
import glob, hashlib, json, os, shutil, tarfile
if USE_DRIVE:
    try:
        from google.colab import drive
        drive.mount("/content/drive")
    except ImportError:
        print("(not Colab: no Google Drive)")
places = [DRIVE_DIR] + (["/content/drive/MyDrive", "/content"] if USE_DRIVE else [])
if TAR_NAME:
    found = [p for p in (os.path.join(d, TAR_NAME) for d in places) if os.path.isfile(p)]
else:
    found = sorted((p for d in places for p in glob.glob(os.path.join(d, "*.tar.gz"))), key=os.path.getmtime)
assert found, ("No .tar.gz found in " + ", ".join(places) + ". "
               + ("Put the package from the robot into Google Drive > SO101 > 데이터셋_업로드 and run again. "
                  "/ 로봇에서 받은 tar.gz 를 Google Drive > SO101 > 데이터셋_업로드 에 올리고 다시 실행하세요."
                  if USE_DRIVE else
                  "Upload it with the Files panel on the left, wait until it is complete, then run again. "
                  "/ 왼쪽 파일 패널로 tar.gz 를 올리고 다 올라가면 다시 실행하세요."))
SRC = found[-1]
TAG = os.path.basename(SRC)[:-len(".tar.gz")]      # <name>_<date>: one training run per package
print(f"dataset package: {SRC}  ({os.path.getsize(SRC) / 1e6:.1f} MB)")
INCOMING = os.path.join(LOCAL_ROOT, "incoming")
os.makedirs(INCOMING, exist_ok=True)
TAR = os.path.join(INCOMING, os.path.basename(SRC))
if not os.path.exists(TAR) or os.path.getsize(TAR) != os.path.getsize(SRC):
    shutil.copyfile(SRC, TAR)          # read Drive once; checking and unpacking then use the fast local disk
sha = SRC[:-len(".tar.gz")] + ".sha256"
if os.path.exists(sha):
    want = open(sha).read().split()[0]
    h = hashlib.sha256()
    with open(TAR, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    if h.hexdigest() != want:
        os.remove(TAR)
        raise SystemExit("SHA-256 does not match: the file was damaged on the way. Upload it again. / SHA-256 불일치: 다시 올리세요")
    print("SHA-256 OK")
else:
    print("(no .sha256 next to it: integrity check skipped / .sha256 없음: 확인 생략)")
DATASETS = os.path.join(LOCAL_ROOT, "datasets")
os.makedirs(DATASETS, exist_ok=True)
with tarfile.open(TAR) as t:
    tops = {n.split("/")[0] for n in t.getnames()}
    assert len(tops) == 1, f"the package should hold one dataset folder, not {sorted(tops)[:5]}"
    NAME = tops.pop()
    ROOT = os.path.join(DATASETS, NAME)
    mark = os.path.join(DATASETS, f".{NAME}.from")
    if not os.path.exists(os.path.join(ROOT, "meta", "info.json")) or not os.path.exists(mark) or open(mark).read() != TAG:
        shutil.rmtree(ROOT, ignore_errors=True)    # another package of the same dataset: unpack this one
        t.extractall(DATASETS, filter="data")
        open(mark, "w").write(TAG)
info = json.load(open(os.path.join(ROOT, "meta", "info.json")))
cams = [k.split(".")[-1] for k, v in info["features"].items() if v.get("dtype") in ("video", "image")]
print(f"dataset {NAME}: format {info['codebase_version']}, robot {info.get('robot_type')}, {info['total_episodes']} episodes, "
      f"{info['total_frames']} frames, {info['fps']} fps, cameras {cams}")
assert info["codebase_version"].startswith("v3"), "not a LeRobot 0.6 dataset (v3.0) / lerobot 0.6 형식(v3.0)이 아닙니다"
""")

code(r"""
#@title 3. GPU check + install LeRobot 0.6.1 (first time 2-4 min) / GPU 확인 + 설치
import subprocess, sys
print("Python", sys.version.split()[0])
try:
    print(subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True).stdout.strip() or "no GPU")
except FileNotFoundError:
    raise SystemExit("No GPU in this runtime. Runtime > Change runtime type > T4 GPU > Save, then Run all. "
                     "/ GPU 없음: 런타임 > 런타임 유형 변경 > T4 GPU > 저장 후 모두 실행")
try:
    import lerobot
    assert lerobot.__version__ == "0.6.1"
    print("lerobot 0.6.1 already installed")
except Exception:
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "lerobot[dataset]==0.6.1", "accelerate"], capture_output=True, text=True)
    print(r.stdout[-1500:], r.stderr[-1500:])
    assert r.returncode == 0, "pip install failed (see above) / pip 설치 실패"
    import lerobot
# Colab's own diffusers can break on an old torchao (ImportError FqnToConfig) and stop lerobot-train: remove torchao (ACT does not use it)
try:
    from diffusers import ModelMixin  # noqa: F401
except Exception as exc:
    print("diffusers import failed ->", str(exc)[:120], "| removing torchao and trying again")
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "-q", "torchao"], capture_output=True)
import torch
print(f"lerobot {lerobot.__version__} | torch {torch.__version__} | CUDA {torch.cuda.is_available()} {torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''}")
assert torch.cuda.is_available(), "No GPU: Runtime > Change runtime type > T4 GPU, then run again / GPU 없음"
""")

code(r"""
#@title 4. Train (after a disconnect: Run all again, it continues from the last checkpoint in Drive) / 학습
import glob, json, os, shlex, shutil, subprocess, sys, threading, time
from pathlib import Path
OUT_LOCAL = os.path.join(LOCAL_ROOT, "outputs", f"{TAG}_{POLICY}")       # where lerobot writes (needs symbolic links -> local disk)
OUT_DRIVE = os.path.join(OUT_ROOT, f"{TAG}_{POLICY}")                     # copy of the newest complete checkpoint (per package)
os.makedirs(os.path.join(OUT_DRIVE, "checkpoints"), exist_ok=True)   # OUT_LOCAL is made by lerobot (it refuses an existing one unless resuming)
TRAIN = shutil.which("lerobot-train") or str(Path(sys.executable).parent / "lerobot-train")

def step_dirs(base):
    return sorted(d for d in glob.glob(os.path.join(base, "checkpoints", "*")) if os.path.basename(d) != "last"
                  and os.path.exists(os.path.join(d, "pretrained_model", "train_config.json")) and os.path.isdir(os.path.join(d, "training_state")))

# Continue: bring the newest checkpoint from Drive to the local disk
local, drive_ck = step_dirs(OUT_LOCAL), step_dirs(OUT_DRIVE)
if drive_ck and (not local or os.path.basename(drive_ck[-1]) > os.path.basename(local[-1])):
    dst = os.path.join(OUT_LOCAL, "checkpoints", os.path.basename(drive_ck[-1]))
    print("checkpoint from Drive:", drive_ck[-1]); shutil.copytree(drive_ck[-1], dst, dirs_exist_ok=True)
    local = step_dirs(OUT_LOCAL)
if local:
    last = local[-1]; step = int(os.path.basename(last))
    cfg = json.load(open(os.path.join(last, "pretrained_model", "train_config.json")))
    total = int(cfg.get("steps", STEPS))
    if step >= total:
        print(f"already {step}/{total} steps for this package: nothing to train (to train again, delete {OUT_DRIVE} in Drive)"); cmd = None
    else:
        print(f"continuing at {step}/{total} steps"); cmd = [TRAIN, f"--config_path={last}/pretrained_model/train_config.json", "--resume=true"]
else:
    if os.path.exists(OUT_LOCAL):
        shutil.rmtree(OUT_LOCAL)   # an output folder without checkpoints would make lerobot refuse to start
    cmd = [TRAIN, f"--dataset.repo_id=local/{NAME}", f"--dataset.root={ROOT}", f"--policy.type={POLICY}", "--policy.device=cuda",
           "--policy.push_to_hub=false", "--wandb.enable=false", f"--batch_size={BATCH}", f"--steps={STEPS}", f"--save_freq={SAVE_FREQ}",
           "--log_freq=100", "--num_workers=2", f"--output_dir={OUT_LOCAL}", f"--job_name={NAME}"]

def mirror_once(done):
    # copy complete checkpoints (the one 'last' points to, or older) to Drive; Drive keeps only the newest
    link = os.path.join(OUT_LOCAL, "checkpoints", "last")
    if not os.path.islink(link):
        return
    newest = os.readlink(link).rstrip("/")
    for d in step_dirs(OUT_LOCAL):
        n = os.path.basename(d)
        if n in done or n > newest:
            continue
        dst = os.path.join(OUT_DRIVE, "checkpoints", n)
        shutil.copytree(d, dst, dirs_exist_ok=True); done.add(n)
        for old in glob.glob(os.path.join(OUT_DRIVE, "checkpoints", "*")):
            if os.path.basename(old) != n and os.path.isdir(old):
                shutil.rmtree(old, ignore_errors=True)
        print(f"[saved to Drive] checkpoint {n} -> {dst}", flush=True)

if cmd:
    print(" ".join(shlex.quote(c) for c in cmd), flush=True)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    done, stop = set(), threading.Event()
    def watcher():
        while not stop.is_set():
            mirror_once(done); stop.wait(20)
    th = threading.Thread(target=watcher, daemon=True); th.start()
    for line in proc.stdout:
        if "torchcodec" in line or "FFmpeg" in line or "load_library" in line:
            continue
        print(line.rstrip(), flush=True)
    rc = proc.wait(); stop.set(); th.join(); mirror_once(done)
    print("training ended, rc =", rc)
    assert rc == 0, "training stopped with an error (see the log above). After a disconnect: Run all again / 학습 오류: 위 로그 확인"
""")

code(r"""
#@title 5. The model zip for the robot's Studio page / 스튜디오에 올릴 모델 zip
import datetime, json, os, zipfile
ck = step_dirs(OUT_LOCAL) or step_dirs(OUT_DRIVE)
assert ck, "no checkpoint yet: training has not finished / 체크포인트 없음: 학습이 끝나지 않았습니다"
PM = os.path.join(ck[-1], "pretrained_model")
cfg = json.load(open(os.path.join(PM, "train_config.json")))
try:
    step = int(json.load(open(os.path.join(ck[-1], "training_state", "training_step.json")))["step"])
except (OSError, ValueError, KeyError):
    step = int(os.path.basename(ck[-1]))
total = int(cfg.get("steps", STEPS))
assert step >= total, f"only {step} of {total} steps: run all again to finish training / {step}/{total} 스텝: 모두 실행을 다시 누르세요"
about = {"step": step, "steps": total, "policy": POLICY, "dataset": NAME, "package": TAG, "robot_type": info.get("robot_type"),
         "episodes": info.get("total_episodes"), "frames": info.get("total_frames"), "fps": info.get("fps"), "cameras": cams,
         "lerobot": lerobot.__version__, "made_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
zip_path = os.path.join(OUT_ROOT, f"{NAME}_{POLICY}_pretrained_model.zip")
os.makedirs(OUT_ROOT, exist_ok=True)
with zipfile.ZipFile(zip_path + ".part", "w", zipfile.ZIP_DEFLATED) as zf:
    for f in sorted(os.listdir(PM)):
        if os.path.isfile(os.path.join(PM, f)):
            zf.write(os.path.join(PM, f), "pretrained_model/" + f)
    zf.writestr("pretrained_model/kiro_training.json", json.dumps(about, indent=1))
os.replace(zip_path + ".part", zip_path)
print(f"step {step}: {zip_path} ({os.path.getsize(zip_path) / 1e6:.0f} MB)")
if USE_DRIVE:
    print("Download it from drive.google.com: My Drive > SO101 > outputs > " + os.path.basename(zip_path) + " (right click > Download)")
    print("drive.google.com 에서 내려받기: 내 드라이브 > SO101 > outputs > " + os.path.basename(zip_path))
else:
    try:
        from google.colab import files
        print("Starting the download (save it now: everything here is gone when the session ends) / 지금 내려받으세요")
        files.download(zip_path)
    except ImportError:
        pass
print("Then: robot Wi-Fi > Studio > Training > Upload a trained model > choose this zip.")
print("그다음: 로봇 Wi-Fi > 스튜디오 > 학습 > 학습된 모델 올리기 > 이 zip 선택.")
""")

md(r"""
## If something goes wrong / 문제 해결

- **No GPU**: Runtime > Change runtime type > T4 GPU. Free accounts sometimes get no GPU; try again later. / GPU 없음: 런타임 유형을 T4 GPU 로. 무료 계정은 GPU 가 없을 때가 있으니 나중에 다시.
- **Disconnected**: open the notebook again and Run all. Cell 4 takes the last checkpoint from Drive and continues. To start from zero, delete `SO101/outputs/<name>_<date>_act/` in Drive. / 연결 끊김: 다시 모두 실행. 처음부터 하려면 Drive 의 `SO101/outputs/<이름>_<날짜>_act/` 삭제.
- **`cannot import name 'FqnToConfig'`** in cell 4: cell 3 removes torchao by itself; with an old copy of this notebook, run `pip uninstall -y torchao` in a new cell, then cell 4 again.
- **pip errors**: LeRobot 0.6.1 installs on Python 3.12 and 3.13. If torch changed during the install, use Runtime > Restart session once and run from cell 3.
- **Speed**: ACT with batch 8 runs about 0.2 to 0.6 s per step on a T4, so 20,000 steps take 1 to 3 hours. The dataset is unpacked on the local disk (/content), so Drive speed does not matter.
- **Drive space**: Drive keeps only the newest checkpoint (about 0.6 GB) and the model zip (about 0.2 GB).
- **The zip download from Colab breaks**: download it from drive.google.com instead (USE_DRIVE on). / Colab 에서 내려받기가 끊기면 drive.google.com 에서 받으세요.
""")

NB = {"nbformat": 4, "nbformat_minor": 0,
      "metadata": {"accelerator": "GPU", "colab": {"provenance": [], "gpuType": "T4"},
                   "kernelspec": {"display_name": "Python 3", "name": "python3"}, "language_info": {"name": "python"}},
      "cells": CELLS}


def render() -> str:
    return json.dumps(NB, ensure_ascii=False, indent=1) + "\n"


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "so101_train_colab.ipynb"
    out.write_text(render(), encoding="utf-8")
    print(f"wrote {out} ({len(CELLS)} cells)")
