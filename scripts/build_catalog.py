#!/usr/bin/env python3
"""Index a sound-effects library into a searchable catalog.json.

The catalog holds only text and numbers (names, tags, durations, where each sound
hits). It never contains audio. Build it once on the machine that has the sounds.

    python3 scripts/build_catalog.py ~/SFX/Sonniss_GDC_2026.zip
    python3 scripts/build_catalog.py ~/SFX/Pack1.zip ~/SFX/Pack2/ --out my_catalog.json

SOURCE can be a .zip, a folder of audio files, or a folder that contains .zip files
(multi-part downloads). Default output: ~/.pro-video-editing/catalog.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sfxlib as L  # noqa: E402


def build_description(s: dict) -> str:
    words = " ".join(s["tags"][:8])
    return (
        f"{s['category']}: {words} "
        f"({s['dur']:.2f}s {s['kind']}, hits at {s['peak_s']:.2f}s)"
    ).strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("sources", nargs="+", help="zip file, folder of audio, or folder of zips")
    ap.add_argument("--pack", help="pack name (only with a single source; default: source name)")
    ap.add_argument("--out", help="output file (default ~/.pro-video-editing/catalog.json)")
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 2), help="parallel workers")
    ap.add_argument("--overrides", help="JSON file with hand-written tags/category/description")
    ap.add_argument("--limit", type=int, help="index only the first N files per source (testing)")
    ap.add_argument("--max-seconds", type=int, default=180, help="analyze at most this much audio per file")
    args = ap.parse_args()

    L.require_tools("ffmpeg", "ffprobe")
    out_path = Path(args.out).expanduser() if args.out else L.USER_DIR / "catalog.json"
    overrides = {}
    if args.overrides:
        overrides = json.loads(Path(args.overrides).expanduser().read_text(encoding="utf-8"))

    packs: dict[str, dict] = {}
    sounds: list[dict] = []
    errors: list[dict] = []
    seen_ids: set[str] = set()
    t0 = time.time()

    for src in args.sources:
        root = Path(src).expanduser()
        if not root.exists():
            L.die(f"source not found: {root}")
        name = args.pack if (args.pack and len(args.sources) == 1) else L.slug(root.stem if root.is_file() else root.name)
        if name in packs:
            L.die(f"two sources resolve to the pack name '{name}'; use --pack with one source at a time")
        lib = L.Library(root)
        entries = list(lib.entries())
        if args.limit:
            entries = entries[: args.limit]
        print(f"[{name}] {len(entries)} audio files in {root}", file=sys.stderr)

        def work(entry, lib=lib, name=name):
            container, path = entry
            try:
                with tempfile.TemporaryDirectory() as td:
                    f = lib.extract(container, path, td, "in" + Path(path).suffix.lower())
                    info = L.probe_audio(f)
                    env = L.analyze_envelope(f, args.max_seconds)
                return entry, info, env, None
            except Exception as exc:  # keep going; report at the end
                return entry, None, None, str(exc)[:200]

        done = 0
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            for entry, info, env, err in ex.map(work, entries):
                done += 1
                if done % 50 == 0:
                    print(f"  {done}/{len(entries)}", file=sys.stderr)
                container, path = entry
                if err or env.get("silent") or info["dur"] <= 0:
                    errors.append({"pack": name, "path": path, "error": err or "silent or empty"})
                    continue
                tags = L.tags_for(path)
                s = {
                    "id": "",
                    "pack": name,
                    "path": path,
                    "dur": round(info["dur"], 3),
                    "sr": info["sr"],
                    "ch": info["ch"],
                    "bits": info["bits"],
                    "lead_s": round(env["lead_s"], 3),
                    "onset_s": round(env["onset_s"], 3),
                    "peak_s": round(env["peak_s"], 3),
                    "tail_s": round(env["tail_s"], 3),
                    "peak_dbfs": round(env["peak_dbfs"], 1),
                    "rms_dbfs": round(env["rms_dbfs"], 1),
                    "kind": L.classify_kind(info["dur"], env["peak_dbfs"], env["rms_dbfs"]),
                    "category": L.categorize(tags),
                    "tags": tags,
                }
                if container:
                    s["container"] = container
                length = 8
                sid = L.sound_id(name, container, path, length)
                while sid in seen_ids:
                    length += 2
                    sid = L.sound_id(name, container, path, length)
                seen_ids.add(sid)
                s["id"] = sid
                ov = overrides.get(sid) or overrides.get(path)
                if ov:
                    if "tags" in ov:
                        s["tags"] = list(dict.fromkeys(list(ov["tags"]) + s["tags"]))[:24]
                    if "category" in ov:
                        s["category"] = ov["category"]
                s["description"] = (ov or {}).get("description") or build_description(s)
                sounds.append(s)

        packs[name] = {"type": lib.kind, "path": str(root.resolve()), "files": len(entries)}

    if not sounds:
        L.die("no usable audio found. Check the source path and that FFmpeg can read the files.")

    sounds.sort(key=lambda s: (s["pack"], s["path"]))
    catalog = {
        "version": 1,
        "generator": "pro-video-editing/build_catalog",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "packs": packs,
        "count": len(sounds),
        "errors": errors[:50],
        "sounds": sounds,
    }
    L.write_catalog(out_path, catalog)

    cats = Counter(s["category"] for s in sounds)
    total = sum(s["dur"] for s in sounds)
    print(f"\nWrote {out_path}", file=sys.stderr)
    print(f"{len(sounds)} sounds, {total/60:.1f} min total, {len(errors)} skipped, {time.time()-t0:.0f}s", file=sys.stderr)
    print("By category: " + ", ".join(f"{c} {n}" for c, n in cats.most_common()), file=sys.stderr)
    if cats.get("other", 0) > len(sounds) * 0.4:
        print(
            "Note: many sounds are uncategorized because their file names carry few keywords.\n"
            "Search still works on names; add hand-written tags with --overrides (see references/sfx-library-setup.md).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
