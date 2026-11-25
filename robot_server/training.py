"""Studio: the training files. Datasets go to Google Colab, trained models come back.

  packages   the old pack_dataset.sh output (<name>_<date>.tar.gz + .sha256), listed for download
  models     every <policies>/<name>/pretrained_model with what its files say: policy type, arm(s),
             cameras, training steps, dataset, size, date. A model trained on both arms (or on the
             left arm) cannot be played yet: the skill runner (run_policy.sh) drives the right arm only
  install    a model zip from the browser: checked (only model files, size, no path tricks,
             config.json + model.safetensors), then moved to <policies>/<name>/pretrained_model
  zip        a model as a zip for download, in the same layout as the Colab zip
  delete     a dataset, a package or a model, only inside its own folder

Only the unlocked engineer reaches any of this (robot_server/main.py).
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import struct
import threading
import time
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Callable, Iterator

log = logging.getLogger("kiro.training")

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")         # always fullmatch: "$" lets a newline through
# Files a model folder may hold: settings and weights only. No pickles (.bin, .pt): loading those can run code.
FILE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,119}\.(json|safetensors)")
EXTRA_FILES = {"README.md"}
SKIPPED = {".DS_Store", "Thumbs.db"}           # added by a Mac or Windows when zipping again: left out, not refused
PKG_RE = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)_(?P<stamp>\d{8}_\d{4})\.tar\.gz")
ZIP_MAX = 3 * 1024 ** 3        # an ACT model zip is about 0.2 GB, a diffusion model about 1 GB
UNPACKED_MAX = 4 * 1024 ** 3
MAX_FILES = 40
JSON_MAX = 10 * 1024 ** 2       # settings files are a few KB; the pages read them every second
CD_MAX = 1024 ** 2              # the zip's table of contents; read before the zip is opened (a huge one would fill the memory)
MIN_FREE_GB = 2.0
INCOMING = ".incoming"          # uploads are unpacked here first (same disk), then moved in one step


class Refused(Exception):
    """A model zip that is not installed: code (translated by the page) and a technical detail."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def _json(path: Path) -> dict:
    try:
        if path.stat().st_size > JSON_MAX:
            return {}
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _dim(feats: dict, key: str) -> int | None:
    shape = (feats.get(key) or {}).get("shape") if isinstance(feats.get(key), dict) else None
    return int(shape[0]) if isinstance(shape, list) and len(shape) == 1 and isinstance(shape[0], int) else None


# Camera names the robot and run_policy.sh actually open. A policy trained under any other name
# (laptop, phone, wrist, ...) cannot be played: lerobot-rollout looks up those names and fails.
ROBOT_CAMS = frozenset({"top", "right_wrist", "left_wrist"})


def arms_of(config: dict) -> str:
    """Which arm(s) a model was trained on, from its config.json: "right", "left", "both", "other" or "?".

    One SO-101 arm has 6 motors, so a one-arm model reads and writes 6 numbers and a two-arm model 12
    (LeRobot 0.6.1's bi_so_follower: left_* then right_*). The camera names do not tell one arm from
    two, but a one-arm model that sees the left hand camera and not the right one was recorded on the
    left arm. "other" = numbers that fit no arm of this robot. "?" = the file does not say (played as before).
    """
    ins, outs = config.get("input_features") or {}, config.get("output_features") or {}
    if not isinstance(ins, dict) or not isinstance(outs, dict):
        return "?"
    a, s = _dim(outs, "action"), _dim(ins, "observation.state")
    if a is None and s is None:
        return "?"
    if a is not None and s is not None and a != s:
        return "other"
    n = a if a is not None else s
    cams = {k.split(".")[-1] for k in ins if k.startswith("observation.images.")}
    if n == 12:
        return "both"
    if n == 6:
        return "left" if "left_wrist" in cams and "right_wrist" not in cams else "right"
    return "other"


