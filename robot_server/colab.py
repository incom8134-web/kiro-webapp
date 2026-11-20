"""Studio: "Train in Colab", the automatic way. Needs internet on the robot (LAN cable; the hotspot shares it).

Nobody signs the robot in anywhere. The Colab notebook (colab/so101_train_colab.ipynb, signed in to Google
by the user) opens a temporary internet address for its session (Cloudflare quick tunnel) with a one-time
key, and shows a "Connect to the robot" link. That link opens the Studio page, which hands the address
and key to this file. From then on the robot only makes outgoing HTTPS calls to that address:

  1 packing    the old pack_dataset.sh makes <name>_<date>.tar.gz + .sha256 (a fresh package is reused)
  2 waiting    for the Colab session (the user: Run all, then Connect)
  3 sending    the package in pieces (Cloudflare takes at most 100 MB per request), then its SHA-256
  4 training   the notebook's progress, every POLL_S seconds
  5 fetching   the model zip when the notebook says done, checked against its SHA-256, and only from the
               session this robot sent this run's package to itself (never a model it did not train)
  6 installed  through training.py's checks, as <package>_<policy> in the policies folder

The notebook's side of the conversation (header X-Kiro-Key on every call, JSON answers):
  GET  /status                          {"kiro": 1, "state": waiting|receiving|received|training|zipping|done|fetched|failed,
                                         "have": package file or null, "step", "steps", "error", "model": {file, bytes, sha256}}
  PUT  /dataset/<file>?part=i&parts=n   one piece, in order (part 0 starts again from nothing)
  POST /dataset/<file>/done?sha256=..   the whole package arrived: checked, then unpacked by the notebook
  GET  /model                           the trained model zip
  POST /fetched                         the robot has it: the notebook can end

Only the unlocked engineer starts, connects or stops (robot_server/main.py). The state is kept in a small
file, so a server restart during the hours of training goes on where it was.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .training import NAME_RE, ZIP_MAX, Refused

log = logging.getLogger("kiro.colab")

URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]{1,80}\.trycloudflare\.com")   # Cloudflare quick tunnels only
MOCK_URL_RE = re.compile(r"http://127\.0\.0\.1:\d{2,5}")    # mock mode only: a notebook run on this computer (tests, demos)
KEY_RE = re.compile(r"[A-Za-z0-9_-]{20,80}")
CHUNK = 32 * 1024 ** 2        # below Cloudflare's 100 MB per request
POLL_S = 15.0
LOST_AFTER = 8                # failed polls in a row (about 2 min) -> "lost": Colab disconnected or closed?
TRIES = 4                     # per piece and per call before giving up on this round
ROUNDS = 3                    # whole sends of the package to one session before giving up
QUIET_S = 1800.0              # training with no new step for this long: the page warns
ACTIVE = ("packing", "waiting", "sending", "training", "fetching", "installing", "lost")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The robot talks to exactly the address it was given: never follow it anywhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


class ColabLink:
    def __init__(self, robot):
        self.r = robot
        self.file = Path(robot.rec.backups).parent / (("mock_" if robot.mock else "") + "colab_run.json")
        self.lock = threading.Lock()
        self.run: dict | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.poll_s = POLL_S
        self._load()

    # ------------------------------------------------------------ state
    def _load(self) -> None:
        try:
            run = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(run, dict) and run.get("state") in ACTIVE + ("installed", "failed", "stopped"):
            self.run = run
            if run["state"] in ACTIVE:
                if run["state"] in ("sending", "fetching", "installing"):
                    run["state"] = "training" if run.get("url") else "waiting"   # interrupted: ask Colab again
                self._go()

    def _save(self) -> None:
        run = self.run
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(run, f)
            os.replace(tmp, self.file)
        except OSError as e:
            log.warning("colab: could not save the run state: %s", e)

    def _set(self, run: dict, **kw) -> bool:
        """Changes this run, only while it is the current one and still going: a late answer can never
        undo a Stop, nor write into the next run."""
        with self.lock:
            if self.run is not run or run["state"] not in ACTIVE:
                return False
            run.update(kw, updated=time.time())
            self._save()
            return True

    def snapshot(self) -> dict | None:
        run = self.run
        if not run:
            return None
        out = {k: run.get(k) for k in ("state", "repo", "pkg", "sent", "total", "step", "steps", "remote", "model",
                                        "error", "detail", "started", "updated", "connected")}
        out["host"] = run["url"].split("//", 1)[-1] if run.get("url") else None   # the key never leaves the robot
        quiet = time.time() - (run.get("step_at") or run.get("connected") or time.time())
        out["quiet_s"] = round(quiet) if run["state"] == "training" and quiet > QUIET_S else None
        return out

    # ----------------------------------------------------------- actions
    def start(self, repo: str, who: str) -> tuple[bool, str]:
        """Train this dataset in Colab: package it (or reuse a fresh package) and wait for the Colab session."""
        with self.lock:
            if self.run and self.run["state"] in ACTIVE:
                return False, "COLAB_BUSY"
        info = self.r.rec.info(repo, size=False)
        if not info["ok"]:
            return False, "NO_DATASET"
        name = repo.split("/")[1]
        pkg = next((p for p in self.r.train.packages()
                    if p["complete"] and p["name"] == name and p["mtime"] >= info.get("changed", 0)), None)
        ps = self.r.rec.pack_state()
        if pkg is None and not (ps and ps["running"] and ps["repo"] == repo):
            ok, msg = self.r.rec.pack(repo, who)
            if not ok:
                return False, msg
        with self.lock:
            self.run = {"state": "waiting" if pkg else "packing", "repo": repo, "pkg": pkg["file"] if pkg else None,
                        "url": None, "key": None, "sent_to": None, "rounds": 0, "sent": 0, "total": 0, "step": None,
                        "steps": None, "step_at": None, "remote": None, "model": None, "error": None, "detail": "",
                        "started": time.time(), "updated": time.time(), "connected": None, "by": who}
            self._save()
        self.r.event(who, f"train in Colab: {repo}" + (f" (package {pkg['file']})" if pkg else " (packaging first)"))
        self._go()
        return True, ""

    def connect(self, url: str, key: str, who: str) -> tuple[bool, str]:
        """The Colab session's address and key, from the "Connect to the robot" link (confirmed on the page).
        Also after Colab was restarted: the robot then sends the package again to the new session."""
        url, key = str(url or "").rstrip("/"), str(key or "")
        if not (URL_RE.fullmatch(url) or (self.r.mock and MOCK_URL_RE.fullmatch(url))) or not KEY_RE.fullmatch(key):
            return False, "COLAB_ADDR"
        with self.lock:
            run = self.run
            if not run or run["state"] not in ACTIVE + ("failed",):
                return False, "COLAB_NORUN"
            if run["state"] in ("sending", "fetching", "installing"):
                return False, "COLAB_MIDWAY"
            if run["state"] == "failed":
                if not run.get("pkg"):
                    return False, "COLAB_NORUN"     # packaging failed: start again
                run["state"] = "waiting"            # e.g. Colab was restarted after an error: try with the new session
            elif run["state"] == "lost":
                run["state"] = "training"
            run.update(url=url, key=key, rounds=0, connected=time.time(), error=None, detail="", updated=time.time())
            self._save()
        self.r.event(who, f"Colab session connected ({url.split('//', 1)[-1]})")
        self._go()
        return True, ""

    def stop(self, who: str) -> tuple[bool, str]:
        with self.lock:
            if not self.run:
                return False, "COLAB_NORUN"
            was = self.run["state"]
            if was in ACTIVE:
                self.run.update(state="stopped", updated=time.time())
                self._save()
            else:
                self.run = None                      # a finished run: just clear it from the page
                self.file.unlink(missing_ok=True)
        self._wake.set()
        if was in ACTIVE:
            self.r.event(who, "train in Colab: stopped on the robot (Colab keeps training until its tab is closed)")
        return True, ""

    def close(self) -> None:
        self._stop.set()
        self._wake.set()

    # ------------------------------------------------------------ worker
    def _go(self) -> None:
        self._wake.set()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._work, name="colab", daemon=True)
        self._thread.start()

    def _sleep(self, s: float) -> None:
        self._wake.wait(s)
        self._wake.clear()

    def _work(self) -> None:
        fails = 0
        while not self._stop.is_set():
            run = self.run
            if not run or run["state"] not in ACTIVE:
                return
            try:
                if run["state"] == "packing":
                    self._packing(run)
                    self._sleep(1.0)
                    continue
                if not run.get("url"):
                    self._sleep(self.poll_s)        # waiting for the "Connect to the robot" link
                    continue
                st = self._call(run, "GET", "/status")
                fails = 0
                self._step(run, st)
            except Refused as e:
                self._set(run, state="failed", error=e.code, detail=e.detail)
            except Exception as e:  # noqa: BLE001 - never die silently: an odd or cut answer counts like no answer
                fails += 1
                log.info("colab: no usable answer (%d): %r", fails, e)
                if fails >= LOST_AFTER and run["state"] != "lost":
                    self._set(run, state="lost", detail=str(e)[:160])
            if self.run is run and run["state"] in ACTIVE:
                self._sleep(self.poll_s)

    def _packing(self, run: dict) -> None:
        ps = self.r.rec.pack_state()
        if ps and ps["running"]:
            return
        name = run["repo"].split("/")[1]
        pkg = next((p for p in self.r.train.packages() if p["complete"] and p["name"] == name), None)
        if ps and ps["ok"] and pkg:
            self._set(run, state="waiting", pkg=pkg["file"])
        else:
            self._set(run, state="failed", error="COLAB_PACK", detail=" ".join((ps or {}).get("tail", [])[-2:])[:160])

    def _step(self, run: dict, st: dict) -> None:
        if st.get("kiro") != 1:
            raise Refused("COLAB_NOTKIRO", "not the HAEBOT notebook")
        remote = str(st.get("state"))[:20]
        have = st.get("have") if isinstance(st.get("have"), str) else None
        step = st.get("step") if isinstance(st.get("step"), int) and not isinstance(st.get("step"), bool) else run.get("step")
        steps = st.get("steps") if isinstance(st.get("steps"), int) and not isinstance(st.get("steps"), bool) else run.get("steps")
        upd = {"remote": remote, "step": step, "steps": steps}
        if step != run.get("step"):
            upd["step_at"] = time.time()
        if run["state"] == "lost":
            upd["state"] = "training"
        if not self._set(run, **upd):
            return                                   # stopped meanwhile
        if remote == "failed":
            raise Refused("COLAB_FAILED", str(st.get("error") or "")[:200])
        if remote in ("waiting", "receiving"):
            self._send(run)
            return
        # Anything further only from the session this robot sent this run's package to itself
        if run.get("sent_to") != run["url"] or have != run["pkg"]:
            raise Refused("COLAB_NOTOURS", str(have or remote)[:120])
        if remote in ("done", "fetched"):
            if not isinstance(st.get("model"), dict):
                raise ValueError("done, but no model described")
            self._fetch(run, st["model"])
        elif run["state"] != "training":
            self._set(run, state="training")

    # ---------------------------------------------------------- transfer
    def _call(self, run: dict, method: str, path: str, data: bytes | None = None, timeout: float = 30, limit: int = 65536) -> dict:
        req = urllib.request.Request(run["url"] + path, data=data, method=method,
                                     headers={"X-Kiro-Key": run["key"], "Content-Type": "application/octet-stream"})
        try:
            with OPENER.open(req, timeout=timeout) as r:
                body = r.read(limit + 1)
        except urllib.error.HTTPError as e:
            body = e.read(limit + 1)
            try:
                v = json.loads(body)
                err = v.get("error") if isinstance(v, dict) else None
            except ValueError:
                err = None
            if e.code in (401, 403):
                raise Refused("COLAB_KEY", str(err or e.reason)[:120])
            raise ValueError(f"HTTP {e.code}: {err or e.reason}")
        if len(body) > limit:
            raise ValueError("answer too big")
        v = json.loads(body)
        if not isinstance(v, dict):
            raise ValueError("odd answer")
        return v

    def _send(self, run: dict) -> None:
        if (run.get("rounds") or 0) >= ROUNDS:
            raise Refused("COLAB_SEND", f"{ROUNDS} times")
        pkg = self.r.train.package_path(run["pkg"] or "")
        sha_file = self.r.train.package_path(run["pkg"][: -len(".tar.gz")] + ".sha256") if run.get("pkg") else None
        if pkg is None or sha_file is None:
            raise Refused("COLAB_NOPKG", str(run.get("pkg")))
        sha = (sha_file.read_text(errors="replace").split() or [""])[0]
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise Refused("COLAB_NOPKG", f"{sha_file.name} holds no SHA-256")
        total = pkg.stat().st_size
        parts = max(1, -(-total // CHUNK))
        if not self._set(run, state="sending", sent=0, total=total, rounds=(run.get("rounds") or 0) + 1, sent_to=None):
            return
        with open(pkg, "rb") as f:
            for i in range(parts):
                piece = f.read(CHUNK)
                for attempt in range(TRIES):
                    if self._stop.is_set() or self.run is not run or run["state"] != "sending":
                        return
                    try:
                        self._call(run, "PUT", f"/dataset/{run['pkg']}?part={i}&parts={parts}", piece, timeout=600)
                        break
                    except Refused:
                        raise
                    except Exception as e:  # noqa: BLE001
                        if attempt == TRIES - 1:
                            raise
                        log.info("colab: piece %d/%d again (%r)", i + 1, parts, e)
                        time.sleep(2 + 3 * attempt)
                self._set(run, sent=min(total, (i + 1) * CHUNK))
        self._call(run, "POST", f"/dataset/{run['pkg']}/done?sha256={sha}", b"", timeout=300)
        if self._set(run, state="training", sent=total, sent_to=run["url"], step_at=time.time()):
            self.r.event("colab", f"dataset {run['pkg']} sent to Colab ({total / 1e6:.0f} MB)")

    def _model_name(self, run: dict, policy: str) -> str:
        tag = run["pkg"][: -len(".tar.gz")]
        base = f"{tag}_{policy}"[:76]
        names = {m["name"] for m in self.r.train.models()}
        name, n = base, 2
        while name in names or (self.r.train.root / name).exists():
            name, n = f"{base}_{n}", n + 1
        return name

    def _fetch(self, run: dict, model: dict) -> None:
        size, sha = model.get("bytes"), model.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= ZIP_MAX \
                or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise Refused("COLAB_MODEL", "odd model description")
        policy = model.get("policy") if isinstance(model.get("policy"), str) and NAME_RE.fullmatch(model["policy"]) else "act"
        name = self._model_name(run, policy)
        if not self._set(run, state="fetching", sent=0, total=size):
            return
        tr = self.r.train
        try:
            tmp = tr.begin_upload(name, size, "Colab")
        except Refused as e:
            if e.code == "BUSY":                    # someone uploads a model by hand right now: next poll
                raise ValueError("another upload is running") from e
            raise
        try:
            req = urllib.request.Request(run["url"] + "/model", headers={"X-Kiro-Key": run["key"]})
            h, n = hashlib.sha256(), 0
            with OPENER.open(req, timeout=60) as r, open(tmp, "wb") as f:
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    n += len(chunk)
                    if n > size:
                        raise Refused("COLAB_MODEL", "bigger than announced")
                    h.update(chunk)
                    f.write(chunk)
                    if n % (16 << 20) < len(chunk):
                        self._set(run, sent=n)
            if n != size or h.hexdigest() != sha:
                raise ValueError(f"model arrived damaged ({n} of {size} bytes)")   # try again at the next poll
            if not self._set(run, state="installing", sent=n):
                return
            info = tr.install(tmp, name, "Colab")
        finally:
            tr.end_upload(tmp)
        self._set(run, state="installed", model=info["name"], detail="")
        self.r.event("colab", f"model {info['name']} trained in Colab and installed")
        try:
            self._call(run, "POST", "/fetched", b"")
        except Exception:  # noqa: BLE001 - only a courtesy: the notebook stops waiting
            pass
