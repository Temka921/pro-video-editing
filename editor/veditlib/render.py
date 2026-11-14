"""Turn a project into video with one FFmpeg run.

Video tracks are composited bottom to top over a solid background. Each picture clip goes through
crop/flip, chroma key, fit, color grade, speed, rotation, opacity, zoom and fades; text is drawn on
top; every clip's sound gets its voice preset, volume (steady or keyframed) and fades, then the mix
is ducked (sound-effect tracks sit under the other audio), normalised if asked, and limited.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import grade, keys, masks, ops, presets, stabilize, timemap
from .project import EditError, Project

SR = 48000
COLOR_RE = re.compile(r"[#A-Za-z0-9@.]+")
INLINE_GRAPH = 20000  # longer filter graphs go in a file: Windows limits a command line to about 32k characters
MAX_GRAPH = 1_000_000


def find_font() -> str:
    env = os.environ.get("VEDIT_FONT")
    cands = [env] if env else []
    cands += [
        r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for c in cands:
        if c and Path(c).is_file():
            return c
    raise EditError("no font found for text clips. Set VEDIT_FONT to the path of a .ttf file.")


def esc_path(p) -> str:
    """A file path inside a quoted filter option."""
    return str(p).replace("\\", "/").replace(":", r"\:")


def atempo(speed: float) -> list[str]:
    out = []
    while speed > 2.0:
        out.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        out.append("atempo=0.5")
        speed /= 0.5
    if abs(speed - 1.0) > 1e-6:
        out.append(f"atempo={speed:.6f}")
    return out


fmt = keys.num


def keyed(c: dict, prop: str, T: str) -> str | None:
    """Expression for a clip property if it has keyframes, else None. T is the clip-time expression."""
    pts = (c.get("keys") or {}).get(prop)
    return keys.expr(pts, T) if pts else None


class Chain:
    """One clip's video filters. Branching steps (masks, animated opacity) call flush() to emit a labelled
    graph statement, so the rest of the chain continues from its output."""

    def __init__(self, plan: "Plan", n: int, src: str):
        self.plan, self.n, self.src, self.parts, self.k = plan, n, src, [], 0

    def add(self, f: str) -> None:
        self.parts.append(f)

    def flush(self, label: str | None = None) -> str:
        label = label or f"c{self.n}_{self.k}"
        self.k += 1
        self.plan.video.append(f"[{self.src}]{','.join(self.parts) or 'null'}[{label}]")
        self.src, self.parts = label, []
        return label


class Plan:
    """The FFmpeg arguments for one render, built from the project."""

    def __init__(self, p: Project, preview: bool, want_audio: bool = True, stabilized: dict | None = None):
        s = p.settings
        self.p, self.preview, self.want_audio = p, preview, want_audio
        self.stabilized = stabilized or {}
        self.fps = float(s["fps"])
        self.W, self.H = int(s["width"]), int(s["height"])
        self.scale = 1.0
        if preview and max(self.W, self.H) > 640:
            self.scale = 640 / max(self.W, self.H)
            self.W = max(2, int(round(self.W * self.scale / 2)) * 2)
            self.H = max(2, int(round(int(s["height"]) * self.scale / 2)) * 2)
        self.total = p.duration()
        if self.total <= 0:
            raise EditError("the timeline is empty; add a clip first")
        bg = str(s.get("bg", "black"))
        if not COLOR_RE.fullmatch(bg):
            raise EditError(f"bad background colour '{bg}'")
        self.bg = bg
        self.inputs: list[str] = []
        self.open_input: dict[str, int] = {}  # clip id -> ffmpeg input index, for clips whose input is open
        self.video: list[str] = []
        self.audio: list[str] = []
        self.tmp = p.out_dir / ".text"
        self._built = None
        self.used_script = False

    def add_input(self, args: list[str]) -> int:
        self.inputs += args
        return sum(1 for a in self.inputs if a == "-i")

    # -- video ----------------------------------------------------------------
    def build_video(self) -> str:
        p, W, H, fps = self.p, self.W, self.H, self.fps
        self.video.append(f"color=c={self.bg}:s={W}x{H}:r={fps:g}:d={fmt(self.total)},format=yuv420p[b0]")
        last, n = "b0", 0
        for t in p.tracks:
            if t["kind"] != "video" or t.get("hide"):
                continue
            for c in sorted((c for c in p.clips if c["track"] == t["id"]), key=lambda c: c["start"]):
                n += 1
                nxt = f"b{n}"
                if c.get("type") == "text":
                    self.video.append(self.text_filter(c, last, nxt))
                else:
                    self.video_clip(c, n, last, nxt)
                last = nxt
        self.video.append(f"[{last}]null[vout]")
        return "vout"

    def video_clip(self, c: dict, n: int, last: str, nxt: str) -> None:
        p, a, fps = self.p, self.p.assets[c["asset"]], self.fps
        path = str(p.asset_path(c["asset"]))
        W, H = self.W, self.H
        lay = masks.layout(a, c, W, H)
        ck = c.get("keys") or {}
        dur, image = c["dur"], a["kind"] == "image"
        hold = bool(c.get("hold")) and not image
        ramped = timemap.is_ramped(c) and not (image or hold)
        reverse = bool(c.get("reverse")) and not (image or hold)
        span = timemap.source_span(c)
        if image:
            idx = self.add_input(["-loop", "1", "-framerate", f"{fps:g}", "-t", fmt(dur), "-i", path])
        elif hold:
            idx = self.add_input(["-ss", fmt(c["src_in"]), "-t", "0.5", "-i", path])
        else:
            idx = self.add_input(["-ss", fmt(c["src_in"]), "-t", fmt(span + 0.05), "-i", path])
        self.open_input[c["id"]] = idx - 1
        start = round(c["start"] * fps) / fps
        b = Chain(self, n, f"{idx - 1}:v")

        # 1. the source: steady the camera (on the original frames), then a steady frame rate, crop, flip, keying
        stab = self.stabilized.get(c["id"])
        if stab:
            smooth = stabilize.smoothing(c["stabilize"]["strength"])
            b.add(f"vidstabtransform=input='{esc_path(stab)}':smoothing={smooth}:zoom=0:optzoom=1:interpol=bicubic")
        b.add("setpts=PTS-STARTPTS" if ramped else f"fps={fps:g}")  # a ramp keeps the native frames until it has been applied
        crop = c.get("crop")
        if crop:
            l, t, r, bt = crop
            b.add(f"crop=trunc(iw*{1 - l - r:.5f}/2)*2:trunc(ih*{1 - t - bt:.5f}/2)*2:"
                  f"trunc(iw*{l:.5f}/2)*2:trunc(ih*{t:.5f}/2)*2")
        if "h" in (c.get("flip") or ""):
            b.add("hflip")
        if "v" in (c.get("flip") or ""):
            b.add("vflip")
        key_filter = grade.chromakey(c.get("chromakey"))
        if key_filter:
            b.add(key_filter)

        # 2. fit into the frame (or the picture-in-picture box) at an exact size, so masks can be placed precisely
        b.add(f"scale={lay.pw}:{lay.ph}")
        if not lay.box and c.get("fit", "contain") == "cover":
            b.add(f"crop={W}:{H}")
        b.add("setsar=1")

        # 3. color grade (before transparent padding so the bars stay untouched)
        if not grade.is_neutral(c.get("color"), ck):
            for f in grade.filters(c.get("color"), ck, p.dir):
                b.add(f)
        b.add("format=yuva420p")
        if not lay.box and c.get("fit", "contain") != "cover" and (lay.pw != W or lay.ph != H):
            b.add(f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black@0")

        # 4. time: freeze, reverse, speed (steady or ramped)
        if hold:
            b.add(f"trim=end_frame=1,loop=loop=-1:size=1:start=0,setpts=N/({fps:g}*TB),trim=duration={fmt(dur)}")
        else:
            if reverse:
                b.add("reverse")
            if ramped:
                b.add(f"setpts='({timemap.remap_expr(c)})/TB'")
                b.add(f"fps={fps:g}")
            else:
                b.add(f"setpts=(PTS-STARTPTS)/{c['speed']:g}")
            b.add(f"trim=end={fmt(dur)},setpts=PTS-STARTPTS")

        # 5. masks (the clock `T` is seconds from the clip's start from here on)
        for m in c.get("masks") or []:
            self.mask_stage(b, c, m, lay, ck, n)

        # 6. everything that changes over clip time
        rot = keyed(c, "rotation", "t")
        if rot:
            b.add(f"rotate=a='({rot})*PI/180':ow=iw:oh=ih:c=none")
        elif abs(c.get("rotation", 0)) > 1e-6:
            b.add(f"rotate=a={fmt(c['rotation'])}*PI/180:ow=iw:oh=ih:c=none")
        opac = keyed(c, "opacity", "T")  # geq's clock is called T
        if opac:
            # Animated opacity: a tiny mask follows the curve, is stretched to the layer's size and multiplied into
            # its alpha. (An expression per pixel on the picture itself is about ten times slower at 1080p.)
            src = b.flush()
            self.video.append(f"[{src}]split[o{n}a][o{n}b]")
            self.video.append(f"[o{n}a]alphaextract[o{n}c]")
            self.video.append(
                f"color=c=white:s=16x9:r={fps:g}:d={fmt(dur + 0.5)},format=gray,geq=lum='255*clip({opac},0,1)',"
                f"scale={lay.lw}:{lay.lh}:flags=neighbor,format=gray[o{n}m]")
            self.video.append(f"[o{n}c][o{n}m]blend=all_mode=multiply:shortest=1[o{n}d]")
            self.video.append(f"[o{n}b][o{n}d]alphamerge[o{n}q]")
            b.src = f"o{n}q"
        elif c.get("opacity", 1) < 0.999:
            b.add(f"colorchannelmixer=aa={c['opacity']:g}")
        zoom = keyed(c, "scale", "t")
        if zoom:
            b.add(f"scale=w='trunc(iw*({zoom})/2)*2':h=-2:eval=frame")  # h=-2 keeps the shape; the curve is written once
        elif abs(c.get("scale", 1) - 1) > 1e-6:
            b.add(f"scale=trunc(iw*{fmt(c['scale'])}/2)*2:trunc(ih*{fmt(c['scale'])}/2)*2")
        if c["fade_in"] > 0:
            b.add(f"fade=t=in:st=0:d={fmt(c['fade_in'])}:alpha=1")
        if c["fade_out"] > 0:
            b.add(f"fade=t=out:st={fmt(max(0.0, dur - c['fade_out']))}:d={fmt(c['fade_out'])}:alpha=1")
        b.add(f"trim=end={fmt(dur)}")  # nothing the mask sources add may outlast the clip
        b.add(f"setpts=PTS+{fmt(start)}/TB")
        b.flush(f"v{n}")

        # 7. place it on the picture; the overlay's clock `t` is timeline time
        local = f"t-{fmt(start)}"
        xe, ye = keyed(c, "x", local), keyed(c, "y", local)
        moved = xe or ye or c.get("x") or c.get("y") or zoom or abs(c.get("scale", 1) - 1) > 1e-6 or rot or c.get("rotation")
        ox, oy, pw_, ph_ = (lay.bx, lay.by, lay.bw, lay.bh) if lay.box else (0, 0, W, H)
        if moved:
            xo = xe or fmt(c.get("x", 0))
            yo = ye or fmt(c.get("y", 0))
            xpos = f"x='{ox}+({pw_}-w)/2+({xo})*{W}'"
            ypos = f"y='{oy}+({ph_}-h)/2+({yo})*{H}'"
        else:
            xpos, ypos = f"x={ox + (pw_ - lay.pw) // 2 if lay.box else 0}", f"y={oy + (ph_ - lay.ph) // 2 if lay.box else 0}"
        self.video.append(f"[{last}][v{n}]overlay={xpos}:{ypos}:eof_action=pass[{nxt}]")

    def mask_stage(self, b: Chain, c: dict, m: dict, lay: "masks.Layout", ck: dict, n: int) -> None:
        """Limit the clip to a shape (effect none) or apply an effect only inside it."""
        k = f"{n}{m['id']}"
        layer = b.flush()
        self.video.append(f"{masks.source_chain(m, lay, ck, self.fps, c['dur'], planes=m['effect'] != 'none')}[mk{k}]")
        if m["effect"] == "none":
            self.video.append(f"[{layer}]split[ma{k}][mb{k}]")
            self.video.append(f"[ma{k}]alphaextract[mc{k}]")
            self.video.append(f"[mc{k}][mk{k}]blend=all_mode=multiply:shortest=1[md{k}]")
            self.video.append(f"[mb{k}][md{k}]alphamerge[mo{k}]")
        else:
            self.video.append(f"[{layer}]split[ma{k}][mb{k}]")
            self.video.append(f"[mb{k}]{','.join(masks.effect_filters(m, lay))}[mf{k}]")
            self.video.append(f"[ma{k}][mf{k}][mk{k}]maskedmerge[mo{k}]")
        b.src = f"mo{k}"

    def text_filter(self, c: dict, last: str, nxt: str) -> str:
        color = c.get("color", "white")
        if not COLOR_RE.fullmatch(str(color)):
            raise EditError(f"clip {c['id']}: bad text colour '{color}'")
        self.tmp.mkdir(exist_ok=True)
        tf = self.tmp / f"{c['id']}.txt"
        tf.write_text(c["text"], encoding="utf-8")
        s, e = c["start"], c["start"] + c["dur"]
        size = max(8, int(round(c.get("size", self.H / 14) * self.scale)))
        local = f"t-{fmt(s)}"
        xe, ye = keyed(c, "x", local), keyed(c, "y", local)
        xo, yo = xe or fmt(c.get("x", 0)), ye or fmt(c.get("y", 0))
        ybase = {"top": "h*0.08", "center": "(h-text_h)/2", "bottom": "h-text_h-h*0.08"}[c.get("pos", "bottom")]
        parts = [
            f"fontfile='{esc_path(find_font())}'", f"textfile='{esc_path(tf)}'", "expansion=none",
            f"fontsize={size}", f"fontcolor={color}",
            f"x='(w-text_w)/2+({xo})*w'", f"y='{ybase}+({yo})*h'",
            f"enable='between(t,{fmt(s)},{fmt(e)})'",
        ]
        if c.get("bg", True):
            parts += ["box=1", "boxcolor=black@0.55", f"boxborderw={max(4, size // 4)}"]
        alpha = []
        if c["fade_in"] > 0:
            alpha.append(f"min(1,max(0,(t-{fmt(s)})/{fmt(c['fade_in'])}))")
        if c["fade_out"] > 0:
            alpha.append(f"min(1,max(0,({fmt(e)}-t)/{fmt(c['fade_out'])}))")
        op = keyed(c, "opacity", local)
        if op:
            alpha.append(f"({op})")
        elif c.get("opacity", 1) < 0.999:
            alpha.append(fmt(c["opacity"]))
        if alpha:
            parts.append("alpha='" + "*".join(alpha) + "'")
        return f"[{last}]drawtext={':'.join(parts)}[{nxt}]"

    # -- audio ----------------------------------------------------------------
    def clip_audio(self, c: dict, label: str) -> str | None:
        """Filter for one clip's sound, or None if it makes none."""
        p = self.p
        if c.get("type") == "text" or c.get("mute") or c.get("hold") or timemap.is_ramped(c):
            return None
        a = p.assets[c["asset"]]
        if not a.get("has_audio", a["kind"] == "audio"):
            return None
        if c["id"] in self.open_input:  # picture clip: its input is already open
            idx = self.open_input[c["id"]]
        else:
            path = str(p.asset_path(c["asset"]))
            idx = self.add_input(["-ss", fmt(c["src_in"]), "-t", fmt(timemap.source_span(c) + 0.05), "-i", path]) - 1
        chain = [f"aresample={SR}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if c.get("reverse"):
            chain.append("areverse")
        chain += atempo(c["speed"])
        if c.get("preset"):
            chain.append(presets.chain_for(c["preset"]))
        vol = keyed(c, "volume_db", "t")
        if vol:
            chain.append(f"volume='pow(10,({vol})/20)':eval=frame")
        else:
            chain.append(f"volume={c.get('volume_db', 0):g}dB")
        dur = c["dur"]
        chain.append(f"atrim=end={fmt(dur)}")
        fi, fo = c.get("afade_in", c["fade_in"]), c.get("afade_out", c["fade_out"])
        if fi > 0:
            chain.append(f"afade=t=in:st=0:d={fmt(fi)}")
        if fo > 0:
            chain.append(f"afade=t=out:st={fmt(max(0.0, dur - fo))}:d={fmt(fo)}")
        delay = round(c["start"] * SR)
        if delay > 0:
            chain.append(f"adelay=delays={delay}S:all=1")
        self.audio.append(f"[{idx}:a]{','.join(chain)}[{label}]")
        return label

    def build_audio(self) -> str:
        p = self.p
        main, duck = [], []
        tracks = {t["id"]: t for t in p.tracks}
        k = 0
        for c in sorted(p.clips, key=lambda c: c["start"]):
            t = tracks[c["track"]]
            if t.get("mute"):
                continue
            if t["kind"] == "video" and c.get("type") != "text" or t["kind"] == "audio":
                k += 1
                lab = self.clip_audio(c, f"a{k}")
                if lab:
                    (duck if t.get("duck") else main).append(lab)

        def bus(labels: list[str], name: str) -> str | None:
            if not labels:
                return None
            if len(labels) == 1:
                self.audio.append(f"[{labels[0]}]anull[{name}]")
            else:
                ins = "".join(f"[{x}]" for x in labels)
                self.audio.append(f"{ins}amix=inputs={len(labels)}:normalize=0:duration=longest:dropout_transition=0[{name}]")
            return name

        m, d = bus(main, "main"), bus(duck, "duckbus")
        if m and d:
            self.audio.append(f"[{m}]asplit=2[bm][bs]")
            self.audio.append(f"[{d}][bs]sidechaincompress=threshold=0.0316:ratio=6:attack=5:release=250:makeup=1[duckd]")
            self.audio.append("[bm][duckd]amix=inputs=2:normalize=0:duration=longest[mix]")
            mix = "mix"
        elif m or d:
            mix = m or d
        else:
            self.audio.append(f"anullsrc=r={SR}:cl=stereo,atrim=duration={fmt(self.total)}[mix]")
            mix = "mix"
        lufs = p.settings.get("master_lufs")
        norm = f"loudnorm=I={fmt(float(lufs))}:TP=-1.5:LRA=11,aresample={SR}," if lufs and (m or d) else ""
        self.audio.append(
            f"[{mix}]{norm}alimiter=limit=0.95:level=disabled,apad=whole_dur={fmt(self.total)},"
            f"atrim=end={fmt(self.total)},asetpts=PTS-STARTPTS[aout]"
        )
        return "aout"

    def build(self) -> tuple[str, str, str | None]:
        """The filter graph and its output labels. Built once, however many times the command is made."""
        if self._built is None:
            vlabel = self.build_video()
            alabel = self.build_audio() if self.want_audio else None
            self._built = (";".join(self.video + self.audio), vlabel, alabel)
        return self._built

    def command(self, out: Path, *, snapshot_at: float | None = None, script_style: str = "new") -> list[str]:
        graph, vlabel, alabel = self.build()
        if len(graph) > MAX_GRAPH:
            raise EditError(f"the project is too large for one render ({len(graph)} characters of filters); render it in parts")
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *self.inputs]
        if len(graph) > INLINE_GRAPH:
            gfile = self.p.out_dir / ".graph.txt"
            gfile.write_text(graph, encoding="utf-8")
            self.used_script = True
            cmd += ["-/filter_complex", str(gfile)] if script_style == "new" else ["-filter_complex_script", str(gfile)]
        else:
            cmd += ["-filter_complex", graph]
        cmd += ["-map", f"[{vlabel}]"]
        if snapshot_at is not None:
            return cmd + ["-ss", fmt(snapshot_at), "-frames:v", "1", str(out)]
        if alabel:
            cmd += ["-map", f"[{alabel}]"]
        crf, preset, ab = ("30", "veryfast", "128k") if self.preview else ("18", "medium", "192k")
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", "-r", f"{self.fps:g}"]
        if alabel:
            cmd += ["-c:a", "aac", "-b:a", ab, "-ar", str(SR)]
        return cmd + ["-t", fmt(self.total), "-movflags", "+faststart", str(out)]

    def execute(self, out: Path, log: Path, on_progress=None, snapshot_at: float | None = None) -> None:
        """Run FFmpeg. A long graph is passed as a file; FFmpeg 7+ spells that -/filter_complex and older ones
        -filter_complex_script, so if the first spelling is not recognised the other is tried."""
        try:
            run(self.command(out, snapshot_at=snapshot_at), self.total, log, on_progress)
        except EditError as exc:
            if not (self.used_script and "Unrecognized option" in str(exc)):
                raise
            run(self.command(out, snapshot_at=snapshot_at, script_style="old"), self.total, log, on_progress)


