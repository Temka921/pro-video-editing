"""Every edit the editor can make, as plain functions on a Project.

The CLI, the batch runner and the web UI all call these, so the AI and the human have
exactly the same powers. Each op takes keyword arguments, changes the project in
place and returns a small JSON-able result (usually the id it created).
Times are seconds on the timeline; everything is rounded to 0.1 ms.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import copy
import re

from . import grade, keys, masks, presets, stabilize, timemap, tracking
from .project import SCRIPTS, EditError, Project, probe_media, r4

OPS: dict = {}
MIN_CLIP = 0.04

# Fields `set` may change, with how to read the value.
def _num(lo=None, hi=None):
    def conv(v):
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise EditError(f"expected a number, got {v!r}")
        if lo is not None and x < lo or hi is not None and x > hi:
            raise EditError(f"{x:g} is outside the allowed range {lo} to {hi}")
        return x
    return conv


def _bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise EditError(f"expected true or false, got {v!r}")


def _choice(*opts):
    def conv(v):
        if v not in opts:
            raise EditError(f"must be one of: {', '.join(opts)}")
        return v
    return conv


def _preset(v):
    if v in (None, "", "none"):
        return None
    return presets.get(v)["name"]


def _box(v):
    if v in (None, "", "none"):
        return None
    if isinstance(v, str):
        v = [float(x) for x in v.split(",")]
    if len(v) != 4 or not all(0 <= float(x) <= 1 for x in v):
        raise EditError("box is x,y,w,h as fractions of the frame, e.g. 0.65,0.05,0.3,0.3")
    return [float(x) for x in v]


def _text(v):
    if not str(v).strip():
        raise EditError("text cannot be empty")
    return str(v)


def _flip(v):
    if v in (None, "", "none"):
        return None
    if v not in ("h", "v", "hv"):
        raise EditError("flip must be h, v, hv or none")
    return v


def _crop(v):
    """Trim the picture's edges: left,top,right,bottom as fractions of the frame."""
    if v in (None, "", "none"):
        return None
    if isinstance(v, str):
        v = [float(x) for x in v.split(",")]
    if len(v) != 4 or not all(0 <= float(x) < 0.9 for x in v) or v[0] + v[2] > 0.9 or v[1] + v[3] > 0.9:
        raise EditError("crop is left,top,right,bottom as fractions of the frame, e.g. 0.1,0,0.1,0 (at most 0.9 in total per axis)")
    return [float(x) for x in v]


def _chromakey(v):
    """Remove a background colour: 'green' or 'color,similarity,blend' or an object."""
    if v in (None, "", "none"):
        return None
    if isinstance(v, str):
        parts = v.split(",")
        v = {"color": parts[0]}
        if len(parts) > 1:
            v["similarity"] = float(parts[1])
        if len(parts) > 2:
            v["blend"] = float(parts[2])
    spec = {"color": str(v.get("color", "0x00ff00")), "similarity": float(v.get("similarity", 0.15)),
            "blend": float(v.get("blend", 0.1))}
    grade.chromakey(spec)  # validates the colour
    if not 0.01 <= spec["similarity"] <= 1 or not 0 <= spec["blend"] <= 1:
        raise EditError("chroma key similarity is 0.01 to 1 and blend 0 to 1")
    return spec


FIELDS = {
    "start": _num(0), "dur": _num(MIN_CLIP),
    "volume_db": _num(-60, 24), "fade_in": _num(0), "fade_out": _num(0),
    "afade_in": _num(0), "afade_out": _num(0),  # sound-only fades (the picture keeps fade_in/fade_out)
    "speed": _num(0.25, 4), "mute": _bool, "preset": _preset,
    "opacity": _num(0, 1), "fit": _choice("contain", "cover"), "box": _box,
    "scale": _num(0.05, 8), "x": _num(-2, 2), "y": _num(-2, 2), "rotation": _num(-360, 360),
    "flip": _flip, "crop": _crop, "chromakey": _chromakey, "reverse": _bool,
    "text": _text, "size": _num(8, 600), "color": str, "pos": _choice("top", "center", "bottom"),
    "bg": _bool, "note": str,
}
TEXT_ONLY = {"text", "size", "color", "pos", "bg"}
PICTURE_ONLY = {"fit", "box", "scale", "rotation", "flip", "crop", "chromakey"}  # not for text clips
PICTURE_OR_TEXT = {"opacity", "x", "y"}

TRANSITIONS = ("dissolve", "dip", "slide-left", "slide-right", "slide-up", "slide-down",
               "push-left", "push-right", "zoom")
PRESETS = {"youtube": (1920, 1080, 30), "hd": (1280, 720, 30), "4k": (3840, 2160, 30),
           "vertical": (1080, 1920, 30), "square": (1080, 1080, 30)}


def op(fn):
    OPS[fn.__name__.lstrip("_")] = fn
    return fn


# --------------------------------------------------------------------------
# Settings, tracks, assets
# --------------------------------------------------------------------------
@op
def set_settings(p: Project, width=None, height=None, fps=None, bg=None, preset=None, master_lufs=None):
    s = p.settings
    if preset is not None:
        if preset not in PRESETS:
            raise EditError(f"unknown preset '{preset}'. Presets: {', '.join(PRESETS)} (vertical suits Reels, TikTok and Shorts)")
        s["width"], s["height"], s["fps"] = PRESETS[preset]
    if master_lufs is not None:
        if str(master_lufs).lower() in ("off", "none"):
            s.pop("master_lufs", None)
        else:
            v = float(master_lufs)
            if not -40 <= v <= -5:
                raise EditError("master_lufs must be between -40 and -5 (YouTube about -14, broadcast -23), or 'off'")
            s["master_lufs"] = v
    for key, val, conv in (("width", width, int), ("height", height, int), ("fps", fps, float), ("bg", bg, str)):
        if val is not None:
            v = conv(val)
            if key in ("width", "height") and (v < 16 or v % 2):
                raise EditError(f"{key} must be an even number of at least 16")
            if key == "fps" and not 1 <= v <= 120:
                raise EditError("fps must be between 1 and 120")
            s[key] = int(v) if key == "fps" and float(v).is_integer() else v
    return dict(s)


