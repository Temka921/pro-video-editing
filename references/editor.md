# The editor (vedit)

A small video editor built for an AI to drive and a person to steer. You (the AI) get full control from the command line; the person can open the same project in a browser, drag clips, and see your changes appear. It covers what most edits need: cuts on a multi-track timeline, keyframed motion and levels, color grading, transitions, titles and captions, green screen, sound effects, voice presets and loudness. It also has speed ramps, reverse and freeze frames, masks, motion tracking and stabilization. It stays small on purpose: no curves or color wheels, no multi-camera sync and no third-party plug-ins.

Run it from the skill folder. On Windows use `python` if `python3` is not found. It needs Python 3.9+ and FFmpeg 5+ and nothing else.

```bash
python3 editor/vedit.py --help
```

## Contents

- Quick start
- How a project is laid out
- Commands
- Keyframes
- Color grading
- Transitions
- Speed, reverse and freeze frames
- Masks
- Motion tracking
- Stabilization
- Captions, green screen, crop and flip
- Delivery: canvas, loudness, tracks
- Working method for an AI
- Letting the person steer (browser editor)
- Project file format
- Limits to be honest about

## Quick start

```bash
python3 editor/vedit.py new myfilm --size 1920x1080 --fps 30     # creates myfilm/
cd myfilm
python3 ../editor/vedit.py import /path/interview.mp4            # prints the asset id, e.g. a1
python3 ../editor/vedit.py add a1 --in 12.5 --dur 8              # 8 s starting 12.5 s into the source
python3 ../editor/vedit.py grade c1 --look cinematic             # color
python3 ../editor/vedit.py text "My Title" --at 0.5 --dur 3
python3 ../editor/vedit.py render --preview                      # small, fast: out/preview.mp4
python3 ../editor/vedit.py frame --at 2.0                        # out/frame_2_0.png: look at the picture
python3 ../editor/vedit.py render                                # out/final.mp4
```

`-p FOLDER` selects a project from anywhere (before or after the command). Without it the current folder is used. Every command prints JSON; errors print `error: ...` and exit non-zero. `python3 editor/vedit.py looks` prints every look, keyframeable property, transition and canvas preset.

## How a project is laid out

```
myfilm/
  project.json     the whole edit (plain JSON; safe to read)
  media/           copies and extracted sound effects (imports are referenced in place unless you pass --copy)
  out/             preview.mp4, final.mp4, frames
  .history/        the last 50 versions, for undo
  .cache/          waveforms and thumbnails for the browser editor
```

- **Tracks** are `V1`, `V2` (video) and `A1`, `A2` (audio). Video tracks stack bottom to top: `V2` draws over `V1`. Add more with `track add video|audio`.
- **Clips** sit on tracks at a start time in seconds. A video clip brings its own sound unless muted.
- `A2` is a **ducking** track: whatever is on it automatically dips under the other audio (dialogue, music, video sound). Sound effects go there by default. `track set A2 --duck off` turns that off.
- Everything is rounded to 0.1 ms. Video starts snap to the frame grid when rendering.

## Commands

