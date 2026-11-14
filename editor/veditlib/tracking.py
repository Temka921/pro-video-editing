"""Motion tracking by template matching, using FFmpeg's find_rect filter.

You point at something in one frame (a face, a sign, a car); a small patch around the point becomes the
template, and FFmpeg searches every frame of the clip for the best match, forwards and backwards from that
frame. The result is a path of positions that masks can follow (to blur a face, say) or that a title or
sticker can be attached to.

This follows rigid, textured things that keep roughly the same size and orientation. It will lose a target
that turns, grows quickly, leaves the frame, is hidden, or has no texture. It reports how much of the clip it
could follow so you can judge.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from . import keys, timemap
from .project import EditError, Project, r4

WORK_WIDTH = 640
THRESHOLD = 0.25       # find_rect score: lower is a better match; above this the frame counts as lost
MAX_JUMP = 0.18        # a target cannot move more than this fraction of the frame between two frames
RDP_EPS = 0.002        # simplify the path to within 0.2% of the frame
MAX_STORED = 150


def check_trackable(c: dict, asset: dict) -> None:
    if c.get("type") == "text" or asset["kind"] != "video":
        raise EditError("tracking needs a video clip")
    if c.get("hold"):
        raise EditError("a frozen clip has no motion to track")
    if c.get("reverse") or timemap.is_ramped(c):
        raise EditError("track a clip before reversing it or ramping its speed (set reverse off, remove speed keys, track, then restore them)")
    if c.get("crop") or c.get("flip"):
        raise EditError("track a clip before cropping or flipping it: positions are measured on the original picture")


def _run(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", cwd=cwd)
    if r.returncode:
        raise EditError("ffmpeg failed while tracking:\n" + "\n".join(r.stderr.strip().splitlines()[-3:]))
    return r.stderr


def _parse(log: str) -> list[tuple[float, float, float, float]]:
    """(time, x, y, score) for every frame in which find_rect reported a match."""
    found = []
    for block in log.split("frame:")[1:]:
        t = re.search(r"pts_time:(-?[\d.]+)", block)
        x, y, s = (re.search(rf"lavfi\.rect\.{k}=(-?[\d.eE+-]+)", block) for k in ("x", "y", "score"))
        if t and x and y and s:
            found.append((float(t.group(1)), float(x.group(1)), float(y.group(1)), float(s.group(1))))
    return found


def _walk(raw, start_xy, frame_w: float) -> list[tuple[float, float, float]]:
    """Accept matches in time order, rejecting ones that jump implausibly far from the last accepted match."""
    out, last = [], start_xy
    for t, x, y, score in raw:
        if score > THRESHOLD:
            continue
        if last is not None and max(abs(x - last[0]), abs(y - last[1])) > MAX_JUMP * frame_w:
            continue
        out.append((t, x, y))
        last = (x, y)
    return out


def rdp(points: list[tuple[float, float]], eps: float) -> list[int]:
    """Indices of the points that keep a polyline within eps of the original (Ramer-Douglas-Peucker)."""
    if len(points) < 3:
        return list(range(len(points)))
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (t0, v0), (t1, v1) = points[a], points[b]
        worst, idx = 0.0, None
        for i in range(a + 1, b):
            t, v = points[i]
            interp = v0 + (v1 - v0) * ((t - t0) / (t1 - t0) if t1 != t0 else 0)
            d = abs(v - interp)
            if d > worst:
                worst, idx = d, i
        if idx is not None and worst > eps:
            keep.add(idx)
            stack += [(a, idx), (idx, b)]
    return sorted(keep)


def simplify(path: list[list[float]], eps: float = RDP_EPS, limit: int = MAX_STORED) -> list[list[float]]:
    """Fewer points, same path: [[t, x, y], ...]."""
    while True:
        ix = set(rdp([(p[0], p[1]) for p in path], eps)) | set(rdp([(p[0], p[2]) for p in path], eps))
        out = [path[i] for i in sorted(ix)]
        if len(out) <= limit or eps > 0.05:
            return out
        eps *= 1.6


def analyze(p: Project, c: dict, at: float, x: float, y: float, size: float = 0.08) -> dict:
    """Track the point (x, y) (fractions of the picture) seen at timeline time `at` through clip `c`."""
    asset = p.assets[c["asset"]]
    check_trackable(c, asset)
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise EditError("x and y are fractions of the picture, from 0 to 1")
    if not 0.02 <= size <= 0.5:
        raise EditError("size (the patch to follow, as a fraction of the picture height) must be between 0.02 and 0.5")
    local = float(at) - c["start"]
    if not -0.001 <= local <= c["dur"] + 0.001:
        raise EditError(f"time {at:g}s is outside clip {c['id']} ({c['start']:g}s to {c['start'] + c['dur']:g}s)")
    local = max(0.0, min(c["dur"], local))
    src = p.asset_path(c["asset"])
    lo = c["src_in"]
    hi = lo + timemap.source_span(c)
    ts = min(hi, lo + local * c.get("speed", 1.0))

    ww = min(WORK_WIDTH, int(asset["width"])) // 2 * 2
    wh = max(2, round(int(asset["height"]) * ww / int(asset["width"]) / 2) * 2)
    side = max(16, int(round(size * wh)))
    left, top = int(round(x * ww - side / 2)), int(round(y * wh - side / 2))
    if left < 0 or top < 0 or left + side > ww or top + side > wh:
        raise EditError("the patch around that point runs off the picture; choose a point farther from the edge or a smaller size")

    with tempfile.TemporaryDirectory(prefix="vedit_track_") as td:
        work = Path(td)
        _run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{ts:.4f}", "-i", str(src), "-frames:v", "1",
              "-vf", f"scale={ww}:{wh}", "frame.png"], work)
        _run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", "frame.png", "-vf",
              f"crop={side}:{side}:{left}:{top},format=gray", "-pix_fmt", "gray", "tmpl.pgm"], work)
        flat = _run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", "tmpl.pgm", "-vf", "signalstats,metadata=print",
                     "-f", "null", "-"], work)
        lo_y, hi_y = (float(m.group(1)) for k in ("YMIN", "YMAX") for m in [re.search(rf"signalstats\.{k}=([\d.]+)", flat)])
        if hi_y - lo_y < 14:
            raise EditError("that spot is too flat and featureless to follow; pick something with detail (an edge, a pattern, a face)")

        common = (f"scale={ww}:{wh},format=gray,")
        match = f"find_rect=object=tmpl.pgm:threshold={THRESHOLD}:mipmaps=3,metadata=print"
        forward = _parse(_run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-ss", f"{ts:.4f}", "-t", f"{max(0.1, hi - ts):.4f}",
                               "-i", str(src), "-vf", common + match, "-f", "null", "-"], work))
        backward = []
        if ts - lo > 0.1:
            rev = _parse(_run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-ss", f"{lo:.4f}", "-t", f"{ts - lo:.4f}",
                               "-i", str(src), "-vf", common + "reverse," + match, "-f", "null", "-"], work))
            backward = [(ts - t, rx, ry, s) for t, rx, ry, s in rev]
    start_xy = (float(left), float(top))
    fwd = _walk(forward, start_xy, ww)
    bwd = _walk(backward, start_xy, ww)
    pts = sorted([(ts + t, rx, ry) for t, rx, ry in fwd] + [(t, rx, ry) for t, rx, ry in bwd if t < ts - 0.001])
    if len(pts) < 3:
        raise EditError("tracking could not follow that spot in more than a couple of frames; pick a more distinctive patch")
    expected = max(1, round((hi - lo) * float(asset.get("fps") or 30)))
    found = min(1.0, len(pts) / expected)

    # Centres as fractions of the picture, lightly smoothed to remove pixel jitter.
    centres = [[t, (rx + side / 2) / ww, (ry + side / 2) / wh] for t, rx, ry in pts]
    smooth = []
    for i, pt in enumerate(centres):
        win = centres[max(0, i - 1):i + 2]
        smooth.append([pt[0], sum(w[1] for w in win) / len(win), sum(w[2] for w in win) / len(win)])
    stored = simplify([[r4(t), round(cx, 5), round(cy, 5)] for t, cx, cy in smooth])
    return {
        "clip": c["id"], "asset": c["asset"], "at_source": r4(ts), "start": {"x": x, "y": y, "size": size},
        "points": stored, "found": round(found, 3), "frames_followed": len(pts),
    }


def points_between(points: list[list[float]], t0: float, t1: float, tmap) -> list[list[float]]:
    """Points of a tracker mapped into another time frame by tmap(source_time), clipped to [t0, t1].
    Edge points are interpolated so the path covers the whole range. Returns [[t, x, y], ...]."""
    mapped = [(tmap(p[0]), p[1], p[2]) for p in points]
    mapped.sort()
    if not mapped or mapped[-1][0] < t0 or mapped[0][0] > t1:
        return []

    def at(t):
        if t <= mapped[0][0]:
            return mapped[0][1], mapped[0][2]
        for (ta, xa, ya), (tb, xb, yb) in zip(mapped, mapped[1:]):
            if t <= tb:
                u = (t - ta) / (tb - ta) if tb != ta else 0
                return xa + (xb - xa) * u, ya + (yb - ya) * u
        return mapped[-1][1], mapped[-1][2]

    inside = [m for m in mapped if t0 < m[0] < t1]
    out = [(t0, *at(t0))] + inside + [(t1, *at(t1))]
    return [[r4(t), x, y] for t, x, y in out]