@op
def add_track(p: Project, kind, id=None, duck=None):
    if kind not in ("video", "audio"):
        raise EditError("kind must be 'video' or 'audio'")
    tid = id or p.new_id("V" if kind == "video" else "A", (t["id"] for t in p.tracks))
    if any(t["id"] == tid for t in p.tracks):
        raise EditError(f"track '{tid}' already exists")
    t = {"id": tid, "kind": kind}
    if duck is not None:
        t["duck"] = _bool(duck)
    # Video tracks stack bottom to top in list order; keep audio tracks after the video ones.
    if kind == "video":
        i = max((k for k, x in enumerate(p.tracks) if x["kind"] == "video"), default=-1) + 1
        p.tracks.insert(i, t)
    else:
        p.tracks.append(t)
    return {"track": tid}


@op
def remove_track(p: Project, id):
    t = p.track(id)
    if any(c["track"] == id for c in p.clips):
        raise EditError(f"track '{id}' still has clips; remove or move them first")
    p.tracks.remove(t)
    return {"removed": id}


@op
def set_track(p: Project, id, duck=None, mute=None, hide=None):
    t = p.track(id)
    for name, val in (("duck", duck), ("mute", mute), ("hide", hide)):
        if val is None:
            continue
        if name == "duck" and t["kind"] != "audio":
            raise EditError("only audio tracks can duck")
        if name == "hide" and t["kind"] != "video":
            raise EditError("only video tracks can be hidden (use mute for sound)")
        if _bool(val):
            t[name] = True
        else:
            t.pop(name, None) if name != "duck" else t.__setitem__("duck", False)
    return t


@op
def import_asset(p: Project, path, copy=False):
    src = Path(path).expanduser()
    if not src.is_file():
        raise EditError(f"file not found: {src}")
    src = src.resolve()
    if _bool(copy):
        media = p.dir / "media"
        media.mkdir(exist_ok=True)
        dest = media / src.name
        if dest.exists() and dest.resolve() != src:
            stem, suffix, n = dest.stem, dest.suffix, 2
            while dest.exists():
                dest = media / f"{stem}-{n}{suffix}"
                n += 1
        if not dest.exists():
            shutil.copyfile(src, dest)
        src = dest.resolve()
    try:
        stored = str(src.relative_to(p.dir.resolve())).replace("\\", "/")
    except ValueError:
        stored = str(src)
    for aid, a in p.assets.items():
        if a["path"] == stored:
            return {"asset": aid, **a, "existing": True}
    info = probe_media(src)
    aid = p.new_id("a", p.assets)
    p.assets[aid] = {"path": stored, "name": src.name, **info}
    return {"asset": aid, **p.assets[aid]}


# --------------------------------------------------------------------------
# Clips
# --------------------------------------------------------------------------
def _new_clip_id(p: Project) -> str:
    return p.new_id("c", (c["id"] for c in p.clips))


def _default_track(p: Project, kind: str, prefer: str | None = None) -> str:
    tracks = [t["id"] for t in p.tracks if t["kind"] == kind]
    if not tracks:
        raise EditError(f"the project has no {kind} track; add one with add_track")
    return prefer if prefer in tracks else tracks[0]


def _check_asset_range(p: Project, c: dict) -> None:
    a = p.assets.get(c.get("asset", ""))
    if not a or a["kind"] == "image":
        return
    span = timemap.source_span(c)
    used = c["src_in"] + span
    if used > a["duration"] + 0.01:
        how = "a speed ramp" if timemap.is_ramped(c) else f"speed {c['speed']:g}"
        raise EditError(
            f"clip needs {used:.2f}s of '{a['name']}' but it is only {a['duration']:.2f}s long "
            f"(src_in {c['src_in']:.2f} + {span:.2f}s of source for {c['dur']:.2f}s at {how})"
        )


@op
def add(p: Project, asset, track=None, at=None, src_in=0, dur=None, speed=1, volume_db=0,
        mute=False, fade_in=0, fade_out=0, preset=None, opacity=1, fit="contain", box=None, note=""):
    a = p.assets.get(asset)
    if not a:
        raise EditError(f"no asset '{asset}'. Import it first: vedit import FILE")
    kind = a["kind"]
    if track is None:
        track = _default_track(p, "audio" if kind == "audio" else "video")
    t = p.track(track)
    if t["kind"] == "video" and kind == "audio":
        raise EditError(f"'{a['name']}' is audio only; put it on an audio track")
    c = {
        "id": _new_clip_id(p), "track": track, "asset": asset,
        "start": r4(p.track_end(track) if at is None else float(at)),
        "src_in": r4(src_in), "speed": float(speed), "volume_db": float(volume_db),
        "fade_in": r4(fade_in), "fade_out": r4(fade_out),
    }
    if c["start"] < 0:
        raise EditError("start cannot be negative")
    if not 0.25 <= c["speed"] <= 4:
        raise EditError("speed must be between 0.25 and 4")
    if dur is None:
        if kind == "image":
            dur = 5.0
        else:
            dur = (a["duration"] - c["src_in"]) / c["speed"]
    c["dur"] = r4(dur)
    if c["dur"] < MIN_CLIP:
        raise EditError("the clip would be shorter than one frame; check src_in and dur")
    if _bool(mute):
        c["mute"] = True
    if kind != "audio" and t["kind"] == "video":
        c["opacity"], c["fit"] = _num(0, 1)(opacity), _choice("contain", "cover")(fit)
        if box:
            c["box"] = _box(box)
    if preset:
        c["preset"] = _preset(preset)
    if note:
        c["note"] = note
    _check_asset_range(p, c)
    p.clips.append(c)
    return {"clip": c["id"], "track": track, "start": c["start"], "dur": c["dur"], "end": r4(c["start"] + c["dur"])}