| Command | What it does |
|---|---|
| `new NAME [--size WxH] [--fps N]` | Create a project folder |
| `import FILE [--copy]` | Register a video, audio or image file; prints its asset id |
| `add ASSET [--track T] [--at S] [--in S] [--dur S] [--speed X] [--volume DB] [--mute] [--fade-in S] [--fade-out S] [--preset NAME] [--opacity X] [--fit contain\|cover] [--box x,y,w,h]` | Put an asset on the timeline. Default track is the first video (or audio) track; default `--at` is the end of that track; `--dur` defaults to the rest of the source (images: 5 s) |
| `text "TEXT" --at S --dur S [--pos top\|center\|bottom] [--size PX] [--color C] [--no-bg] [--fade-in S] [--fade-out S]` | Title or caption (default track `V2`) |
| `captions FILE [--offset S] [--pos P] [--size PX]` | One text clip per subtitle in an `.srt` or `.vtt` file |
| `sfx SOUND_ID --at S [--sync peak\|onset\|start] [--gain DB] [--duration S]` | Place a catalog sound so its loudest moment (`peak`) lands on the time. Find ids with `python3 scripts/sfx_search.py` |
| `split CLIP --at S` | Cut a clip in two at a timeline time. Returns the new clip id. Keyframes are split with it |
| `trim CLIP [--start S] [--end S]` | Move the head and/or tail; the picture (and its keyframes) stay in sync with the source |
| `move CLIP [--at S] [--track T]` | Move in time and/or to another track |
| `set CLIP key=value ...` | Change fields (see below) |
| `remove CLIP [--ripple]` | Delete; `--ripple` closes the gap on that track |
| `key CLIP PROP --at S [--value V] [--ease E]` | Add a keyframe |
| `keys CLIP PROP 'JSON'` | Replace all of a property's keyframes at once |
| `unkey CLIP PROP [--at S]` | Remove one keyframe, or all of them |
| `grade CLIP [--look NAME] [--lut FILE] [--brightness ...] [--reset]` | Color-grade a picture clip |
| `transition CLIP_A CLIP_B [--type T] [--dur S] [--no-ripple]` | Transition between two neighbouring clips |
| `freeze CLIP --at S [--dur S]` | Hold the frame at a time (default 2 s); later clips on the track move later |
| `reverse CLIP [--off]` | Play a clip backwards |
| `mask CLIP [--id M] [--shape rect\|ellipse] [--x --y --w --h] [--feather F] [--effect E] [--amount A] [--invert]`, `unmask CLIP M` | Add or change a mask; remove one |
| `tracker add CLIP --at S --x X --y Y [--size S] [--name N]`, `tracker remove ID` | Motion tracking: follow a point through a clip |
| `follow CLIP MASK TRACKER [--dx --dy]`, `attach CLIP TRACKER [--to CLIP] [--dx --dy]` | Make a mask, or a title or overlay, follow a tracker |
| `stabilize CLIP [--strength S] [--off]` | Smooth camera shake (needs FFmpeg with libvidstab) |
| `track add KIND [--id ID] [--duck on\|off]`, `track remove ID`, `track set ID [--duck] [--mute on\|off] [--hide on\|off]` | Manage tracks |
| `settings [--width N --height N --fps N --bg COLOR --preset NAME --lufs N\|off]` | Show or change the canvas and final loudness |
| `show [--json]` | Print the timeline (`--json` for the full project) |
| `check` | Problems before rendering. Exit code 1 if there are errors |
| `render [--preview] [-o FILE]` | Render. `--preview` is 640 px on its longer side and fast |
| `frame --at S [-o FILE]` | Save one frame as a PNG so you can look at it |
| `measure --at S` | Brightness and color numbers for a frame, with plain-language hints |
| `undo` | Revert the last change (a batch counts as one) |
| `batch FILE` (`-` for stdin) | Apply a JSON list of ops as one change: all or nothing |
| `analyze FILE` | Silences, scene changes and where sound is, for choosing cuts |
| `presets list \| show NAME \| import FILE.prfpset` | Voice presets (see `voice-presets.md`) |
| `looks` | Reference: looks, keyframeable properties, eases, transitions, canvas presets |
| `serve [--port N] [--no-open]` | The browser editor |

**`set` fields:** `start`, `dur`, `speed` (0.25..4), `fade_in`, `fade_out`, `afade_in`, `afade_out` (sound-only fades), `volume_db` (-60..24), `mute`, `preset` (voice preset or `none`), and for picture clips `opacity` (0..1), `scale` (zoom, 1 fits), `x`, `y` (offset as a fraction of the frame), `rotation` (degrees), `fit`, `box`, `flip` (`h`, `v`, `hv`, `none`), `crop` (`left,top,right,bottom` as fractions), `chromakey` (see below). Text clips take `text`, `size`, `color`, `pos`, `bg`, and `x`, `y`, `opacity`. `note` is free text.

**Dissolves:** `transition` below does this properly. You can also overlap two clips on one track by hand and give the later one a `fade_in` equal to the overlap.

**Picture-in-picture:** `--box 0.65,0.05,0.3,0.3` puts the clip in a rectangle (x, y, width, height as fractions of the frame), usually on `V2`. Zoom, shift and rotation then work inside that box.

### Batches

Use a batch when several edits belong together; it saves once and `undo` reverts all of it. Ops use the same names and the argument names of the Python functions in `editor/veditlib/ops.py` (`src_in` for `--in`, `volume_db` for `--volume`, and so on):

