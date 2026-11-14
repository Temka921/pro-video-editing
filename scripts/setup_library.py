#!/usr/bin/env python3
"""Get the default sound library and index it, in one step.

This repository ships no audio: the sounds' license does not allow redistributing
them. Instead this downloads the free Sonniss GDC 2026 Game Audio Bundle from
Sonniss's own server onto this machine, then builds the search catalog.

    python3 scripts/setup_library.py --accept-license               # part 1: 1.3 GB, 82 sounds
    python3 scripts/setup_library.py --accept-license --parts all   # all 5 parts: 7.5 GB, 347 sounds
    python3 scripts/setup_library.py --accept-license --parts 1,3

Parts already in the library folder are reused, so a zip you downloaded by hand from
https://gdc.sonniss.com/ can simply be dropped into <skill>/library/ first. Interrupted
downloads resume. Any other zips or audio in that folder are indexed too.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sfxlib as L  # noqa: E402

PARTS = 5
NAME = "Sonniss.com-GDC2026-GameAudioBundle{n}of5.zip"
BASE_URL = "https://downloads.sonniss.com/"
PACK = "sonniss_gdc2026"  # fixed, so sound ids are the same on every machine
LICENSE_NOTE = """\
The Sonniss #GameAudioGDC bundle is free, but it is licensed, not public domain.
Full terms: https://gdc.sonniss.com/ ("Read the Licence Agreement"); a copy is inside each zip.
In short:
  - Free for personal and commercial media projects (video, film, games), no attribution.
  - Do not resell or redistribute the sounds as sound effects, original or edited.
  - Do not use them to train AI or machine-learning systems.
Re-run with --accept-license to agree and download."""


def parse_parts(text: str) -> list[int]:
    if text == "all":
        return list(range(1, PARTS + 1))
    try:
        parts = sorted({int(p) for p in text.split(",")})
    except ValueError:
        parts = []
    if not parts or parts[0] < 1 or parts[-1] > PARTS:
        L.die(f"--parts takes numbers from 1 to {PARTS} separated by commas, or 'all'")
    return parts


def download(url: str, dest: Path) -> None:
    """Download url to dest, resuming from dest.part if it exists."""
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "pro-video-editing-setup"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code != 416:  # 416: the .part file is already the whole file
            L.die(f"download failed ({exc.code} {exc.reason}): {url}")
        resp = None
    except OSError as exc:
        L.die(f"download failed ({exc}): {url}\nRun the same command again to resume.")

    if resp is not None:
        with resp:
            if have and resp.status != 206:  # server ignored the range; start over
                have = 0
            remaining = int(resp.headers.get("Content-Length") or 0)
            total = have + remaining
            if remaining > shutil.disk_usage(dest.parent).free:
                L.die(f"not enough free disk space in {dest.parent} ({remaining / 1e9:.1f} GB needed)")
            print(f"downloading {dest.name} ({total / 1e9:.2f} GB)", file=sys.stderr)
            shown = -1
            try:
                with open(part, "ab" if have else "wb") as out:
                    while chunk := resp.read(1 << 20):
                        out.write(chunk)
                        have += len(chunk)
                        pct = have * 10 // total if total else 0
                        if pct != shown:
                            shown = pct
                            print(f"  {pct * 10}%", file=sys.stderr)
            except OSError as exc:
                L.die(f"download interrupted ({exc}). Run the same command again to resume.")
            if total and have != total:
                L.die("download ended early. Run the same command again to resume.")

    if not zipfile.is_zipfile(part):
        part.unlink()
        L.die(f"{dest.name} did not download as a valid zip; the partial file was removed. Try again.")
    part.replace(dest)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--parts", default="1", help=f"which of the {PARTS} parts: e.g. 1 (default), 1,2 or all")
    ap.add_argument("--accept-license", action="store_true", help="agree to the Sonniss license (needed to download)")
    ap.add_argument("--dir", help="where the zips live (default: <skill>/library)")
    ap.add_argument("--catalog", help="catalog file to write (default ~/.pro-video-editing/catalog.json)")
    args = ap.parse_args()

    L.require_tools("ffmpeg", "ffprobe")
    lib_dir = Path(args.dir).expanduser() if args.dir else L.SKILL_ROOT / "library"
    lib_dir.mkdir(parents=True, exist_ok=True)

    names = [NAME.format(n=n) for n in parse_parts(args.parts)]
    missing = [n for n in names if not zipfile.is_zipfile(lib_dir / n)]
    if missing and not args.accept_license:
        L.die(f"{len(missing)} part(s) need downloading.\n\n{LICENSE_NOTE}")
    for name in names:
        if name in missing:
            download(BASE_URL + name, lib_dir / name)
        else:
            print(f"already have {name}", file=sys.stderr)

    cmd = [sys.executable, str(Path(__file__).with_name("build_catalog.py")), str(lib_dir), "--pack", PACK]
    if args.catalog:
        cmd += ["--out", args.catalog]
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()