@op
def add_text(p: Project, text, at, dur, track=None, size=None, color="white", pos="bottom",
             bg=True, fade_in=0.15, fade_out=0.15, note=""):
    track = track or _default_track(p, "video", prefer=[t["id"] for t in p.tracks if t["kind"] == "video"][-1])
    if p.track(track)["kind"] != "video":
        raise EditError("text goes on a video track")
    c = {
        "id": _new_clip_id(p), "track": track, "type": "text", "text": _text(text),
        "start": r4(at), "dur": r4(dur), "size": float(size or max(16, p.settings["height"] // 14)),
        "color": color, "pos": _choice("top", "center", "bottom")(pos), "bg": _bool(bg),
        "fade_in": r4(fade_in), "fade_out": r4(fade_out),
    }
    if c["start"] < 0 or c["dur"] < MIN_CLIP:
        raise EditError("text needs a start of 0 or more and a duration of at least one frame")
    if note:
        c["note"] = note
    p.clips.append(c)
    return {"clip": c["id"], "track": track, "start": c["start"], "end": r4(c["start"] + c["dur"])}


@op
def add_sfx(p: Project, sound, at, sync="peak", gain_db=-9, track=None, duration=None,
            catalog=None, library=None, note=""):
    """Place a sound from the SFX catalog so that its peak (or onset, or start) lands on `at`."""
    sys.path.insert(0, str(SCRIPTS))
    import sfxlib as L

    try:
        cat = L.load_catalog(catalog)
    except SystemExit:  # sfxlib reports missing catalogs by exiting
        raise EditError("no sound library is set up yet. Run: python scripts/setup_library.py")
    s = cat["_by_id"].get(sound)
    if not s:
        raise EditError(f"unknown sound id '{sound}'. Find ids with: python scripts/sfx_search.py QUERY")
    if sync not in ("peak", "onset", "start"):
        raise EditError("sync must be peak, onset or start")
    at = float(at)
    off = {"peak": s["peak_s"], "onset": s["onset_s"], "start": 0.0}[sync]
    start, head = at - off, 0.0
    if start < 0:
        head, start = -start, 0.0
    avail = s["dur"] - head
    if avail <= 0.02:
        raise EditError(f"'{s['name']}' ends before its {sync} can land at {at:.2f}s")
    length = min(avail, float(duration)) if duration else avail

    sfx_dir = p.dir / "media" / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    dest = sfx_dir / f"{s['id']}.wav"
    if not dest.exists():
        try:
            libs = L.Libraries(cat, [library] if library else None)
            raw = libs.get(s["pack"]).extract(s.get("container", ""), s["path"], sfx_dir,
                                              f"{s['id']}_src" + Path(s["path"]).suffix)
        except SystemExit:
            raise EditError(f"the audio for pack '{s['pack']}' was not found; pass library=PATH or run scripts/setup_library.py")
        try:
            import subprocess
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", "-i", str(raw), "-ar", "48000",
                                "-c:a", "pcm_s24le", str(dest)], capture_output=True, text=True)
            if r.returncode:
                raise EditError(f"could not convert '{s['name']}': {r.stderr.strip()[:200]}")
        finally:
            raw.unlink(missing_ok=True)
    asset = import_asset(p, str(dest))
    t = track or _default_track(p, "audio", prefer="A2")
    if p.track(t)["kind"] != "audio":
        raise EditError("sound effects go on an audio track")
    c = {
        "id": _new_clip_id(p), "track": t, "asset": asset["asset"], "sfx": s["id"],
        "start": r4(start), "src_in": r4(head), "dur": r4(length), "speed": 1.0,
        "volume_db": float(gain_db), "fade_in": 0.002, "fade_out": 0.01,
        "note": note or f"{s['name']} ({sync} at {at:.2f}s)",
    }
    p.clips.append(c)
    return {"clip": c["id"], "track": t, "start": c["start"], "dur": c["dur"], "sound": s["name"]}


@op
def split(p: Project, clip, at):
    c = p.clip(clip)
    at = r4(at)
    end = c["start"] + c["dur"]
    if not c["start"] + MIN_CLIP <= at <= end - MIN_CLIP:
        raise EditError(f"split time {at:g}s must be inside clip {clip} ({c['start']:g}s to {end:g}s)")
    second = copy.deepcopy(c)  # deep: the halves must not share their color or keyframes
    second["id"] = _new_clip_id(p)
    first_dur = r4(at - c["start"])
    second["start"], second["dur"] = at, r4(end - at)
    if "asset" in c and not c.get("hold"):
        c["src_in"], second["src_in"] = (r4(v) for v in timemap.split_windows(c, first_dur))
    c["dur"] = first_dur
    # The first half keeps the original fade-in, the second keeps the original fade-out.
    c["fade_out"], second["fade_in"] = 0, 0
    c.pop("afade_out", None)
    second.pop("afade_in", None)
    second.pop("transition", None)  # a transition belongs to the start of the first half
    if c.get("keys"):  # each half keeps the exact value at the cut
        original, c["keys"], second["keys"] = c["keys"], {}, {}
        for prop, pts in original.items():
            c["keys"][prop], second["keys"][prop] = keys.split_points(pts, first_dur)
    p.clips.append(second)
    return {"first": clip, "second": second["id"], "at": at}


@op
def trim(p: Project, clip, start=None, end=None):
    """Move the clip's head and/or tail on the timeline; the picture stays in sync with the source."""
    c = p.clip(clip)
    old_start, old_end = c["start"], c["start"] + c["dur"]
    new_start = old_start if start is None else r4(start)
    new_end = old_end if end is None else r4(end)
    if new_start < 0:
        raise EditError("start cannot be negative")
    if new_end - new_start < MIN_CLIP:
        raise EditError("the clip would be shorter than one frame")
    if "asset" in c and not c.get("hold"):
        new_in = timemap.trimmed_src_in(c, new_start - old_start, new_end - new_start)
        if new_in < -0.0005:
            raise EditError(f"the source has nothing before this point; the head can move back by "
                            f"{c['src_in'] / max(timemap.speed_at(c, 0.0), 1e-6):.2f}s at most")
        c["src_in"] = r4(max(0.0, new_in))
    if c.get("keys") and abs(new_start - old_start) > 1e-6:  # keys follow the picture, not the clip edge
        c["keys"] = {prop: keys.shift_points(pts, -(new_start - old_start)) for prop, pts in c["keys"].items()}
    c["start"], c["dur"] = new_start, r4(new_end - new_start)
    _check_asset_range(p, c)
    return {"clip": clip, "start": c["start"], "end": r4(c["start"] + c["dur"])}


