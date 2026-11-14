#!/usr/bin/env python3
"""Shared helpers for the pro-video-editing SFX tools.

Standard library + FFmpeg only. Imported by the other scripts in this folder.
"""
from __future__ import annotations

import hashlib
import json
import math
import operator
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from array import array
from pathlib import Path

AUDIO_EXTS = {".wav", ".flac", ".ogg", ".mp3", ".aif", ".aiff", ".m4a"}
SKILL_ROOT = Path(__file__).resolve().parent.parent
USER_DIR = Path.home() / ".pro-video-editing"

# --------------------------------------------------------------------------
# Vocabulary: category -> words that signal it. Used to tag sounds from their
# file and folder names, and to expand search queries ("swoosh" finds "whoosh").
# --------------------------------------------------------------------------
VOCAB = {
    "whoosh": "whoosh woosh swoosh swish swipe flyby fly pass sweep swing whip airy",
    "impact": "impact hit slam punch thud crash smash bang knock thump strike slap",
    "boom": "boom sub rumble drop low bass deep thunder",
    "explosion": "explosion explode blast detonation bomb burst fireball",
    "riser": "riser rise build buildup swell uplifter upsweep tension crescendo",
    "transition": "transition reverse rewind downlifter downsweep stinger sting morph",
    "ui": "ui click tap button beep blip notification interface menu toggle switch "
    "select cursor confirm error alert ping tick pop plop",
    "glitch": "glitch digital data static corrupt stutter",
    "ambience": "ambience ambient atmosphere atmos room tone wind rain city forest nature "
    "birds night street cafe ocean sea river fire storm winds haunting ghostly",
    "foley": "foley footstep footsteps step cloth rustle door creak cup paper keys glass "
    "bottle scrape zipper chair fabric rope book books page pages clock typewriter "
    "suitcase luggage lock telephone phone dial scissor scissors dryer trimmer brush "
    "cards card shuffle shuffling broom sleigh seat squeak squeaks shield armor metal "
    "box tape blowtorch lid prop props object",
    "voice": "voice vox voxmale voxfem voxreac yell yells breath breaths breathing exhale "
    "scream shout grunt radio police spectator speech dialogue",
    "magic": "magic sparkle shimmer chime bell glitter twinkle fairy spell",
    "electric": "electric spark buzz zap laser shock hum power energy scifi",
    "cartoon": "cartoon boing bonk slide whistle spring comic funny",
    "crowd": "applause cheer laugh laughter clap crowd audience boo gasp",
    "vehicle": "car engine motor truck vehicle tire skid horn plane jet helicopter train",
    "water": "water splash drip pour stream dive bubble",
    "animal": "animal dog cat bird lion roar growl bark dinosaur dinosaurs raptor rex "
    "hatchling anmlrept creature",
}
VOCAB = {k: set(v.split()) for k, v in VOCAB.items()}

STOP = set(
    "wav mp3 ogg flac aif aiff m4a sfx fx sound sounds audio take file the and for with "
    "from of in on to an vol ver version mix master stereo mono khz hz lib library pack "
    "bit copy new final".split()
)


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def require_tools(*names: str) -> None:
    for n in names:
        if shutil.which(n) is None:
            die(f"'{n}' not found on PATH. Install FFmpeg (https://ffmpeg.org) and retry.")


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return s or "pack"


def safe_name(text: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", text)


# --------------------------------------------------------------------------
# Text -> tags / category
# --------------------------------------------------------------------------
def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # split camelCase
    out = []
    for w in re.findall(r"[A-Za-z]+", text):
        w = w.lower()
        if len(w) >= 2 and w not in STOP:
            out.append(w)
    return out


def tags_for(path: str, max_tags: int = 20) -> list[str]:
    p = Path(path)
    ordered = tokens(p.stem)
    for parent in list(p.parents)[:3]:
        if parent.name:
            ordered += tokens(parent.name)
    seen, out = set(), []
    for t in ordered:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:max_tags]


def categorize(tags: list[str]) -> str:
    best, best_hits = "other", 0
    for cat, words in VOCAB.items():
        hits = sum(1 for t in tags if t == cat or t in words)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def related_categories(word: str) -> set[str]:
    return {c for c, words in VOCAB.items() if word == c or word in words}


