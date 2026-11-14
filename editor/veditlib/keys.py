"""Keyframes: values that change over a clip's time, compiled to FFmpeg expressions.

A clip stores `keys`: {property: [[seconds, value, ease], ...]}. Times are seconds from the
START OF THE CLIP (so keys travel with the clip when it moves, and are kept aligned when it
is split or trimmed). Between two keys the value eases from the first to the second using the
first key's ease. Before the first key and after the last, the value holds.

ease: linear | in (slow start) | out (slow end) | inout (smooth both ends) | hold (jump at the next key)
"""
from __future__ import annotations

import re

from .project import EditError

# property -> (default, lowest, highest, what it is)
PROPS = {
    "speed": (1.0, 0.1, 8.0, "playback speed; keyframes make a speed ramp (the clip's own sound is muted)"),
    "scale": (1.0, 0.05, 8.0, "zoom; 1 fits the frame"),
    "x": (0.0, -2.0, 2.0, "horizontal offset, fraction of the frame width"),
    "y": (0.0, -2.0, 2.0, "vertical offset, fraction of the frame height"),
    "rotation": (0.0, -360.0, 360.0, "degrees, clockwise"),
    "opacity": (1.0, 0.0, 1.0, "0 transparent to 1 solid"),
    "brightness": (0.0, -1.0, 1.0, "color: brightness"),
    "contrast": (1.0, 0.0, 3.0, "color: contrast"),
    "saturation": (1.0, 0.0, 3.0, "color: saturation"),
    "gamma": (1.0, 0.1, 3.0, "color: gamma"),
    "volume_db": (0.0, -60.0, 24.0, "sound level in dB"),
}
COLOR_PROPS = {"brightness", "contrast", "saturation", "gamma"}
EASES = ("linear", "in", "out", "inout", "hold")
MAX_POINTS = 400  # expressions are balanced trees, so even long tracked paths stay shallow

# A mask's position and size can be keyframed too, as "<mask id>.x", ".y", ".w", ".h" (for example "m1.x").
MASK_PROP = re.compile(r"(m\d+)\.(x|y|w|h)")
MASK_RANGES = {"x": (0.5, -1.0, 2.0), "y": (0.5, -1.0, 2.0), "w": (0.3, 0.01, 3.0), "h": (0.3, 0.01, 3.0)}


def info(prop: str):
    """(default, lowest, highest, description) for a property, or None if it is not keyframeable."""
    if prop in PROPS:
        return PROPS[prop]
    m = MASK_PROP.fullmatch(prop)
    if m:
        d, lo, hi = MASK_RANGES[m.group(2)]
        return d, lo, hi, f"mask {m.group(1)} {m.group(2)}"
    return None


def num(x: float) -> str:
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def applicable(clip: dict, track_kind: str, has_sound: bool, is_video_file: bool = False) -> set[str]:
    """Which properties make sense on this clip."""
    if clip.get("type") == "text":
        return {"x", "y", "opacity"}
    props = set()
    if track_kind == "video":
        props |= {"scale", "x", "y", "rotation", "opacity"} | COLOR_PROPS
        if is_video_file and not clip.get("hold"):
            props.add("speed")
        props |= {f"{m['id']}.{k}" for m in clip.get("masks") or [] for k in "xywh"}
    if has_sound:
        props.add("volume_db")
    return props


def base_value(clip: dict, prop: str) -> float:
    """The clip's steady value for a property (what it is when it has no keys)."""
    m = MASK_PROP.fullmatch(prop)
    if m:
        mask = next((x for x in clip.get("masks") or [] if x["id"] == m.group(1)), None)
        return float((mask or {}).get(m.group(2), MASK_RANGES[m.group(2)][0]))
    if prop in COLOR_PROPS:
        return float((clip.get("color") or {}).get(prop, PROPS[prop][0]))
    return float(clip.get(prop, PROPS[prop][0]))


