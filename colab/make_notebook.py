#!/usr/bin/env python3
"""Writes colab/so101_train_colab.ipynb, the training notebook the Studio page opens in Google Colab.

Based on the old ~/so101/colab/make_notebook.py (21 Oct 2026, used for the first model right_arm_task2_act),
with its cell 3 SyntaxError fixed. Two ways to get the dataset in and the model out:

  robot  (default) cell 1 opens a temporary internet address for this session (Cloudflare quick tunnel,
         pinned cloudflared with its SHA-256) with a one-time key and shows "Connect to the robot". The
         robot (robot_server/colab.py, needs internet) then sends the dataset and fetches the model itself.
  drive  the package waits in Google Drive (or the Files panel); the model zip is downloaded by hand.

Cells are plain Python (no ! magics), so they also run in a local venv (tests/test_notebook.py runs them).

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

**The automatic way** (the robot has internet through its LAN cable):

1. You came here from **Studio > Train in Colab**. Sign in with any Google account if Colab asks.
2. Menu **Runtime > Run all** (Ctrl+F9). The notebook asks for a T4 GPU by itself; if Colab says no GPU, choose **Runtime > Change runtime type > T4 GPU > Save** and Run all again.
3. If Colab warns that the notebook is not from Google, press **Run anyway**. When Google asks about Google Drive, allow it and tick every box (**Select all**): Drive keeps the checkpoints if Colab disconnects.
4. Cell 1 shows **Connect to the robot**: click it, and press **Send** on the robot page that opens. The robot sends the dataset, training starts by itself.
5. Wait about 3 to 4 hours with this tab open. The robot's Studio page shows the progress. At the end the robot fetches the model and installs it: it appears under **Autonomous skills**. Then you can close this tab.

If Colab disconnects: **Run all** again and click **Connect to the robot** again. Training continues from the last checkpoint in Drive.

**The manual way** (the robot has no internet): set `SOURCE` to `drive` in cell 1, put the package from **Studio > Train a skill > Manual way** in Google Drive > `SO101` > `데이터셋_업로드`, Run all, and at the end download `<name>_act_pretrained_model.zip` from Drive (My Drive > `SO101` > `outputs`) and upload it in the Studio.

---

## 한국어 안내

**자동 방식** (로봇이 LAN 케이블로 인터넷에 연결된 경우):

1. **스튜디오 > Colab 에서 학습** 으로 여기에 왔습니다. Colab 이 물으면 아무 Google 계정으로 로그인하세요.
2. 메뉴 **런타임 > 모두 실행** (Ctrl+F9). 노트북이 T4 GPU 를 스스로 요청합니다. GPU 가 없다고 하면 **런타임 > 런타임 유형 변경 > T4 GPU > 저장** 후 다시 모두 실행.
3. Google 이 만든 노트북이 아니라는 경고가 나오면 그래도 실행 (**Run anyway**). Google Drive 접근을 물으면 허용하고 모든 항목에 체크 (**모두 선택**): 연결이 끊겨도 Drive 에 체크포인트가 남습니다.
4. 1번 셀에 **로봇에 연결** 버튼이 나옵니다. 누른 뒤 열리는 로봇 페이지에서 **보내기** 를 누르세요. 로봇이 데이터셋을 보내고 학습이 저절로 시작됩니다.
5. 이 탭을 연 채 약 3~4 시간 기다리세요. 진행 상황은 로봇의 스튜디오 페이지에 나옵니다. 끝나면 로봇이 모델을 가져와 설치합니다: **자율 동작** 에 나타납니다. 그다음 이 탭을 닫아도 됩니다.

연결이 끊기면: **모두 실행** 을 다시 누르고 **로봇에 연결** 을 다시 누르세요. Drive 의 마지막 체크포인트부터 이어서 학습합니다.

**수동 방식** (로봇에 인터넷이 없을 때): 1번 셀의 `SOURCE` 를 `drive` 로 바꾸고, **스튜디오 > 스킬 학습 > 수동 방식** 의 묶음을 Google Drive > `SO101` > `데이터셋_업로드` 에 올린 뒤 모두 실행. 끝나면 Drive (내 드라이브 > `SO101` > `outputs`) 에서 `<이름>_act_pretrained_model.zip` 을 내려받아 스튜디오에 올리세요.
""")