def model_info(d: Path) -> dict:
    """One model folder (<policies>/<name>/pretrained_model) as the pages show it."""
    c = _json(d / "config.json")
    tc = _json(d / "train_config.json")
    ins = c.get("input_features") if isinstance(c.get("input_features"), dict) else {}
    cams = sorted(k.split(".")[-1] for k in ins if k.startswith("observation.images."))
    arms = arms_of(c)
    unknown = [c for c in cams if c not in ROBOT_CAMS]
    if arms in ("both", "left", "other"):
        block = arms
    elif unknown:
        block = "cams"
    else:
        block = ""
    size, files = 0, 0
    try:
        for f in d.iterdir():
            if f.is_file():
                size += f.stat().st_size
                files += 1
        mtime = (d / "model.safetensors").stat().st_mtime
    except OSError:
        mtime = 0.0
    ds = tc.get("dataset") if isinstance(tc.get("dataset"), dict) else {}
    steps = tc.get("steps") if isinstance(tc.get("steps"), int) else None
    done = _json(d / "kiro_training.json").get("step")       # written by the Studio's Colab notebook
    return {"name": d.parent.name, "path": str(d), "type": str(c.get("type") or "?"), "cams": cams, "arms": arms,
            "block": block, "playable": not block, "steps": done if isinstance(done, int) else steps,
            "dataset": str(ds.get("repo_id") or ""), "batch": tc.get("batch_size") if isinstance(tc.get("batch_size"), int) else None,
            "mb": round(size / 1e6, 1), "files": files, "mtime": mtime}


_INFO_CACHE: dict[str, tuple[tuple, dict]] = {}


def _cached_info(d: Path) -> dict:
    """model_info, read again only when a file in the folder changed (the pages ask every second)."""
    try:
        sig = tuple(sorted((e.name, e.stat().st_size, e.stat().st_mtime_ns) for e in os.scandir(d)))
    except OSError:
        sig = ()
    hit = _INFO_CACHE.get(str(d))
    if hit and hit[0] == sig:
        return dict(hit[1])
    info = model_info(d)
    _INFO_CACHE[str(d)] = (sig, info)
    return dict(info)


def list_models(root: Path) -> list[dict]:
    """Every usable model folder, by name. Folders starting with a dot (uploads in progress) are left out."""
    out = []
    for cfgf in root.glob("*/pretrained_model/config.json"):
        d = cfgf.parent
        if d.parent.name.startswith(".") or not (d / "model.safetensors").is_file():
            continue
        out.append(_cached_info(d))
    out.sort(key=lambda m: m["name"])
    return out


def zip_entries(path: Path) -> tuple[int, int]:
    """(entries, table size) from the end of a zip, read without opening it. Raises Refused."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 65557))
            tail = f.read()
    except OSError as e:
        raise Refused("NOT_ZIP", str(e)[:120])
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or len(tail) < i + 22:
        raise Refused("NOT_ZIP", "no end record")
    n, cd = struct.unpack("<H", tail[i + 10:i + 12])[0], struct.unpack("<I", tail[i + 12:i + 16])[0]
    if n == 0xFFFF or cd == 0xFFFFFFFF:          # zip64: the real numbers are in the zip64 end record
        j = tail.rfind(b"PK\x06\x06", 0, i)
        if j < 0 or len(tail) < j + 56:
            raise Refused("NOT_ZIP", "no zip64 end record")
        n, cd = struct.unpack("<Q", tail[j + 32:j + 40])[0], struct.unpack("<Q", tail[j + 40:j + 48])[0]
    return n, cd


def file_chunks(path: Path, alive: Callable[[], bool]) -> Iterator[bytes]:
    """A file for download, stopped as soon as alive() says the Studio was locked or left."""
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            if not alive():
                return
            yield chunk


def _safetensors_ok(path: Path) -> bool:
    """A safetensors file starts with the length of a JSON header, then the header (the tensor list)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if not 2 <= n <= 100_000_000 or n + 8 > size:
                return False
            head = json.loads(f.read(n))
        return isinstance(head, dict)
    except (OSError, ValueError, struct.error):
        return False


class _Sink:
    """What zipfile writes, handed out piece by piece (a model zip is streamed, never kept whole)."""

    def __init__(self):
        self.parts: list[bytes] = []

    def write(self, b) -> int:
        self.parts.append(bytes(b))
        return len(b)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out, self.parts = b"".join(self.parts), []
        return out


