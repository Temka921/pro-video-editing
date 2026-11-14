---
name: pro-video-editing
description: Edit video like a professional editor and add sound effects from a local SFX library. Use when cutting, pacing, restructuring, reviewing or exporting video; when choosing cut points; when adding, syncing or mixing sound effects (whooshes, impacts, risers, UI sounds, ambience); or when preparing proxies, color, codecs and delivery. Includes vedit, a simple timeline editor you drive from the command line (import, cut, keyframes, color grading and looks, transitions, speed ramps, reverse and freeze frames, masks, motion tracking, stabilization, titles and captions, green screen, sound effects, voice presets from Premiere .prfpset files, loudness, render, and a browser editor for the person to steer), plus scripts that index a sound library, search it by meaning, and render frame-accurate SFX cues onto video with FFmpeg.
---

# Pro Video Editing

This skill has three parts that work together:

1. **Editorial judgment**: what to cut, where, and why, so the result feels made by a person with taste and not assembled by a machine.
2. **The editor (`vedit`)**: a small timeline editor you operate entirely from the command line. Cuts, titles, fades, sound effects and voice presets all go through it, and the person can open the same project in a browser.
3. **Sound tools**: index a sound-effects library, search it, and place sounds so the hit lands on the exact frame.

Paths below are relative to this skill's folder. The tools need Python 3.9+ and FFmpeg 5+ (nothing else to install).

## Be honest about what you can and cannot do

- You cannot hear audio or watch video. Judge by measurement (transcripts, scene and silence detection, `scripts/qc_audio.py`) and say so. After rendering, name two or three timestamps for the user to check by ear.
- Never invent sound file names or ids. Only use ids returned by `scripts/sfx_search.py`.
- If `scripts/sfx_search.py --stats` says no catalog exists, the sound library is not set up yet. Offer the one-time setup below and wait for a yes before downloading. If the user declines or it fails, do the editorial work anyway and point them to `references/sfx-library-setup.md`. Never download any other sound library yourself.

## Core principles (the short version)

Full text with examples: `references/editorial-principles.md`. Read it before cutting or reviewing a cut.

1. **Emotion first.** Ask what the viewer should feel at each point. If they feel it throughout, the edit works.
2. **Cut hierarchy.** When a cut cannot satisfy everything, give up items from the bottom of this list first: emotion, story, rhythm, eye-trace, planarity, spatial continuity. Never trade a higher item for a lower one.
3. **Cut where a thought ends**, the moment a viewer would blink. A cut point should be a possible blink point.
4. **Do the most with the least.** Suggestion beats exposition. Fewer, better choices. Not cutting is a decision too.
5. **Judge only what is on screen.** Ignore effort, cost and intent.
6. **Misdirect attention** like a magician: decide where the eye goes, and make the next shot meet it.
7. **Diagnose before you fix.** A problem is often earlier than where viewers say they felt it.

## Workflow

| Stage | What to do | Reference |
|---|---|---|
| 1. Intake | Confirm deliverable (platform, aspect, fps, length) and the intent in one sentence. | `technical-pipeline.md` |
| 2. Log | Index all footage: transcript with word times, shot type, silences, best takes. Pick a representative frame per setup. | `editorial-principles.md` |
| 3. Assemble | Build the story in order with the best material. Structure before polish. | `editorial-principles.md` |
| 4. Refine | Apply the cut hierarchy and blink timing. Revisit rejected footage after the first full pass. | `editorial-principles.md` |
| 5. Sound | Spot, search, place and mix SFX. | `sfx-placement.md` |
| 6. Check | Watch straight through as an audience. Run the QC script. | below |
| 7. Deliver | Relink to full quality, match color/levels, export a master first. | `technical-pipeline.md` |

When handing back work, state: the intent you edited toward, main structural choices, what you removed and why, known compromises, and alternatives worth trying.

## The editor (vedit)

Use it to actually make the edit. Read `references/editor.md` first; it has every command, the project format and a working method. The essentials:

