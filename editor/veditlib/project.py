"""The project file: one JSON document, safe for an AI and a human to edit at the same time.

Everything the editor knows lives in <project>/project.json. Writes are atomic and
serialized by a lock file, and every save keeps the previous version in .history/ so
`undo` works from the CLI, the UI or a script.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the skill folder
SCRIPTS = ROOT / "scripts"
FILE = "project.json"
VERSION = 1
HISTORY_KEEP = 50
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

DEFAULT_SETTINGS = {"width": 1280, "height": 720, "fps": 30, "bg": "black"}
DEFAULT_TRACKS = [
    {"id": "V1", "kind": "video"},
    {"id": "V2", "kind": "video"},
    {"id": "A1", "kind": "audio"},
    {"id": "A2", "kind": "audio", "duck": True},  # sound effects sit here and duck under A1/video audio
]


class EditError(Exception):
    """A problem with what was asked for (bad id, bad value). Shown to the user as-is."""


def r4(x: float) -> float:
    return round(float(x), 4)


# --------------------------------------------------------------------------
# Locating, loading, saving
# --------------------------------------------------------------------------
def find_project(arg: str | os.PathLike | None = None) -> Path:
    p = Path(arg or ".").expanduser()
    if p.is_dir():
        p = p / FILE
    if not p.is_file():
        raise EditError(f"no project at {p}. Create one with: vedit new NAME")
    return p.resolve()


@contextmanager
def locked(path: Path):
    lock = path.with_name(path.stem + ".lock")
    deadline = time.time() + 10
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 300:  # left behind by a crash (tracking can hold it for a while)
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                raise EditError("the project is locked by another process; try again in a moment")
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


class Project:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    # -- convenient views --------------------------------------------------
    @property
    def dir(self) -> Path:
        return self.path.parent

    @property
    def settings(self) -> dict:
        return self.data["settings"]

    @property
    def assets(self) -> dict:
        return self.data["assets"]

    @property
    def tracks(self) -> list:
        return self.data["tracks"]

    @property
    def clips(self) -> list:
        return self.data["clips"]

    @property
    def out_dir(self) -> Path:
        d = self.dir / "out"
        d.mkdir(exist_ok=True)
        return d

    def track(self, tid: str) -> dict:
        for t in self.tracks:
            if t["id"] == tid:
                return t
        raise EditError(f"no track '{tid}' (tracks: {', '.join(t['id'] for t in self.tracks)})")

    def clip(self, cid: str) -> dict:
        for c in self.clips:
            if c["id"] == cid:
                return c
        raise EditError(f"no clip '{cid}'. Run: vedit show")

    def asset_path(self, asset_id: str) -> Path:
        a = self.assets.get(asset_id)
        if not a:
            raise EditError(f"no asset '{asset_id}'. Run: vedit show")
        p = Path(a["path"])
        return p if p.is_absolute() else (self.dir / p)

    def track_end(self, tid: str) -> float:
        return max((c["start"] + c["dur"] for c in self.clips if c["track"] == tid), default=0.0)

    def duration(self) -> float:
        return r4(max((c["start"] + c["dur"] for c in self.clips), default=0.0))

    def new_id(self, prefix: str, existing) -> str:
        used = set(existing)
        n = 1
        while f"{prefix}{n}" in used:
            n += 1
        return f"{prefix}{n}"


def new_data(width=None, height=None, fps=None) -> dict:
    s = dict(DEFAULT_SETTINGS)
    for k, v in (("width", width), ("height", height), ("fps", fps)):
        if v:
            s[k] = v
    return {
        "version": VERSION,
        "rev": 0,
        "settings": s,
        "assets": {},
        "tracks": json.loads(json.dumps(DEFAULT_TRACKS)),
        "clips": [],
    }


def _read(path: Path) -> dict:
    last = None
    for _ in range(20):  # a reader can collide with an atomic replace on Windows
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError, FileNotFoundError) as exc:
            last = exc
            time.sleep(0.05)
    raise EditError(f"cannot read {path}: {last}")


def load(path: Path) -> Project:
    data = _read(path)
    if data.get("version") != VERSION:
        raise EditError(f"{path} is project version {data.get('version')}; this editor reads version {VERSION}")
    return Project(path, data)


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save(project: Project, keep_history: bool = True) -> None:
    """Caller holds the lock. Bumps rev and archives the version being replaced."""
    path = project.path
    if keep_history and path.exists():
        hist = path.parent / ".history"
        hist.mkdir(exist_ok=True)
        old = _read(path)
        shutil.copyfile(path, hist / f"{int(old.get('rev', 0)):06d}.json")
        for stale in sorted(hist.glob("*.json"))[:-HISTORY_KEEP]:
            stale.unlink(missing_ok=True)
    project.data["rev"] = int(project.data.get("rev", 0)) + 1
    _write(path, project.data)


def create(folder: Path, width=None, height=None, fps=None) -> Project:
    folder = folder.expanduser().resolve()
    path = folder / FILE
    if path.exists():
        raise EditError(f"{path} already exists")
    (folder / "media").mkdir(parents=True, exist_ok=True)
    (folder / "out").mkdir(exist_ok=True)
    project = Project(path, new_data(width, height, fps))
    with locked(path):
        save(project, keep_history=False)
    return project


def transact(path: Path, fn):
    """Load, apply fn(project), save, all under the lock. Returns fn's result."""
    with locked(path):
        project = load(path)
        result = fn(project)
        save(project)
        return result, project


def undo(path: Path) -> int:
    with locked(path):
        hist = path.parent / ".history"
        snaps = sorted(hist.glob("*.json")) if hist.is_dir() else []
        if not snaps:
            raise EditError("nothing to undo")
        data = _read(snaps[-1])
        current = _read(path)
        data["rev"] = int(current.get("rev", 0)) + 1
        _write(path, data)
        snaps[-1].unlink()
        return data["rev"]


# --------------------------------------------------------------------------
# Media probing
# --------------------------------------------------------------------------
def probe_media(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        raise EditError("ffprobe not found on PATH. Install FFmpeg 5 or newer (https://ffmpeg.org).")
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode:
        raise EditError(f"cannot read {path.name} as media: {r.stderr.strip()[:200]}")
    j = json.loads(r.stdout)
    streams = j.get("streams", [])
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    has_audio = any(s["codec_type"] == "audio" for s in streams)
    info = {"has_audio": has_audio}
    if v and path.suffix.lower() in IMAGE_EXTS:
        info.update(kind="image", duration=0.0, width=int(v["width"]), height=int(v["height"]))
    elif v:
        num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den) if den and float(den) else 0.0
        dur = float(j["format"].get("duration") or v.get("duration") or 0)
        info.update(kind="video", duration=r4(dur), width=int(v["width"]), height=int(v["height"]), fps=round(fps, 3))
    elif has_audio:
        info.update(kind="audio", duration=r4(float(j["format"].get("duration") or 0)))
    else:
        raise EditError(f"{path.name} has no audio or video stream")
    return info
