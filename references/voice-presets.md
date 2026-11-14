# Voice presets (from Premiere Pro)

`presets/isaacverse-voice-presets.prfpset` is a bundle of 15 Adobe Premiere Pro audio-effect presets for voice. The editor extracts them into FFmpeg filter chains, so they work in `vedit` without Premiere. The same bundle still imports into Premiere itself.

## Using them in vedit

The extracted presets ship with the editor. Use one by name on any clip that has sound:

```bash
python3 editor/vedit.py presets list                          # names, groups, effects
python3 editor/vedit.py set c3 preset="phone call"            # apply
python3 editor/vedit.py set c3 preset=none                    # remove
```

In the browser editor, pick it under **Voice preset** in the inspector.

## Extracting a .prfpset (this bundle or any other)

```bash
python3 editor/vedit.py presets import "path/to/my presets.prfpset"
```

Every preset in the file becomes `~/.pro-video-editing/voice_presets/<name>.json` (use `--dest` for another folder) and is available immediately. A preset with the same name as a bundled one overrides it. Each JSON file holds:

- `chain`: the FFmpeg filters the renderer applies (you can edit it by hand)
- `effects`: for each Premiere effect, its filters, a `match` rating, notes, and the **original parameter values** exactly as Premiere stored them
- `group`, `source`: where it came from

The import prints each effect's `match`:

| match | meaning |
|---|---|
| `close` | the settings map cleanly (EQ, amplify, dynamics curve, a soloed multiband, the FFT curve) |
| `rough` | the effect has no exact FFmpeg twin, so a similar filter with inferred ranges is used |
| `unsupported` | no equivalent yet; the effect is skipped and reported, never silently dropped |
| `skipped` | the effect was bypassed in the preset |

## Be honest about accuracy

Premiere stores every parameter as a 0 to 1 number and keeps the real units inside Adobe's software, and Adobe's audio processing is not public. So the conversion is a faithful best effort, not a bit-for-bit copy. What is firm and what is assumed:

- **Firm** (confirmed against the data): frequencies run 0 to 24 kHz (the 20 Hz minimum is stored as exactly 0.000834); EQ gain is neutral at 0.5; compressor ratios land on whole numbers (4:1 and 12:1).
- **Assumed:** pitch range (+-12 semitones), compressor threshold, attack and release ranges, flanger ranges, and the reverb (FFmpeg has no studio reverb, so three echoes stand in).

Tests measure that each preset does what its name says (chipmunk rises in pitch, deeper voice and villain voice fall, muffled voice cuts highs, phone call cuts lows, pilot voice keeps only a mid band), but that is a check of direction, not of match to Premiere. If a preset sounds off, open its JSON and adjust `chain`, for example the semitone numbers in a pitch preset's `asetrate`/`atempo`. Since you cannot hear, ask the person to audition it.

## The 15 presets

**useful:** voice enhancer, noise remover, deeper voice, muffled voice, phone call, echo, movie trailer voice
**fun:** alien voice, robot voice, villain voice, pilot voice, inner monologue, chipmunk, 90s, torture viewers

"torture viewers" is a deliberately harsh effect meant as a joke; check levels before anyone plays it.

## Using the bundle in Premiere itself

Effects panel menu, **Import Presets**, pick `presets/isaacverse-voice-presets.prfpset`. They appear under **Presets > ISAACVERSE > Isaac voice presets**.
