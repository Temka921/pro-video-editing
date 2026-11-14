#!/usr/bin/env python3
"""Measure a finished file's loudness and flag problems. Claude cannot listen, so this
is the objective check to run after every render.

    python3 scripts/qc_audio.py output.mp4
    python3 scripts/qc_audio.py output.mp4 --target -14     # web/social
    python3 scripts/qc_audio.py output.mp4 --target -23     # broadcast (EBU R128)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sfxlib as L  # noqa: E402


def measure(path: str) -> dict:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-i", path, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    text = r.stderr
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else ""
    def grab(pattern):
        m = re.search(pattern, summary)
        return float(m.group(1)) if m else None
    return {
        "lufs": grab(r"I:\s+(-?[\d.]+) LUFS"),
        "lra": grab(r"LRA:\s+(-?[\d.]+) LU"),
        "true_peak": grab(r"Peak:\s+(-?[\d.]+) dBFS"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--target", type=float, default=-14.0, help="target integrated loudness in LUFS (default -14)")
    ap.add_argument("--tolerance", type=float, default=2.0, help="allowed distance from target in LU")
    args = ap.parse_args()
    L.require_tools("ffmpeg")

    m = measure(args.file)
    if m["lufs"] is None:
        L.die("could not measure this file (no audio, or silent).")
    print(f"integrated loudness: {m['lufs']:.1f} LUFS (target {args.target:g})")
    print(f"loudness range:      {m['lra']:.1f} LU" if m["lra"] is not None else "loudness range: n/a")
    print(f"true peak:           {m['true_peak']:.1f} dBTP" if m["true_peak"] is not None else "true peak: n/a")
    problems = 0
    if m["true_peak"] is not None and m["true_peak"] > -1.0:
        print("PROBLEM: true peak above -1 dBTP; risk of clipping after encoding. Lower SFX gain or the master.")
        problems += 1
    if abs(m["lufs"] - args.target) > args.tolerance:
        direction = "loud" if m["lufs"] > args.target else "quiet"
        print(f"PROBLEM: {abs(m['lufs'] - args.target):.1f} LU too {direction} for the target.")
        problems += 1
    if m["lra"] is not None and m["lra"] > 15:
        print("NOTE: very wide loudness range; check that loud effects are not overpowering speech.")
    if not problems:
        print("levels OK")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