```json
[
  {"op": "add", "asset": "a1", "src_in": 12.5, "dur": 8},
  {"op": "grade", "clip": "c1", "look": "teal-orange", "contrast": 1.1},
  {"op": "key", "clip": "c1", "prop": "scale", "at": 0, "value": 1.0},
  {"op": "key", "clip": "c1", "prop": "scale", "at": 8, "value": 1.15, "ease": "inout"},
  {"op": "add_text", "text": "Chapter 1", "at": 0.5, "dur": 3},
  {"op": "add_sfx", "sound": "a3f9c012", "at": 8.0, "gain_db": -9}
]
```

Ops: `set_settings`, `add_track`, `remove_track`, `set_track`, `import_asset`, `add`, `add_text`, `add_sfx`, `import_captions`, `split`, `trim`, `move`, `set`, `remove`, `key`, `set_keys`, `unkey`, `grade`, `transition`, `check`.

## Keyframes

A keyframe pins a value at a moment; between two keyframes the value eases from one to the next. Use them for a slow push-in, a punch-in on a punchline, a picture-in-picture that slides in, a title that drifts, a color that shifts, or music that ramps down under speech.

| Property | Meaning | Range |
|---|---|---|
| `scale` | zoom; 1 fits the frame | 0.05 to 8 |
| `x`, `y` | shift, as a fraction of the frame width/height | -2 to 2 |
| `rotation` | degrees, clockwise | -360 to 360 |
| `opacity` | 0 transparent to 1 solid | 0 to 1 |
| `brightness`, `contrast`, `saturation`, `gamma` | the color controls | as in grading |
| `volume_db` | sound level | -60 to 24 |
| `speed` | playback speed; keyframes make a ramp (video clips; mutes the clip's own sound) | 0.1 to 8 |
| `m1.x`, `m1.y`, `m1.w`, `m1.h` | a mask's centre and size (use the mask's id) | see Masks |

Text clips can key `x`, `y` and `opacity`. Sound-only clips can key `volume_db`.

```bash
python3 editor/vedit.py key c1 scale --at 10 --value 1.0                      # --at is TIMELINE time, inside the clip
python3 editor/vedit.py key c1 scale --at 14 --value 1.25 --ease inout
python3 editor/vedit.py keys c2 volume_db '[[0,0],[1.5,0],[2.5,-18]]'         # JSON times are seconds from the CLIP's start
python3 editor/vedit.py unkey c1 scale --at 14
```

- **Eases** (set on the keyframe a segment starts from): `linear`, `in` (slow start), `out` (slow end), `inout` (smooth both ends, best for camera-like moves), `hold` (stay put, then jump at the next keyframe).
- Before the first keyframe and after the last, the value holds.
- Without `--value`, `key` freezes whatever the property is at that moment, which is a quick way to add a mid-point.
- Keyframes belong to the clip: they move with it, split correctly when you `split` (both halves keep the exact value at the cut) and stay on the same picture when you `trim` the head.
- A keyframed property overrides that property's steady value; `set` warns you when you try to change one.
- A move reads best when it is slow and small: 5 to 15 percent zoom over several seconds. Fast, large moves look like a mistake unless they hit a beat.

## Color grading

`grade` sets a clip's look. It affects the picture only, not the transparent bars around it, and it is applied before zoom and rotation.

```bash
python3 editor/vedit.py grade c1 --look cinematic
python3 editor/vedit.py grade c1 --brightness 0.05 --contrast 1.15 --saturation 1.1 --temperature 0.2
python3 editor/vedit.py grade c1 --lut ~/luts/film.cube       # a .cube look-up table
python3 editor/vedit.py grade c1 --look none                  # clear one field; --reset clears the whole grade
```

| Field | Effect | Range |
|---|---|---|
| `brightness` | added to every pixel | -1 to 1 |
| `contrast` | 1 is unchanged | 0 to 3 |
| `saturation` | 0 is black and white | 0 to 3 |
| `gamma` | above 1 lifts the midtones | 0.1 to 3 |
| `temperature` | negative cooler, positive warmer | -1 to 1 |
| `tint` | negative green, positive magenta | -1 to 1 |
| `vignette` | darkened corners | 0 to 1 |
| `sharpen`, `blur` | finishing | 0 to 1, 0 to 50 |

