#!/usr/bin/env python3
"""Copy chosen sounds out of the library (zip or folder) into a working folder.

    python3 scripts/sfx_fetch.py a3f9c012 19be4d70 --out ./sfx_work
    python3 scripts/sfx_fetch.py a3f9c012 --out ./sfx_work --convert   # 48 kHz 24-bit WAV

Only the sounds you ask for are extracted; the rest of the library stays packed.
Delete the output folder when the project is finished.
(render_mix.py extracts what it needs by itself; use this for manual work.)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sfxlib as L  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("ids", nargs="+", help="sound ids from sfx_search.py")
    ap.add_argument("--out", required=True, help="folder to write into (created if missing)")
    ap.add_argument("--convert", action="store_true", help="convert to 48 kHz 24-bit stereo WAV")
    ap.add_argument("--catalog")
    ap.add_argument("--library", action="append", help="PATH or PACK=PATH (repeatable)")
    args = ap.parse_args()

    cat = L.load_catalog(args.catalog)
    libs = L.Libraries(cat, args.library)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    if args.convert:
        L.require_tools("ffmpeg")

    for sid in args.ids:
        s = cat["_by_id"].get(sid)
        if not s:
            L.die(f"unknown id: {sid}")
        raw = libs.get(s["pack"]).extract(s.get("container", ""), s["path"], out, f"{sid}_{s['name']}{Path(s['path']).suffix.lower()}")
        if args.convert:
            wav = raw.with_suffix(".wav") if raw.suffix.lower() != ".wav" else raw.with_name(raw.stem + "_48k.wav")
            r = subprocess.run(
                ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(raw), "-ar", "48000", "-ac", "2",
                 "-c:a", "pcm_s24le", str(wav)], capture_output=True, text=True)
            if r.returncode:
                L.die(r.stderr.strip())
            raw.unlink()
            raw = wav
        print(raw)


if __name__ == "__main__":
    main()