code(r"""
#@title 1. Settings + link to the robot / 설정 + 로봇 연결
SOURCE = "robot"       #@param ["robot", "drive"]
ROBOT = "http://10.42.0.1:8080"   #@param {type:"string"}
USE_DRIVE = True       #@param {type:"boolean"}
DRIVE_DIR = "/content/drive/MyDrive/SO101/데이터셋_업로드"  #@param {type:"string"}
TAR_NAME = ""          #@param {type:"string"}
STEPS = 20000          #@param {type:"integer"}
BATCH = 8              #@param {type:"integer"}
SAVE_FREQ = 2000       #@param {type:"integer"}
POLICY = "act"         #@param ["act", "diffusion"]
OUT_ROOT = "/content/drive/MyDrive/SO101/outputs"  #@param {type:"string"}
LOCAL_ROOT = "/content/so101"   # local work folder for the dataset and the training (gone when the session ends)
# SOURCE="robot": click "Connect to the robot" below; the robot sends the dataset and fetches the model itself.
# SOURCE="drive": the .tar.gz waits in Google Drive (DRIVE_DIR), or upload it with the Files panel (USE_DRIVE off).
# USE_DRIVE=True: checkpoints and the model zip are kept in Google Drive, so a cut session continues (Run all again).
import hashlib, html, http.server, json, os, queue, re, secrets, shutil, subprocess, sys, threading, time, urllib.parse, urllib.request
if not USE_DRIVE:
    DRIVE_DIR = "/content"
    OUT_ROOT = "/content/so101/outputs_keep"
assert SOURCE in ("robot", "drive"), "SOURCE is robot or drive"
assert STEPS >= 20000, "STEPS must be 20000 or more (the first robot skill used 20000) / STEPS 는 20000 이상이어야 합니다"
os.makedirs(LOCAL_ROOT, exist_ok=True)
LINK = globals().get("LINK") if SOURCE == "robot" else None
if LINK is not None and not LINK.url:
    LINK = None                                   # the last try got no address: start again

def fail(msg):
    # stop here, and tell the robot why (it shows it on the Studio page)
    if globals().get("LINK"):
        LINK.set(state="failed", error=str(msg)[:300])
    raise SystemExit(msg)

def _report(shell, etype, evalue, tb, tb_offset=None):
    if globals().get("LINK"):
        LINK.set(state="failed", error=f"{etype.__name__}: {evalue}"[:300])
    shell.showtraceback((etype, evalue, tb), tb_offset=tb_offset)

try:
    get_ipython().set_custom_exc((Exception, KeyboardInterrupt), _report)   # any error (or Interrupt) in any cell also reaches the robot
except NameError:
    pass
if LINK is not None and LINK.st["state"] == "failed":   # Run all again after an error: ready for the robot again
    _have = LINK.st.get("have")
    LINK.set(state="received" if _have and os.path.exists(os.path.join(LINK.dir, _have)) else "waiting", error=None)
if USE_DRIVE:
    try:
        from google.colab import drive
        drive.mount("/content/drive")
    except ImportError:
        print("(not Colab: no Google Drive)")
print(f"policy {POLICY}, {STEPS} steps, batch {BATCH}, checkpoint every {SAVE_FREQ} steps, Drive {USE_DRIVE}, dataset from {SOURCE}")

CLOUDFLARED = ("https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64",
               "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872")   # pinned release and its SHA-256
PIECE_MAX = 64 * 1024 ** 2

class Link:
    # What the robot talks to (robot_server/colab.py): one dataset in, the progress and the model out. Every call needs the key.
    def __init__(self, folder):
        self.key, self.dir, self.url = secrets.token_urlsafe(24), folder, None
        os.makedirs(folder, exist_ok=True)
        self.st = {"kiro": 1, "state": "waiting", "have": None, "step": None, "steps": STEPS, "error": None, "model": None}
        self.part = None                # [file, next piece, bytes so far]
        self.lock = threading.Lock()

    def set(self, **kw):
        with self.lock:
            self.st.update(kw)

class LinkHandler(http.server.BaseHTTPRequestHandler):
    link = None

    def log_message(self, *a):
        pass

    def out(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def allowed(self):
        if secrets.compare_digest(self.headers.get("X-Kiro-Key", "").encode("utf-8", "replace"), self.link.key.encode()):
            return True
        self.out(403, {"error": "wrong key"})
        return False

    def do_GET(self):
        if not self.allowed():
            return
        L = self.link
        if self.path == "/status":
            with L.lock:
                return self.out(200, {k: v for k, v in L.st.items() if k != "path"})
        if self.path == "/model":
            m = L.st.get("model")
            if not m or L.st["state"] not in ("done", "fetched"):
                return self.out(409, {"error": "no model yet"})
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(m["bytes"]))
            self.end_headers()
            with open(L.model_path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
            return
        self.out(404, {"error": "no such thing"})

    def body(self):
        # the request body in pieces: with a Content-Length, or chunked (a proxy may send either)
        n = self.headers.get("Content-Length")
        if n is not None:
            left = int(n)
            if not 0 <= left <= PIECE_MAX:
                raise ValueError("piece too big")
            while left:
                b = self.rfile.read(min(left, 1 << 20))
                if not b:
                    raise ValueError("cut short")
                left -= len(b)
                yield b
        elif "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            total = 0
            while True:
                size = int(self.rfile.readline(100).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1000) not in (b"\r\n", b"\n", b""):
                        pass
                    return
                total += size
                if total > PIECE_MAX:
                    raise ValueError("piece too big")
                left = size
                while left:
                    b = self.rfile.read(min(left, 1 << 20))
                    if not b:
                        raise ValueError("cut short")
                    left -= len(b)
                    yield b
                self.rfile.readline(10)
        else:
            raise ValueError("no length")

    def do_PUT(self):
        if not self.allowed():
            return
        L = self.link
        m = re.fullmatch(r"/dataset/([A-Za-z0-9][A-Za-z0-9._-]*_\d{8}_\d{4}\.tar\.gz)\?part=(\d+)&parts=(\d+)", self.path)
        if not m:
            return self.out(400, {"error": "bad piece"})
        name, i, parts = m.group(1), int(m.group(2)), int(m.group(3))
        with L.lock:
            if i == 0:
                L.part = [name, 0, 0]
                L.st.update(state="receiving", have=None)
            if not L.part or L.part[0] != name or L.part[1] != i or i >= parts:
                return self.out(409, {"error": f"expected piece {L.part[1] if L.part else 0}"})
            start = L.part[2]
        n = 0
        try:     # a piece sent again overwrites what a cut try left behind
            with open(os.path.join(L.dir, name + ".part"), "wb" if i == 0 else "r+b") as f:
                f.seek(start)
                f.truncate()
                for b in self.body():
                    f.write(b)
                    n += len(b)
        except (ValueError, OSError) as e:
            return self.out(400, {"error": str(e)[:100]})
        with L.lock:
            L.part[1] += 1
            L.part[2] += n
        return self.out(200, {"ok": True, "got": L.part[2]})

    def do_POST(self):
        if not self.allowed():
            return
        L = self.link
        m = re.fullmatch(r"/dataset/([^/?]+)/done\?sha256=([0-9a-f]{64})", self.path)
        if m:
            name, want = m.groups()
            part = os.path.join(L.dir, name + ".part")
            if not L.part or L.part[0] != name or not os.path.exists(part):
                return self.out(409, {"error": "no such upload"})
            h = hashlib.sha256()
            with open(part, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            if h.hexdigest() != want:
                os.remove(part)
                L.part = None
                L.set(state="waiting", have=None)
                return self.out(400, {"error": "SHA-256 does not match"})
            os.replace(part, os.path.join(L.dir, name))
            L.set(have=name, **({} if L.st["state"] == "failed" else {"state": "received"}))   # a failure stays visible
            return self.out(200, {"ok": True})
        if self.path == "/fetched":
            L.set(state="fetched")
            return self.out(200, {"ok": True})
        self.out(404, {"error": "no such thing"})

def start_tunnel(port):
    # A temporary https address for this session (Cloudflare quick tunnel: free, no account)
    if os.environ.get("KIRO_NO_TUNNEL"):            # tests on a computer without Colab
        return f"http://127.0.0.1:{port}"
    exe = os.path.join(LOCAL_ROOT, "cloudflared")
    if not os.path.exists(exe):
        data = urllib.request.urlopen(CLOUDFLARED[0], timeout=180).read()
        if hashlib.sha256(data).hexdigest() != CLOUDFLARED[1]:
            fail("cloudflared download damaged: run this cell again / 다운로드 손상: 이 셀을 다시 실행하세요")
        with open(exe, "wb") as f:
            f.write(data)
        os.chmod(exe, 0o755)
    p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in p.stdout], daemon=True).start()   # keeps reading its output
    url, end = None, time.time() + 90
    while url is None and time.time() < end and p.poll() is None:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            continue
        m = re.search(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com", line)   # not Cloudflare's own api. address
        if m:
            url = m.group(0)
    if not url:
        p.terminate()
        fail("Cloudflare gave no address (busy? try again in a minute): run this cell again "
             "/ 주소를 받지 못했습니다 (잠시 뒤): 이 셀을 다시 실행하세요")
    return url

if SOURCE == "robot" and LINK is None:
    LINK = Link(os.path.join(LOCAL_ROOT, "incoming"))
    LinkHandler.link = LINK
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LinkHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LINK.url = start_tunnel(server.server_address[1])
if LINK:
    go = f"{ROBOT.rstrip('/')}/studio.html#colab={urllib.parse.quote(LINK.url, safe='')}&k={LINK.key}"
    try:
        from IPython.display import HTML, display
        display(HTML('<p style="font:14px sans-serif;margin:8px 0">Click, then press <b>Send</b> on the robot page that opens. '
                     '/ 누른 뒤 열리는 로봇 페이지에서 <b>보내기</b> 를 누르세요.</p>'
                     f'<a href="{html.escape(go)}" target="_blank" rel="noopener" style="display:inline-block;padding:10px 18px;'
                     'background:#2563eb;color:#fff;border-radius:8px;font:600 15px sans-serif;text-decoration:none">'
                     'Connect to the robot / 로봇에 연결</a>'))
    except ImportError:
        print("Connect to the robot:", go)
    print("Run all goes on: cell 3 waits until the robot has sent the dataset. / 3번 셀이 데이터셋을 기다립니다.")
""")

