"""Measure a media file so an AI (which cannot watch or listen) can pick cut points."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .project import EditError, probe_media, r4


def measure_frame(png: Path) -> dict:
    """Brightness and color numbers for one image, with plain-language hints."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(png),
                        "-vf", "signalstats,metadata=print", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    v = {k: float(x) for k, x in re.findall(r"lavfi\.signalstats\.(\w+)=(-?[\d.]+)", r.stderr)}
    if "YAVG" not in v:
        raise EditError("could not measure the frame:\n" + "\n".join(r.stderr.strip().splitlines()[-3:]))
    out = {
        "luma_avg": round(v["YAVG"], 1), "luma_min": v["YMIN"], "luma_max": v["YMAX"],
        "luma_p10": v.get("YLOW"), "luma_p90": v.get("YHIGH"),
        "saturation_avg": round(v.get("SATAVG", 0), 1),
        "chroma_u_avg": round(v["UAVG"], 1), "chroma_v_avg": round(v["VAVG"], 1),
    }
    hints = []
    if out["luma_avg"] < 55:
        hints.append("dark overall (average luma under 55 of 255)")
    elif out["luma_avg"] > 185:
        hints.append("bright overall (average luma over 185 of 255)")
    if out["luma_max"] >= 234:  # 235 is full white in video range
        hints.append("highlights reach white; detail may be clipped")
    if out["luma_min"] <= 18:
        hints.append("blacks reach the bottom; shadow detail may be crushed")
    if out["luma_p90"] is not None and out["luma_p10"] is not None and out["luma_p90"] - out["luma_p10"] < 60:
        hints.append("low contrast (the bulk of the picture spans a narrow brightness range)")
    if out["saturation_avg"] < 15:
        hints.append("nearly colorless")
    if out["chroma_v_avg"] - 128 > 6 and 128 - out["chroma_u_avg"] > 3:
        hints.append("leans warm (red/orange)")
    elif 128 - out["chroma_v_avg"] > 6 and out["chroma_u_avg"] - 128 > 3:
        hints.append("leans cool (blue/cyan)")
    out["hints"] = hints or ["nothing unusual in the averages"]
    out["note"] = "Averages over the whole frame. Look at the frame too; numbers cannot judge skin tones or taste."
    return out


def analyze(path: Path, silence_db: float = -35.0, min_silence: float = 0.4, scene: float = 0.35) -> dict:
    path = Path(path).expanduser()
    if not path.is_file():
        raise EditError(f"file not found: {path}")
    info = probe_media(path)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path)]
    if info["kind"] == "video":
        cmd += ["-vf", f"select='gt(scene,{scene})',showinfo"]
    elif info["kind"] == "image":
        raise EditError("images have no timeline to analyze")
    if info["has_audio"]:
        cmd += ["-af", f"silencedetect=noise={silence_db}dB:d={min_silence}"]
    cmd += ["-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise EditError("ffmpeg could not analyze the file:\n" + "\n".join(r.stderr.strip().splitlines()[-4:]))
    log = r.stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    silences = []
    for i, s in enumerate(starts):
        silences.append({"start": r4(max(0.0, s)), "end": r4(ends[i]) if i < len(ends) else info["duration"]})
    scenes = [r4(float(x)) for x in re.findall(r"pts_time:([\d.]+)", log)]
    out = {"file": path.name, **info, "silences": silences, "scene_changes": scenes}
    if info["has_audio"]:
        speech, cur = [], 0.0
        for s in silences + [{"start": info["duration"], "end": info["duration"]}]:
            if s["start"] - cur > 0.15:
                speech.append({"start": r4(cur), "end": s["start"]})
            cur = s["end"]
        out["sound_between_silences"] = speech
    return out
