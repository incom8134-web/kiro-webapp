"""Studio: records demonstration datasets for training skills, with the old recorder.

The recording itself is LeRobot's `lerobot-record`, set up the way the old "SO-101 데이터 기록" window
(~/so101/record_gui.py) does it, started through record_runner.py as the job "record". This file:

  checks    the settings and the robot before a recording (arm ports, power, calibration, cameras,
            dataset new or added to, disk), each problem with what to do
  start     one recording at a time; a copy of the dataset is made before episodes are added to it
  control   next episode / record it again / stop and save (stdin of the runner), force stop (Ctrl+C)
  progress  from the runner's @REC lines: phase, episode, when the phase began
  review    episode and frame counts read back from the dataset (meta/info.json) after it ended
  pack      the old pack_dataset.sh (tar.gz + checksum + training notes for the upload)

Only the unlocked engineer reaches any of this (robot_server/main.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .jobs import JobSpec

log = logging.getLogger("kiro.studio")
HERE = Path(__file__).resolve().parent

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
TASK_MAX = 200
# The old window's number boxes: (lowest, highest)
LIMITS = {"episodes": (1, 500), "episode_s": (5, 600), "reset_s": (0, 300), "fps": (5, 60)}
DEFAULTS = {"arm": "right", "mode": "new", "repo": "", "task": "", "episodes": 10, "episode_s": 30, "reset_s": 10,
            "fps": 30, "vcodec": "h264", "streaming": True, "flicker": False}
CAMS_FOR = {"right": ["top", "right_wrist"], "left": ["top", "left_wrist"], "both": ["top", "right_wrist", "left_wrist"]}
MIN_FREE_GB = 2.0        # the old window's disk check
STOP_SAVE_S = 180.0      # the old window: after "stop" the recorder has 180 s to save, then Ctrl+C
MIN_PHASE_S = 1.0        # next / again / stop only after the phase ran this long (no empty episode, no double click)
BACKUPS_KEPT = 3
# Copy of ~/so101/record_gui.py ARMS (ports, followers, calibration files), used only in mock mode.
MOCK_ARMS = {
    "right": {"ports": ["right_follower", "right_leader"], "followers": ["right"],
              "calib": ["robots/so_follower/right_follower.json", "teleoperators/so_leader/right_leader.json"]},
    "left": {"ports": ["left_follower", "left_leader"], "followers": ["left"],
             "calib": ["robots/so_follower/left_follower.json", "teleoperators/so_leader/left_leader.json"]},
    "both": {"ports": ["left_follower", "right_follower", "left_leader", "right_leader"], "followers": ["left", "right"],
             "calib": ["robots/so_follower/so101_left.json", "robots/so_follower/so101_right.json",
                       "teleoperators/so_leader/so101_left.json", "teleoperators/so_leader/so101_right.json"]},
}
# The old window's HINTS: words in a failed recording's output -> what to do (the page has the text)
HINTS = [("read thread is not running", "camlost"), ("read failed (status=False)", "camread"),
         ("requires an explicit 'root'", "root"), ("Could not connect", "connect"), ("already exists", "exists"),
         ("FileExistsError", "exists"), ("Calibration", "calib"), ("camera not connected", "camread")]


# No left leader arm: the silver arm (the right leader) drives the green arm, or both arms (record_runner.py).
ONE_LEADER = {
    "left": {"ports": ["left_follower", "right_leader"], "followers": ["left"],
             "calib": ["robots/so_follower/left_follower.json", "teleoperators/so_leader/right_leader.json"]},
    "both": {"ports": ["left_follower", "right_follower", "right_leader"], "followers": ["left", "right"],
             "calib": ["robots/so_follower/left_follower.json", "robots/so_follower/right_follower.json",
                       "teleoperators/so_leader/right_leader.json"]},
}


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def robot_type(arm: str) -> str:
    return "bi_so_follower" if arm == "both" else "so101_follower"


def clean(settings: object) -> tuple[dict, list[dict]]:
    """The page's settings -> safe values, plus a check row for every setting that is wrong."""
    s = dict(DEFAULTS)
    raw = settings if isinstance(settings, dict) else {}
    rows: list[dict] = []
    s["arm"] = raw.get("arm") if raw.get("arm") in CAMS_FOR else "right"
    s["mode"] = "add" if raw.get("mode") == "add" else "new"
    s["repo"] = str(raw.get("repo", "")).strip()
    s["task"] = " ".join(str(raw.get("task", "")).split())
    s["vcodec"] = raw.get("vcodec") if raw.get("vcodec") in ("h264", "libsvtav1") else "h264"
    s["streaming"] = bool(raw.get("streaming", True))
    s["flicker"] = bool(raw.get("flicker", False))
    cams = raw.get("cams")
    s["cams"] = [str(c) for c in cams][:6] if isinstance(cams, list) else list(CAMS_FOR[s["arm"]])
    bad = []
    for k, (lo, hi) in LIMITS.items():
        try:
            v = int(raw.get(k, DEFAULTS[k]))
        except (TypeError, ValueError):
            v = None
        if v is None or not lo <= v <= hi:
            bad.append({"what": k, "lo": lo, "hi": hi})
            v = DEFAULTS[k]
        s[k] = v
    if not REPO_RE.match(s["repo"]) or ".." in s["repo"]:
        rows.append(row("name", "fail", "ck.name", fix="fix.name.format", d=s["repo"]))
    elif s["repo"].split("/", 1)[1].startswith("eval_"):
        rows.append(row("name", "fail", "ck.name", fix="fix.name.eval", d=s["repo"]))
    if not s["task"]:
        rows.append(row("task", "fail", "ck.task", fix="fix.task.empty"))
    elif len(s["task"]) > TASK_MAX:
        rows.append(row("task", "fail", "ck.task", fix="fix.task.long", fa={"n": TASK_MAX}))
    elif not s["task"].isascii():
        rows.append(row("task", "fail", "ck.task", fix="fix.task.english"))
    for b in bad:
        rows.append(row("num_" + b["what"], "fail", "ck.number", a={"what": b["what"]}, fix="fix.number", fa=b))
    return s, rows