code(r"""
#@title 2. GPU check + install LeRobot 0.6.1 (first time 2-4 min) / GPU 확인 + 설치
import subprocess, sys
print("Python", sys.version.split()[0])
try:
    print(subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True).stdout.strip() or "no GPU")
except FileNotFoundError:
    fail("No GPU in this runtime. Runtime > Change runtime type > T4 GPU > Save, then Run all. "
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
if not torch.cuda.is_available():
    fail("No GPU: Runtime > Change runtime type > T4 GPU, then run again / GPU 없음")
""")

code(r"""
#@title 3. The dataset: from the robot, or found in Drive; checked and unpacked / 데이터셋 받기·확인·풀기
import glob, hashlib, json, os, re, shutil, tarfile, time
if SOURCE == "robot":
    shown = 0
    while LINK.st["state"] not in ("received", "training", "zipping", "done", "fetched"):
        if time.time() - shown > 60:
            print(f"receiving from the robot: {LINK.part[2] / 1e6:.0f} MB" if LINK.part else
                  "waiting for the robot: click 'Connect to the robot' in cell 1 / 1번 셀의 '로봇에 연결' 을 누르세요", flush=True)
            shown = time.time()
        time.sleep(2)
    SRC = TAR = os.path.join(LINK.dir, LINK.st["have"])
    print(f"dataset package from the robot: {os.path.basename(SRC)} ({os.path.getsize(SRC) / 1e6:.1f} MB), SHA-256 OK")
else:
    places = [DRIVE_DIR] + (["/content/drive/MyDrive", "/content"] if USE_DRIVE else [])
    if TAR_NAME:
        found = [p for p in (os.path.join(d, TAR_NAME) for d in places) if os.path.isfile(p)]
    else:   # packages from the robot are <name>_<YYYYmmdd>_<HHMM>.tar.gz; the first folder that has one wins, newest there
        for d in places:
            found = sorted((p for p in glob.glob(os.path.join(d, "*.tar.gz")) if re.fullmatch(r".+_\d{8}_\d{4}\.tar\.gz", os.path.basename(p))),
                           key=os.path.getmtime)
            if found:
                break
    if not found:
        fail("No .tar.gz found in " + ", ".join(places) + ". "
             + ("Put the package from the robot into Google Drive > SO101 > 데이터셋_업로드 and run again. "
                "/ 로봇에서 받은 tar.gz 를 Google Drive > SO101 > 데이터셋_업로드 에 올리고 다시 실행하세요."
                if USE_DRIVE else
                "Upload it with the Files panel on the left, wait until it is complete, then run again. "
                "/ 왼쪽 파일 패널로 tar.gz 를 올리고 다 올라가면 다시 실행하세요."))
    SRC = found[-1]
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
            fail("SHA-256 does not match: the file was damaged on the way. Upload it again. / SHA-256 불일치: 다시 올리세요")
        print("SHA-256 OK")
    else:
        print("(no .sha256 next to it: integrity check skipped / .sha256 없음: 확인 생략)")
TAG = os.path.basename(SRC)[:-len(".tar.gz")]      # <name>_<date>: one training run per package
DATASETS = os.path.join(LOCAL_ROOT, "datasets")
os.makedirs(DATASETS, exist_ok=True)
with tarfile.open(TAR) as t:
    tops = {n.split("/")[0] for n in t.getnames()}
    if len(tops) != 1:
        fail(f"the package should hold one dataset folder, not {sorted(tops)[:5]}")
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
if not info["codebase_version"].startswith("v3"):
    fail("not a LeRobot 0.6 dataset (v3.0) / lerobot 0.6 형식(v3.0)이 아닙니다")
""")
code(r"""
#@title 4. Train (after a disconnect: Run all again, it continues from the last checkpoint in Drive) / 학습
import glob, json, os, re, shlex, shutil, subprocess, sys, threading, time
from pathlib import Path
OUT_LOCAL = os.path.join(LOCAL_ROOT, "outputs", f"{TAG}_{POLICY}")       # where lerobot writes (needs symbolic links -> local disk)
OUT_DRIVE = os.path.join(OUT_ROOT, f"{TAG}_{POLICY}")                     # copy of the newest complete checkpoint (per package)
os.makedirs(os.path.join(OUT_DRIVE, "checkpoints"), exist_ok=True)   # OUT_LOCAL is made by lerobot (it refuses an existing one unless resuming)
TRAIN = shutil.which("lerobot-train") or str(Path(sys.executable).parent / "lerobot-train")

def copied_whole(d):
    # a Drive copy counts only when every file in its size list is there with that size (a cut session can leave half a copy)
    try:
        want = json.load(open(os.path.join(d, "kiro_copy.json")))
        return bool(want) and all(os.path.getsize(os.path.join(d, f)) == n for f, n in want.items())
    except (OSError, ValueError):
        return False

def step_dirs(base, whole=False):
    return sorted(d for d in glob.glob(os.path.join(base, "checkpoints", "*")) if os.path.basename(d).isdigit()
                  and os.path.exists(os.path.join(d, "pretrained_model", "train_config.json")) and os.path.isdir(os.path.join(d, "training_state"))
                  and (not whole or copied_whole(d)))

# Continue: bring the newest checkpoint from Drive to the local disk
local, drive_ck = step_dirs(OUT_LOCAL), step_dirs(OUT_DRIVE, whole=True)
if drive_ck and (not local or os.path.basename(drive_ck[-1]) > os.path.basename(local[-1])):
    dst = os.path.join(OUT_LOCAL, "checkpoints", os.path.basename(drive_ck[-1]))
    print("checkpoint from Drive:", drive_ck[-1]); shutil.copytree(drive_ck[-1], dst, dirs_exist_ok=True)
    local = step_dirs(OUT_LOCAL)
total = STEPS
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

def big(s):
    # lerobot's log writes steps like 1K, 12.5K
    m = re.fullmatch(r"([0-9.]+)([KMB]?)", s)
    return int(float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]) if m else None

if LINK:
    LINK.set(state="training", steps=total, step=int(os.path.basename(local[-1])) if local else 0)

def mirror_once(done):
    # copy finished checkpoints (the one 'last' points to, or older) to Drive: first to <step>.part with a size
    # list, then renamed, so a cut session never leaves a half copy that looks whole. Drive keeps the newest two.
    link = os.path.join(OUT_LOCAL, "checkpoints", "last")
    if not os.path.islink(link):
        return
    newest = os.path.basename(os.readlink(link).rstrip("/"))
    for d in step_dirs(OUT_LOCAL):
        n = os.path.basename(d)
        if n in done or n > newest:
            continue
        dst = os.path.join(OUT_DRIVE, "checkpoints", n)
        part = dst + ".part"
        shutil.rmtree(part, ignore_errors=True)
        shutil.copytree(d, part)
        sizes = {os.path.relpath(os.path.join(r, f), part): os.path.getsize(os.path.join(r, f))
                 for r, _, fs in os.walk(part) for f in fs if f != "kiro_copy.json"}
        with open(os.path.join(part, "kiro_copy.json"), "w") as fh:
            json.dump(sizes, fh)
        shutil.rmtree(dst, ignore_errors=True)
        os.rename(part, dst); done.add(n)
        if LINK:
            LINK.set(step=max(LINK.st.get("step") or 0, int(n)))
        keep = step_dirs(OUT_DRIVE, whole=True)[-2:]
        for old in glob.glob(os.path.join(OUT_DRIVE, "checkpoints", "*")):
            if old not in keep and os.path.isdir(old) and not old.endswith(".part"):
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
        m = re.search(r"\bstep:([0-9.]+[KMB]?)", line)
        if LINK and m and big(m.group(1)) is not None:
            LINK.set(step=big(m.group(1)))
    rc = proc.wait(); stop.set(); th.join(); mirror_once(done)
    print("training ended, rc =", rc)
    if rc != 0:
        fail("training stopped with an error (see the log above). After a disconnect: Run all again / 학습 오류: 위 로그 확인")
""")