@op
def move(p: Project, clip, at=None, track=None):
    c = p.clip(clip)
    if track is not None and track != c["track"]:
        t = p.track(track)
        kind = "text" if c.get("type") == "text" else p.assets[c["asset"]]["kind"]
        if (kind == "audio" and t["kind"] == "video") or (kind in ("text", "image") and t["kind"] == "audio"):
            raise EditError(f"a {kind} clip cannot go on {t['kind']} track {track}")
        c["track"] = track
    if at is not None:
        if float(at) < 0:
            raise EditError("start cannot be negative")
        c["start"] = r4(at)
    return {"clip": clip, "track": c["track"], "start": c["start"]}


@op
def _set(p: Project, clip, **fields):
    c = p.clip(clip)
    if not fields:
        raise EditError("nothing to set. Fields: " + ", ".join(sorted(FIELDS)))
    is_text = c.get("type") == "text"
    on_video = p.track(c["track"])["kind"] == "video"
    for k, v in fields.items():
        if k not in FIELDS:
            raise EditError(f"unknown field '{k}'. Fields: " + ", ".join(sorted(FIELDS)))
        if k in TEXT_ONLY and not is_text:
            raise EditError(f"'{k}' only applies to text clips")
        if k in PICTURE_ONLY and (is_text or not on_video):
            raise EditError(f"'{k}' only applies to picture clips on a video track")
        if k == "reverse" and (is_text or c.get("hold")):
            raise EditError("a title or frozen frame cannot be reversed")
        if k in PICTURE_OR_TEXT and not (is_text or on_video):
            raise EditError(f"'{k}' only applies to clips on a video track")
        val = FIELDS[k](v)
        if val is None:
            c.pop(k, None)
        else:
            c[k] = r4(val) if isinstance(val, float) and k in ("start", "dur", "fade_in", "fade_out", "afade_in", "afade_out") else val
    if "speed" in fields and "asset" in c:
        _check_asset_range(p, c)
    if "dur" in fields:
        _check_asset_range(p, c)
    out = {k: v for k, v in c.items()}
    shadowed = [k for k in fields if k in (c.get("keys") or {})]
    if shadowed:
        out["warning"] = f"{', '.join(shadowed)} has keyframes, which override this steady value; use set_keys or unkey to change them"
    return out


# --------------------------------------------------------------------------
# Color, keyframes, transitions, captions
# --------------------------------------------------------------------------
def _picture_clip(p: Project, c: dict, what: str) -> None:
    if c.get("type") == "text" or p.track(c["track"])["kind"] != "video":
        raise EditError(f"{what} only applies to picture clips on a video track")


@op
def grade_clip(p: Project, clip, reset=False, look=None, lut=None, **fields):
    """Color-grade a picture clip. Fields: brightness contrast saturation gamma temperature tint vignette sharpen blur.
    Pass the value 'none' to clear a field. look: one of the named looks. lut: path to a .cube file."""
    c = p.clip(clip)
    _picture_clip(p, c, "color grading")
    color = {} if _bool(reset) else dict(c.get("color") or {})
    incoming = {k: v for k, v in fields.items() if v is not None}
    if look is not None:
        incoming["look"] = look
    if lut is not None:
        incoming["lut"] = lut
    for k in [k for k, v in incoming.items() if str(v).lower() == "none"]:
        color.pop(k, None)
        incoming.pop(k)
    color.update(grade.validate(incoming))
    if color.get("lut"):
        path = Path(color["lut"]).expanduser()
        if not (path if path.is_absolute() else p.dir / path).is_file():
            raise EditError(f"LUT file not found: {color['lut']}")
    if color:
        c["color"] = color
    else:
        c.pop("color", None)
    return {"clip": clip, "color": color, "looks": list(grade.LOOK_NAMES)}


OPS["grade"] = OPS.pop("grade_clip")  # the function is named grade_clip so it does not shadow the grade module


def _sound_in(p: Project, c: dict) -> bool:
    if c.get("type") == "text":
        return False
    a = p.assets.get(c.get("asset", ""))
    return bool(a and (a["kind"] == "audio" or a.get("has_audio")))


def _key_props(p: Project, c: dict) -> set:
    a = p.assets.get(c.get("asset", ""))
    return keys.applicable(c, p.track(c["track"])["kind"], _sound_in(p, c), bool(a and a["kind"] == "video"))


def _keys_dict(c: dict) -> dict:
    return c.setdefault("keys", {})


@op
def key(p: Project, clip, prop, at, value=None, ease="linear"):
    """Add (or replace) one keyframe. `at` is TIMELINE time in seconds; it must fall inside the clip.
    Without `value` the keyframe freezes whatever the property is at that moment."""
    c = p.clip(clip)
    allowed = _key_props(p, c)
    if prop not in allowed:
        raise EditError(f"'{prop}' cannot be keyframed on clip {clip}. Choose from: {', '.join(sorted(allowed)) or 'nothing'}")
    local = r4(float(at) - c["start"])
    if local < -0.0005 or local > c["dur"] + 0.0005:
        raise EditError(f"time {float(at):g}s is outside clip {clip} ({c['start']:g}s to {c['start'] + c['dur']:g}s)")
    local = max(0.0, min(c["dur"], local))
    pts = (c.get("keys") or {}).get(prop)
    if value is None:
        value = keys.value_at(pts, local) if pts else keys.base_value(c, prop)
    merged = [pt for pt in (pts or []) if abs(pt[0] - local) > 1e-4] + [[local, float(value), ease]]
    _keys_dict(c)[prop] = keys.clean(prop, merged)
    if prop == "speed":
        _check_asset_range(p, c)
    return {"clip": clip, "prop": prop, "keys": c["keys"][prop]}


@op
def set_keys(p: Project, clip, prop, points):
    """Replace all keyframes of one property. `points` is [[seconds_from_clip_start, value, ease?], ...];
    an empty list removes the property's keyframes."""
    c = p.clip(clip)
    if not points:
        (c.get("keys") or {}).pop(prop, None)
    else:
        if prop not in _key_props(p, c):
            raise EditError(f"'{prop}' cannot be keyframed on clip {clip}. Choose from: {', '.join(sorted(_key_props(p, c)))}")
        _keys_dict(c)[prop] = keys.clean(prop, points)
    if not c.get("keys"):
        c.pop("keys", None)
    if prop == "speed" and "asset" in c:
        _check_asset_range(p, c)
    return {"clip": clip, "prop": prop, "keys": (c.get("keys") or {}).get(prop, [])}


