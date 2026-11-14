#!/usr/bin/env python3
"""Mix sound-effect cues onto a video with frame-accurate sync.

    python3 scripts/render_mix.py input.mp4 cues.json -o output.mp4
    python3 scripts/render_mix.py input.mp4 cues.json --check          # lint only
    python3 scripts/render_mix.py input.mp4 cues.json --dry-run        # show the plan + FFmpeg command
    python3 scripts/render_mix.py input.mp4 cues.json --sfx-only fx.wav  # SFX stem, no video

The video stream is copied untouched. Audio is rebuilt: original audio + SFX bus,
with the SFX ducked under dialogue and a limiter at the end.

Cue sheet (JSON):
{
  "fps": 25,                      optional; read from the video if omitted
  "default_gain_db": -9,          level for cues that do not set gain_db
  "duck": true,                   lower SFX while the original audio is loud
  "humanize": true,               tiny gain/pitch changes when the same sound repeats
  "cues": [
    {"id": "a3f9c012", "at": 2.50, "sync": "peak", "gain_db": -6,
     "fade_in": 0.002, "fade_out": 0.01, "trim_start": 0, "duration": null,
     "pitch_semitones": 0, "note": "whoosh into the title card"}
  ]
}
"at" is the video time (seconds) where the sound's sync point must land.
Use "at_frame" instead of "at" to place by frame number.
"sync": "peak" (default) lands the loudest moment on "at"; "onset" lands the attack;
"start" lands the first sample.
"""
from __future__ import annotations

import argparse
import json
import random
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sfxlib as L  # noqa: E402

SR = 48000
SYNC_MODES = ("peak", "onset", "start")


