# Sound-effect placement

How to decide what sounds go where, and how to express that as a cue sheet for `scripts/render_mix.py`.

## Contents

- Why sound effects fail
- Spotting method
- Choosing sounds by purpose
- Timing and sync
- Level, layering and ducking
- Search tips
- Checks after rendering

## Why sound effects fail

Viewers rarely identify the individual sound. They notice the feeling that someone pasted effects in. The usual causes:

- The sound is **near** the action, not **on** it (even 3 to 4 frames early or late feels off).
- The same sound repeats in a row.
- Everything has a sound, so nothing stands out.
- The effect fights the dialogue or music for space.
- The sound does not match the picture's scale or material (a heavy boom on a light graphic).

## Spotting method

1. **List the candidate moments**: hard cuts, scene changes, text or graphic appearances, a key movement, an emphasized word, a reveal, a beat of silence.
2. **Rank them by importance.** Ask of each: what should the viewer feel here, and does a sound deliver it better than silence?
3. **Cut the list down.** Keep the moments that carry story or emotion. Aim to leave most cuts silent. A rough ceiling is 12 cues in 10 seconds, and that is already very busy; calm material needs far fewer.
4. **Choose function per moment**: accent (impact), movement (whoosh), anticipation (riser), release (hit after a riser), space (ambience), texture (foley), feedback (UI).
5. **Write the reason** for each cue in `note`.

## Choosing sounds by purpose

| Purpose | Search with | Typical length | Notes |
|---|---|---|---|
| Fast movement, cut accent | `whoosh swish swipe` | 0.3 to 1.2 s | Land the peak on the cut or at the moment of motion. Short and airy for light motion, heavier for large objects. |
| Hit, reveal, title slam | `impact hit slam thud` | 0.5 to 3 s | Pair with a soft sub layer for weight. Use `sync: peak`. |
| Build to a moment | `riser swell tension` | 1 to 6 s | The end of the riser should arrive at the payoff. Place with the hit as the sync target. |
| Release after a build | `boom sub drop` | 1 to 4 s | Often the second layer under an impact. |
| Interface, text pop | `ui click pop tick` | 0.05 to 0.4 s | Keep very quiet and consistent across the piece. |
| Scene space | `ambience room tone` (use `--kind bed`) | long | Under the whole scene at a low level, with fades in and out. |
| Physical texture | `foley footstep cloth door` | short | Only where the sound is implied by something visible. |
| Transition | `transition reverse rewind` | 0.5 to 2 s | Use sparingly; one style per piece. |

Run `scripts/sfx_search.py --stats` first to see which categories the library actually has.

## Timing and sync

- `sync: peak` puts the loudest moment of the sound exactly on `at`. Use it for impacts and whooshes.
- `sync: onset` puts the attack on `at`. Use it for sounds whose loudest part comes later than the part that should be heard first (such as a swell).
- `sync: start` puts the first sample on `at`. Use it for beds and ambience.
- If the sound's peak is 0.5 s into the file and `at` is 0.2 s, the tool trims the head so the peak still lands on 0.2 s.
- For motion that arrives at a cut, place the whoosh so its peak lands on the cut frame. For a riser, place its end (use `trim_start` and `duration` to shorten) on the payoff frame.
- Times snap to the video's frame grid unless `"snap": false` is set. Pitch-shifted cues can drift by up to about 10 ms because the waveform shifts.

## Level, layering and ducking

- Default gain is -9 dB. Professional libraries are mastered loud, so unreduced sounds will swamp the picture's own audio. Under speech go lower; for a hero impact on a silent beat you may go higher.
- Never above 0 dB gain. The tool warns.
- Layer at most three sounds at one moment, each with a different job (for example: sharp impact, sub weight, short tail). Offset layers by a few frames for a bigger feel.
- `"duck": true` automatically lowers the whole SFX bus while the original audio is loud, so dialogue stays clear. Keep it on whenever there is speech.
- `"humanize": true` varies gain by about 1 dB and pitch by about 0.3 semitone when the same sound repeats. It is no substitute for choosing different sounds.
- Fades: defaults (2 ms in, 10 ms out) prevent clicks. For beds, set `fade_in` and `fade_out` to 0.5 to 2 s.
- Silence is a tool. A short gap before a hit makes it land harder.

## Search tips

- Describe the sound by feel and material: `soft paper whoosh`, `deep cinematic impact`, `small digital click`.
- Filter by length with `--max-dur` and `--min-dur`, by type with `--category`, and by long beds with `--kind bed`.
- Use `--exclude ID1,ID2` to get a different sound each time.
- If a query returns nothing, loosen it to one word or drop a filter. File names with few keywords have weaker tags; see `sfx-library-setup.md` for curation.

## Checks after rendering

1. `python3 scripts/render_mix.py in.mp4 cues.json --check` reports no errors and no unexplained warnings.
2. `python3 scripts/qc_audio.py out.mp4 --target -14` (web) or `--target -23` (broadcast) shows true peak at or below -1 dBTP and loudness near the target.
3. You cannot hear the result. Give the user three timestamps to listen to: the most important hit, the busiest stretch, and a dialogue section with SFX nearby.
