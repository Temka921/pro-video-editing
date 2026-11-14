#!/usr/bin/env python3
"""Search the SFX catalog by meaning.

    python3 scripts/sfx_search.py "fast airy whoosh" --max-dur 1.5 --limit 8
    python3 scripts/sfx_search.py --category impact --min-dur 0.5
    python3 scripts/sfx_search.py "button click" --exclude a3f9c012,19be4d70
    python3 scripts/sfx_search.py --stats

Output columns: id, duration, category, where the sound hits (peak), tags, file name.
Use the id in a cue sheet (see references/sfx-placement.md).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sfxlib as L  # noqa: E402


def score_sound(s: dict, query_tokens: list[str]) -> float:
    tags = set(s["tags"])
    name = s["name"].lower()
    score = 0.0
    for t in query_tokens:
        if t in tags:
            score += 2.0
        elif len(t) >= 3 and any(g.startswith(t) or (len(g) >= 4 and t.startswith(g)) for g in tags):
            score += 1.0
        if t in name:
            score += 0.5
        rel = L.related_categories(t)
        if s["category"] in rel:
            score += 1.5
        elif rel and tags & set().union(*(L.VOCAB[c] for c in rel)):
            score += 0.4
    return score


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("query", nargs="?", default="", help="words describing the sound")
    ap.add_argument("--catalog", help="catalog file (default: auto-detect)")
    ap.add_argument("--category", help="only this category (see --stats)")
    ap.add_argument("--kind", choices=["one-shot", "bed"], help="one-shot hits vs long beds/ambience")
    ap.add_argument("--min-dur", type=float, help="minimum duration in seconds")
    ap.add_argument("--max-dur", type=float, help="maximum duration in seconds")
    ap.add_argument("--dur", type=float, help="target duration; closer matches rank higher on ties")
    ap.add_argument("--pack", help="only this pack")
    ap.add_argument("--exclude", default="", help="comma-separated ids to skip (avoid repeating a take)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--stats", action="store_true", help="show what the catalog contains")
    args = ap.parse_args()

    cat = L.load_catalog(args.catalog)
    sounds = cat["sounds"]

    if args.stats:
        cats = Counter(s["category"] for s in sounds)
        total = sum(s["dur"] for s in sounds)
        print(f"{cat['count']} sounds, {total/60:.1f} min, packs: {', '.join(cat['packs'])}")
        print(f"catalog: {cat['_path']}")
        for c, n in cats.most_common():
            durs = sorted(s["dur"] for s in sounds if s["category"] == c)
            print(f"  {c:<11}{n:>6}   median {durs[len(durs)//2]:.2f}s")
        return

    excluded = {x.strip() for x in args.exclude.split(",") if x.strip()}
    qtokens = L.tokens(args.query)
    if not qtokens and not (args.category or args.kind or args.pack):
        L.die("give a query or at least one filter (--category, --kind, --pack). Try --stats.")

    ranked = []
    for s in sounds:
        if s["id"] in excluded:
            continue
        if args.category and s["category"] != args.category:
            continue
        if args.kind and s["kind"] != args.kind:
            continue
        if args.pack and s["pack"] != args.pack:
            continue
        if args.min_dur is not None and s["dur"] < args.min_dur:
            continue
        if args.max_dur is not None and s["dur"] > args.max_dur:
            continue
        sc = score_sound(s, qtokens) if qtokens else 1.0
        if qtokens and sc <= 0:
            continue
        gap = abs(s["dur"] - args.dur) if args.dur else 0.0
        ranked.append((-sc, gap, s["name"], s))
    ranked.sort(key=lambda r: r[:3])
    hits = [r[3] for r in ranked[: args.limit]]

    if args.json:
        keys = ["id", "name", "pack", "category", "kind", "dur", "onset_s", "peak_s", "tail_s", "tags", "description"]
        print(json.dumps([{k: s[k] for k in keys} for s in hits], indent=1))
        return
    if not hits:
        print("no matches. Try broader words, drop a filter, or run --stats to see categories.")
        return
    multi = len(cat["packs"]) > 1
    for s in hits:
        pack = f"[{s['pack']}] " if multi else ""
        print(
            f"{s['id']}  {s['dur']:6.2f}s  {s['category']:<10} peak@{s['peak_s']:.2f}s  "
            f"{' '.join(s['tags'][:6]):<40} {pack}{s['name']}"
        )
    if len(ranked) > len(hits):
        print(f"... {len(ranked) - len(hits)} more (raise --limit or add filters)")


if __name__ == "__main__":
    main()
