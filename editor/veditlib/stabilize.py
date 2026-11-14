"""Stabilization with FFmpeg's vid.stab filters (needs an FFmpeg built with libvidstab).

Two passes: an analysis pass measures the camera shake of the clip's part of the source file and is
cached by file, position and length, then the render applies the smoothing transform. There is
deliberately no fallback: FFmpeg's built-in `deshake` barely reduces real shake, and a placebo would
mislead. Without vid.stab the editor says so.
"""
from __future__ import annotations

import functools
import hashlib
import subprocess
from pathlib import Path

from . import timemap
from .project import EditError, Project


@functools.lru_cache(maxsize=1)
def available() -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "vidstabdetect" in r.stdout and "vidstabtransform" in r.stdout


def smoothing(strength: float) -> int:
    """Frames of camera motion to average: gentle at 0, heavy (a locked-off look) at 1."""
    return int(round(5 + 40 * max(0.0, min(1.0, strength))))


def needs_stabilizing(c: dict) -> bool:
    return bool(c.get("stabilize")) and not c.get("hold") and c.get("type") != "text"


def require() -> None:
    if not available():
        raise EditError("stabilization needs an FFmpeg built with libvidstab (the vidstabdetect filter). "
                        "Install a full FFmpeg build, or remove the stabilize setting.")


def analysis_file(p: Project, c: dict) -> Path:
    src = p.asset_path(c["asset"])
    st = src.stat()
    span = timemap.source_span(c)
    key = hashlib.sha1(f"{src}|{st.st_mtime_ns}|{st.st_size}|{c['src_in']:.4f}|{span:.4f}".encode()).hexdigest()[:16]
    d = p.dir / ".cache"
    d.mkdir(exist_ok=True)
    return d / f"stab_{key}.trf"


def prepare(p: Project) -> dict[str, Path]:
    """Run the analysis pass for every stabilized clip that lacks one. Returns clip id -> analysis file."""
    out: dict[str, Path] = {}
    clips = [c for c in p.clips if needs_stabilizing(c)]
    if not clips:
        return out
    require()
    for c in clips:
        trf = analysis_file(p, c)
        if not trf.is_file():
            span = timemap.source_span(c)
            tmp = trf.with_name(trf.stem + ".tmp.trf")
            # Run inside the cache folder so the result path has no drive letter or colon to escape.
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{c['src_in']:.4f}", "-t", f"{span + 0.05:.4f}",
                 "-i", str(p.asset_path(c["asset"])), "-vf", f"vidstabdetect=shakiness=6:accuracy=9:result={tmp.name}",
                 "-f", "null", "-"],
                capture_output=True, text=True, cwd=trf.parent)
            if r.returncode or not tmp.is_file():
                raise EditError(f"could not analyse camera shake for clip {c['id']}:\n" + "\n".join(r.stderr.strip().splitlines()[-3:]))
            tmp.replace(trf)
        out[c["id"]] = trf
    return out