# --------------------------------------------------------------------------
# Library access: a zip, a folder of audio, or a folder containing zips.
# --------------------------------------------------------------------------
def _is_audio(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return (
        Path(name).suffix.lower() in AUDIO_EXTS
        and not base.startswith("._")
        and "__MACOSX" not in name
    )


class Library:
    """Read access to one sound pack on disk."""

    def __init__(self, root):
        self.root = Path(root).expanduser()
        if not self.root.exists():
            die(f"library not found: {self.root}")
        if self.root.is_file() and not zipfile.is_zipfile(self.root):
            die(f"not a zip file: {self.root}")
        self._tls = threading.local()

    def _zip(self, container: str) -> zipfile.ZipFile:
        cache = getattr(self._tls, "zips", None)
        if cache is None:
            cache = self._tls.zips = {}
        if container not in cache:
            zpath = self.root if self.root.is_file() else self.root / container
            cache[container] = zipfile.ZipFile(zpath)
        return cache[container]

    @property
    def kind(self) -> str:
        return "zip" if self.root.is_file() else "folder"

    def entries(self):
        """Yield (container, path) for every audio file. container is '' for loose files."""
        if self.root.is_file():
            for n in self._zip("").namelist():
                if _is_audio(n):
                    yield "", n
            return
        for p in sorted(self.root.rglob("*")):
            if p.is_dir() or p.name.startswith("._") or "__MACOSX" in p.parts:
                continue
            rel = p.relative_to(self.root).as_posix()
            suffix = p.suffix.lower()
            if suffix in AUDIO_EXTS:
                yield "", rel
            elif suffix == ".zip" and zipfile.is_zipfile(p):
                for n in self._zip(rel).namelist():
                    if _is_audio(n):
                        yield rel, n

    def extract(self, container: str, path: str, dest_dir, filename: str | None = None) -> Path:
        dest = Path(dest_dir) / safe_name(filename or Path(path).name)
        if self.root.is_file() or container:
            with self._zip(container).open(path) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
        else:
            shutil.copyfile(self.root / path, dest)
        return dest


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------
def find_catalog(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            die(f"catalog not found: {p}")
        return p
    env = os.environ.get("SFX_CATALOG")
    candidates = [Path(env).expanduser()] if env else []
    candidates += [USER_DIR / "catalog.json", SKILL_ROOT / "catalog" / "catalog.json"]
    for c in candidates:
        if c.is_file():
            return c
    die(
        "no SFX catalog found. Set the sound library up first:\n"
        "  python3 scripts/setup_library.py                      (default library, free download)\n"
        "  python3 scripts/build_catalog.py /path/to/sounds.zip  (your own sounds)\n"
        "(see references/sfx-library-setup.md)"
    )


def load_catalog(explicit: str | None = None) -> dict:
    p = find_catalog(explicit)
    data = json.loads(p.read_text(encoding="utf-8"))
    for s in data["sounds"]:
        s["name"] = Path(s["path"]).stem
    data["_path"] = str(p)
    data["_by_id"] = {s["id"]: s for s in data["sounds"]}
    return data


def write_catalog(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    head = {k: v for k, v in data.items() if k != "sounds"}
    with open(path, "w", encoding="utf-8") as f:
        f.write("{\n")
        for k, v in head.items():
            f.write(f" {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)},\n")
        f.write(' "sounds": [\n')
        f.write(
            ",\n".join(
                "  " + json.dumps(s, ensure_ascii=False, separators=(",", ":"))
                for s in data["sounds"]
            )
        )
        f.write("\n ]\n}\n")


class Libraries:
    """Resolve each pack named in a catalog to a Library on this machine."""

    def __init__(self, catalog: dict, library_args=None):
        self.packs = catalog["packs"]
        args = list(library_args or [])
        env = os.environ.get("SFX_LIBRARY")
        if env and not args:
            args = [a for a in env.split(os.pathsep) if a]
        self.overrides: dict[str, str] = {}
        for a in args:
            if "=" in a and not Path(a).exists():
                k, v = a.split("=", 1)
                self.overrides[k] = v
            else:
                if len(self.packs) != 1:
                    die(
                        "this catalog has several packs; pass --library PACK=PATH "
                        f"(packs: {', '.join(self.packs)})"
                    )
                self.overrides[next(iter(self.packs))] = a
        self._cache: dict[str, Library] = {}

    def get(self, pack: str) -> Library:
        if pack not in self._cache:
            path = self.overrides.get(pack) or self.packs.get(pack, {}).get("path")
            if not path or not Path(path).expanduser().exists():
                die(
                    f"audio for pack '{pack}' not found on this machine "
                    f"(looked for: {path}).\n"
                    f"Point to it with --library {pack}=PATH or SFX_LIBRARY=PATH."
                )
            self._cache[pack] = Library(path)
        return self._cache[pack]


# --------------------------------------------------------------------------
# Audio analysis (ffprobe + ffmpeg)
# --------------------------------------------------------------------------
ENV_SR = 16000
BLOCK = 160  # 10 ms at ENV_SR


def probe_audio(path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries",
        "stream=sample_rate,channels,bits_per_sample,bits_per_raw_sample,duration:format=duration",
        "-of", "json", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "ffprobe failed")
    j = json.loads(r.stdout)
    streams = j.get("streams") or []
    if not streams:
        raise RuntimeError("no audio stream")
    s = streams[0]
    dur = float(s.get("duration") or j.get("format", {}).get("duration") or 0)
    bits = int(s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 0)
    return {
        "dur": dur,
        "sr": int(s.get("sample_rate") or 0),
        "ch": int(s.get("channels") or 0),
        "bits": bits,
    }


def analyze_envelope(path, max_seconds: int = 180) -> dict:
    """Decode to 16 kHz mono and measure where the sound starts, hits and ends."""
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-t", str(max_seconds), "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(ENV_SR), "-f", "s16le", "-",
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode or not r.stdout:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or "decode failed")
    a = array("h")
    a.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    n = len(a)
    peaks, sumsq = [], 0
    for i in range(0, n, BLOCK):
        blk = a[i : i + BLOCK]
        peaks.append(max(max(blk), -min(blk)))
        sumsq += sum(map(operator.mul, blk, blk))
    peak = max(peaks)
    if peak == 0:
        return {"lead_s": 0.0, "onset_s": 0.0, "peak_s": 0.0, "tail_s": 0.0,
                "peak_dbfs": -120.0, "rms_dbfs": -120.0, "silent": True}

    def first_cross(threshold: float) -> float:
        for bi, p in enumerate(peaks):
            if p >= threshold:
                base = bi * BLOCK
                for j, v in enumerate(a[base : base + BLOCK]):
                    if abs(v) >= threshold:
                        return (base + j) / ENV_SR
        return 0.0

    pk_block = peaks.index(peak)
    blk = a[pk_block * BLOCK : (pk_block + 1) * BLOCK]
    off = max(range(len(blk)), key=lambda j: abs(blk[j]))
    peak_s = (pk_block * BLOCK + off) / ENV_SR
    last = max(i for i, p in enumerate(peaks) if p >= peak * 0.01)
    tail_end = min((last + 1) * BLOCK, n) / ENV_SR
    return {
        "lead_s": first_cross(peak * 0.01),
        "onset_s": first_cross(peak * 0.1),
        "peak_s": peak_s,
        "tail_s": max(0.0, tail_end - peak_s),
        "peak_dbfs": 20 * math.log10(peak / 32768),
        "rms_dbfs": 20 * math.log10(max(math.sqrt(sumsq / n), 1e-9) / 32768),
        "silent": False,
    }


def classify_kind(dur: float, peak_dbfs: float, rms_dbfs: float) -> str:
    """'bed' = long, steady material (ambience/loop); 'one-shot' = everything else."""
    crest = peak_dbfs - rms_dbfs
    return "bed" if dur >= 6.0 and crest < 20.0 else "one-shot"


def sound_id(pack: str, container: str, path: str, length: int = 8) -> str:
    return hashlib.sha1(f"{pack}|{container}|{path}".encode()).hexdigest()[:length]
