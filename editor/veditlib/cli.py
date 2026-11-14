"""Command line for vedit. Every command prints JSON (or a readable timeline for `show`)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ops, presets, project as P
from .project import EditError


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def one(project_file: Path, op_name: str, **kw):
    kw = {k: v for k, v in kw.items() if v is not None}
    results, _ = ops.apply(project_file, [{"op": op_name, **kw}])
    out(results[0])


def show_text(p: P.Project) -> None:
    s = p.settings
    print(f"{p.dir.name}: {s['width']}x{s['height']} @ {s['fps']:g} fps, {p.duration():g}s, rev {p.data['rev']}")
    if p.assets:
        print("assets:")
        for aid, a in p.assets.items():
            dur = f"{a['duration']:g}s" if a["kind"] != "image" else "image"
            print(f"  {aid}  {a['kind']:<5} {dur:>9}  {a['name']}")
    for t in p.tracks:
        flag = " (ducks under other audio)" if t.get("duck") else ""
        print(f"track {t['id']} [{t['kind']}]{flag}")
        for c in sorted((c for c in p.clips if c["track"] == t["id"]), key=lambda c: c["start"]):
            if c.get("type") == "text":
                what = f"text {c['text'][:40]!r}"
            else:
                what = p.assets[c["asset"]]["name"] if c["asset"] in p.assets else "?"
                if c.get("sfx"):
                    what = "sfx " + what
            extras = []
            for k, label in (("src_in", "in"), ("speed", "x"), ("volume_db", "dB"), ("fade_in", "fi"),
                             ("fade_out", "fo"), ("opacity", "op")):
                if c.get(k) not in (None, 0, 1, 1.0):
                    extras.append(f"{label}={c[k]:g}")
            if c.get("preset"):
                extras.append(f"preset={c['preset']!r}")
            for flag, label in (("reverse", "reversed"), ("hold", "frozen"), ("stabilize", "stabilized")):
                if c.get(flag):
                    extras.append(label)
            if "speed" in (c.get("keys") or {}):
                extras.append("speed-ramp")
            if c.get("masks"):
                extras.append("masks=" + ",".join(f"{m['id']}:{m['effect']}" for m in c["masks"]))
            if c.get("mute"):
                extras.append("muted")
            print(f"  {c['id']:<5} {c['start']:>8.3f} -> {c['start'] + c['dur']:>8.3f}  {what}  {' '.join(extras)}")


def show_trackers(p: P.Project) -> None:
    for tid, t in (p.data.get("trackers") or {}).items():
        print(f"tracker {tid}: followed {round(t.get('found', 1) * 100)}% of clip {t['clip']}'s footage ({len(t['points'])} points)")


def parser() -> argparse.ArgumentParser:
    # -p works before or after the command. SUPPRESS stops a command's own default from erasing it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--project", default=argparse.SUPPRESS,
                        help="project folder or project.json (default: current folder)")

    ap = argparse.ArgumentParser(
        prog="vedit",
        description="A small video editor for an AI to drive. Every edit is a command; the project is plain JSON.",
    )
    ap.add_argument("-p", "--project", help="project folder or project.json (default: current folder)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    def cmd(name, help, **kw):
        sp = sub.add_parser(name, help=help, parents=[common], **kw)
        return sp

    s = cmd("new", "create a project folder")
    s.add_argument("name")
    s.add_argument("--size", default=None, help="WIDTHxHEIGHT, default 1280x720")
    s.add_argument("--fps", type=float)

    s = cmd("import", "add a media file to the project's assets")
    s.add_argument("file")
    s.add_argument("--copy", action="store_true", help="copy into the project's media folder")

    s = cmd("add", "put an asset on the timeline")
    s.add_argument("asset")
    s.add_argument("--track")
    s.add_argument("--at", type=float, help="timeline start in seconds (default: end of the track)")
    s.add_argument("--in", dest="src_in", type=float, help="start inside the source file, seconds")
    s.add_argument("--dur", type=float, help="length on the timeline, seconds")
    s.add_argument("--speed", type=float)
    s.add_argument("--volume", dest="volume_db", type=float)
    s.add_argument("--mute", action="store_true", default=None)
    s.add_argument("--fade-in", dest="fade_in", type=float)
    s.add_argument("--fade-out", dest="fade_out", type=float)
    s.add_argument("--preset", help="voice preset name")
    s.add_argument("--opacity", type=float)
    s.add_argument("--fit", choices=["contain", "cover"])
    s.add_argument("--box", help="picture-in-picture: x,y,w,h as fractions of the frame")
    s.add_argument("--note")

    s = cmd("text", "add a title or caption")
    s.add_argument("text")
    s.add_argument("--at", type=float, required=True)
    s.add_argument("--dur", type=float, required=True)
    s.add_argument("--track")
    s.add_argument("--size", type=float)
    s.add_argument("--color")
    s.add_argument("--pos", choices=["top", "center", "bottom"])
    s.add_argument("--no-bg", dest="bg", action="store_const", const=False, default=None)
    s.add_argument("--fade-in", dest="fade_in", type=float)
    s.add_argument("--fade-out", dest="fade_out", type=float)

    s = cmd("sfx", "place a catalog sound effect so its hit lands on a time")
    s.add_argument("sound", help="id from scripts/sfx_search.py")
    s.add_argument("--at", type=float, required=True)
    s.add_argument("--sync", choices=["peak", "onset", "start"])
    s.add_argument("--gain", dest="gain_db", type=float)
    s.add_argument("--track")
    s.add_argument("--duration", type=float)
    s.add_argument("--catalog")
    s.add_argument("--library")
    s.add_argument("--note")

    s = cmd("split", "cut a clip in two")
    s.add_argument("clip")
    s.add_argument("--at", type=float, required=True, help="timeline time")

    s = cmd("trim", "change a clip's start and/or end on the timeline")
    s.add_argument("clip")
    s.add_argument("--start", type=float)
    s.add_argument("--end", type=float)

    s = cmd("move", "move a clip in time and/or to another track")
    s.add_argument("clip")
    s.add_argument("--at", type=float)
    s.add_argument("--track")

    s = cmd("set", "change clip fields: set c1 volume_db=-6 preset='phone call'")
    s.add_argument("clip")
    s.add_argument("fields", nargs="+", metavar="key=value")

    s = cmd("remove", "delete a clip")
    s.add_argument("clip")
    s.add_argument("--ripple", action="store_true", help="close the gap on that track")

    s = cmd("key", "add a keyframe so a value changes over the clip")
    s.add_argument("clip")
    s.add_argument("prop", help="scale x y rotation opacity brightness contrast saturation gamma volume_db")
    s.add_argument("--at", type=float, required=True, help="TIMELINE time in seconds, inside the clip")
    s.add_argument("--value", type=float, help="default: what the property is at that moment")
    s.add_argument("--ease", choices=["linear", "in", "out", "inout", "hold"], help="how it eases to the next keyframe")

    s = cmd("keys", "replace all keyframes of a property: keys c1 scale '[[0,1],[4,1.3,\"inout\"]]'")
    s.add_argument("clip")
    s.add_argument("prop")
    s.add_argument("points", help="JSON list of [seconds_from_clip_start, value, ease?]; [] removes them")

    s = cmd("unkey", "remove a keyframe (or all of a property's keyframes)")
    s.add_argument("clip")
    s.add_argument("prop")
    s.add_argument("--at", type=float, help="timeline time of the keyframe; omit to remove them all")

    s = cmd("grade", "color-grade a picture clip")
    s.add_argument("clip")
    s.add_argument("--look", help="named look, or none")
    s.add_argument("--lut", help=".cube file, or none")
    for f in ("brightness", "contrast", "saturation", "gamma", "temperature", "tint", "vignette", "sharpen", "blur"):
        s.add_argument(f"--{f}", help="number, or none to clear")
    s.add_argument("--reset", action="store_true", help="clear the clip's grade first")

    s = cmd("transition", "transition between two neighbouring clips on a video track")
    s.add_argument("clip_a", help="the earlier clip")
    s.add_argument("clip_b", help="the clip that follows")
    s.add_argument("--type", dest="type", choices=list(ops.TRANSITIONS))
    s.add_argument("--dur", type=float, help="seconds, default 0.6")
    s.add_argument("--no-ripple", dest="ripple", action="store_false", default=None,
                   help="only move clip_b, not the rest of the track")

    s = cmd("captions", "turn an .srt or .vtt file into caption clips")
    s.add_argument("file")
    s.add_argument("--offset", type=float, help="shift all captions by this many seconds")
    s.add_argument("--track")
    s.add_argument("--pos", choices=["top", "center", "bottom"])
    s.add_argument("--size", type=float)

    s = cmd("freeze", "hold the frame at a time for a few seconds (later clips on the track move later)")
    s.add_argument("clip")
    s.add_argument("--at", type=float, required=True, help="timeline time of the frame to hold")
    s.add_argument("--dur", type=float, help="seconds to hold, default 2")

    s = cmd("reverse", "play a clip backwards (reverse --off to undo)")
    s.add_argument("clip")
    s.add_argument("--off", action="store_true")

    s = cmd("mask", "add a mask to a picture clip, or change one with --id")
    s.add_argument("clip")
    s.add_argument("--id", help="change this existing mask")
    s.add_argument("--shape", choices=["rect", "ellipse"])
    for f in ("x", "y", "w", "h", "feather", "amount"):
        s.add_argument(f"--{f}", type=float, help={"x": "centre across, 0 to 1 of the picture", "y": "centre down, 0 to 1",
                                                   "w": "width, 1 is the whole picture", "h": "height",
                                                   "feather": "edge softness, 0 to 1", "amount": "effect strength, 0 to 1"}[f])
    s.add_argument("--effect", choices=["none", "blur", "pixelate", "darken", "brighten", "desaturate"],
                   help="none = the clip shows only inside the mask")
    s.add_argument("--invert", dest="invert", action="store_const", const=True, default=None, help="apply outside the shape instead")
    s.add_argument("--no-invert", dest="invert", action="store_const", const=False)

    s = cmd("unmask", "remove a mask")
    s.add_argument("clip")
    s.add_argument("id")

    s = cmd("tracker", "motion tracking: add follows a point through a clip, remove deletes a tracker")
    s.add_argument("action", choices=["add", "remove"])
    s.add_argument("target", help="the clip id for add, the tracker id for remove")
    s.add_argument("--at", type=float, help="timeline time at which you are pointing (add)")
    s.add_argument("--x", type=float, help="across the picture, 0 to 1 (add)")
    s.add_argument("--y", type=float, help="down the picture, 0 to 1 (add)")
    s.add_argument("--size", type=float, help="patch to follow, as a fraction of the picture height (default 0.08)")
    s.add_argument("--name")

    s = cmd("follow", "make a mask follow a tracker")
    s.add_argument("clip")
    s.add_argument("mask")
    s.add_argument("tracker")
    s.add_argument("--dx", type=float, help="shift, as a fraction of the picture")
    s.add_argument("--dy", type=float)

    s = cmd("attach", "make a title or overlay clip follow a tracked point")
    s.add_argument("clip", help="the title or overlay that will follow")
    s.add_argument("tracker")
    s.add_argument("--to", help="the clip showing the tracked footage (default: where the tracker was made)")
    s.add_argument("--dx", type=float, help="nudge, as a fraction of the frame")
    s.add_argument("--dy", type=float)

    s = cmd("stabilize", "smooth out camera shake (needs FFmpeg with libvidstab)")
    s.add_argument("clip")
    s.add_argument("--strength", type=float, help="0 gentle to 1 locked off, default 0.5")
    s.add_argument("--off", action="store_true")

    cmd("looks", "list color looks, keyframeable properties, transitions and canvas presets")

    s = cmd("track", "add, remove or configure tracks")
    s.add_argument("action", choices=["add", "remove", "set"])
    s.add_argument("target", help="kind (video|audio) for add, track id otherwise")
    s.add_argument("--id")
    s.add_argument("--duck", choices=["on", "off"])
    s.add_argument("--mute", choices=["on", "off"], help="silence every clip on the track")
    s.add_argument("--hide", choices=["on", "off"], help="hide a video track's picture")

    s = cmd("settings", "show or change canvas size, fps, background and master loudness")
    s.add_argument("--width", type=int)
    s.add_argument("--height", type=int)
    s.add_argument("--fps", type=float)
    s.add_argument("--bg")
    s.add_argument("--preset", choices=list(ops.PRESETS), help="canvas size: vertical suits Reels, TikTok and Shorts")
    s.add_argument("--lufs", help="normalise the final loudness to this level (YouTube about -14), or off")

    s = cmd("show", "print the timeline")
    s.add_argument("--json", action="store_true")

    cmd("check", "list problems before rendering")

    s = cmd("render", "render the video")
    s.add_argument("-o", "--out")
    s.add_argument("--preview", action="store_true", help="small, fast render")

    s = cmd("frame", "save one frame of the edit as a PNG, to look at the picture")
    s.add_argument("--at", type=float, required=True)
    s.add_argument("-o", "--out")

    s = cmd("measure", "brightness and color numbers for a frame of the edit (to judge exposure and grade)")
    s.add_argument("--at", type=float, required=True)

    cmd("undo", "revert the last change")

    s = cmd("batch", "apply a JSON list of ops as one change (use - for stdin)")
    s.add_argument("file")

    s = cmd("presets", "voice presets: list, show, or import a Premiere .prfpset")
    s.add_argument("action", choices=["list", "show", "import"])
    s.add_argument("target", nargs="?", help="preset name (show) or .prfpset file (import)")
    s.add_argument("--dest", help="folder to write imported presets (default ~/.pro-video-editing/voice_presets)")

    s = cmd("analyze", "silences and scene changes of a media file")
    s.add_argument("file")
    s.add_argument("--silence-db", type=float, default=-35.0)
    s.add_argument("--min-silence", type=float, default=0.4)
    s.add_argument("--scene", type=float, default=0.35)

    s = cmd("serve", "open the visual editor in the browser")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true")
    return ap


def parse_fields(pairs):
    fields = {}
    for pair in pairs:
        if "=" not in pair:
            raise EditError(f"'{pair}' should look like key=value")
        k, v = pair.split("=", 1)
        fields[k.strip()] = v
    return fields


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return dispatch(args) or 0
    except EditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def dispatch(a) -> int | None:
    c = a.cmd
    if c == "new":
        w = h = None
        if a.size:
            try:
                w, h = (int(x) for x in a.size.lower().split("x"))
            except ValueError:
                raise EditError("--size must look like 1920x1080")
        folder = Path(a.project) / a.name if a.project else Path(a.name)
        proj = P.create(folder, w, h, a.fps)
        out({"project": str(proj.path), "settings": proj.settings})
        return
    if c == "looks":  # reference only; needs no project
        from . import grade, keys, masks
        out({"looks": {k: v for k, v in grade.LOOKS.items()},
             "color_fields": {k: {"default": v[0], "min": v[1], "max": v[2]} for k, v in grade.FIELDS.items()},
             "keyframe_properties": {k: {"default": v[0], "min": v[1], "max": v[2], "what": v[3]} for k, v in keys.PROPS.items()},
             "eases": list(keys.EASES), "transitions": list(ops.TRANSITIONS),
             "mask_shapes": list(masks.SHAPES), "mask_effects": list(masks.EFFECTS),
             "canvas_presets": {k: {"width": v[0], "height": v[1], "fps": v[2]} for k, v in ops.PRESETS.items()}})
        return
    if c == "presets":
        return presets_cmd(a)
    if c == "analyze":
        from .analyze import analyze
        out(analyze(Path(a.file), a.silence_db, a.min_silence, a.scene))
        return

    path = P.find_project(a.project)
    if c == "import":
        one(path, "import_asset", path=a.file, copy=a.copy or None)
    elif c == "add":
        one(path, "add", asset=a.asset, track=a.track, at=a.at, src_in=a.src_in, dur=a.dur, speed=a.speed,
            volume_db=a.volume_db, mute=a.mute, fade_in=a.fade_in, fade_out=a.fade_out, preset=a.preset,
            opacity=a.opacity, fit=a.fit, box=a.box, note=a.note)
    elif c == "text":
        one(path, "add_text", text=a.text, at=a.at, dur=a.dur, track=a.track, size=a.size, color=a.color,
            pos=a.pos, bg=a.bg, fade_in=a.fade_in, fade_out=a.fade_out)
    elif c == "sfx":
        one(path, "add_sfx", sound=a.sound, at=a.at, sync=a.sync, gain_db=a.gain_db, track=a.track,
            duration=a.duration, catalog=a.catalog, library=a.library, note=a.note)
    elif c == "split":
        one(path, "split", clip=a.clip, at=a.at)
    elif c == "trim":
        one(path, "trim", clip=a.clip, start=a.start, end=a.end)
    elif c == "move":
        one(path, "move", clip=a.clip, at=a.at, track=a.track)
    elif c == "set":
        one(path, "set", clip=a.clip, **parse_fields(a.fields))
    elif c == "remove":
        one(path, "remove", clip=a.clip, ripple=a.ripple or None)
    elif c == "key":
        one(path, "key", clip=a.clip, prop=a.prop, at=a.at, value=a.value, ease=a.ease)
    elif c == "keys":
        try:
            pts = json.loads(a.points)
        except json.JSONDecodeError as exc:
            raise EditError(f"points must be JSON like [[0,1],[4,1.3]]: {exc}")
        one(path, "set_keys", clip=a.clip, prop=a.prop, points=pts)
    elif c == "unkey":
        one(path, "unkey", clip=a.clip, prop=a.prop, at=a.at)
    elif c == "grade":
        fields = {f: getattr(a, f) for f in ("brightness", "contrast", "saturation", "gamma", "temperature",
                                              "tint", "vignette", "sharpen", "blur")}
        one(path, "grade", clip=a.clip, look=a.look, lut=a.lut, reset=a.reset or None, **fields)
    elif c == "freeze":
        one(path, "freeze", clip=a.clip, at=a.at, dur=a.dur)
    elif c == "reverse":
        one(path, "set", clip=a.clip, reverse=not a.off)
    elif c == "mask":
        one(path, "mask", clip=a.clip, id=a.id, shape=a.shape, x=a.x, y=a.y, w=a.w, h=a.h, feather=a.feather,
            invert=a.invert, effect=a.effect, amount=a.amount)
    elif c == "unmask":
        one(path, "unmask", clip=a.clip, id=a.id)
    elif c == "tracker":
        if a.action == "remove":
            one(path, "remove_tracker", id=a.target)
        else:
            if a.at is None or a.x is None or a.y is None:
                raise EditError("tracker add needs --at (timeline time), --x and --y (where to look, 0 to 1)")
            one(path, "track", clip=a.target, at=a.at, x=a.x, y=a.y, size=a.size, name=a.name)
    elif c == "follow":
        one(path, "follow", clip=a.clip, mask=a.mask, tracker=a.tracker, dx=a.dx, dy=a.dy)
    elif c == "attach":
        one(path, "attach", clip=a.clip, tracker=a.tracker, to=a.to, dx=a.dx, dy=a.dy)
    elif c == "stabilize":
        one(path, "stabilize", clip=a.clip, strength="off" if a.off else a.strength)
    elif c == "transition":
        one(path, "transition", clip_a=a.clip_a, clip_b=a.clip_b, type=a.type, dur=a.dur, ripple=a.ripple)
    elif c == "captions":
        one(path, "import_captions", path=a.file, offset=a.offset, track=a.track, pos=a.pos, size=a.size)
    elif c == "track":
        flag = lambda v: None if v is None else v == "on"  # noqa: E731
        if a.action == "add":
            one(path, "add_track", kind=a.target, id=a.id, duck=flag(a.duck))
        elif a.action == "remove":
            one(path, "remove_track", id=a.target)
        else:
            one(path, "set_track", id=a.target, duck=flag(a.duck), mute=flag(a.mute), hide=flag(a.hide))
    elif c == "settings":
        if any(v is not None for v in (a.width, a.height, a.fps, a.bg, a.preset, a.lufs)):
            one(path, "set_settings", width=a.width, height=a.height, fps=a.fps, bg=a.bg, preset=a.preset,
                master_lufs=a.lufs)
        else:
            out(P.load(path).settings)
    elif c == "show":
        proj = P.load(path)
        if a.json:
            out(ops.describe(proj))
        else:
            show_text(proj)
            show_trackers(proj)
    elif c == "check":
        res = ops.check(P.load(path))
        out(res)
        return 1 if res["errors"] else 0
    elif c == "render":
        from . import render
        proj = P.load(path)
        last = [-1]

        def prog(f):
            pct = int(f * 100)
            if pct // 10 != last[0] // 10:
                print(f"  {pct}%", file=sys.stderr)
            last[0] = pct

        dest = render.render(proj, Path(a.out).expanduser() if a.out else None, a.preview, prog)
        out({"rendered": str(dest), "duration": proj.duration(), "preview": bool(a.preview)})
    elif c == "frame":
        from . import render
        dest = render.snapshot(P.load(path), a.at, Path(a.out).expanduser() if a.out else None)
        out({"frame": str(dest), "at": a.at})
    elif c == "measure":
        from . import render
        from .analyze import measure_frame
        proj = P.load(path)
        png = render.snapshot(proj, a.at)
        out({"at": a.at, "frame": str(png), **measure_frame(png)})
    elif c == "undo":
        out({"rev": P.undo(path)})
    elif c == "batch":
        raw = sys.stdin.read() if a.file == "-" else Path(a.file).expanduser().read_text(encoding="utf-8")
        try:
            steps = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EditError(f"batch file is not valid JSON: {exc}")
        results, _ = ops.apply(path, steps)
        out(results)
    elif c == "serve":
        from .server import serve
        serve(path, a.port, not a.no_open)


def presets_cmd(a) -> int | None:
    if a.action == "list":
        allp = presets.load_all()
        out([{"name": v["name"], "group": v.get("group", ""), "effects": [e["effect"] for e in v["effects"]]}
             for v in sorted(allp.values(), key=lambda v: (v.get("group", ""), v["name"]))])
    elif a.action == "show":
        if not a.target:
            raise EditError("which preset? vedit presets show NAME")
        out(presets.get(a.target))
    else:
        if not a.target:
            raise EditError("which file? vedit presets import FILE.prfpset")
        stored = presets.import_prfpset(Path(a.target).expanduser(), Path(a.dest).expanduser() if a.dest else None)
        out({"imported": [{"name": s["name"], "group": s["group"], "chain": s["chain"],
                           "effects": [{"effect": e["effect"], "match": e["match"]} for e in s["effects"]]}
                          for s in stored],
             "into": str(Path(a.dest).expanduser() if a.dest else presets.USER_DIR)})