**Looks:** `warm`, `cool`, `vivid`, `faded`, `black-white`, `noir`, `vintage`, `teal-orange`, `cinematic`. Fields you give explicitly override the look's values. `brightness`, `contrast`, `saturation` and `gamma` can be keyframed (for example a slow fade to black-and-white).

**Grade like an editor:** first match shots to each other (same warmth and exposure within a scene), then add one look over the whole film. Do not stack strong looks. A grade the person cannot judge is worse than none.

**You cannot judge color by eye.** Use `measure --at S`: it renders that frame and returns average and spread of brightness (luma 0 to 255, video black is 16 and white is 235), saturation, and warm or cool lean, plus hints such as "highlights reach white" or "low contrast". Compare frames from different clips, and `frame --at S` to see the picture. Averages cannot judge skin tones or taste, so say what you measured and ask the person to look.

## Transitions

```bash
python3 editor/vedit.py transition c1 c2 --type slide-left --dur 0.6
```

Types: `dissolve`, `dip` (through the background color), `slide-left`, `slide-right`, `slide-up`, `slide-down` (the next clip slides in over the last), `push-left`, `push-right` (both move), `zoom` (the next clip zooms in as it fades up).

- The two clips must be on the same video track and neighbours (within 0.25 s).
- Every type except `dip` **overlaps** the clips by the transition length: clip B is pulled earlier, and by default the rest of that track follows (ripple), so the track gets shorter by that length. Tracks you did not name do not move, so shift synced music or titles yourself, or pass `--no-ripple` to move only clip B.
- Sound crossfades automatically.
- Applying a transition again replaces the previous one. `undo` removes it.
- A hard cut is usually right. Use a transition for a change of time or place, and keep them short (0.3 to 0.8 s) and consistent. One style per film.

## Speed, reverse and freeze frames

- **Steady speed:** `set CLIP speed=0.5` (slow motion) or `speed=2`, 0.25 to 4. The clip's `dur` stays the length you see on the timeline; it reads `dur x speed` seconds of the source. Sound speeds with the picture without changing pitch.
- **Speed ramp:** keyframe the `speed` property (0.1 to 8): `key c1 speed --at 10 --value 1`, then `key c1 speed --at 12 --value 0.25 --ease inout`. The speed eases between keyframes, so a clip can race, slow to a crawl on the key moment and race again. `dur` is still the output length; the editor works out how much source the ramp uses and refuses a ramp that would run past the end of the file. Slow motion repeats frames (there is no frame interpolation), so it looks best on 60 fps footage. **A ramped clip's own sound is muted** (pitch-shifted sound is rarely wanted); put music or effects on an audio track.
- **Reverse:** `reverse c1` (or `set c1 reverse=true`; `reverse c1 --off` to undo). Sound reverses too. Reversing holds every frame of the clip in memory, so `check` warns above about 1 GB and refuses above about 3 GB; split a long clip or lower the canvas size.
- **Freeze frame:** `freeze c1 --at 12.4 --dur 2`. The clip is cut at that time, the frame is held for 2 seconds (silent), and everything later on that track moves later by 2 seconds; tracks you did not name do not move. Freezing at a clip's very start or end holds its first or last frame. The held clip keeps the clip's color, masks and a steady copy of its animation values.
- Split, trim and freeze all keep reversed and ramped clips on the same picture; you do not have to do any arithmetic.

## Masks

A mask limits a picture clip to a shape, or applies an effect only inside (or outside) it. Typical uses: blur a face or a license plate, darken the background to pull the eye to the subject, pixelate a screen, or show a clip only inside an ellipse.

```bash
python3 editor/vedit.py mask c1 --shape ellipse --x 0.62 --y 0.35 --w 0.18 --h 0.28 --effect blur --amount 0.9 --feather 0.2
python3 editor/vedit.py mask c1 --id m1 --invert              # change a mask: blur everything EXCEPT the shape
python3 editor/vedit.py mask c1 --shape rect --effect none     # the clip shows only inside the rectangle
python3 editor/vedit.py unmask c1 m1
```