code(r"""
#@title 5. The model: to the robot (and Drive) / 모델을 로봇 (과 Drive) 으로
import datetime, hashlib, json, os, shutil, time, zipfile
ck = step_dirs(OUT_LOCAL) or step_dirs(OUT_DRIVE, whole=True)
if not ck:
    fail("no checkpoint yet: training has not finished / 체크포인트 없음: 학습이 끝나지 않았습니다")
PM = os.path.join(ck[-1], "pretrained_model")
cfg = json.load(open(os.path.join(PM, "train_config.json")))
try:
    step = int(json.load(open(os.path.join(ck[-1], "training_state", "training_step.json")))["step"])
except (OSError, ValueError, KeyError):
    step = int(os.path.basename(ck[-1]))
total = int(cfg.get("steps", STEPS))
if step < total:
    fail(f"only {step} of {total} steps: Run all again to finish training / {step}/{total} 스텝: 모두 실행을 다시 누르세요")
if LINK:
    LINK.set(state="zipping", step=step)
about = {"step": step, "steps": total, "policy": POLICY, "dataset": NAME, "package": TAG, "robot_type": info.get("robot_type"),
         "episodes": info.get("total_episodes"), "frames": info.get("total_frames"), "fps": info.get("fps"), "cameras": cams,
         "lerobot": lerobot.__version__, "made_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
zip_name = f"{NAME}_{POLICY}_pretrained_model.zip"
zip_local = os.path.join(LOCAL_ROOT, zip_name)
with zipfile.ZipFile(zip_local + ".part", "w", zipfile.ZIP_DEFLATED) as zf:
    for f in sorted(os.listdir(PM)):
        if os.path.isfile(os.path.join(PM, f)):
            zf.write(os.path.join(PM, f), "pretrained_model/" + f)
    zf.writestr("pretrained_model/kiro_training.json", json.dumps(about, indent=1))
os.replace(zip_local + ".part", zip_local)
zip_path = os.path.join(OUT_ROOT, zip_name)          # a copy in Drive (or the session's keep folder)
os.makedirs(OUT_ROOT, exist_ok=True)
shutil.copyfile(zip_local, zip_path + ".part")
os.replace(zip_path + ".part", zip_path)
print(f"step {step}: {zip_path} ({os.path.getsize(zip_path) / 1e6:.0f} MB)")
fetched = False
if LINK:
    h = hashlib.sha256()
    with open(zip_local, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    LINK.model_path = zip_local
    LINK.set(state="done", model={"file": zip_name, "bytes": os.path.getsize(zip_local), "sha256": h.hexdigest(), "policy": POLICY})
    print("The robot fetches the model now (it asks every few seconds)... / 로봇이 모델을 가져가는 중...", flush=True)
    end = time.time() + 1800
    while LINK.st["state"] != "fetched" and time.time() < end:
        time.sleep(3)
    fetched = LINK.st["state"] == "fetched"
    print("The robot has the model: it is under Autonomous skills. You can close this tab. / 로봇이 모델을 받았습니다: 자율 동작에 있습니다. 이 탭을 닫아도 됩니다."
          if fetched else "The robot did not fetch the model within 30 min: download it from Drive and upload it in the Studio. "
          "/ 로봇이 30분 안에 가져가지 않았습니다: Drive 에서 내려받아 스튜디오에 올리세요.")
if USE_DRIVE:
    try:
        from google.colab import drive
        drive.flush_and_unmount()      # Drive saves in the background: wait until the zip is really there
        print("saved in Google Drive / Google Drive 에 저장됨")
    except ImportError:
        pass
    if not fetched:
        print("Download it from drive.google.com: My Drive > SO101 > outputs > " + zip_name + " (right click > Download)")
        print("drive.google.com 에서 내려받기: 내 드라이브 > SO101 > outputs > " + zip_name)
elif not fetched:
    try:
        from google.colab import files
        print("Starting the download (save it now: everything here is gone when the session ends) / 지금 내려받으세요")
        files.download(zip_local)
    except ImportError:
        pass
if not fetched:
    print("Then: robot Wi-Fi > Studio > Train a skill > Manual way > Upload the model to the robot > choose this zip.")
    print("그다음: 로봇 Wi-Fi > 스튜디오 > 스킬 학습 > 수동 방식 > 모델을 로봇에 올리기 > 이 zip 선택.")
""")

