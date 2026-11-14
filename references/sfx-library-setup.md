# Setting up a sound-effects library

The skill never ships audio. One command downloads a free default library, or you bring your own. Either way the skill builds a small text catalog so Claude can search by meaning without loading gigabytes of sound.

## Contents

- The default library (one command)
- How it fits together
- Getting sounds (and licenses)
- Building the catalog
- Where files are looked up
- Improving search with hand-written tags
- Using it in Claude Code, the desktop app and the web app
- Sharing a catalog
- Troubleshooting

## The default library (one command)

The default is the Sonniss GDC 2026 Game Audio Bundle: professional sound design, free, in 5 zip parts (7.5 GB, 347 WAV files in total; part 1 alone is 1.3 GB with 82 sounds).

```bash
python3 scripts/setup_library.py --accept-license               # part 1
python3 scripts/setup_library.py --accept-license --parts all   # all 5 parts
python3 scripts/setup_library.py --accept-license --parts 1,3   # a selection
```

What it does:

- Downloads the chosen parts from Sonniss's own server (`downloads.sonniss.com`) into `library/` inside the skill folder. Use `--dir` for another location.
- Skips parts that are already there, so you can also download the zips by hand from https://gdc.sonniss.com/ and drop them into `library/`. With every part present, no download happens and `--accept-license` is not needed.
- Resumes an interrupted download when you run the same command again.
- Builds the catalog from everything in that folder. The pack name is fixed (`sonniss_gdc2026`), so sound ids are the same on every machine and cue sheets can be shared.

Adding parts later: run it again with more `--parts`. The catalog is rebuilt, and ids of sounds you already used do not change.

**License.** The bundle is free but licensed. `--accept-license` means you agree to the Sonniss #GameAudioGDC agreement (linked from https://gdc.sonniss.com/, and included in each zip). In short: free for personal and commercial media projects without attribution; you may not resell or redistribute the sounds as sound effects, original or edited; you may not use them to train AI or machine-learning systems. That is why this repository downloads the bundle from Sonniss and never contains it, and why `library/` is git-ignored.

## How it fits together

```
sounds.zip (yours, any size)  --build_catalog.py-->  catalog.json (text, small)
                                                        |
Claude searches the catalog  <--sfx_search.py-----------+
Claude writes a cue sheet    --render_mix.py--> extracts only the sounds used,
                                                mixes them, deletes the temp copies
```

- Sounds stay packed in the zip or folder. Only the few sounds a project uses are extracted, into a temporary folder that is deleted afterwards.
- The catalog stores, for each sound: id, path, duration, sample rate, channels, where it starts, where its loudest moment is (used for frame-accurate sync), tail length, loudness, a long-versus-short flag, a category and tags derived from file and folder names.

## Getting sounds (and licenses)

Choose a library whose license allows your use. Read the license shipped with every download, because terms differ and change.

| Source | Good for | Watch out for |
|---|---|---|
| Sonniss "GameAudioGDC" yearly bundles (sonniss.com/gameaudiogdc) | Large, professional, royalty-free game and film sound design | Do not redistribute the files themselves; read the current license text |
| Freesound.org, filtered to CC0 | Anything you may redistribute freely | Quality varies; per-sound licenses (CC-BY needs credit) |
| Mixkit, Pixabay | Quick single sounds | Standalone redistribution is not allowed |
| BBC Sound Effects archive | Excellent quality | Free use is limited to personal, educational or research purposes; commercial use needs a paid license |
| Sounds you record yourself | Full control | None |

This skill's own license (MIT) covers the code only. It grants no rights to any sound library.

## Building the catalog

Requirements: Python 3.9+ and FFmpeg (with `ffprobe`) on your PATH.

```bash
# One zip
python3 scripts/build_catalog.py ~/SFX/Sonniss_GDC_2026.zip

# A folder of audio files
python3 scripts/build_catalog.py ~/SFX/MyRecordings/

# A folder that contains several zips (multi-part downloads)
python3 scripts/build_catalog.py ~/SFX/Sonniss_GDC_2024/

# Several packs into one catalog
python3 scripts/build_catalog.py ~/SFX/PackA.zip ~/SFX/PackB/ --out ~/.pro-video-editing/catalog.json
```