def clean(prop: str, points) -> list[list]:
    """Validate and sort points: [[t, value], [t, value, ease], ...]."""
    spec = info(prop)
    if spec is None:
        raise EditError(f"'{prop}' cannot be keyframed. Choose from: {', '.join(PROPS)}, or a mask's m1.x, m1.y, m1.w, m1.h")
    default, lo, hi, _ = spec
    if not isinstance(points, (list, tuple)) or not points:
        raise EditError(f"keys for {prop} need a list like [[0, 1.0], [2, 1.3, \"inout\"]]")
    if len(points) > MAX_POINTS:
        raise EditError(f"at most {MAX_POINTS} keyframes per property")
    out: dict[float, list] = {}
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) not in (2, 3):
            raise EditError(f"a keyframe is [seconds, value] or [seconds, value, ease], got {p!r}")
        try:
            t, v = round(float(p[0]), 4), float(p[1])
        except (TypeError, ValueError):
            raise EditError(f"keyframe {p!r}: time and value must be numbers")
        ease = p[2] if len(p) == 3 else "linear"
        if t < 0:
            raise EditError(f"keyframe time {t:g}s is before the start of the clip")
        if not lo <= v <= hi:
            raise EditError(f"{prop} must be between {lo:g} and {hi:g} (got {v:g})")
        if ease not in EASES:
            raise EditError(f"ease must be one of: {', '.join(EASES)}")
        out[t] = [t, round(v, 6), ease]
    return [out[t] for t in sorted(out)]


def shape(u: float, ease: str) -> float:
    u = max(0.0, min(1.0, u))
    return {"linear": u, "in": u * u, "out": 1 - (1 - u) ** 2, "inout": u * u * (3 - 2 * u), "hold": 0.0}[ease]


def value_at(points: list[list], t: float) -> float:
    """The value at clip time t (the same curve FFmpeg will draw)."""
    if t <= points[0][0]:
        return points[0][1]
    for a, b in zip(points, points[1:]):
        if t < b[0]:
            return a[1] + (b[1] - a[1]) * shape((t - a[0]) / (b[0] - a[0]), a[2] if len(a) > 2 else "linear")
    return points[-1][1]


def expr(points: list[list], T: str) -> str:
    """An FFmpeg expression for the curve, where T is the expression for clip time in seconds."""
    pts = points
    if len(pts) == 1:
        return num(pts[0][1])

    def segment(i: int) -> str:
        t0, v0, e = pts[i][0], pts[i][1], (pts[i][2] if len(pts[i]) > 2 else "linear")
        t1, v1 = pts[i + 1][0], pts[i + 1][1]
        u = f"(({T})-{num(t0)})/{num(t1 - t0)}"
        sh = {
            "linear": u,
            "in": f"pow({u},2)",
            "out": f"(1-pow(1-{u},2))",
            "inout": f"({u}*{u}*(3-2*{u}))",
            "hold": "0",
        }[e]
        return num(v0) if e == "hold" else f"{num(v0)}+({num(v1 - v0)})*{sh}"

    def tree(lo: int, hi: int) -> str:
        """Segments lo..hi-1 as a balanced tree of if()s: FFmpeg's parser fails past about 100 nested levels."""
        if hi - lo == 1:
            return segment(lo)
        mid = (lo + hi) // 2
        return f"if(lt({T},{num(pts[mid][0])}),{tree(lo, mid)},{tree(mid, hi)})"

    return (f"if(lt({T},{num(pts[0][0])}),{num(pts[0][1])},"
            f"if(lt({T},{num(pts[-1][0])}),{tree(0, len(pts) - 1)},{num(pts[-1][1])}))")


def ease_at(points: list[list], t: float) -> str:
    """The ease of the segment that contains time t."""
    for a, b in zip(points, points[1:]):
        if a[0] <= t < b[0]:
            return a[2] if len(a) > 2 else "linear"
    return "linear"


def split_points(points: list[list], at: float) -> tuple[list[list], list[list]]:
    """Cut a curve at clip time `at`. Both halves keep the exact value at the cut."""
    v = value_at(points, at)
    first = [p for p in points if p[0] < at - 1e-6]
    ease_here = ease_at(points, at)  # the ease of the segment being cut carries on in the second half
    first.append([round(at, 4), round(v, 6), "linear"])
    second = [[0.0, round(v, 6), ease_here]]
    second += [[round(p[0] - at, 4), p[1], p[2] if len(p) > 2 else "linear"] for p in points if p[0] > at + 1e-6]
    return first, second


def shift_points(points: list[list], delta: float) -> list[list]:
    """Move every key later by delta seconds (negative = earlier), keeping the value at time 0."""
    moved = [[round(p[0] + delta, 4), p[1], p[2] if len(p) > 2 else "linear"] for p in points]
    if delta >= 0:
        return moved
    v0 = value_at(points, -delta)
    kept = [p for p in moved if p[0] > 1e-6]
    return [[0.0, round(v0, 6), ease_at(points, -delta)]] + kept