def row(id_: str, st: str, k: str, a: dict | None = None, d: str = "", fix: str = "", fa: dict | None = None) -> dict:
    """One check line for the page: st ok / warn / fail, k + a = what (translated by the page),
    d = technical detail (paths, numbers), fix + fa = what to do."""
    return {"id": id_, "st": st, "k": k, "a": a or {}, "d": d, "fix": fix, "fa": fa or {}}


class Recorder:
    def __init__(self, robot):
        self.r = robot
        cfg = robot.cfg
        default = "~/.cache/kiro-webapp/mock_datasets" if robot.mock else os.environ.get("HF_LEROBOT_HOME", "~/.cache/huggingface/lerobot")
        self.root = Path(cfg.datasets_dir or default).expanduser()
        self.backups = Path(cfg.backups_dir or ("~/.cache/kiro-webapp/" + ("mock_" if robot.mock else "") + "dataset_backups")).expanduser()
        self.lock = threading.Lock()
        self.starting = False
        self.session: dict | None = None
        self.last_check: dict | None = None
        self.pack_repo: str | None = None
        self._ds_cache: tuple[float, list] = (0.0, [])
        self.mock_connect_s = 1.5
        self.arms: dict = MOCK_ARMS
        from .robot import PORT
        self.has_left_leader = lambda: robot.mock or os.path.exists(PORT["left_leader"])

    def one_leader(self, arm: str) -> bool:
        return arm in ONE_LEADER and not self.has_left_leader()

    def arm_def(self, arm: str) -> dict:
        return ONE_LEADER[arm] if self.one_leader(arm) else self.arms.get(arm, MOCK_ARMS[arm])

    # ------------------------------------------------------------ datasets
    def path_of(self, repo: str) -> Path | None:
        if not REPO_RE.match(repo or "") or ".." in repo:
            return None
        p = (self.root / repo).resolve()
        return p if self.root.resolve() in p.parents else None

    def info(self, repo: str, size: bool = True) -> dict:
        p = self.path_of(repo)
        out = {"repo": repo, "path": str(p) if p else "", "exists": bool(p and p.exists()), "ok": False}
        if not p or not (p / "meta" / "info.json").is_file():
            return out
        try:
            i = json.loads((p / "meta" / "info.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return out
        cams = sorted(k.split(".")[-1] for k, v in i.get("features", {}).items()
                      if k.startswith("observation.images.") and v.get("dtype") in ("video", "image"))
        out.update(ok=True, episodes=i.get("total_episodes", 0), frames=i.get("total_frames", 0), fps=i.get("fps"),
                   robot_type=i.get("robot_type"), cams=cams, mtime=p.stat().st_mtime)
        if size:
            out["mb"] = round(dir_size(p) / 1e6, 1)
        return out

    def datasets(self) -> list[dict]:
        """Every dataset in the folder, newest first (cached for a few seconds)."""
        t, cached = self._ds_cache
        if time.monotonic() - t < 3.0:
            return cached
        out = []
        if self.root.is_dir():
            for meta in self.root.glob("*/*/meta/info.json"):
                d = meta.parent.parent
                if d.parent.name.startswith("."):
                    continue
                out.append(self.info(f"{d.parent.name}/{d.name}"))
        out.sort(key=lambda x: x.get("mtime", 0), reverse=True)
        self._ds_cache = (time.monotonic(), out)
        return out

    # --------------------------------------------------------------- facts
    def facts(self) -> dict:
        """What the old code knows about the arms and cameras. Mock mode: a pretend robot where all is fine."""
        if self.r.mock:
            cams = [{"key": c["key"], "device": c.get("device"), "present": bool(c.get("present")) and not c.get("auto"),
                     "reason": c.get("reason"), "source": "gemini" if c["key"] == "top" else "by-path"} for c in self.r.cam_info]
            calib = {rel for a in MOCK_ARMS.values() for rel in a["calib"]}
            return {"tool": "lerobot-record (mock)", "tool_ok": True, "arms": MOCK_ARMS, "calib_dir": "(mock)",
                    "calib_ok": {rel: True for rel in calib}, "min_volt": 4.5, "users": {}, "cams": cams, "orbbec_ros": False}
        if not self.r.bus_lock.acquire(timeout=10):   # no servo probe while other programs are listed
            raise RuntimeError("Servo check is busy. Try again.")
        try:
            r = subprocess.run([str(self.r.venv / "bin" / "python"), str(HERE / "record_runner.py"), "check", "{}"],
                               capture_output=True, text=True, timeout=30,
                               env={**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, "KIRO_SO101_DIR": str(self.r.so101)})
        finally:
            self.r.bus_lock.release()
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError("Recorder check failed: " + ((r.stderr.strip().splitlines() or ["no output"])[-1])[:160])

    # --------------------------------------------------------------- checks
    def check(self, settings: object, estop: bool = False) -> dict:
        s, rows = clean(settings)
        try:
            f = self.facts()
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            rows.append(row("tool", "fail", "ck.tool", d=str(e)[:160], fix="fix.tool"))
            return self._result(s, rows)
        self.arms = f["arms"]
        arm = self.arm_def(s["arm"])
        if self.one_leader(s["arm"]):
            rows.append(row("leader", "ok", "ck.oneleader." + s["arm"], d="right_leader"))
        if self.r._arm_ok(s["arm"]):
            rows.append(row("arm", "fail", "ck.arm", a={"arm": s["arm"]}, fix="fix.leftlock"))
        if estop:
            rows.append(row("estop", "fail", "ck.estop", fix="fix.estop"))
        if self.r.jobs.running("record") or self.r.jobs.running("pack"):
            rows.append(row("running", "fail", "ck.running", fix="fix.running"))
        rows.append(row("tool", "ok" if f["tool_ok"] else "fail", "ck.tool", d=f["tool"], fix="" if f["tool_ok"] else "fix.tool"))

        boards = (self.r.health or {}).get("boards", {})
        from .robot import PORT
        followers = {f"{x}_follower" for x in arm["followers"]}
        for part in arm["ports"]:
            dev = PORT[part]
            b = boards.get(part) or {}
            job = self.r.jobs.busy(frozenset({dev}))
            st, d, fix, fa = "ok", dev, "", {}
            if job and job not in ("record", "pack"):
                st, fix, fa = "fail", "fix.busyjob", {"job": job}
            elif f["users"].get(part):
                st, fix, fa = "fail", "fix.busyother", {"prog": f["users"][part][:60]}
            elif self.r.mock:
                d = f"{dev} (mock)"
            elif not os.path.exists(dev) or b.get("state") == "missing":
                st, fix = "fail", "fix.usb"
            elif not b:
                st, fix = "warn", "fix.notread"
            elif b.get("state") == "silent":
                st, fix = "fail", "fix.power"
            elif b.get("state") == "busy":
                st, fix, fa = "fail", "fix.busyother", {"prog": str(b.get("by", "?"))}
            elif b.get("state") != "ok":
                st, fix, fa = "fail", "fix.board", {"msg": str(b.get("msg", b.get("state")))[:80]}
            else:
                v = b.get("vmin")
                d = f"{dev} · {b.get('n')}/{b.get('of')} · {v} V"
                if part in followers and (v is None or v < f["min_volt"]):
                    st, fix, fa = "fail", "fix.lowvolt", {"v": v, "min": f["min_volt"]}
            rows.append(row("port_" + part, st, "ck.port", a={"part": part}, d=d, fix=fix, fa=fa))
        for rel in arm["calib"]:
            ok = f["calib_ok"].get(rel, False)
            rows.append(row("calib_" + Path(rel).stem, "ok" if ok else "fail", "ck.calib", a={"file": Path(rel).name},
                            d=f"{f['calib_dir']}/{rel}", fix="" if ok else "fix.calib"))

        known = {c["key"]: c for c in f["cams"]}
        preview = {c["key"]: c for c in self.r.cam_info}
        for key in s["cams"]:
            c = known.get(key)
            job = self.r.jobs.busy(frozenset({f"cam:{key}"}))
            st, fix, fa, d = "ok", "", {}, (c or {}).get("device") or ""
            if c is None:
                st, fix = "fail", "fix.camconfig"
            elif not c.get("present"):
                auto = (preview.get(key) or {}).get("auto")
                st, fix, fa = "fail", ("fix.camauto" if auto else "fix.cam"), {"port": auto or ""}
                d = c.get("reason") or ""
            elif job and job not in ("record",):
                st, fix, fa = "fail", "fix.cambusy", {"job": job}
            elif c.get("source") == "gemini" and f.get("orbbec_ros"):
                st, fix = "fail", "fix.orbbec"
            rows.append(row("cam_" + key, st, "ck.cam", a={"key": key}, d=d, fix=fix, fa=fa))
        if not s["cams"]:
            rows.append(row("nocam", "warn", "ck.nocam"))

        rows.append(self._dataset_row(s))
        base = self.root if self.root.exists() else Path.home()
        free = shutil.disk_usage(base).free / 1e9
        rows.append(row("disk", "ok" if free >= MIN_FREE_GB else "fail", "ck.disk", a={"gb": round(free, 1)},
                        d=str(self.root), fix="" if free >= MIN_FREE_GB else "fix.disk"))
        if s["mode"] == "add":
            i = self.info(s["repo"])
            if i["ok"]:
                room = self._backup_room(i.get("mb", 0) * 1e6)
                rows.append(row("backup", "ok" if room else "warn", "ck.backup", d=str(self.backups),
                                a={"mb": i.get("mb", 0)}, fix="" if room else "fix.nobackup"))
        return self._result(s, rows)

    def _dataset_row(self, s: dict) -> dict:
        p = self.path_of(s["repo"])
        if p is None:
            return row("dataset", "fail", "ck.ds", a={"mode": s["mode"]}, fix="fix.name.format")
        i = self.info(s["repo"], size=False)
        if s["mode"] == "new":
            if p.exists():
                return row("dataset", "fail", "ck.ds", a={"mode": "new"}, d=str(p), fix="fix.dsbroken" if not i["ok"] else "fix.dsexists",
                           fa={"n": i.get("episodes", 0)})
            return row("dataset", "ok", "ck.ds", a={"mode": "new"}, d=str(p))
        if not i["ok"]:
            return row("dataset", "fail", "ck.ds", a={"mode": "add"}, d=str(p), fix="fix.dsbroken" if p.exists() else "fix.dsmissing")
        want = robot_type(s["arm"])
        if i.get("robot_type") and i["robot_type"] != want:
            return row("dataset", "fail", "ck.ds", a={"mode": "add"}, d=str(p), fix="fix.dsrobot", fa={"have": i["robot_type"], "want": want})
        if i.get("fps") and int(i["fps"]) != s["fps"]:
            return row("dataset", "fail", "ck.ds", a={"mode": "add"}, d=str(p), fix="fix.dsfps", fa={"have": i["fps"], "want": s["fps"]})
        if sorted(i.get("cams", [])) != sorted(s["cams"]):
            return row("dataset", "fail", "ck.ds", a={"mode": "add"}, d=str(p), fix="fix.dscams",
                       fa={"have": ", ".join(i.get("cams", [])) or "-", "want": ", ".join(s["cams"]) or "-"})
        return row("dataset", "ok", "ck.ds", a={"mode": "add"}, d=f"{p} · {i['episodes']} ep · {i['frames']} frames")

    def _result(self, s: dict, rows: list[dict]) -> dict:
        res = {"ok": not any(x["st"] == "fail" for x in rows), "rows": rows, "settings": s, "t": time.time()}
        self.last_check = res
        return res

    # ----------------------------------------------------------------- start
    def _backup_room(self, size: float) -> bool:
        try:
            self.backups.mkdir(parents=True, exist_ok=True)
            return size * 1.2 < shutil.disk_usage(self.backups).free - MIN_FREE_GB * 1e9
        except OSError:
            return False

    def _backup(self, repo: str) -> str | None:
        """A full copy before LeRobot adds episodes: adding rewrites the data and video files, and a
        crash in the middle must not cost the episodes already recorded. The newest copies are kept."""
        src = self.path_of(repo)
        if src is None or not self._backup_room(dir_size(src)):
            return None
        stem = repo.replace("/", "__")
        dst = self.backups / f"{stem}__{time.strftime('%Y%m%d_%H%M%S')}"
        shutil.copytree(src, dst)
        for old in sorted(self.backups.glob(f"{stem}__*"))[:-BACKUPS_KEPT]:
            shutil.rmtree(old, ignore_errors=True)
        return str(dst)

    def start(self, settings: object, who: str, estop: bool = False) -> tuple[bool, str]:
        with self.lock:   # a double click or a second device cannot start a second recording
            if self.starting or self.r.jobs.running("record"):
                return False, "A recording is already running."
            self.starting = True
        try:
            res = self.check(settings, estop)
            if not res["ok"]:
                return False, "CHECKS_FAILED"
            s = res["settings"]
            info = self.info(s["repo"], size=False)
            backup = self._backup(s["repo"]) if s["mode"] == "add" else None
            cfg = {**s, "root": str(self.path_of(s["repo"])), "resume": s["mode"] == "add", "mock_connect_s": self.mock_connect_s,
                   "one_leader": self.one_leader(s["arm"])}
            from .robot import PORT
            armdef = self.arm_def(s["arm"])
            why = self.r._sync_goals([PORT[f"{x}_follower"] for x in armdef["followers"]])
            if why:   # LeRobot switches torque on: stale targets would make an arm flip
                return False, why
            ports = {PORT[p] for p in armdef["ports"]}
            uses = frozenset(ports | {f"cam:{k}" for k in s["cams"]})
            if self.r.mock:
                cmd = [sys.executable, "-u", str(HERE / "record_runner.py"), "mock", json.dumps(cfg)]
            else:
                cmd = [str(self.r.venv / "bin" / "python"), "-u", str(HERE / "record_runner.py"), "record", json.dumps(cfg)]
            sess = {"settings": s, "base": info.get("episodes", 0) if s["mode"] == "add" else 0, "started": time.time(),
                    "phase": "starting", "since": time.time(), "ep": None, "redo": 0, "sent": None, "stop_at": None,
                    "forced": False, "cmd": "", "errors": [], "result": None, "backup": backup, "saved": 0}
            env = {**self.r._venv_env(), "KIRO_SO101_DIR": str(self.r.so101), "HF_HUB_OFFLINE": "1"}
            spec = JobSpec(cmd, uses, cwd=str(self.r.so101) if not self.r.mock else None, env=env, label="recording",
                           stop_wait=60.0, stdin=True, on_line=lambda line: self._line(sess, line))
            ok, msg = self.r._start("record", spec, who)
            if ok:
                self.session = sess
                self.r.event(who, f"recording: {s['arm']} arm, {s['episodes']} episodes" + (" (added to a dataset)" if backup or s["mode"] == "add" else ""))
            return ok, msg
        finally:
            self.starting = False

    # -------------------------------------------------------------- progress
    def _line(self, sess: dict, line: str) -> None:
        if not line.startswith("@REC "):
            if "Traceback" in line or "Error" in line or "error" in line:
                sess["errors"] = (sess["errors"] + [line.strip()[:240]])[-8:]
            return
        m = json.loads(line[5:])
        t = m.get("t", time.time())
        if "cmd" in m:
            sess["cmd"] = m["cmd"]
        if m.get("phase") in ("tuning", "connecting"):
            sess.update(phase=m["phase"], since=t)
        elif m.get("phase") == "forced":
            sess["forced"] = True
        say = m.get("say", "")
        if say.startswith("Recording episode"):
            n = int(say.split()[-1])
            sess.update(phase="rec", since=t, ep=n, sent=None, saved=n - sess["base"])
        elif say.startswith("Reset the environment"):
            sess.update(phase="reset", since=t, sent=None)
        elif say.startswith("Re-record episode"):
            sess["redo"] += 1
        elif say.startswith("Stop recording"):
            sess.update(phase="saving", since=t, sent=None)

    def poll(self) -> None:
        """Called every few seconds and before each snapshot: the end of a recording, the save timeout."""
        sess, j = self.session, self.r.jobs.get("record")
        if not sess or not j or sess["result"] is not None:
            return
        if j.running:
            if sess["stop_at"] and time.time() - sess["stop_at"] > STOP_SAVE_S and not sess.get("sigint"):
                sess["sigint"] = True   # the old window: saving took too long, Ctrl+C
                log.warning("recording: still saving after %d s, sending Ctrl+C", STOP_SAVE_S)
                threading.Thread(target=self.r.jobs.stop, args=("record",), daemon=True).start()
            return
        if j.code is None:      # output not fully read yet
            return
        info = self.info(sess["settings"]["repo"])
        added = max(0, info.get("episodes", 0) - sess["base"]) if info["ok"] else 0
        hint = next((h for pat, h in HINTS if any(pat in e for e in sess["errors"])), "other")
        # -9: E-STOP kills the recorder at once like teleop (robot.on_estop); it cannot finish the dataset then
        state = "estop" if j.code == -9 else "stopped" if (sess["forced"] or j.code in (130, -2)) else "done" if j.code == 0 else "failed"
        sess["result"] = {"state": state, "code": j.code, "info": info, "added": added, "hint": hint if state == "failed" else "",
                          "ended": j.ended or time.time()}
        sess["phase"] = state
        self._ds_cache = (0.0, [])
        self.r.event("studio", f"recording ended: {state}, {added} episode(s) saved")

    # --------------------------------------------------------------- control
    def control(self, cmd: str, ep: object, phase: object, who: str) -> tuple[bool, str]:
        sess, j = self.session, self.r.jobs.get("record")
        if not sess or not j or not j.running:
            return False, "No recording is running."
        if cmd == "kill":   # Ctrl+C: LeRobot drops the unfinished episode and finishes the saved ones
            sess["forced"] = True
            threading.Thread(target=self.r.jobs.stop, args=("record",), daemon=True).start()
            self.r.event(who, "recording: force stop")
            return True, "FORCE_SENT"
        if cmd not in ("next", "redo", "stop"):
            return False, "Unknown command."
        if sess["stop_at"]:
            return False, "STOPPING"
        if sess["phase"] not in ("rec", "reset"):
            return False, "NOT_NOW"
        if cmd != "stop" and (ep != sess["ep"] or phase != sess["phase"]):
            return False, "STALE"    # the page saw an older episode or phase: a late double click
        if sess["sent"] is not None:
            return False, "STALE"    # one command per phase; the next phase accepts the next one
        if time.time() - sess["since"] < MIN_PHASE_S:
            return False, "TOO_SOON"
        if not j.send(cmd):
            return False, "The recorder did not take the command."
        sess["sent"] = cmd
        if cmd == "stop":
            sess["stop_at"] = time.time()
        self.r.event(who, f"recording: {cmd}")
        return True, ""

    # ------------------------------------------------------------------ pack
    def pack(self, repo: str, who: str) -> tuple[bool, str]:
        """The old 'package for upload' button: pack_dataset.sh <name> -> ~/Desktop/데이터셋_업로드/."""
        if self.r.jobs.running("record") or self.r.jobs.running("pack"):
            return False, "RUNNING"
        i = self.info(repo, size=False)
        if not i["ok"]:
            return False, "NO_DATASET"
        if self.r.mock:
            out = self.root.parent / "mock_uploads" / (repo.split("/")[1] + "_mock.tar.gz")
            code = ("import sys,tarfile,os,time;src,out=sys.argv[1],sys.argv[2];os.makedirs(os.path.dirname(out),exist_ok=True);"
                    "print('묶는 중: '+out,flush=True);time.sleep(1);"
                    "t=tarfile.open(out,'w:gz');t.add(src,arcname=os.path.basename(src));t.close();print('완료:',flush=True)")
            cmd = [sys.executable, "-c", code, i["path"], str(out)]
        else:
            cmd = ["bash", str(self.r.so101 / "pack_dataset.sh"), repo]
        env = {**self.r._venv_env(), "HF_LEROBOT_HOME": str(self.root), "LEROBOT_VENV": str(self.r.venv)}
        ok, msg = self.r._start("pack", JobSpec(cmd, frozenset(), cwd=str(self.r.so101) if not self.r.mock else None,
                                                env=env, label="package dataset", stop_wait=10.0), who)
        if ok:
            self.pack_repo = repo
        return ok, msg

    def pack_state(self) -> dict | None:
        j = self.r.jobs.get("pack")
        if not j or not self.pack_repo:
            return None
        lines = j.output()
        out = next((x.split(":", 1)[1].strip() for x in lines if x.startswith("묶는 중:")), "")
        return {"repo": self.pack_repo, "running": j.running, "code": j.code, "file": out, "started": j.started,
                "ok": j.code == 0 and bool(out) and os.path.exists(out), "tail": lines[-6:]}

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        self.poll()
        sess = self.session
        j = self.r.jobs.get("record")
        view = None
        if sess:
            view = {k: sess[k] for k in ("settings", "base", "started", "phase", "since", "ep", "redo", "sent", "forced",
                                         "result", "backup", "saved", "cmd")}
            view["stopping"] = bool(sess["stop_at"])
            view["running"] = bool(j and j.running)
            view["log"] = [x for x in (j.output() if j else []) if not x.startswith("@REC ")][-40:]
            view["errors"] = sess["errors"][-3:]
        return {"t": "studio", "now": time.time(), "root": str(self.root), "datasets": self.datasets(), "session": view,
                "check": self.last_check, "pack": self.pack_state(), "defaults": DEFAULTS, "limits": LIMITS,
                "cams_for": CAMS_FOR, "starting": self.starting}