def probe_video(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,r_frame_rate", "-of", "json", path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        L.die(f"cannot read {path}: {r.stderr.strip()}")
    j = json.loads(r.stdout)
    streams = j.get("streams", [])
    vs = next((s for s in streams if s["codec_type"] == "video"), None)
    if vs is None:
        L.die("input has no video stream")
    num, _, den = vs["r_frame_rate"].partition("/")
    fps = float(num) / float(den) if den and float(den) else None
    return {
        "dur": float(j["format"]["duration"]),
        "fps": fps,
        "has_audio": any(s["codec_type"] == "audio" for s in streams),
    }


def plan_cues(sheet: dict, catalog: dict, vid: dict):
    """Turn the cue sheet into concrete trims/delays. Returns (plans, warnings, errors)."""
    warnings, errors, plans = [], [], []
    fps = sheet.get("fps") or vid["fps"]
    default_gain = float(sheet.get("default_gain_db", -9.0))
    humanize = bool(sheet.get("humanize", True))
    last_seen: dict[str, float] = {}
    count_seen: dict[str, int] = {}

    for i, cue in enumerate(sheet.get("cues", [])):
        tag = f"cue {i + 1}"
        sound = catalog["_by_id"].get(cue.get("id", ""))
        if not sound:
            errors.append(f"{tag}: unknown sound id '{cue.get('id')}'")
            continue
        if "at_frame" in cue:
            if not fps:
                errors.append(f"{tag}: at_frame needs an fps (set 'fps' in the sheet)")
                continue
            at = float(cue["at_frame"]) / fps
        elif "at" in cue:
            at = float(cue["at"])
            if fps and sheet.get("snap", True):
                at = round(at * fps) / fps
        else:
            errors.append(f"{tag}: needs 'at' or 'at_frame'")
            continue
        if at < 0 or at > vid["dur"]:
            errors.append(f"{tag}: time {at:.3f}s is outside the video (0 to {vid['dur']:.3f}s)")
            continue

        sync = cue.get("sync", "peak")
        if sync not in SYNC_MODES:
            errors.append(f"{tag}: sync must be one of {SYNC_MODES}")
            continue
        trim = float(cue.get("trim_start", 0.0))
        if sync == "start":
            sync_off = 0.0
        else:
            sync_off = (sound["peak_s"] if sync == "peak" else sound["onset_s"]) - trim
            if sync_off < 0:
                warnings.append(f"{tag}: trim_start removes the sync point; landing the first kept sample instead")
                sync_off = 0.0
        start = at - sync_off
        head = 0.0
        if start < 0:
            head, start = -start, 0.0
        t_from = trim + head
        avail = sound["dur"] - t_from
        if avail <= 0.02:
            errors.append(f"{tag}: sound '{sound['name']}' ends before the sync point can land at {at:.2f}s")
            continue
        length = avail
        if cue.get("duration"):
            length = min(avail, float(cue["duration"]))

        gain = float(cue.get("gain_db", default_gain))
        pitch = float(cue.get("pitch_semitones", 0.0))
        sid = sound["id"]
        if sid in last_seen and at - last_seen[sid] < 8.0:
            warnings.append(
                f"{tag}: same sound '{sound['name']}' used again {at - last_seen[sid]:.1f}s after its last use; "
                "pick a different take for variety"
            )
        if humanize and sid in count_seen:
            rng = random.Random(f"{sid}:{i}")
            gain += rng.uniform(-1.0, 1.0)
            pitch += rng.uniform(-0.3, 0.3)
        last_seen[sid] = at
        count_seen[sid] = count_seen.get(sid, 0) + 1
        if gain > 0:
            warnings.append(f"{tag}: gain_db is above 0 dB; SFX should sit under the picture's own audio")
        if abs(pitch) > 12:
            errors.append(f"{tag}: pitch_semitones must be within +/-12")
            continue

        fi = min(float(cue.get("fade_in", 0.002)), length / 2)
        fo = min(float(cue.get("fade_out", 0.01)), length / 2)
        plans.append({
            "sound": sound, "at": at, "start": start, "t_from": t_from, "length": length,
            "gain": gain, "pitch": pitch, "fi": fi, "fo": fo, "sync": sync,
            "note": cue.get("note", ""), "i": i + 1,
        })

    # Density / layering checks.
    if plans:
        span = max(vid["dur"], 1.0)
        per10 = len(plans) / span * 10
        if per10 > 12:
            warnings.append(f"{len(plans)} cues in {span:.0f}s ({per10:.0f} per 10s) is busy; do the most with the least")
        for p in plans:
            layered = [q for q in plans if abs(q["at"] - p["at"]) < 0.3]
            if len(layered) > 3:
                warnings.append(f"{len(layered)} cues stacked near {p['at']:.2f}s; keep layers to 3 or fewer")
                break
    return plans, warnings, errors


def build_filter(plans, vid, sheet, stem_only: bool) -> str:
    g = []
    for k, p in enumerate(plans, start=1):
        chain = [
            f"atrim=start={p['t_from']:.5f}:duration={p['length']:.5f}",
            "asetpts=PTS-STARTPTS",
            f"aresample={SR}",
            "aformat=sample_fmts=fltp:channel_layouts=stereo",
        ]
        if abs(p["pitch"]) > 1e-3:
            r = 2 ** (p["pitch"] / 12)
            chain += [f"asetrate={SR * r:.3f}", f"aresample={SR}", f"atempo={1 / r:.6f}"]
        chain.append(f"volume={p['gain']:.2f}dB")
        if p["fi"] > 0:
            chain.append(f"afade=t=in:st=0:d={p['fi']:.4f}")
        if p["fo"] > 0:
            chain.append(f"afade=t=out:st={max(p['length'] - p['fo'], 0):.4f}:d={p['fo']:.4f}")
        delay = round(p["start"] * SR)
        if delay > 0:
            chain.append(f"adelay=delays={delay}S:all=1")
        g.append(f"[{k}:a]{','.join(chain)}[c{k}]")

    n = len(plans)
    labels = "".join(f"[c{k}]" for k in range(1, n + 1))
    if n == 1:
        g.append("[c1]anull[sfx]")
    else:
        g.append(f"{labels}amix=inputs={n}:normalize=0:duration=longest:dropout_transition=0[sfx]")

    if stem_only:
        # Pad to the video length; the output is cut to length with -t. Do not use
        # atrim=duration here: after a pitch-shifted cue (atempo) it cuts at the wrong time.
        g.append(f"[sfx]apad=whole_dur={vid['dur']:.4f}[stem]")
        return ";".join(g)

    if vid["has_audio"]:
        g.append(f"[0:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo[base]")
    else:
        g.append(f"anullsrc=r={SR}:cl=stereo,atrim=duration={vid['dur']:.4f}[base]")

    duck = bool(sheet.get("duck", True)) and vid["has_audio"]
    if duck:
        thr = 10 ** (float(sheet.get("duck_threshold_db", -30)) / 20)
        ratio = float(sheet.get("duck_ratio", 6))
        g.append("[base]asplit=2[bm][bs]")
        g.append(
            f"[sfx][bs]sidechaincompress=threshold={thr:.5f}:ratio={ratio:g}:attack=5:release=250:makeup=1[sfxd]"
        )
        mix_in = "[bm][sfxd]"
    else:
        g.append("[base]anull[bm]")
        mix_in = "[bm][sfx]"
    g.append(f"{mix_in}amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95:level=disabled[aout]")
    return ";".join(g)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("cues", help="cue sheet JSON")
    ap.add_argument("-o", "--output", help="output video file")
    ap.add_argument("--sfx-only", metavar="WAV", help="write only the SFX stem (24-bit WAV)")
    ap.add_argument("--check", action="store_true", help="validate the cue sheet and stop")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and FFmpeg command, do not render")
    ap.add_argument("--catalog")
    ap.add_argument("--library", action="append", help="path to the sounds (PATH, or PACK=PATH; repeatable)")
    args = ap.parse_args()

    L.require_tools("ffmpeg", "ffprobe")
    if not (args.output or args.sfx_only or args.check or args.dry_run):
        L.die("choose one of: -o OUTPUT, --sfx-only WAV, --check, --dry-run")
    if args.output and Path(args.output).resolve() == Path(args.video).resolve():
        L.die("output must be a different file from the input")

    sheet = json.loads(Path(args.cues).read_text(encoding="utf-8"))
    catalog = L.load_catalog(args.catalog)
    vid = probe_video(args.video)
    plans, warnings, errors = plan_cues(sheet, catalog, vid)

    print(f"video: {vid['dur']:.2f}s, {vid['fps'] and round(vid['fps'], 3)} fps, audio: {'yes' if vid['has_audio'] else 'no'}")
    print(f"{'#':>3} {'at':>8} {'starts':>8}  {'sync':<6} {'gain':>6}  sound")
    for p in plans:
        print(f"{p['i']:>3} {p['at']:>8.3f} {p['start']:>8.3f}  {p['sync']:<6} {p['gain']:>5.1f}  {p['sound']['name']}  {p['note']}")
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    if errors:
        sys.exit(2)
    if not plans:
        L.die("the cue sheet has no cues")
    if args.check:
        print("cue sheet OK")
        return

    stem_only = bool(args.sfx_only)
    graph = build_filter(plans, vid, sheet, stem_only)

    with tempfile.TemporaryDirectory(prefix="sfxmix_") as td:
        libs = L.Libraries(catalog, args.library) if not args.dry_run else None
        inputs, extracted = [], {}
        for k, p in enumerate(plans, start=1):
            s = p["sound"]
            if args.dry_run:
                inputs += ["-i", f"<{s['name']}>"]
                continue
            if s["id"] not in extracted:
                lib = libs.get(s["pack"])
                extracted[s["id"]] = lib.extract(s.get("container", ""), s["path"], td, f"{s['id']}{Path(s['path']).suffix.lower()}")
            inputs += ["-i", str(extracted[s["id"]])]

        base = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
        if stem_only:
            # input 0 is the video so that cue inputs keep the numbering [1:a], [2:a], ...
            cmd = base + ["-i", args.video] + inputs + [
                "-filter_complex", graph, "-map", "[stem]", "-t", f"{vid['dur']:.4f}",
                "-c:a", "pcm_s24le", args.sfx_only,
            ]
        else:
            ext = Path(args.output).suffix.lower() if args.output else ".mp4"
            acodec = ["-c:a", "pcm_s24le"] if ext == ".mov" else ["-c:a", "aac", "-b:a", "256k"]
            tail = ["-movflags", "+faststart"] if ext in (".mp4", ".m4v", ".mov") else []
            cmd = base + ["-i", args.video] + inputs + [
                "-filter_complex", graph, "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
                *acodec, *tail, args.output or "output.mp4",
            ]
        if args.dry_run:
            print("\n" + shlex.join(cmd))
            return
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            L.die("FFmpeg failed:\n" + r.stderr.strip()[-1500:])
    print(f"wrote {args.sfx_only or args.output}")


if __name__ == "__main__":
    main()