@op
def unkey(p: Project, clip, prop, at=None):
    """Remove the keyframe at a timeline time (within 0.04 s), or all of the property's keyframes."""
    c = p.clip(clip)
    pts = (c.get("keys") or {}).get(prop)
    if not pts:
        raise EditError(f"clip {clip} has no keyframes on '{prop}'")
    if at is None:
        remaining = []
    else:
        local = float(at) - c["start"]
        remaining = [pt for pt in pts if abs(pt[0] - local) > 0.04]
        if len(remaining) == len(pts):
            raise EditError(f"no '{prop}' keyframe near {float(at):g}s on clip {clip} (keys are at "
                            + ", ".join(f"{c['start'] + pt[0]:g}s" for pt in pts) + ")")
    if remaining:
        c["keys"][prop] = remaining
    else:
        del c["keys"][prop]
        if not c["keys"]:
            del c["keys"]
    return {"clip": clip, "prop": prop, "keys": remaining}


@op
def transition(p: Project, clip_a, clip_b, type="dissolve", dur=0.6, ripple=True):
    """A transition between two neighbouring clips on a video track (A first, B next).
    Overlapping types pull B earlier so the clips overlap by `dur`, and by default the rest of that track
    follows (ripple), so the track gets shorter by `dur`. Other tracks are not moved."""
    if type not in TRANSITIONS:
        raise EditError(f"unknown transition '{type}'. Types: {', '.join(TRANSITIONS)}")
    a, b = p.clip(clip_a), p.clip(clip_b)
    if a["track"] != b["track"]:
        raise EditError("both clips must be on the same track")
    if a.get("type") == "text" or b.get("type") == "text" or p.track(a["track"])["kind"] != "video":
        raise EditError("transitions go between picture clips on a video track")
    if b["start"] < a["start"]:
        raise EditError(f"clip {clip_b} starts before clip {clip_a}; give them in timeline order")
    dur = r4(dur)
    if dur < 0.05:
        raise EditError("a transition needs at least 0.05 s")
    a_end = r4(a["start"] + a["dur"])
    gap = r4(b["start"] - a_end)
    if gap > 0.25:
        raise EditError(f"the clips are {gap:g}s apart; move {clip_b} up against {clip_a} first")
    if "transition" in b:  # re-applying: clear what the last one set
        for prop in ("x", "y", "scale"):
            (b.get("keys") or {}).pop(prop, None)
        b.pop("afade_in", None)
        b["fade_in"] = 0
    shift = 0.0
    if type == "dip":
        half = r4(dur / 2)
        if half > min(a["dur"], b["dur"]):
            raise EditError("the transition is longer than a clip")
        a["fade_out"], b["fade_in"] = half, half
    else:
        dur = max(dur, r4(-gap))  # an existing overlap longer than asked becomes the transition
        if dur > min(a["dur"], b["dur"]):
            raise EditError("the transition is longer than a clip")
        shift = r4(b["start"] - (a_end - dur))
        if shift > 0.0005:
            if _bool(ripple):
                for c in p.clips:
                    if c["track"] == a["track"] and c["start"] >= b["start"] - 1e-6:
                        c["start"] = r4(c["start"] - shift)
            else:
                b["start"] = r4(b["start"] - shift)
        a["afade_out"] = dur  # sound crossfades; A's picture stays solid under B
        bk, ak = b.setdefault("keys", {}), a.setdefault("keys", {})
        if type == "dissolve":
            b["fade_in"] = dur
        elif type == "zoom":
            b["fade_in"] = dur
            bk["scale"] = keys.clean("scale", [[0, 1.4, "out"], [dur, 1.0]])
        else:
            b["fade_in"] = 0
            b["afade_in"] = dur
            kind, direction = type.split("-")
            if kind == "slide":
                axis, sign = ("x", 1.0) if direction == "left" else ("x", -1.0) if direction == "right" \
                    else ("y", 1.0) if direction == "up" else ("y", -1.0)
                bk[axis] = keys.clean(axis, [[0, sign, "out"], [dur, 0.0]])
            else:  # push: A leaves as B arrives
                sign = 1.0 if direction == "left" else -1.0
                bk["x"] = keys.clean("x", [[0, sign, "inout"], [dur, 0.0]])
                ak["x"] = keys.clean("x", [[r4(a["dur"] - dur), 0.0, "inout"], [a["dur"], -sign]])
        for c in (a, b):
            if not c.get("keys"):
                c.pop("keys", None)
    b["transition"] = {"type": type, "dur": dur, "from": a["id"]}
    return {"transition": type, "dur": dur, "b_start": b["start"], "shifted_by": shift}


_CUE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")


