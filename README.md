# Pro Video Editing

A Claude skill that helps Claude edit video the way an editor would, plus vedit, a small command-line video editor that Claude drives and that you can open in your browser to adjust by hand. vedit also runs on its own, with no AI involved.

The skill covers the editing method (where to cut, how to pace, when to leave silence) and the tools to do the work: a multi-track timeline, keyframes, color grading, masks, motion tracking, stabilization, titles, sound effects placed on the exact frame, and rendering through FFmpeg.

## What is in it

- **vedit**, the editor. Cuts, speed ramps, reverse and freeze frames, keyframed zoom, position, rotation, opacity, color, volume and speed, color looks and LUTs, transitions, masks, motion tracking, stabilization, green screen, titles, SRT captions, loudness normalization and canvas presets such as vertical video. A browser interface shows the same project and updates when Claude changes it.
- **Sound effect tools.** Search a sound library by description, place a sound so its loudest moment lands on a chosen frame, duck effects under dialogue, and check loudness.
- **Editing guidance** that Claude reads before it cuts: emotion first, cut where a thought ends, do the most with the least, and how to verify an edit when you cannot watch or listen to it.
- **Voice presets.** A bundle of 15 Premiere Pro voice presets (`.prfpset`) already converted to FFmpeg filters, and an importer for any other `.prfpset` file.

No audio is included in this repository. Sound licenses do not allow redistribution, so one command downloads a free sound library from its publisher instead (see below).

## Requirements

- Python 3.9 or newer
- FFmpeg 5 or newer, with `ffprobe`, on your PATH

There are no Python packages to install. Stabilization needs an FFmpeg built with `libvidstab`, which most full builds include.

Installing FFmpeg:

```bash
# Windows
winget install --id Gyan.FFmpeg

# macOS
brew install ffmpeg

# Debian and Ubuntu
sudo apt install ffmpeg
```

Check that it worked with `ffmpeg -version` and `python --version` (use `python3` on macOS and Linux).

## Download and install

### Claude Code

Clone the repository into your skills folder.

On macOS and Linux:

```bash
git clone https://github.com/Temka921/pro-video-editing ~/.claude/skills/pro-video-editing
```

On Windows (PowerShell):

```powershell
git clone https://github.com/Temka921/pro-video-editing "$env:USERPROFILE\.claude\skills\pro-video-editing"
```

To use it in one project only, clone into `.claude/skills/` inside that project. To update later, run `git pull` in the skill folder.

### Claude desktop or web app

1. On the repository page, choose Code, then Download ZIP.
2. Unzip it and rename the folder from `pro-video-editing-main` to `pro-video-editing`.
3. Zip that folder again so the folder itself is at the top of the archive with `SKILL.md` inside it.
4. Upload the zip in the app's skill settings. Menu names change between versions; look under Settings for Capabilities or Skills.

The web app runs scripts in a sandbox that cannot see your disk, so the command-line tools work best in Claude Code or the desktop app with access to your computer.

### Without Claude

Clone or unzip the repository anywhere and run the commands from its folder. Everything under "Using vedit yourself" below works without Claude.

```bash
git clone https://github.com/Temka921/pro-video-editing
cd pro-video-editing
```

## Set up the sound library

This step is optional. Without a library you can still cut, grade, animate and caption. With one, Claude can search for effects such as whooshes, impacts and ambience and place them.

```bash
python scripts/setup_library.py --accept-license
```

That downloads part 1 of the free Sonniss GDC 2026 Game Audio Bundle (1.3 GB, 82 sounds) from Sonniss into the `library/` folder and builds a search catalog. Add `--parts all` for all five parts (7.5 GB, 347 sounds). An interrupted download resumes when you run the command again.

