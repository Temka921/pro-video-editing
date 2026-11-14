"""How a clip's output time maps onto its source file: constant speed, speed ramps, reverse and freeze.

A clip shows `dur` seconds on the timeline. It reads a window of its source, [src_in, src_in + span],
where span is the integral of its speed over the clip. A ramped clip has `keys.speed`, a list of
keyframes (see keys.py) giving the speed over output time. A reversed clip plays that window backwards.
A frozen clip (`hold`) shows the single source frame at src_in for its whole length.
"""
from __future__ import annotations

from . import keys

STEPS_PER_SECOND = 60


def speed_points(c: dict):
    return (c.get("keys") or {}).get("speed")


def speed_at(c: dict, t: float) -> float:
    pts = speed_points(c)
    return keys.value_at(pts, t) if pts else float(c.get("speed", 1.0))


def offset(c: dict, t: float) -> float:
    """Seconds of source passed by output time t (0 at the clip's start). Holds the edge speed outside the clip."""
    if c.get("hold"):
        return 0.0
    pts = speed_points(c)
    if not pts:
        return t * float(c.get("speed", 1.0))
    if t < 0:
        return t * speed_at(c, 0.0)
    n = max(4, int(t * STEPS_PER_SECOND) + 1)
    h = t / n
    total = 0.0
    for i in range(n):  # midpoint rule: the curves are smooth and n is large
        total += speed_at(c, (i + 0.5) * h) * h
    return total


def source_span(c: dict) -> float:
    return offset(c, c["dur"])


def is_ramped(c: dict) -> bool:
    return bool(speed_points(c))


def source_time_at(c: dict, local: float) -> float:
    """The moment in the source file shown at clip time `local`."""
    lo = c.get("src_in", 0.0)
    if c.get("hold"):
        return lo
    if c.get("reverse"):
        return lo + source_span(c) - offset(c, local)
    return lo + offset(c, local)


def split_windows(c: dict, first_dur: float) -> tuple[float, float]:
    """src_in for the two halves when the clip is cut `first_dur` seconds in."""
    lo = c["src_in"]
    off = offset(c, first_dur)
    if c.get("reverse"):  # the first half plays the END of the window, backwards
        return lo + source_span(c) - off, lo
    return lo, lo + off


def trimmed_src_in(c: dict, head_cut: float, new_dur: float) -> float:
    """src_in after removing `head_cut` seconds from the head (negative extends it) and keeping `new_dur` seconds."""
    lo = c["src_in"]
    if c.get("hold"):
        return lo
    new_span = offset(c, head_cut + new_dur) - offset(c, head_cut)
    if not c.get("reverse"):
        return lo + offset(c, head_cut)
    hi = lo + source_span(c)
    return hi - offset(c, head_cut) - new_span


def remap_points(c: dict) -> list[list]:
    """(source seconds, output seconds) pairs describing a ramp, for FFmpeg's setpts."""
    n = max(10, min(100, round(c["dur"] * 5)))
    pts = []
    for k in range(n + 1):
        t = c["dur"] * k / n
        pts.append([round(offset(c, t), 5), round(t, 5), "linear"])
    return pts


def remap_expr(c: dict) -> str:
    """setpts expression (in seconds, divide by TB) turning source time into output time for a ramped clip."""
    return keys.expr(remap_points(c), "PTS*TB")