def parse_captions(text: str) -> list[tuple[float, float, str]]:
    """Cues from SRT or WebVTT text: [(start, end, text), ...]."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            m = _CUE.search(line)
            if not m:
                continue
            g = m.groups()

            def sec(h, mi, s, ms):
                return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000

            start, end = sec(*g[0:4]), sec(*g[4:8])
            body = re.sub(r"<[^>]+>|\{\\[^}]*\}", "", "\n".join(lines[i + 1:])).strip()
            if body and end > start:
                cues.append((start, end, body))
            break
    return cues


@op
def import_captions(p: Project, path=None, srt=None, offset=0, track=None, pos="bottom", size=None, bg=True):
    """Turn an SRT or WebVTT file (or its text) into caption clips, one per cue."""
    if (path is None) == (srt is None):
        raise EditError("give either path (a .srt or .vtt file) or srt (the text itself)")
    if path is not None:
        f = Path(path).expanduser()
        if not f.is_file():
            raise EditError(f"file not found: {f}")
        srt = f.read_text(encoding="utf-8-sig", errors="replace")
    cues = parse_captions(srt)
    if not cues:
        raise EditError("no captions found; expected lines like 00:00:01,000 --> 00:00:03,500")
    if len(cues) > 2000:
        raise EditError("more than 2000 captions; split the file")
    off = float(offset)
    steps = []
    for start, end, body in cues:
        s = start + off
        if end + off <= 0:
            continue
        s = max(0.0, s)
        steps.append(add_text(p, text=body, at=s, dur=max(MIN_CLIP, end + off - s), track=track, pos=pos, size=size,
                              bg=bg, fade_in=0, fade_out=0)["clip"])
    return {"captions": len(steps), "first": steps[0] if steps else None, "last": steps[-1] if steps else None}


@op
def remove(p: Project, clip, ripple=False):
    c = p.clip(clip)
    p.clips.remove(c)
    if _bool(ripple):
        for o in p.clips:
            if o["track"] == c["track"] and o["start"] >= c["start"] + c["dur"] - 1e-6:
                o["start"] = r4(o["start"] - c["dur"])
    return {"removed": clip}


# --------------------------------------------------------------------------
# Freeze frames, masks, tracking, stabilization
# --------------------------------------------------------------------------
def _video_clip(p: Project, c: dict, what: str) -> dict:
    a = p.assets.get(c.get("asset", ""))
    if c.get("type") == "text" or not a or a["kind"] != "video":
        raise EditError(f"{what} needs a video clip")
    return a


@op
def freeze(p: Project, clip, at, dur=2.0):
    """Hold the frame shown at timeline time `at` for `dur` seconds (no sound). The clip is cut there if
    needed, and everything later on that track moves later by `dur`."""
    c = p.clip(clip)
    a = _video_clip(p, c, "a freeze frame")
    if c.get("hold"):
        raise EditError("that clip is already a frozen frame")
    dur, at = r4(dur), r4(at)
    if not MIN_CLIP <= dur <= 120:
        raise EditError("a freeze must last between one frame and 120 seconds")
    start, end = c["start"], r4(c["start"] + c["dur"])
    if not start - 1e-6 <= at <= end + 1e-6:
        raise EditError(f"time {at:g}s is outside clip {clip} ({start:g}s to {end:g}s)")
    local = max(0.0, min(c["dur"], at - start))
    src_time = max(0.0, min(timemap.source_time_at(c, local), a["duration"] - 0.04))

    if start + MIN_CLIP < at < end - MIN_CLIP:
        split(p, clip=clip, at=at)  # c is now the first half; the second half starts at `at`
        local = c["dur"]
    elif at >= end - MIN_CLIP:
        at = end
    else:
        at = start
    base = copy.deepcopy(c)
    held = {prop: [[0.0, round(keys.value_at(pts, local), 6), "linear"]]
            for prop, pts in (base.get("keys") or {}).items() if prop != "speed"}
    for o in p.clips:  # make room: everything from `at` onward on this track moves later
        if o["track"] == c["track"] and o["start"] >= at - 1e-6:
            o["start"] = r4(o["start"] + dur)
    base.update(id=_new_clip_id(p), hold=True, src_in=r4(src_time), dur=dur, start=at, speed=1.0, mute=True,
                fade_in=0, fade_out=0)
    for gone in ("reverse", "transition", "afade_in", "afade_out", "stabilize", "preset", "keys"):
        base.pop(gone, None)
    if held:
        base["keys"] = held
    p.clips.append(base)
    return {"hold": base["id"], "start": at, "end": r4(at + dur), "source_time": base["src_in"]}


@op
def mask(p: Project, clip, id=None, shape=None, x=None, y=None, w=None, h=None, feather=None, invert=None,
         effect=None, amount=None):
    """Add a mask to a picture clip, or change one by id. See masks.py for what the fields mean."""
    c = p.clip(clip)
    _picture_clip(p, c, "masks")
    ms = c.setdefault("masks", [])
    given = {k: v for k, v in dict(shape=shape, x=x, y=y, w=w, h=h, feather=feather, invert=invert,
                                   effect=effect, amount=amount).items() if v is not None}
    if "invert" in given:
        given["invert"] = _bool(given["invert"])
    if id:
        m = next((m for m in ms if m["id"] == id), None)
        if not m:
            raise EditError(f"clip {clip} has no mask '{id}' (masks: {', '.join(x['id'] for x in ms) or 'none'})")
        m.update(masks.validate({**m, **given}))
    else:
        if len(ms) >= masks.MAX_MASKS:
            raise EditError(f"at most {masks.MAX_MASKS} masks per clip")
        m = masks.validate({"id": p.new_id("m", (x["id"] for x in ms)), **given})
        ms.append(m)
    return {"clip": clip, "mask": m["id"], **{k: v for k, v in m.items() if k != "id"}}


@op
def unmask(p: Project, clip, id):
    c = p.clip(clip)
    ms = c.get("masks") or []
    m = next((m for m in ms if m["id"] == id), None)
    if not m:
        raise EditError(f"clip {clip} has no mask '{id}'")
    ms.remove(m)
    for prop in [k for k in (c.get("keys") or {}) if k.startswith(id + ".")]:
        del c["keys"][prop]
    for gone in [k for k in ("masks", "keys") if not c.get(k)]:
        c.pop(gone)
    return {"removed": id}


def _trackers(p: Project) -> dict:
    return p.data.setdefault("trackers", {})


def _tracker(p: Project, tid: str) -> dict:
    t = _trackers(p).get(tid)
    if not t:
        raise EditError(f"no tracker '{tid}' (trackers: {', '.join(_trackers(p)) or 'none'}). Make one with: vedit track")
    return t


@op
def track(p: Project, clip, at, x, y, size=0.08, name=None):
    """Follow the point (x, y) (fractions of the picture) seen at timeline time `at` through the clip.
    Slow for long clips (it analyses every frame), but only once; the result is stored as a tracker."""
    c = p.clip(clip)
    _video_clip(p, c, "tracking")
    tid = name or p.new_id("t", _trackers(p))
    if not re.fullmatch(r"[A-Za-z][\w-]{0,20}", tid):
        raise EditError("a tracker name starts with a letter and uses letters, digits, - or _")
    result = tracking.analyze(p, c, float(at), float(x), float(y), float(size))
    _trackers(p)[tid] = result
    out = {"tracker": tid, "found": result["found"], "frames_followed": result["frames_followed"],
           "points": len(result["points"])}
    if result["found"] < 0.6:
        out["warning"] = (f"tracking followed the target in only {round(result['found'] * 100)}% of the frames; it may have turned, "
                          "left the picture or been hidden. Check with a frame, or track from a different moment.")
    return out


@op
def set_tracker(p: Project, id, clip, points):
    """Store a motion path you measured yourself: points = [[source_seconds, x, y], ...] (x, y are picture fractions)."""
    c = p.clip(clip)
    _video_clip(p, c, "a tracker")
    try:
        pts = sorted([[r4(t), float(px), float(py)] for t, px, py in points])
    except (TypeError, ValueError):
        raise EditError("points must be a list of [source_seconds, x, y]")
    if len(pts) < 2 or len(pts) > 600:
        raise EditError("a tracker needs between 2 and 600 points")
    _trackers(p)[id] = {"clip": clip, "asset": c["asset"], "at_source": pts[0][0], "points": pts, "found": 1.0,
                        "frames_followed": len(pts), "start": None}
    return {"tracker": id, "points": len(pts)}


@op
def remove_tracker(p: Project, id):
    _tracker(p, id)
    del _trackers(p)[id]
    return {"removed": id}


def _plain_timing(c: dict, what: str) -> None:
    if c.get("reverse") or timemap.is_ramped(c) or c.get("hold"):
        raise EditError(f"{what}: this clip is reversed, ramped or frozen, so a tracked position cannot be matched to time; "
                        "do it on a normal-speed clip")


def _limit(prop: str, v: float) -> float:
    _, lo, hi, _ = keys.info(prop)
    return max(lo, min(hi, v))


@op
def follow(p: Project, clip, mask, tracker, dx=0.0, dy=0.0):
    """Make a mask follow a tracker: its centre keyframes are filled from the tracked path."""
    c = p.clip(clip)
    _picture_clip(p, c, "following")
    m = next((m for m in c.get("masks") or [] if m["id"] == mask), None)
    if not m:
        raise EditError(f"clip {clip} has no mask '{mask}'. Add one with: vedit mask")
    t = _tracker(p, tracker)
    if t["asset"] != c["asset"]:
        raise EditError("that tracker was made on different footage")
    _plain_timing(c, "following")
    if c.get("crop") or c.get("flip"):
        raise EditError("remove the crop or flip first: tracked positions are measured on the original picture")
    speed = float(c.get("speed", 1.0))
    pts = tracking.points_between(t["points"], 0.0, c["dur"], lambda ts: (ts - c["src_in"]) / speed)
    if not pts:
        raise EditError("the tracker has no data inside this part of the footage")
    ks = c.setdefault("keys", {})
    for axis, col, off in (("x", 1, float(dx)), ("y", 2, float(dy))):
        prop = f"{mask}.{axis}"
        ks[prop] = keys.clean(prop, [[pt[0], round(_limit(prop, pt[col] + off), 6), "linear"] for pt in pts])
    return {"clip": clip, "mask": mask, "tracker": tracker, "keyframes": len(pts)}


@op
def attach(p: Project, clip, tracker, to=None, dx=0.0, dy=0.0):
    """Make a title or overlay clip follow a tracked point as it appears on screen. `to` is the clip showing the
    tracked footage (default: the one the tracker was made on); dx, dy nudge it, as fractions of the frame."""
    c = p.clip(clip)
    if p.track(c["track"])["kind"] != "video" or (c.get("type") != "text" and "asset" not in c):
        raise EditError("only a title or a picture clip on a video track can follow a tracker")
    t = _tracker(p, tracker)
    src = p.clip(to or t["clip"])
    a = _video_clip(p, src, "the tracked footage")
    if t["asset"] != src["asset"]:
        raise EditError("that tracker was made on different footage than the clip it is attached to")
    _plain_timing(src, "attaching")
    if src.get("crop") or src.get("flip"):
        raise EditError("remove the crop or flip from the tracked footage first")
    W, H = int(p.settings["width"]), int(p.settings["height"])
    lay = masks.layout(a, src, W, H)
    speed = float(src.get("speed", 1.0))
    if c.get("box"):
        bx, by = c["box"][0] + c["box"][2] / 2, c["box"][1] + c["box"][3] / 2  # offsets are from the box centre
    else:
        bx = by = 0.5
    on_screen = []
    for ts, cx, cy in t["points"]:
        if lay.box:
            X = (lay.bx + (lay.bw - lay.pw) / 2 + cx * lay.pw) / W
            Y = (lay.by + (lay.bh - lay.ph) / 2 + cy * lay.ph) / H
        else:
            X, Y = (lay.px0 + cx * lay.pw) / W, (lay.py0 + cy * lay.ph) / H
        on_screen.append([ts, X - bx + float(dx), Y - by + float(dy)])
    pts = tracking.points_between(on_screen, 0.0, c["dur"],
                                  lambda ts: src["start"] + (ts - src["src_in"]) / speed - c["start"])
    if not pts:
        raise EditError("the tracked footage is not on screen during this clip; move the clip over it")
    ks = c.setdefault("keys", {})
    for axis, col in (("x", 1), ("y", 2)):
        ks[axis] = keys.clean(axis, [[pt[0], round(_limit(axis, pt[col]), 6), "linear"] for pt in pts])
    if c.get("type") == "text":
        c["pos"] = "center"  # offsets are measured from the middle of the frame
    return {"clip": clip, "tracker": tracker, "keyframes": len(pts)}


@op
def stabilize_clip(p: Project, clip, strength=0.5):
    """Smooth out camera shake. strength 0 is gentle, 1 is a locked-off look (with more zoom to hide the edges).
    Pass 'off' to remove it. Needs an FFmpeg with libvidstab."""
    c = p.clip(clip)
    _video_clip(p, c, "stabilization")
    if c.get("hold"):
        raise EditError("a frozen frame has no shake to remove")
    if str(strength).lower() in ("off", "none"):
        c.pop("stabilize", None)
        return {"clip": clip, "stabilize": None}
    s = float(strength)
    if not 0 <= s <= 1:
        raise EditError("strength is between 0 and 1")
    stabilize.require()
    c["stabilize"] = {"strength": s}
    return {"clip": clip, "stabilize": c["stabilize"], "smoothing_frames": stabilize.smoothing(s)}


OPS["stabilize"] = OPS.pop("stabilize_clip")  # named stabilize_clip so it does not shadow the stabilize module


# --------------------------------------------------------------------------
# Reading the project
# --------------------------------------------------------------------------
@op
def check(p: Project):
    """Problems a render would hit or that would look wrong. Returns {errors, warnings}."""
    errors, warnings = [], []
    known = presets.load_all()
    for aid in p.assets:
        if not p.asset_path(aid).is_file():
            errors.append(f"asset {aid} ({p.assets[aid]['name']}): file is missing at {p.asset_path(aid)}")
    for c in p.clips:
        tag = f"clip {c['id']}"
        if "asset" in c:
            if c["asset"] not in p.assets:
                errors.append(f"{tag}: refers to missing asset {c['asset']}")
                continue
            try:
                _check_asset_range(p, c)
            except EditError as exc:
                errors.append(f"{tag}: {exc}")
        if c.get("preset") and c["preset"].lower() not in known:
            errors.append(f"{tag}: unknown voice preset '{c['preset']}'")
        lut = None if c.get("type") == "text" else (c.get("color") or {}).get("lut")  # text uses 'color' for its font colour
        if lut:
            lp = Path(lut).expanduser()
            if not (lp if lp.is_absolute() else p.dir / lp).is_file():
                errors.append(f"{tag}: LUT file is missing: {lut}")
        for prop in c.get("keys") or {}:
            if prop not in _key_props(p, c):
                warnings.append(f"{tag}: keyframes on '{prop}' have no effect on this clip")
        if c["fade_in"] + c["fade_out"] > c["dur"] + 1e-6:
            warnings.append(f"{tag}: fades ({c['fade_in'] + c['fade_out']:g}s) are longer than the clip ({c['dur']:g}s)")
        if c.get("volume_db", 0) > 6:
            warnings.append(f"{tag}: volume {c['volume_db']:g} dB is very loud; the limiter will squash the mix")
    for c in p.clips:
        if c.get("type") == "text":
            continue
        a = p.assets.get(c.get("asset", ""))
        if c.get("stabilize") and not stabilize.available():
            errors.append(f"clip {c['id']}: stabilization needs an FFmpeg with libvidstab, which this one lacks")
        if timemap.is_ramped(c) and a and a.get("has_audio") and not c.get("mute") and p.track(c["track"])["kind"] == "video":
            warnings.append(f"clip {c['id']}: a speed ramp mutes the clip's own sound; put music or effects on an audio track")
        if c.get("reverse") and a and a["kind"] == "video" and p.track(c["track"])["kind"] == "video":
            lay = masks.layout(a, c, int(p.settings["width"]), int(p.settings["height"]))
            frames = timemap.source_span(c) * float(p.settings["fps"])
            gb = frames * lay.lw * lay.lh * 2.5 / 1e9  # reversing holds every frame in memory
            if gb > 3:
                errors.append(f"clip {c['id']}: reversing {timemap.source_span(c):.0f}s at {lay.lw}x{lay.lh} needs about {gb:.1f} GB of memory; "
                              "shorten the clip, lower the canvas size, or split it into pieces")
            elif gb > 1:
                warnings.append(f"clip {c['id']}: reversing needs about {gb:.1f} GB of memory; rendering may be slow")
    # Black gaps on the lowest video track.
    base = next((t["id"] for t in p.tracks if t["kind"] == "video"), None)
    if base:
        cur = 0.0
        for c in sorted((c for c in p.clips if c["track"] == base), key=lambda c: c["start"]):
            if c["start"] - cur > 0.05:
                warnings.append(f"track {base}: {c['start'] - cur:.2f}s of black at {cur:.2f}s (a gap before clip {c['id']})")
            cur = max(cur, c["start"] + c["dur"])
        if base and p.duration() - cur > 0.05 and cur > 0:
            warnings.append(f"track {base} ends at {cur:.2f}s but the project runs to {p.duration():.2f}s")
    # Busy sound design.
    sfx = [c for c in p.clips if c.get("sfx")]
    if sfx and p.duration() > 0:
        per10 = len(sfx) / p.duration() * 10
        if per10 > 12:
            warnings.append(f"{len(sfx)} sound effects in {p.duration():.0f}s ({per10:.0f} per 10s) is busy; do the most with the least")
    if not p.clips:
        warnings.append("the timeline is empty")
    return {"errors": errors, "warnings": warnings}


def apply(project_path: Path, steps: list):
    """Run several ops as one change: all of them are saved, or none (and undo reverts them together)."""
    if not isinstance(steps, list) or not steps:
        raise EditError("batch needs a non-empty list of steps like [{\"op\": \"add\", \"asset\": \"a1\"}]")

    def run(p: Project):
        results = []
        for i, step in enumerate(steps, 1):
            if not isinstance(step, dict) or "op" not in step:
                raise EditError(f"step {i}: each step must be an object with an 'op' field")
            args = dict(step)
            name = args.pop("op")
            fn = OPS.get(name)
            if not fn:
                raise EditError(f"step {i}: unknown op '{name}'. Ops: {', '.join(sorted(OPS))}")
            try:
                results.append(fn(p, **args))
            except EditError as exc:
                raise EditError(f"step {i} ({name}): {exc}")
            except TypeError as exc:
                raise EditError(f"step {i} ({name}): bad arguments ({exc})")
        return results

    from .project import transact
    return transact(project_path, run)


def describe(p: Project) -> dict:
    """The whole project in the shape the UI and `show --json` use."""
    return {
        "rev": p.data.get("rev", 0),
        "settings": p.settings,
        "duration": p.duration(),
        "assets": p.assets,
        "tracks": p.tracks,
        "clips": sorted(p.clips, key=lambda c: (c["track"], c["start"])),
        "trackers": {k: {"clip": t["clip"], "asset": t["asset"], "found": t.get("found"), "points": len(t["points"])}
                     for k, t in (p.data.get("trackers") or {}).items()},
    }