Useful options: `--pack NAME` (name for a single source), `--jobs N` (parallel workers, default 4), `--limit N` (index only the first N files, to try it out), `--overrides FILE` (see below), `--out FILE`.

The tool decodes each file once to measure it, then writes the catalog and prints a summary of sounds per category. Large libraries take a while; run a `--limit 50` trial first. Files that cannot be decoded, or are silent, are skipped and listed under `errors` in the catalog.

Check the result:

```bash
python3 scripts/sfx_search.py --stats
python3 scripts/sfx_search.py "deep cinematic impact" --limit 5
```

## Where files are looked up

Catalog (first match wins): `--catalog FILE`, `$SFX_CATALOG`, `~/.pro-video-editing/catalog.json`, `<skill>/catalog/catalog.json`.

Audio: each catalog remembers the path it was built from. If the sounds have moved, or the catalog was built on another machine, point to them:

```bash
python3 scripts/render_mix.py in.mp4 cues.json -o out.mp4 --library /new/path/sounds.zip
# several packs:
python3 scripts/render_mix.py in.mp4 cues.json -o out.mp4 --library PackA=/path/a.zip --library PackB=/path/b
# or set it once:
export SFX_LIBRARY=/new/path/sounds.zip
```

Sound ids are derived from the pack name and the path inside the pack. Renaming files or packs changes the ids, so keep names stable once cue sheets exist.

## Improving search with hand-written tags

Tags come from file and folder names. Libraries with descriptive names work well; cryptic names (`SFX_0381.wav`) end up uncategorized. Add your own:

```json
{
  "a3f9c012": {"tags": ["cinematic", "trailer", "deep"], "category": "impact"},
  "pack/Impacts/Door_Slam_03.wav": {"description": "heavy wooden door slam, dry"}
}
```

Keys are an id or the exact path inside the pack. Then rebuild:

```bash
python3 scripts/build_catalog.py ~/SFX/sounds.zip --overrides overrides.json
```

Categories: whoosh, impact, boom, explosion, riser, transition, ui, glitch, ambience, foley, magic, electric, cartoon, crowd, vehicle, water, animal, voice, other. Add keywords for your own libraries in the `VOCAB` table at the top of `scripts/sfxlib.py`.

## Using it in Claude Code, the desktop app and the web app

- **Claude Code and the desktop app with access to your computer:** work best. Claude runs the scripts where the sounds already are.
- **Web app:** the scripts run in a sandbox that cannot see your disk. Make the catalog and the sounds available in the conversation or skill workspace. Very large zips may exceed upload limits; in that case upload only the sounds a project needs (or a small zip of curated favorites) and build a catalog from that.
- If FFmpeg is missing, each script says so. Nothing else needs installing.

## Sharing a catalog

`catalog/*.json` is excluded from git by default. A catalog lists file names from the library it was built on. File names are factual metadata, but if the library's license or your risk tolerance says otherwise, do not publish it. To publish one anyway, remove the line from `.gitignore` and commit it.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `no SFX catalog found` | Run `setup_library.py` (default library) or `build_catalog.py` (your own sounds) first, or pass `--catalog`. |
| Download stops or fails | Run the same `setup_library.py` command again; it resumes. Or download the zip in a browser from https://gdc.sonniss.com/ and put it in `library/`. |
| `audio for pack 'X' not found on this machine` | The sounds moved. Pass `--library X=PATH` or set `SFX_LIBRARY`. |
| Most sounds are category `other` | File names carry few keywords. Add `--overrides` tags or extend `VOCAB`. |
| `ffmpeg` / `ffprobe` not found | Install FFmpeg 5 or newer and make sure both are on PATH. |
| A sound lands slightly early or late | Check its `peak@` in search output. For swells use `"sync": "onset"` or `"start"`; pitch-shifted cues can drift by about 10 ms. |
| Some files are skipped | Listed under `errors` in the catalog. Usually unsupported or empty files; the rest still work. |