```bash
python3 editor/vedit.py new myfilm --size 1920x1080          # a project folder (use `python` on Windows if python3 is missing)
python3 editor/vedit.py -p myfilm settings --preset vertical # canvas for the destination (Reels/TikTok/Shorts); also --lufs -14
python3 editor/vedit.py -p myfilm analyze clip.mp4           # silences and scene changes, to choose cuts
python3 editor/vedit.py -p myfilm import clip.mp4            # prints an asset id such as a1
python3 editor/vedit.py -p myfilm add a1 --in 12.5 --dur 8   # put 8 s of it on the timeline
python3 editor/vedit.py -p myfilm grade c1 --look cinematic  # color; also --brightness --contrast --saturation --temperature --lut FILE
python3 editor/vedit.py -p myfilm key c1 scale --at 0 --value 1; python3 editor/vedit.py -p myfilm key c1 scale --at 8 --value 1.15 --ease inout   # slow push-in
python3 editor/vedit.py -p myfilm transition c1 c2 --type dissolve --dur 0.5
python3 editor/vedit.py -p myfilm key c1 speed --at 0 --value 1   # speed ramp: add more keyframes on `speed` (the clip's own sound is muted); also: reverse c1, freeze c1 --at 4 --dur 2
python3 editor/vedit.py -p myfilm tracker add c1 --at 4 --x 0.6 --y 0.3   # follow a point; then: mask c1 --effect blur, follow c1 m1 t1 (blur a face) or attach c2 t1 (a title rides on it)
python3 editor/vedit.py -p myfilm stabilize c1               # smooth camera shake (needs FFmpeg with libvidstab)
python3 editor/vedit.py -p myfilm captions subtitles.srt     # one text clip per subtitle
python3 editor/vedit.py -p myfilm sfx a3f9c012 --at 8.0      # sound id from scripts/sfx_search.py; hit lands on 8.0 s
python3 editor/vedit.py -p myfilm set c1 preset="voice enhancer"
python3 editor/vedit.py -p myfilm check                      # fix every error it reports
python3 editor/vedit.py -p myfilm render --preview           # fast; then `frame --at T` and look at the PNG
python3 editor/vedit.py -p myfilm measure --at 4             # brightness/color numbers, to judge exposure and grade
python3 editor/vedit.py -p myfilm serve                      # let the person open and adjust it in the browser
```

- You have full control: every command prints JSON, errors are plain, `batch` applies several edits as one change, and `undo` reverts one. Keyframes can animate zoom, position, rotation, opacity, color, volume and speed; `editor.md` lists the rest (masks, tracking, green screen, crop, flip, track mute and hide, canvas presets).
- Look at the picture with `frame --at T` instead of guessing, judge color with `measure`, and judge sound by measurement, not by claiming it sounds good.
- Use restraint: a gentle look, one transition style, a few slow moves. If removing an effect loses nothing, remove it.
- Tracking and masks are measured, not seen: after `tracker add`, check `found` and look at frames with `frame --at T`. Tracking suits rigid, textured things with a stable size; it loses targets that turn or hide.
- The person may change the timeline in the browser while you work. Run `show` before editing after they say they did.
- Voice presets (`phone call`, `deeper voice`, `robot voice`, ...) come from the bundled Premiere `.prfpset` and can be extracted from any other with `presets import FILE.prfpset`. They approximate Premiere's effects; see `references/voice-presets.md`.
- If no sound library is set up, `sfx` fails with a clear message; offer the SFX setup below.

## SFX toolkit

All scripts print `--help`. They use the standard library and FFmpeg only.

**One-time setup.** The skill ships no audio, because the sounds' license forbids redistributing them. The default library is the free Sonniss GDC 2026 Game Audio Bundle, downloaded from Sonniss's own server. Before running this, tell the user the size (part 1 is 1.3 GB with 82 sounds; all 5 parts are 7.5 GB with 347 sounds) and the license in one line (free for personal and commercial media projects, no attribution; no reselling or redistributing the sounds; no AI training), and get their yes:

```bash
python3 scripts/setup_library.py --accept-license               # part 1
python3 scripts/setup_library.py --accept-license --parts all   # everything
```

It saves the zips in `library/` inside this skill, resumes interrupted downloads, reuses parts already there, and then builds the catalog. On Windows use `python` if `python3` is not found.

