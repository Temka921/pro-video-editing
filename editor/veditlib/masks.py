"""Masks: limit a clip to, or apply an effect inside (or outside) a rectangle or ellipse.

A mask is described in fractions of the PICTURE (not the canvas), so it stays on the same part of the
image when the clip is zoomed, moved or letterboxed: x and y are the centre (0 to 1 across and down), w and h
are the size (1 is the whole picture). Its x, y, w and h can be keyframed or made to follow a tracker.

effect: none (the clip is only visible inside the mask), blur, pixelate, darken, brighten, desaturate.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import keys
from .project import EditError

SHAPES = ("rect", "ellipse")
EFFECTS = ("none", "blur", "pixelate", "darken", "brighten", "desaturate")
MAX_MASKS = 4
MASK_RES = 320  # the shape is drawn at this width and stretched, which also softens its edge slightly


@dataclass
class Layout:
    pw: int        # the picture's size after fitting
    ph: int
    lw: int        # the layer the picture sits in (the canvas, or just the picture for a box)
    lh: int
    px0: float     # where the picture starts inside the layer (negative when it overflows, as with cover)
    py0: float
    box: bool
    bw: int = 0    # picture-in-picture box size and position, in canvas pixels
    bh: int = 0
    bx: int = 0
    by: int = 0


def picture_size(asset: dict, c: dict) -> tuple[int, int]:
    """Width and height of the source picture after cropping, matching the filter's own rounding."""
    sw, sh = int(asset["width"]), int(asset["height"])
    crop = c.get("crop")
    if crop:
        l, t, r, b = crop
        sw, sh = max(2, int(sw * (1 - l - r) / 2) * 2), max(2, int(sh * (1 - t - b) / 2) * 2)
    return sw, sh


def layout(asset: dict, c: dict, W: int, H: int) -> Layout:
    sw, sh = picture_size(asset, c)
    box = c.get("box")
    if box:
        bw, bh = max(2, int(box[2] * W) // 2 * 2), max(2, int(box[3] * H) // 2 * 2)
        s = min(bw / sw, bh / sh)
        pw, ph = max(2, int(round(sw * s / 2)) * 2), max(2, int(round(sh * s / 2)) * 2)
        return Layout(pw, ph, pw, ph, 0.0, 0.0, True, bw, bh, int(box[0] * W), int(box[1] * H))
    cover = c.get("fit", "contain") == "cover"
    s = max(W / sw, H / sh) if cover else min(W / sw, H / sh)
    pw, ph = max(2, int(round(sw * s / 2)) * 2), max(2, int(round(sh * s / 2)) * 2)
    if not cover:
        pw, ph = min(pw, W), min(ph, H)
    return Layout(pw, ph, W, H, (W - pw) / 2, (H - ph) / 2, False)


def validate(m: dict) -> dict:
    """Check a mask definition from the user; returns a clean copy."""
    out = dict(m)
    if out.get("shape", "ellipse") not in SHAPES:
        raise EditError(f"mask shape must be one of: {', '.join(SHAPES)}")
    out["shape"] = out.get("shape", "ellipse")
    if out.get("effect", "none") not in EFFECTS:
        raise EditError(f"mask effect must be one of: {', '.join(EFFECTS)}")
    out["effect"] = out.get("effect", "none")
    for k in ("x", "y", "w", "h", "feather", "amount"):
        try:
            out[k] = float(out.get(k, {"x": 0.5, "y": 0.5, "w": 0.3, "h": 0.3, "feather": 0.1, "amount": 0.8}[k]))
        except (TypeError, ValueError):
            raise EditError(f"mask {k} must be a number")
    for k, lo, hi in (("x", -1, 2), ("y", -1, 2), ("w", 0.01, 3), ("h", 0.01, 3), ("feather", 0, 1), ("amount", 0, 1)):
        if not lo <= out[k] <= hi:
            raise EditError(f"mask {k} must be between {lo:g} and {hi:g} (got {out[k]:g})")
    out["invert"] = bool(out.get("invert", False))
    return out


def _num(clip_keys: dict, m: dict, prop: str) -> str:
    pts = clip_keys.get(f"{m['id']}.{prop}")
    return keys.expr(pts, "T") if pts else keys.num(m[prop])


def shape_expr(m: dict, lay: Layout, clip_keys: dict) -> str:
    """geq expression (0 to 255) for the mask at pixel X,Y of the mask image, at clip time T."""
    u, v = "((X+0.5)/W)", "((Y+0.5)/H)"
    cx = f"(({keys.num(lay.px0)}+({_num(clip_keys, m, 'x')})*{lay.pw})/{lay.lw})"
    cy = f"(({keys.num(lay.py0)}+({_num(clip_keys, m, 'y')})*{lay.ph})/{lay.lh})"
    rx = f"(({_num(clip_keys, m, 'w')})*{lay.pw}/{lay.lw}/2)"
    ry = f"(({_num(clip_keys, m, 'h')})*{lay.ph}/{lay.lh}/2)"
    nx, ny = f"(({u}-{cx})/{rx})", f"(({v}-{cy})/{ry})"
    d = f"hypot({nx},{ny})" if m["shape"] == "ellipse" else f"max(abs({nx}),abs({ny}))"
    s = max(0.02, m["feather"])
    inside = f"clip((1-{d})/{keys.num(2 * s)}+0.5,0,1)"
    return f"255*(1-{inside})" if m["invert"] else f"255*{inside}"


def source_chain(m: dict, lay: Layout, clip_keys: dict, fps: float, dur: float, planes: bool = False) -> str:
    """Filter chain (starts with a source) that makes the mask as a video the size of the layer.
    Gray by default (for alpha masks); with planes=True it is yuva420p with the weights written into every plane,
    which is what blending an effect into a colour picture needs (colour planes are masked too)."""
    mw = min(MASK_RES, lay.lw)
    mh = max(2, round(mw * lay.lh / lay.lw))
    e = shape_expr(m, lay, clip_keys)
    head = f"color=c=black:s={mw}x{mh}:r={fps:g}:d={keys.num(dur + 0.5)},"
    if planes:
        return (head + f"format=yuva420p,geq=lum='{e}':cb='{e}':cr='{e}':a='{e}',"
                f"scale={lay.lw}:{lay.lh}:flags=bilinear,format=yuva420p")
    return head + f"format=gray,geq=lum='{e}',scale={lay.lw}:{lay.lh}:flags=bilinear,format=gray"


def effect_filters(m: dict, lay: Layout) -> list[str]:
    """Filters applied to the copy of the picture that is blended in through the mask."""
    a, e = m["amount"], m["effect"]
    if e == "blur":
        return [f"gblur=sigma={keys.num(max(1.0, a * 40 * lay.lw / 1920))}"]
    if e == "pixelate":
        block = max(4, round((0.01 + 0.07 * a) * lay.lw))
        return [f"scale={max(2, lay.lw // block)}:{max(2, lay.lh // block)}", f"scale={lay.lw}:{lay.lh}:flags=neighbor"]
    if e == "darken":
        return [f"eq=brightness={keys.num(-0.8 * a)}"]
    if e == "brighten":
        return [f"eq=brightness={keys.num(0.5 * a)}"]
    if e == "desaturate":
        return [f"eq=saturation={keys.num(1 - a)}"]
    return []
