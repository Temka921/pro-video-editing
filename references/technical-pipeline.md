# Technical pipeline

Tool-agnostic practice for moving footage from camera to delivery without losing quality or losing track of files. It applies to any editor or an FFmpeg-based workflow. It is distilled from broadcast and post-production practice, including the offline/online workflow described in Avid's High-Resolution Workflows Guide; product-specific steps are omitted.

## Contents

- Media organization
- Metadata
- Proxy (offline/online) workflow
- Frame size and reframing
- Color management
- HDR
- Codecs and quality tiers
- Interchange and handoff
- Export

## Media organization

- Copy camera media to robust storage before anything else. Keep each card's folder structure and sidecar files intact; they often hold needed metadata.
- Use folder names with letters, numbers and underscores only. Keep names short and paths shallow. Illegal characters and very deep paths cause files to show as offline in some tools.
- Keep separate folders for: original media, proxies, renders, and exchange files (EDL, AAF, XML, LUTs, CDLs).
- Archive the organized originals before editing starts.
- Once editing begins, **do not rename, move or reorganize source files or folders.** Relinking depends on them. Do not delete the clips or bins created when linking.
- Back up project and timeline files often. They are small.

## Metadata

File-based media has no physical label, so tracking matters.

- Preserve the original filename as the source identifier. Never rely on a renamed clip.
- Record per clip: original filename and path, reel or card ID, camera model, scene and take, frame rate, timecode, duration, resolution, color encoding, and whether a look or CDL is already baked in.
- Source frame rate must match project frame rate for clean relinking (23.976 media belongs in a 23.976 project).
- For files without timecode, derive one consistently (for example from the creation time) and keep it.
- Put notes and descriptions on the original source clips, not on proxies, so they survive relinking.

## Proxy (offline/online) workflow

1. Link to the originals and log them.
2. Apply camera-wide corrections (color transform, framing) at the source-clip level.
3. Transcode to lightweight proxies. Quarter or sixteenth resolution is typical at 4K and above.
4. Edit with proxies. Match preview resolution to proxy resolution so nothing is rescaled during playback.
5. When the cut is locked, relink to originals or to a high-quality intermediate for color, effects and output.
6. **Turn proxy mode off before final export.** Effects rendered at proxy resolution must be re-rendered.

## Frame size and reframing

- Set the project frame size from the primary deliverable. Every source is scaled, cropped or padded to it.
- Resolution is pixel count; size is the frame the image must fill. Scaling down is safe. Scaling up never matches a native image, so avoid punching in beyond the source's real pixels.
- Footage shot larger than delivery gives room to reframe, stabilize, hide stray equipment, or animate a pan across the frame with keyframes.
- Reframe at the source-clip level so every use inherits it. For vertical or square versions, reframe per shot; a center crop is not enough.

## Color management

- Identify each source's color encoding (camera log, Rec.709, HDR) and transform it into one working space for the whole project.
- Choose working and output spaces by deliverable: Rec.709 for HD and web, Rec.2020 for UHD and HDR, DCI-P3 for cinema. ACES suits archival and multi-deliverable projects.
- **Levels:** Rec.709 video range is 16 to 235 in 8-bit terms; many tools write full range (0 to 255). A mismatch gives crushed or washed-out pictures. Check every transcode and convert when needed.
- **LUTs** map input color to output color. A 1D LUT works per channel; a 3D LUT maps full RGB triplets. Apply a camera's technical LUT at the source level and track which clips already have it baked in.
- **CDL** carries a look between systems as slope, offset, power and saturation. Read it from the dailies metadata, keep it with the clip, and pass it on in the exchange file.
- YCbCr 4:2:2 is lighter and fine for most editing. Use RGB 4:4:4 for heavy grading, keying and compositing.
- Preserve bit depth end to end. Do not pass 10 or 12-bit sources through an 8-bit intermediate.
- The monitor is part of the chain. Judge color only on a display set to the target standard.

## HDR

- Choose the transfer function at setup: PQ (SMPTE 2084) or HLG.
- Use a 12-bit intermediate and high-precision effects processing.
- Check levels on an HDR scope. Typical PQ masters peak at 1000 nits; HLG stays within 0 to 1.0.
- HDR looks flat on an SDR viewer. A Rec.709 viewing transform for comfort is fine as long as it does not touch the output.
- Export with the same high bit depth codec and correct HDR tagging.

## Codecs and quality tiers

Pick the lightest codec that suits the stage. The DNxHR family is a useful reference; ProRes has equivalents. Figures are approximate for UHD (3840 x 2160) at 24p.

| Tier | Sampling and depth | Use | Approx. bitrate |
|---|---|---|---|
| LB | 4:2:2 8-bit | Offline proxy | 144 Mbps (about 36 at quarter resolution, 9 at sixteenth) |
| SQ | 4:2:2 8-bit | Standard delivery | 462 Mbps |
| HQ | 4:2:2 8-bit | High quality | 699 Mbps |
| HQX | 4:2:2 12-bit | UHD broadcast, HDR | 699 Mbps |
| 444 | 4:4:4 12-bit | Cinema-quality finishing | 1399 Mbps |

HD projects and higher-than-HD projects use different codec families, and renders do not carry across, so changing project format means re-rendering.

## Interchange and handoff

- **EDL:** the simplest format; cuts and source references only. Use it when a system cannot read richer formats. Reel names may be limited to 8 characters.
- **AAF or XML:** carries tracks, effects and metadata. Prefer it for color, VFX and audio handoff.
- Before exporting, simplify: collapse multicam groups to the angles actually used and remove unused sources.
- Send picture tracks to color and audio tracks to the mix as separate exports, with handles on each clip.
- Send the media at best quality alongside the exchange file, or make sure the receiver can reach the originals.
- If a sequence changes after handoff, send a change list, not a fresh sequence.
- Returned material comes back as new media plus an updated exchange file. Conform it against the locked cut.
- When you send the SFX to a mixer, `render_mix.py --sfx-only` writes the SFX bus as a separate 24-bit WAV.

## Export

1. Relink to full quality and turn proxy mode off.
2. Render all effects and linked clips.
3. Confirm frame size, frame rate, color space, levels and audio layout against the delivery spec.
4. Export a high-quality master first. Derive web and social versions from the master, not from the timeline at lower quality.
5. Run `scripts/qc_audio.py` on the result.
6. Keep the master, the final timeline file and the exchange files together.