class Training:
    def __init__(self, robot):
        self.r = robot
        cfg = robot.cfg
        self.root = Path(cfg.policies_dir or ("~/.cache/kiro-webapp/mock_policies" if robot.mock else "~/policies")).expanduser()
        # pack_dataset.sh writes here (its own default on the robot); mock packages stay apart
        self.pkg_dir = Path(cfg.packages_dir).expanduser() if cfg.packages_dir else (
            robot.rec.root.parent / "mock_uploads" if robot.mock else Path("~/Desktop/데이터셋_업로드").expanduser())
        self.colab_url = cfg.colab_url
        self.notebook = Path(__file__).resolve().parent.parent / "colab" / "so101_train_colab.ipynb"
        self.lock = threading.Lock()
        self.uploading: dict | None = None
        shutil.rmtree(self.root / INCOMING, ignore_errors=True)    # left over from a server that stopped mid-upload

    # ------------------------------------------------------------ listing
    def models(self) -> list[dict]:
        return list_models(self.root) if self.root.is_dir() else []

    def packages(self) -> list[dict]:
        out = []
        if self.pkg_dir.is_dir():
            for f in self.pkg_dir.glob("*.tar.gz"):
                m = PKG_RE.fullmatch(f.name)
                if not m or not f.is_file():
                    continue
                sha = f.with_name(f.name[: -len(".tar.gz")] + ".sha256")
                st = f.stat()
                # pack_dataset.sh writes the checksum after the tar.gz: without it the package is still being
                # written, or packing failed and the file is cut short. Such a file is listed, never offered.
                out.append({"file": f.name, "name": m["name"], "mb": round(st.st_size / 1e6, 1), "mtime": st.st_mtime,
                            "sha": sha.name if sha.is_file() else None, "complete": sha.is_file()})
        out.sort(key=lambda p: p["mtime"], reverse=True)
        return out

    def package_path(self, file: str) -> Path | None:
        """A listed package or its checksum, for download. Anything else: None."""
        for p in self.packages():
            if p["complete"] and file in (p["file"], p["sha"]):
                return self.pkg_dir / file
        return None

    def model_dir(self, name: str) -> Path | None:
        if not NAME_RE.fullmatch(name or ""):
            return None
        d = self.root / name / "pretrained_model"
        return d if any(m["name"] == name for m in self.models()) else None

    def snapshot(self) -> dict:
        up = self.uploading
        return {"models": self.models(), "packages": self.packages(), "colab": self.colab_url,
                "notebook": self.notebook.is_file(), "policies": str(self.root), "pkg_dir": str(self.pkg_dir),
                "zip_max_mb": ZIP_MAX // 10 ** 6, "uploading": {k: up[k] for k in ("name", "by", "started")} if up else None}

    # ----------------------------------------------------------- download
    def zip_stream(self, name: str, alive: Callable[[], bool] = lambda: True) -> Iterator[bytes]:
        """The model as <name>_pretrained_model.zip with the folder pretrained_model/ inside, as the
        Colab notebook makes it, so the same zip can be uploaded again on another robot. Stops when
        alive() says the Studio was locked or left. Fastest compression: weights hardly shrink."""
        d = self.model_dir(name)
        if d is None:
            return
        sink = _Sink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for f in sorted(d.iterdir()):
                if not f.is_file() or f.is_symlink():
                    continue
                zi = zipfile.ZipInfo.from_file(f, "pretrained_model/" + f.name)
                zi.compress_type = zipfile.ZIP_DEFLATED
                setattr(zi, "compress_level" if hasattr(zi, "compress_level") else "_compresslevel", 1)   # from_file leaves it unset (= 6)
                with open(f, "rb") as src, zf.open(zi, "w") as dst:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        if not alive():
                            return
                        dst.write(chunk)
                        yield sink.take()
                yield sink.take()
        yield sink.take()

    # ------------------------------------------------------------- upload
    def begin_upload(self, name: str, size: int | None, who: str) -> Path:
        """Before the bytes arrive: the name, the size, the disk. Returns the file to write the zip to."""
        if not NAME_RE.fullmatch(name or ""):
            raise Refused("NAME")
        if (self.root / name).exists():
            raise Refused("EXISTS", name)
        if size is not None and size > ZIP_MAX:
            raise Refused("TOO_BIG", f"{size / 1e6:.0f} MB")
        with self.lock:
            if self.uploading:
                raise Refused("BUSY", self.uploading["by"])
            self.uploading = {"name": name, "by": who, "started": time.time()}
        try:
            inc = self.root / INCOMING
            inc.mkdir(parents=True, exist_ok=True)
            need = (size or 0) * 2 + MIN_FREE_GB * 1e9
            free = shutil.disk_usage(inc).free
            if free < need:
                raise Refused("DISK", f"{free / 1e9:.1f} GB")
            return inc / f"{uuid.uuid4().hex}.zip"
        except BaseException:
            self.end_upload(None)
            raise

    def end_upload(self, tmp: Path | None) -> None:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        self.uploading = None

    def install(self, zpath: Path, name: str, who: str) -> dict:
        """Checks the uploaded zip and installs it as <policies>/<name>/pretrained_model. Raises Refused."""
        if not NAME_RE.fullmatch(name or ""):
            raise Refused("NAME")
        target = self.root / name
        if target.exists():
            raise Refused("EXISTS", name)
        n, cd = zip_entries(zpath)
        if n > MAX_FILES * 2 or cd > CD_MAX:
            raise Refused("TOO_MANY", str(n))
        try:
            zf = zipfile.ZipFile(zpath)
        except (zipfile.BadZipFile, OSError, ValueError, EOFError) as e:
            raise Refused("NOT_ZIP", str(e)[:120])
        stage = self.root / INCOMING / uuid.uuid4().hex
        try:
            with zf:
                files = self._pick(zf)
                need = sum(i.file_size for i in files.values()) + MIN_FREE_GB * 1e9
                stage.parent.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(stage.parent).free
                if free < need:       # what it unpacks to, not what the zip weighs: the disk must not fill up
                    raise Refused("DISK", f"{free / 1e9:.1f} GB")
                stage.mkdir(parents=True)
                d = stage / "pretrained_model"
                d.mkdir()
                total = 0
                for base, info in files.items():
                    with zf.open(info) as src, open(d / base, "xb") as dst:
                        n = 0
                        for chunk in iter(lambda: src.read(1 << 20), b""):
                            n += len(chunk)
                            total += len(chunk)
                            if n > info.file_size or total > UNPACKED_MAX:
                                raise Refused("TOO_BIG", base)
                            dst.write(chunk)
            for f in d.glob("*.json"):
                if not _json(f) and f.read_bytes().strip() != b"{}":
                    raise Refused("BAD_CONFIG", f.name)
            c = _json(d / "config.json")
            if not isinstance(c.get("type"), str) or not isinstance(c.get("input_features"), dict) \
                    or not isinstance(c.get("output_features"), dict):
                raise Refused("BAD_CONFIG", "config.json")
            for f in d.glob("*.safetensors"):
                if not _safetensors_ok(f):
                    raise Refused("BAD_WEIGHTS", f.name)
            missing = self._missing(d)
            if missing:
                raise Refused("MISSING", ", ".join(missing))
            try:
                target.mkdir()          # fails if the name was taken in the meantime
            except FileExistsError:
                raise Refused("EXISTS", name)
            os.rename(d, target / "pretrained_model")
        except (zipfile.BadZipFile, zlib.error, EOFError) as e:   # damaged while unpacking (CRC, cut short, bad data)
            raise Refused("NOT_ZIP", str(e)[:120])
        except NotImplementedError as e:   # e.g. Deflate64 from Windows "compressed folder" for big files
            raise Refused("ZIP_METHOD", str(e)[:120])
        except OSError as e:
            raise Refused("DISK" if e.errno == errno.ENOSPC else "WRITE", str(e)[:120])
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        info = model_info(target / "pretrained_model")
        self.r.event(who, f"installed model {name} ({info['type']}, {info['mb']} MB)")
        return info

    @staticmethod
    def _missing(d: Path) -> list[str]:
        """What LeRobot 0.6.1's lerobot-rollout loads besides config.json and model.safetensors: the two
        processor files and every state file they name (normalisation numbers). Without them Play fails."""
        out = []
        for proc in ("policy_preprocessor.json", "policy_postprocessor.json"):
            if not (d / proc).is_file():
                out.append(proc)
                continue
            steps = _json(d / proc).get("steps")
            for step in steps if isinstance(steps, list) else []:
                sf = step.get("state_file") if isinstance(step, dict) else None
                if sf is not None and (not isinstance(sf, str) or not FILE_RE.fullmatch(sf) or not (d / sf).is_file()):
                    out.append(str(sf)[:80])
        return out

    @staticmethod
    def _pick(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
        """The zip's model files by name. Only one model folder (config.json + model.safetensors side by
        side, at the top or in a folder like pretrained_model/), nothing else, no tricks."""
        by_dir: dict[str, dict[str, zipfile.ZipInfo]] = {}
        infos = zf.infolist()
        if len(infos) > MAX_FILES * 2:
            raise Refused("TOO_MANY", str(len(infos)))
        for i in infos:
            n = i.filename
            parts = n.rstrip("/").split("/")
            if not n or n.startswith("/") or "\\" in n or "\x00" in n or ":" in parts[0] or any(p in ("", ".", "..") for p in parts):
                raise Refused("BAD_PATH", n[:120])
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise Refused("BAD_PATH", n[:120])          # a link pointing somewhere else
            if parts[0] == "__MACOSX" or parts[-1] in SKIPPED or i.is_dir():
                continue
            if i.flag_bits & 0x1:
                raise Refused("ENCRYPTED", n[:120])
            folder = "/".join(parts[:-1])
            if parts[-1] in by_dir.setdefault(folder, {}):
                raise Refused("BAD_PATH", n[:120])
            by_dir[folder][parts[-1]] = i
        homes = [f for f, names in by_dir.items() if {"config.json", "model.safetensors"} <= set(names)]
        if not homes:
            have = {n for names in by_dir.values() for n in names}
            raise Refused("NO_MODEL", ", ".join(x for x in ("config.json", "model.safetensors") if x not in have))
        if len(homes) > 1:
            raise Refused("MANY_MODELS", ", ".join(sorted(h or "/" for h in homes))[:160])
        home = homes[0]
        if home.count("/") > 2:
            raise Refused("BAD_PATH", home[:120])
        other = sorted(f"{f}/{n}" if f else n for f, names in by_dir.items() if f != home for n in names)
        if other:
            raise Refused("EXTRA_FILES", ", ".join(other[:4]) + (" ..." if len(other) > 4 else ""))
        files = by_dir[home]
        bad = sorted(n for n in files if not FILE_RE.fullmatch(n) and n not in EXTRA_FILES)
        if bad:
            raise Refused("BAD_FILE", ", ".join(bad[:4]))
        if len(files) > MAX_FILES:
            raise Refused("TOO_MANY", str(len(files)))
        big = sorted(n for n, i in files.items() if n.endswith(".json") and i.file_size > JSON_MAX)
        if big:
            raise Refused("TOO_BIG", ", ".join(big[:4]))
        if sum(i.file_size for i in files.values()) > UNPACKED_MAX:
            raise Refused("TOO_BIG", f"{sum(i.file_size for i in files.values()) / 1e6:.0f} MB")
        return files

    # ------------------------------------------------------------- delete
    def delete(self, kind: str, name: str, who: str) -> tuple[bool, str]:
        """Deletes one dataset, package or model for good, only if it is in its own listing."""
        name = str(name or "")
        if kind == "model":
            if not any(m["name"] == name for m in self.models()) or not NAME_RE.fullmatch(name):
                return False, "NOT_FOUND"
            if self.r.jobs.running("skill"):
                return False, "IN_USE"
            path = self.root / name
        elif kind == "dataset":
            if not any(d["repo"] == name and d.get("ok") for d in self.r.rec.datasets(fresh=True)):
                return False, "NOT_FOUND"
            if self.r.jobs.running("record") or self.r.jobs.running("pack") or self.r.rec.starting:
                return False, "IN_USE"
            path = self.r.rec.path_of(name)
            if path is None or name.split("/")[0] == "calibration":
                return False, "NOT_FOUND"
        elif kind == "package":
            p = next((p for p in self.packages() if p["file"] == name), None)
            if p is None:
                return False, "NOT_FOUND"
            if self.r.jobs.running("pack"):
                return False, "IN_USE"
            tag = name[: -len(".tar.gz")]
            for f in (name, f"{tag}.sha256", f"{tag}_README.md"):
                (self.pkg_dir / f).unlink(missing_ok=True)
            self.r.event(who, f"deleted package {name}")
            return True, ""
        else:
            return False, "NOT_FOUND"
        base = self.root if kind == "model" else self.r.rec.root
        if path.is_symlink() or not path.is_dir() or base.resolve() not in path.resolve().parents:
            return False, "NOT_FOUND"
        shutil.rmtree(path)
        if kind == "dataset":
            self.r.rec.forget()
            try:
                path.parent.rmdir()       # the user folder, when this was its last dataset
            except OSError:
                pass
        self.r.event(who, f"deleted {kind} {name}")
        return True, ""
