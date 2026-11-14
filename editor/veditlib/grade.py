"""Color grading: clip color settings and the FFmpeg filters they become.

A clip's `color` dict may hold any of the fields below plus `look` (a named preset) and `lut`
(a .cube file). Explicit fields override the look. brightness, contrast, saturation and
gamma can also be keyframed (see keys.py).
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from . import keys
from .project import EditError

# field -> (default, lowest, highest)
FIELDS = {
    "brightness": (0.0, -1.0, 1.0),    # added to every pixel
    "contrast": (1.0, 0.0, 3.0),
    "saturation": (1.0, 0.0, 3.0),     # 0 is black and white
    "gamma": (1.0, 0.1, 3.0),          # above 1 lifts the midtones
    "temperature": (0.0, -1.0, 1.0),   # negative cooler (blue), positive warmer (orange)
    "tint": (0.0, -1.0, 1.0),          # negative green, positive magenta
    "vignette": (0.0, 0.0, 1.0),       # darkened corners
    "sharpen": (0.0, 0.0, 1.0),
    "blur": (0.0, 0.0, 50.0),          # gaussian sigma in pixels
}

# Named looks: starting values for the fields above, plus optional split-tone (colorbalance) amounts.
LOOKS = {
    "warm": {"temperature": 0.35, "saturation": 1.05},
    "cool": {"temperature": -0.35},
    "vivid": {"saturation": 1.35, "contrast": 1.1},
    "faded": {"contrast": 0.85, "brightness": 0.04, "saturation": 0.85, "gamma": 1.05},
    "black-white": {"saturation": 0.0, "contrast": 1.1},
    "noir": {"saturation": 0.0, "contrast": 1.35, "brightness": -0.05, "vignette": 0.5},
    "vintage": {"saturation": 0.8, "contrast": 0.92, "temperature": 0.25, "vignette": 0.3},
    "teal-orange": {"contrast": 1.08, "balance": {"rs": -0.12, "bs": 0.14, "rh": 0.14, "bh": -0.12}},
    "cinematic": {"contrast": 1.15, "saturation": 0.9, "vignette": 0.35,
                  "balance": {"rs": -0.06, "bs": 0.08, "rh": 0.07, "bh": -0.06}},
}
LOOK_NAMES = tuple(LOOKS)


def validate(color: dict) -> dict:
    """Check a color dict from the user; returns a clean copy."""
    if not isinstance(color, dict):
        raise EditError("color must be an object of fields")
    out = {}
    for k, v in color.items():
        if k == "look":
            if v not in LOOKS:
                raise EditError(f"unknown look '{v}'. Looks: {', '.join(LOOK_NAMES)}")
            out[k] = v
        elif k == "lut":
            if not str(v).strip():
                raise EditError("lut needs a path to a .cube file")
            out[k] = str(v)
        elif k in FIELDS:
            _, lo, hi = FIELDS[k]
            try:
                x = float(v)
            except (TypeError, ValueError):
                raise EditError(f"{k} must be a number")
            if not lo <= x <= hi:
                raise EditError(f"{k} must be between {lo:g} and {hi:g} (got {x:g})")
            out[k] = x
        else:
            raise EditError(f"unknown color field '{k}'. Fields: {', '.join(FIELDS)}, look, lut")
    return out


def effective(color: dict | None) -> dict:
    """Defaults, then the look, then the clip's own fields."""
    eff = {k: v[0] for k, v in FIELDS.items()}
    eff["balance"] = {}
    color = color or {}
    if color.get("look"):
        eff.update(LOOKS[color["look"]])
    for k, v in color.items():
        if k in FIELDS:
            eff[k] = v
    eff["lut"] = color.get("lut")
    return eff


def _esc(path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:")


def is_neutral(color: dict | None, clip_keys: dict | None) -> bool:
    e = effective(color)
    if any(k in (clip_keys or {}) for k in keys.COLOR_PROPS):
        return False
    return (not e["lut"] and not e["balance"]
            and all(abs(e[k] - FIELDS[k][0]) < 1e-9 for k in FIELDS))


def filters(color: dict | None, clip_keys: dict | None, project_dir: Path) -> list[str]:
    """FFmpeg filters for a clip's grade, in a sensible order. Time is clip-local (`t`)."""
    clip_keys = clip_keys or {}
    e = effective(color)
    out: list[str] = []

    # 1. exposure and contrast (the only parts that can be keyframed)
    eq_parts, animated = [], False
    for name, opt in (("brightness", "brightness"), ("contrast", "contrast"),
                      ("saturation", "saturation"), ("gamma", "gamma")):
        if name in clip_keys:
            eq_parts.append(f"{opt}='{keys.expr(clip_keys[name], 't')}'")
            animated = True
        elif abs(e[name] - FIELDS[name][0]) > 1e-9:
            eq_parts.append(f"{opt}={keys.num(e[name])}")
    if eq_parts:
        out.append("eq=" + ":".join(eq_parts) + (":eval=frame" if animated else ""))

    # 2. white balance and split-tone
    bal = dict(e["balance"])
    t, tint = e["temperature"], e["tint"]
    if t:
        bal["rm"] = bal.get("rm", 0) + 0.20 * t
        bal["rh"] = bal.get("rh", 0) + 0.15 * t
        bal["bm"] = bal.get("bm", 0) - 0.20 * t
        bal["bh"] = bal.get("bh", 0) - 0.15 * t
    if tint:
        bal["gm"] = bal.get("gm", 0) - 0.15 * tint
        bal["gh"] = bal.get("gh", 0) - 0.10 * tint
    if bal:
        out.append("colorbalance=" + ":".join(f"{k}={keys.num(max(-1.0, min(1.0, v)))}" for k, v in sorted(bal.items())))

    # 3. look-up table
    if e["lut"]:
        p = Path(e["lut"]).expanduser()
        p = p if p.is_absolute() else project_dir / p
        out.append(f"lut3d=file='{_esc(p)}'")

    # 4. finishing
    if e["vignette"] > 0:
        # FFmpeg's lens angle: the larger it is, the stronger the darkening (0.31 rad is faint, 0.63 clear, 1.1 heavy)
        out.append(f"vignette=angle={keys.num(max(0.05, math.pi / 2 * 0.7 * e['vignette']))}")
    if e["sharpen"] > 0:
        out.append(f"unsharp=lx=5:ly=5:la={keys.num(e['sharpen'] * 1.5)}")
    if e["blur"] > 0:
        out.append(f"gblur=sigma={keys.num(e['blur'])}")
    return out


COLOR_RE = re.compile(r"(0x[0-9a-fA-F]{6}|[a-z]{3,20})")


def chromakey(spec: dict | None) -> str | None:
    """Filter that removes a background color (green screen)."""
    if not spec:
        return None
    color = str(spec.get("color", "0x00ff00"))
    if not COLOR_RE.fullmatch(color):
        raise EditError(f"bad chroma key color '{color}'; use a name like green or 0x00ff00")
    sim = float(spec.get("similarity", 0.15))
    blend = float(spec.get("blend", 0.1))
    return f"chromakey=color={color}:similarity={keys.num(sim)}:blend={keys.num(blend)}"