The flag `--accept-license` means you agree to the [Sonniss license](https://gdc.sonniss.com/). In short, the sounds are free for personal and commercial projects without credit, you may not resell or redistribute them as sound effects, and you may not use them to train AI systems.

To use your own sounds instead, point the catalog builder at a zip file or a folder:

```bash
python scripts/build_catalog.py /path/to/sounds.zip
```

You can also ask Claude to "set up the sound library". It will tell you the size and license and wait for your approval before downloading.

## Using it with Claude

Once the skill is installed, describe what you want. Some examples:

- "Cut this interview down to 60 seconds for Reels, and add sound effects where they help."
- "Make my clip vertical, add the subtitles from this .srt file, and normalize the loudness for YouTube."
- "Blur the license plate in this shot and keep it blurred as the car moves."
- "Give these three clips a matching warm look and add a slow push-in on the second one."

Claude follows `SKILL.md`. It analyzes the footage, builds the timeline with vedit, checks the result, renders a preview, looks at frames, and reports what it changed and where you should listen. Claude cannot hear or watch, so it measures what it can and gives you timestamps to check yourself. Listen before you publish.

When you want to adjust something yourself, ask Claude to open the editor (below). It works on the same project file, so your changes and Claude's do not overwrite each other.

## Using vedit yourself

Run these from the repository folder. Replace `python` with `python3` on macOS and Linux.

```bash
python editor/vedit.py new myfilm --size 1920x1080
python editor/vedit.py -p myfilm import interview.mp4        # prints an asset id, such as a1
python editor/vedit.py -p myfilm add a1 --in 12.5 --dur 8    # 8 seconds, starting 12.5 seconds into the file
python editor/vedit.py -p myfilm grade c1 --look cinematic
python editor/vedit.py -p myfilm text "Chapter one" --at 0.5 --dur 3
python editor/vedit.py -p myfilm render --preview            # small and fast: myfilm/out/preview.mp4
python editor/vedit.py -p myfilm render                      # full quality: myfilm/out/final.mp4
```

Other useful commands:

```bash
python editor/vedit.py -p myfilm show                        # print the timeline
python editor/vedit.py -p myfilm check                       # list problems before rendering
python editor/vedit.py -p myfilm frame --at 4.0              # save one frame as a PNG to look at
python editor/vedit.py -p myfilm undo                        # revert the last change
python editor/vedit.py --help                                # every command
```

### The browser editor

```bash
python editor/vedit.py -p myfilm serve
```

This opens `http://127.0.0.1:8765/`. You can import or drop in files, drag clips to move them, drag their edges to trim, split at the playhead, and edit timing, zoom, color, masks, volume and text in the inspector. A diamond button beside each animatable value adds a keyframe at the playhead. The page redraws within a second when anyone changes the project from the command line. It only answers on your own computer.

Shortcuts: space plays and pauses, `S` splits, Delete removes, the arrow keys step a frame, and Ctrl+Z undoes.

### Where the details are

- [references/editor.md](references/editor.md) is the full vedit manual: every command, the project file format, keyframes, color, transitions, masks, tracking and stabilization.
- [references/voice-presets.md](references/voice-presets.md) explains the voice presets and how to import your own `.prfpset` file.
- [references/sfx-library-setup.md](references/sfx-library-setup.md) covers sound libraries, catalogs and licensing.
- [references/sfx-placement.md](references/sfx-placement.md) describes how to choose and place effects.

### Sound effect tools

These also work directly from the command line:

```bash
python scripts/sfx_search.py "fast airy whoosh" --max-dur 1.5
python scripts/render_mix.py in.mp4 cues.json --check
python scripts/render_mix.py in.mp4 cues.json -o out.mp4
python scripts/qc_audio.py out.mp4 --target -14
```

A sample cue sheet is in `examples/cuesheet.example.json`. Its sound ids are placeholders; use ids from your own search results.

## How sound effect placement works

The catalog records where each sound's loudest moment (its peak) and its attack (its onset) occur. In a cue you say when that moment must land. The renderer subtracts the offset to find when to start the file, trims the head if the sound would start before time zero, snaps to the video's frame grid, and delays the sound by an exact number of samples. On synthetic sounds, an uncompressed stem lands within 1 ms of the target. A full render to AAC adds about 5 ms of encoder delay, and pitch-shifted cues land about 8 to 11 ms early because shifting moves the waveform's peak.

## Limits

- Claude cannot hear or watch. The skill replaces that with measurements (loudness, brightness and color numbers, saved frames) and tells you what to check by ear and by eye.
- vedit is compact. It has no rotoscoping or freehand masks, no optical-flow slow motion, no color curves or wheels (grading is one look per clip), no multi-camera sync and no plug-ins. For those, use a full editor.
- Rendering uses the CPU only. A keyframed, graded 1080p clip takes roughly 2 to 3 seconds per second of video on the test laptop. Use `--preview` while working.
- Motion tracking is template matching, not AI. It follows rigid, textured things that keep a steady size and angle, such as a sign, a logo or a face seen from the front. It loses targets that turn, hide or leave the frame, and it reports how much of the clip it could follow.
- A speed ramp mutes the clip's own sound, and slow motion repeats frames.
- Voice presets are close translations. Premiere stores effect settings as normalized numbers and Adobe's processing is not public, so the FFmpeg versions are approximations. Tests check that each preset does what its name says, such as raising or lowering pitch or cutting highs, but not that it sounds identical to Premiere.
- Sound search quality follows file names. Libraries with cryptic names need hand-written tags (`--overrides`).
- The browser editor has no phone layout.

## Testing and platform status

The test suite has 150 tests. Most render real frames or audio and measure the result: whether a zoom moved the picture, whether a tracker stayed within 2 percent of a known path, whether stabilization reduced shake, and whether loudness normalization landed near its target. The tests generate their own media with FFmpeg, so no assets are needed.

```bash
python -m unittest discover -s tests -v
```

It takes a few minutes. I developed and ran everything on Windows 10 with Python 3.15 and FFmpeg 9.0. The repository includes a GitHub Actions workflow that runs the tests on Linux; see the Actions tab for its current status. macOS has not been tried. FFmpeg's `amix`, `sidechaincompress` and `alimiter` behave differently across versions, which is why FFmpeg 5 or newer is required.

Contributions are welcome, especially platform testing, more vocabulary for the sound categories (`VOCAB` in `scripts/sfxlib.py`) and more editing references. Keep scripts to the standard library plus FFmpeg.

## Sound libraries and licensing

The code, documentation and examples in this repository are MIT licensed. That license grants no rights to any sound library. Use sounds only under their own licenses.

- Read the license that comes with every pack. Terms differ and change.
- Do not commit audio to this repository. The `.gitignore` blocks the `library/` folder and common audio and zip files.
- Do not redistribute packs you do not have the right to share. A catalog contains only file names and measurements, but it still describes someone else's library, so `catalog/*.json` is ignored by default.
- The BBC Sound Effects archive, for example, is free only for personal, educational and research use. Commercial use needs a paid license.

## Credits

- The editing principles draw on Walter Murch, *In the Blink of an Eye* (Silman-James Press). This repository paraphrases and condenses the ideas for working use. It does not replace the book, and if you edit, read it.
- Technical pipeline notes come from general post-production practice, including Avid's *High-Resolution Workflows Guide*. Avid, Media Composer and DNxHR are trademarks of Avid Technology, Inc.

This project is independent. It is not affiliated with or endorsed by Walter Murch, Avid, Adobe, Anthropic, Sonniss, the BBC or any other party named here. Product and company names belong to their owners.

## License

MIT for the code, documentation and examples. See `LICENSE`.