md(r"""
## If something goes wrong / 문제 해결

- **The robot shows "lost"**: Colab disconnected or the tab was closed. Run all again and click **Connect to the robot** again.
- **Connect does nothing**: the robot page must be reachable from this computer (its Wi-Fi) at the address in cell 1 (`ROBOT`, normally http://10.42.0.1:8080), and the robot needs internet (LAN cable).
- **No GPU**: Runtime > Change runtime type > T4 GPU. Free accounts sometimes get no GPU; try again later. / GPU 없음: 런타임 유형을 T4 GPU 로. 무료 계정은 GPU 가 없을 때가 있으니 나중에 다시.
- **Disconnected**: open the notebook again and Run all. Cell 4 takes the last checkpoint from Drive and continues (with the robot: click Connect again). To start from zero, delete `SO101/outputs/<name>_<date>_act/` in Drive. / 연결 끊김: 다시 모두 실행. 처음부터 하려면 Drive 의 `SO101/outputs/<이름>_<날짜>_act/` 삭제.
- **`cannot import name 'FqnToConfig'`** in cell 4: cell 2 removes torchao by itself; with an old copy of this notebook, run `pip uninstall -y torchao` in a new cell, then cell 4 again.
- **pip errors**: LeRobot 0.6.1 installs on Python 3.12 and 3.13. If torch changed during the install, use Runtime > Restart session once, then Runtime > Run all again (cell 3 is quick the second time).
- **Speed**: ACT with batch 8 runs about 0.5 to 0.6 s per step on a T4, so 20,000 steps take 3 to 4 hours (the first skill: 3.5 hours). Free GPU time is limited; if Colab gives no GPU, continue another day: Run all picks up the checkpoint in Drive. The dataset is unpacked on the local disk (/content), so Drive speed does not matter.
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