| Field | Meaning |
|---|---|
| `shape` | `ellipse` or `rect` |
| `x`, `y` | centre, as a fraction of the **picture** across and down (0.5, 0.5 is the middle) |
| `w`, `h` | size, as a fraction of the picture (1 is the whole width or height) |
| `feather` | edge softness, 0 (crisp) to 1 (soft throughout) |
| `effect` | `blur`, `pixelate`, `darken`, `brighten`, `desaturate`, or `none` (the clip is visible only inside) |
| `amount` | strength of the effect, 0 to 1 |
| `invert` | apply outside the shape instead |

- Positions are fractions of the **picture**, not the canvas, so a mask stays on the same part of the image when the picture is letterboxed, zoomed, moved or rotated. Use `frame --at T` to look at the picture and read off where the thing is.
- Up to four masks per clip, applied in order. Each mask's `x`, `y`, `w` and `h` can be keyframed with the properties `m1.x`, `m1.y`, `m1.w`, `m1.h` (the mask id plus the field), or made to follow a tracked object (next section).
- Masks apply to picture clips; they are applied after speed and before zoom, rotation and opacity.

## Motion tracking

Tracking follows a spot through a clip so a mask or a title can move with it. You point at something in one frame; the editor takes a patch around the point as a template and searches every frame for the best match, forwards and backwards from that frame (FFmpeg's `find_rect`). The result is stored as a *tracker*.

```bash
python3 editor/vedit.py frame --at 12.0                                          # look: where is the face, as fractions of the picture?
python3 editor/vedit.py tracker add c1 --at 12.0 --x 0.62 --y 0.35 --size 0.12   # {"tracker": "t1", "found": 0.97, ...}
python3 editor/vedit.py mask c1 --shape ellipse --effect blur --w 0.2 --h 0.3
python3 editor/vedit.py follow c1 m1 t1                                          # the blur now follows the face
python3 editor/vedit.py attach c3 t1 --dy -0.12                                  # a title (or sticker) rides above the face
python3 editor/vedit.py tracker remove t1
```

- `--at` is a timeline time inside the clip; `--x` and `--y` are fractions of the picture (0 to 1); `--size` is the size of the patch as a fraction of the picture height (default 0.08; make it big enough to hold real detail, not so big that it includes a lot of background).
- `found` is the share of the clip's frames in which the target was followed. Below 0.6 the command warns that it may have turned, left the picture, or been hidden. Always look at the result with `frame` at a few moments.
- `follow` fills a mask's centre keyframes from the tracker. `attach` fills a title's or overlay's `x`/`y` keyframes so its centre sits on the tracked point as it appears on screen (for a title it also sets `pos` to `center`; `--dx`/`--dy` nudge it by a fraction of the frame). A picture-in-picture clip (`--box`) is positioned relative to its box centre.
- A tracker is stored against the footage's own time, so it still works after you split, trim or move the clip, and can be used on every piece of that footage.
- It tracks **rigid, textured things that keep roughly the same size and angle**: a sign, a logo, a face seen from the front, a car in a steady shot. It will lose a target that turns away, grows or shrinks quickly, is hidden, leaves the frame, or has no detail (a flat patch is refused). Track **before** reversing, ramping, cropping or flipping the clip; those are refused because they change where things are.
- If you measured the motion some other way, `{"op": "set_tracker", "id": "t1", "clip": "c1", "points": [[source_seconds, x, y], ...]}` stores your path.

## Stabilization

```bash
python3 editor/vedit.py stabilize c1                  # strength 0.5
python3 editor/vedit.py stabilize c1 --strength 0.8   # 0 gentle ... 1 locked off
python3 editor/vedit.py stabilize c1 --off
```

Two passes: the first measures the camera shake of the clip's part of the source file (cached, so changing other things does not repeat it), the second smooths it when rendering. Stronger smoothing needs more zoom to hide the moving edges, so the picture crops in. It removes handheld wobble and jitter; it cannot fix rolling-shutter wobble, motion blur, or a camera that is deliberately panning. **It needs an FFmpeg built with `libvidstab`** (most full builds have it). Without it the editor says so and does nothing: FFmpeg's built-in `deshake` was measured and barely reduces real shake, so it is not used as a stand-in.

## Captions, green screen, crop and flip

- **Captions:** `captions subtitles.srt` creates one text clip per subtitle (SRT or WebVTT), on the top video track at the bottom of the frame with a dark box. `--offset S` shifts them all. If you have a transcript with times, write the cues to an SRT file or use `add_text` ops in a batch.
- **Green screen:** `set CLIP chromakey=green` removes a background color. Fine-tune with `chromakey=0x00ff00,0.2,0.1` (color, tolerance 0.01 to 1, edge softness 0 to 1). Put the keyed clip on `V2` above the new background on `V1`.
- **Crop and flip:** `set CLIP crop=0.1,0,0.1,0` trims 10 percent off the left and right; `set CLIP flip=h` mirrors the picture (never flip a clip that contains text).

## Delivery: canvas, loudness, tracks

- `settings --preset vertical` sets 1080x1920 for Reels, TikTok and Shorts. Presets: `youtube` (1920x1080), `hd` (1280x720), `4k`, `vertical`, `square` (1080x1080). Change the canvas before placing clips so `fit` and positions are right; clips use `contain` by default (add `fit=cover` to fill a vertical frame from a wide shot).
- `settings --lufs -14` normalises the final mix to that loudness (YouTube about -14, broadcast -23). `--lufs off` leaves levels as mixed. A limiter always guards against clipping.
- `track set A1 --mute on` silences a track; `track set V2 --hide on` hides a video track's picture. Handy for comparing and for isolating a problem.

## Working method for an AI

You cannot watch or listen. Replace that with measurement and sight:

1. **Set the canvas** for the destination (`settings --preset ...`), then log the footage: `analyze FILE` gives silences, scene changes and the stretches that have sound. Pick cut points where a thought ends (see `editorial-principles.md`).
2. **Assemble in order**, structure first: `import`, then `add` with `--in` and `--dur`, most important material first. Check with `show`.
3. **Refine**: `trim`, `split`, `move`, fades, transitions only where time or place changes. Cut for emotion first, then story, then rhythm.
4. **Polish picture**: match color across clips with `grade`, add a gentle look, a slow push-in (`key` on `scale`) where a still shot needs life, titles and captions. Stabilize shaky handheld shots, blur what must not be seen (a mask, tracked if it moves), and use a speed ramp or freeze frame only on a key moment.
5. **Sound**: choose effects with `scripts/sfx_search.py`, place them with `sfx`. Apply voice presets with `set CLIP preset=...` only when the sound calls for it. Duck music with `volume_db` keyframes under speech. Set `--lufs` for the destination. Follow `sfx-placement.md`.
6. **Verify**: run `check`. Then `render --preview`; use `frame --at T` on every cut and title, and `measure --at T` on graded shots; read the PNGs. Final `render` only when satisfied.
7. **Hand back** with the intent you edited toward, what you cut and why, and two or three timestamps for the person to listen to, since you could not.

Rules that prevent most mistakes:

- Never invent asset, clip or sound ids; read them from command output.
- Prefer a batch for related edits; use `undo` rather than hand-editing `project.json`.
- Fix everything `check` reports. Warnings about black gaps usually mean a clip is missing or misplaced.
- Restraint: a few purposeful moves beat constant motion. If removing a move or effect loses nothing, remove it.
- Do not claim the result sounds or looks good. Say what you measured and what you looked at.

## Letting the person steer (browser editor)

```bash
python3 editor/vedit.py -p myfilm serve
```

Opens `http://127.0.0.1:8765/`. The person can import or drop in files, drag clips to move them, drag clip edges to trim, split at the playhead (`S`), delete, and edit everything in the inspector: timing, zoom, shift, rotation, opacity, color (look, sliders, LUT), green screen, volume, voice preset, text, and transitions from the previous clip. Timeline clips show thumbnails and waveforms, and small diamonds mark keyframes. Beside every animatable value there is a diamond button: click it at the playhead to add a keyframe (click again on that keyframe to remove it); once a value is animated, typing a new value sets a keyframe at the playhead, and the field shows the value at the playhead as it scrubs. The inspector also has a Time section (play backwards, freeze frame at the playhead, stabilize), speed as an animatable value (keyframe it for a ramp), a Masks section (shape, effect, strength, soft edge, invert, and keyframeable centre and size), and Motion tracking: press Pick on preview, click the thing to follow, then Track from the playhead; trackers can then be applied to a mask (Follow) or to a title or overlay (Attach). Tracks have mute (`M`) and hide (`H`) buttons, and the left panel imports captions and sets the canvas and final loudness. Space plays, arrow keys step a frame, `Ctrl+Z` undoes.

You and the person edit the same file. The page checks the project revision every second and redraws when you change it from the command line, and a badge shows whether the preview matches the latest edit. Your commands and their clicks never overwrite each other; each change is applied to the latest saved version under a lock. The server only answers on your own computer.

If the person says "I moved things in the editor", run `show` before editing so you work from what is on the timeline now.

## Project file format

`project.json` (version 1). Safe to read; edit it through commands so the history stays correct.

```json
{
  "version": 1, "rev": 12,
  "settings": {"width": 1280, "height": 720, "fps": 30, "bg": "black", "master_lufs": -14},
  "assets": {"a1": {"path": "C:/clips/a.mp4", "name": "a.mp4", "kind": "video", "duration": 6.0,
                    "width": 640, "height": 360, "fps": 30.0, "has_audio": true}},
  "tracks": [{"id": "V1", "kind": "video"}, {"id": "V2", "kind": "video"},
             {"id": "A1", "kind": "audio"}, {"id": "A2", "kind": "audio", "duck": true}],
  "clips": [
    {"id": "c1", "track": "V1", "asset": "a1", "start": 0, "src_in": 0, "dur": 4, "speed": 1,
     "volume_db": 0, "fade_in": 0, "fade_out": 0, "opacity": 1, "fit": "contain", "preset": "voice enhancer",
     "color": {"look": "cinematic", "temperature": 0.2},
     "keys": {"scale": [[0, 1.0, "linear"], [4, 1.2, "inout"]]},
     "transition": {"type": "dissolve", "dur": 0.6, "from": "c0"}},
    {"id": "c2", "track": "V2", "type": "text", "text": "Title", "start": 0.5, "dur": 3,
     "size": 51, "color": "white", "pos": "bottom", "bg": true, "fade_in": 0.15, "fade_out": 0.15}
  ]
}
```

`dur` is the length on the timeline. The source consumed is `dur x speed` (the integral of the speed curve if it is ramped), starting at `src_in`. Other clip fields: `reverse`, `hold` (a frozen frame), `stabilize` (`{"strength": 0.5}`), `masks` (a list; each has `id`, `shape`, `x`, `y`, `w`, `h`, `feather`, `invert`, `effect`, `amount`). The project also has a `trackers` object: `{"t1": {"clip", "asset", "points": [[source_seconds, x, y], ...], "found"}}`. `keys` times are seconds from the clip's start. Sound-effect clips also carry `sfx` (the catalog id). Asset paths are absolute, or relative to the project folder. On a picture clip `color` is the grade; on a text clip `color` is the font color.

## Limits to be honest about

- **No hearing or watching.** You verify by measurement (`analyze`, `check`, `measure`, `frame`, the QC script). Always give the person timestamps to listen to and a frame to look at.
- **What it does not have:** rotoscoping and animated freehand masks, optical-flow slow motion, curves and color wheels, multi-camera sync, and third-party plug-ins. For those, use a full editor.
- **Tracking is template matching, not AI.** It follows rigid, textured things with a stable size and angle and reports how much of the clip it could follow. It cannot recognise faces, and it does not handle turning, hidden or fast-growing targets.
- **Stabilization needs libvidstab** and crops in to hide the moving edges. Reverse needs memory for every frame of the clip. A speed ramp mutes the clip's own sound, and slow motion repeats frames.
- **Color is global per clip.** Grading cannot isolate a skin tone or a sky.
- **Speed.** Rendering is software-only. A keyframed zoom with a grade at 1080p takes roughly 2 to 3 seconds of render per second of video on a typical laptop; plain cuts are several times faster. Use `--preview` while working.
- **Text** needs a font file. It finds Arial or DejaVu automatically; otherwise set `VEDIT_FONT=path/to/font.ttf`. Text cannot be zoomed or rotated, only moved and faded.
- **Voice presets are approximations** of Premiere's effects. See `voice-presets.md`.
- **Previews are small** (640 px on the longer side, heavier compression). The final render uses the project's real size.
- A render needs the source files where `project.json` says they are. Use `import --copy` if the project must travel.