def run(cmd: list[str], total: float, log: Path, on_progress=None) -> None:
    if not shutil.which("ffmpeg"):
        raise EditError("ffmpeg not found on PATH. Install FFmpeg 5 or newer (https://ffmpeg.org).")
    with open(log, "wb") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        for line in proc.stdout:
            if line.startswith("out_time_us=") and on_progress:
                try:
                    on_progress(min(1.0, int(line.split("=")[1]) / 1e6 / total))
                except ValueError:
                    pass
        code = proc.wait()
    if code:
        tail = log.read_text(errors="replace").strip().splitlines()[-6:]
        raise EditError("ffmpeg failed:\n" + "\n".join(tail))


def render(p: Project, out: Path | None = None, preview: bool = False, on_progress=None) -> Path:
    problems = ops.check(p)
    if problems["errors"]:
        raise EditError("fix these first:\n  " + "\n  ".join(problems["errors"]))
    plan = Plan(p, preview, stabilized=stabilize.prepare(p))
    out = out or p.out_dir / ("preview.mp4" if preview else "final.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    plan.execute(tmp, p.out_dir / ".render.log", on_progress)
    os.replace(tmp, out)
    # Remember which revision this file shows, so the UI can tell when it is out of date.
    (out.parent / f".{out.stem}.json").write_text(json.dumps({"rev": p.data.get("rev", 0)}), encoding="utf-8")
    return out


def snapshot(p: Project, at: float, out: Path | None = None) -> Path:
    """One frame of the edit at time `at`, for looking at the picture."""
    problems = ops.check(p)
    if problems["errors"]:
        raise EditError("fix these first:\n  " + "\n  ".join(problems["errors"]))
    if not 0 <= at <= p.duration():
        raise EditError(f"time {at:g}s is outside the project (0 to {p.duration():g}s)")
    plan = Plan(p, preview=True, want_audio=False, stabilized=stabilize.prepare(p))
    out = out or p.out_dir / f"frame_{str(at).replace('.', '_')}.png"
    plan.execute(out, p.out_dir / ".render.log", snapshot_at=min(at, max(0.0, plan.total - 1 / plan.fps)))
    return out