To use the user's own sounds instead (or as well), index them directly (see `references/sfx-library-setup.md`):

```bash
python3 scripts/build_catalog.py /path/to/sounds.zip        # or a folder, or a folder of zips
```

Either way the result is a text-only catalog at `~/.pro-video-editing/catalog.json`. It contains no audio.

**Per project:**

```bash
python3 scripts/sfx_search.py --stats                       # what is available
python3 scripts/sfx_search.py "fast airy whoosh" --max-dur 1.5 --limit 8
python3 scripts/sfx_search.py "button click" --exclude ID1,ID2    # avoid repeating a take

python3 scripts/render_mix.py in.mp4 cues.json --check      # lint the cue sheet
python3 scripts/render_mix.py in.mp4 cues.json -o out.mp4   # render
python3 scripts/render_mix.py in.mp4 cues.json --sfx-only fx.wav   # SFX stem for a DAW or colorist

python3 scripts/qc_audio.py out.mp4 --target -14            # loudness and true-peak check
```

Search output columns: `id`, duration, category, `peak@` (where the sound hits), tags, file name. Use `peak@` to understand what will line up with the cut.

### Cue sheet

```json
{
  "default_gain_db": -9,
  "duck": true,
  "humanize": true,
  "cues": [
    {"id": "a3f9c012", "at": 2.50, "sync": "peak", "note": "whoosh into the title card"},
    {"id": "19be4d70", "at": 2.50, "gain_db": -12, "note": "soft layer under it"}
  ]
}
```

- `at` is the video time where the sound's **sync point** must land. `at_frame` places by frame number.
- `sync`: `peak` (default) lands the loudest moment, `onset` lands the attack, `start` lands the first sample. The tool computes the start time for you, so the hit is frame-accurate even when the sound begins before the cut.
- Optional per cue: `gain_db`, `fade_in`, `fade_out`, `trim_start`, `duration`, `pitch_semitones` (within 12), `note`.
- `duck` lowers SFX under the original audio (dialogue). `humanize` adds tiny gain and pitch variation when the same sound repeats.
- Put the *reason* in `note`. It makes the sheet reviewable and helps the next edit.

`--check` reports unknown ids, times outside the video, repeated takes, stacked layers and overly busy sheets. Fix everything it reports before rendering.

## SFX rules

Full guide with a spotting method: `references/sfx-placement.md`.

- **Spot with a reason.** Every sound must serve emotion or story. If removing it loses nothing, remove it.
- **Land the hit on the cut or the movement**, not near it. Use `sync: peak` for whooshes and impacts.
- **Layer lightly**: at most three sounds at one moment (for example impact, sub, tail).
- **Vary takes.** Never use the same sound twice in a row; use `--exclude` when searching again.
- **Leave silence.** A gap before a hit makes the hit land harder.
- **Keep speech clear.** Keep `duck` on when there is dialogue, and keep default gain at -9 dB or lower under speech.
- **Density**: roughly 12 cues per 10 seconds is already very busy. Calm material needs far fewer.

## Verification checklist

Before saying a cut is finished:

- [ ] Every cut passes the hierarchy, with emotion first
- [ ] No cut exists only to add pace; holds are deliberate
- [ ] Cut points sit on thought boundaries, not mid-thought
- [ ] `render_mix.py --check` reports no errors, and warnings are resolved or justified
- [ ] `qc_audio.py` shows true peak at or below -1 dBTP and loudness near the target
- [ ] Output duration, frame size, frame rate and audio layout match the delivery spec
- [ ] The user has been given specific timestamps to listen to

## Reference index

- `references/editorial-principles.md`: the philosophy, cut hierarchy, blink timing, dialogue, pacing, diagnosing problems
- `references/editor.md`: the editor's commands, keyframes, color grading, transitions, captions, delivery settings, project format, working method for an AI, browser editor
- `references/voice-presets.md`: voice presets, extracting Premiere `.prfpset` files, accuracy limits
- `references/sfx-placement.md`: spotting method, sound-by-purpose guide, levels, layering, ducking
- `references/sfx-library-setup.md`: getting a library, building the catalog, curating tags, licensing
- `references/technical-pipeline.md`: media organization, proxies, color, codecs, interchange, export
