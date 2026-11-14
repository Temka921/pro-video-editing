"""Voice presets: import Premiere Pro audio-effect presets (.prfpset) and turn them into FFmpeg filters.

A .prfpset is an XML document holding named audio-effect presets. Premiere stores every
parameter as a normalized 0..1 number and keeps the real units inside Adobe's own
code, so a conversion needs a per-effect mapping. Adobe's DSP is not public either,
so no conversion can be sample-exact. This module maps each effect to the closest
FFmpeg filters, records which mappings are firm and which are rough, and keeps the
untouched normalized values in the preset file so a better mapping can be applied later.

Units used (inferred from the values in real presets):
  frequency  = value * 24000 Hz                     (the 20 Hz minimum is exactly 0.000834)
  EQ gain    = (value - 0.5) * 40 dB                (0.5 is flat)
  amplify    = -20 + 30 * value dB                  (2/3 is 0 dB)
  ratio      = 1 + 29 * value                       (real presets land on 4:1 and 12:1)
  pitch      = (value - 0.5) * 24 semitones         (assumed +-12; edit "chain" to change)
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .project import EditError, ROOT

BUNDLED_DIR = ROOT / "editor" / "voice_presets"
USER_DIR = Path.home() / ".pro-video-editing" / "voice_presets"
SR = 48000
FREQ_MAX = 24000.0
SKIP_PARAMS = {"Bypass", "Channel Count", "Link Sliders", "Link Keyframes"}


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def hz(v: float) -> float:
    return clamp(v * FREQ_MAX, 20.0, 20000.0)


def eq_db(v: float) -> float:
    return (v - 0.5) * 40.0


def amp_db(v: float) -> float:
    return -20.0 + 30.0 * v


def f(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".") or "0"


# --------------------------------------------------------------------------
# Effect converters: params (name -> normalized float) -> (filters, notes, match)
# --------------------------------------------------------------------------
def _parametric_eq(p):
    out, notes = [], []
    if p.get("High-Pass Filter Enable", 0) >= 0.5:
        out.append(f"highpass=f={f(hz(p['High-Pass Cutoff Frequency']))}:poles=2")
    if p.get("Low-Pass Filter Enable", 0) >= 0.5:
        out.append(f"lowpass=f={f(hz(p['Low-Pass Cutoff Frequency']))}:poles=2")
    for key, fn in (("Low Shelf", "bass"), ("High Shelf", "treble")):
        if p.get(f"{key} Enable", 0) >= 0.5:
            g = eq_db(p[f"{key} Gain"])
            if abs(g) >= 0.1:
                poles = 2 if p.get(f"{key} Filter Order", 0) >= 0.5 else 1
                out.append(f"{fn}=f={f(hz(p[f'{key} Frequency']))}:g={f(g)}:p={poles}")
    for n in range(1, 6):
        if p.get(f"EQ Band {n} Enable", 0) >= 0.5:
            g = eq_db(p[f"EQ Band {n} Gain"])
            if abs(g) >= 0.1:
                centre = hz(p[f"EQ Band {n} Center Frequency"])
                bw = max(p.get(f"EQ Band {n} Bandwidth", 0.0) * FREQ_MAX, 1.0)
                q = clamp(centre / bw, 0.2, 10.0)
                out.append(f"equalizer=f={f(centre)}:t=q:w={f(q)}:g={f(g)}")
    master = amp_db(p.get("Gain", 2 / 3))
    if abs(master) >= 0.1:
        out.append(f"volume={f(master)}dB")
    return out, notes, "close"


def _amplify(p):
    db = amp_db((p.get("Left", 2 / 3) + p.get("Right", 2 / 3)) / 2)
    return ([f"volume={f(db)}dB"] if abs(db) >= 0.05 else []), [], "close"


def _hard_limiter(p):
    ceiling = -20.0 + 20.0 * p.get("Maximum Amplitude", 1.0)
    return (
        [f"alimiter=limit={10 ** (ceiling / 20):.4f}:level=disabled"],
        ["only the ceiling is applied; input boost, look-ahead and release use FFmpeg defaults"],
        "rough",
    )


def _compressor(p):
    thr = -60.0 + 60.0 * p.get("Threshold", 1.0)
    ratio = 1.0 + 29.0 * p.get("Ratio", 0.0)
    attack = max(0.01, p.get("Attack", 0.02) * 500.0)
    release = max(10.0, p.get("Release", 0.02) * 5000.0)
    makeup_db = (p.get("Gain", 0.5) - 0.5) * 40.0
    makeup = clamp(10 ** (makeup_db / 20), 1.0, 64.0)
    return (
        [f"acompressor=threshold={10 ** (thr / 20):.5f}:ratio={f(ratio)}:attack={f(attack)}:release={f(release)}:makeup={f(makeup)}"],
        ["threshold, attack and release ranges are assumed"],
        "rough",
    )


def _denoise(p):
    nr = clamp(p.get("Amount", 0.2) * 60.0, 0.5, 40.0)
    return [f"afftdn=nr={f(nr)}:nf=-35:tn=1"], ["amount is mapped to the reduction in dB"], "rough"


def _deesser(p):
    i = clamp(1.0 - p.get("Threshold", 0.5), 0.1, 1.0)
    freq = clamp(p.get("Center Frequency", 0.4), 0.0, 1.0)
    return [f"deesser=i={f(i)}:m=0.5:f={f(freq)}"], ["FFmpeg's de-esser has fewer controls than Premiere's"], "rough"


def _dynamics(p):
    n = int(round(clamp(p.get("Number of X/Y Pairs", 0.7143), 0, 1) * 7))
    pts = []
    for k in range(1, n + 1):
        x = -100.0 + 100.0 * p.get(f"Location{k}", 0.0)
        y = -100.0 + 100.0 * p.get(f"Value{k}", 0.0)
        if pts and x <= pts[-1][0]:
            continue
        pts.append((x, y))
    if len(pts) < 2:
        return [], ["no usable transfer curve"], "rough"
    spec = "|".join(f"{f(x)}/{f(y)}" for x, y in pts)
    out = [f"compand=attacks=0.01:decays=0.15:points={spec}:soft-knee=6"]
    makeup = amp_db(p.get("Make-Up Gain", 2 / 3)) if "Make-Up Gain" in p and p["Make-Up Gain"] > 0 else 0.0
    if abs(makeup) >= 0.1:
        out.append(f"volume={f(makeup)}dB")
    return out, ["transfer curve spans -100..0 dB (assumed)"], "close"


def _pitch(p):
    st = (p.get("Transpose Ratio", 0.5) - 0.5) * 24.0
    if abs(st) < 0.05:
        return [], [], "rough"
    r = 2 ** (st / 12)
    return (
        [f"asetrate={SR * r:.2f}", f"aresample={SR}", f"atempo={1 / r:.6f}"],
        [f"pitch {st:+.1f} semitones, assuming the control spans +-12 semitones; change the numbers in 'chain' to adjust"],
        "rough",
    )


def _flanger(p):
    d0 = clamp(p.get("Initial Delay Time", 0.1) * 20.0, 0.0, 30.0)
    d1 = clamp(p.get("Final Delay Time", 0.1) * 20.0, 0.0, 30.0)
    depth = clamp(abs(d1 - d0), 0.0, 10.0)
    speed = clamp(0.1 + p.get("Modulation Rate (Frequency)", 0.1) * 9.9, 0.1, 10.0)
    regen = clamp(p.get("Feedback", 0.0) * 95.0, 0.0, 95.0)
    width = clamp(p.get("Mix", 0.7) * 100.0, 0.0, 100.0)
    phase = clamp(p.get("Stereo Phasing", 0.25) * 100.0, 0.0, 100.0)
    return (
        [f"flanger=delay={f(min(d0, d1))}:depth={f(depth)}:regen={f(regen)}:width={f(width)}:speed={f(speed)}:shape=sinusoidal:phase={f(phase)}"],
        ["delay, rate and feedback ranges are assumed"],
        "rough",
    )


def _reverb(p):
    room = p.get("Room Size", 0.5)
    decay = p.get("Decay", 0.5)
    wet = p.get("Wet Output Level", 0.5)
    d1 = 30 + room * 120
    delays = [d1, d1 * 1.7, d1 * 2.6]
    base = 0.25 + decay * 0.6
    decays = [base, base * 0.7, base * 0.45]
    out_gain = clamp(0.3 + 0.6 * wet, 0.1, 0.95)
    return (
        [f"aecho=0.9:{f(out_gain)}:{'|'.join(f(d) for d in delays)}:{'|'.join(f(x) for x in decays)}"],
        ["FFmpeg has no studio reverb; three echo taps approximate the room"],
        "rough",
    )


def _multiband(p):
    cuts = [p.get(f"Cutoff {k}", 0.5) * FREQ_MAX for k in (1, 2, 3)]
    solo = next((b for b in range(1, 5) if p.get(f"Solo {b}", 0) >= 0.5), None)
    out, notes = [], []
    if solo:
        lo = None if solo == 1 else cuts[solo - 2]
        hi = None if solo == 4 else cuts[solo - 1]
        if lo:
            out.append(f"highpass=f={f(clamp(lo, 20, 20000))}:poles=2")
        if hi:
            out.append(f"lowpass=f={f(clamp(hi, 20, 20000))}:poles=2")
        thr = -60.0 + 60.0 * p.get(f"Threshold {solo}", 1.0)
        ratio = 1.0 + 29.0 * p.get(f"Ratio {solo}", 0.0)
        if ratio > 1.05:
            out.append(f"acompressor=threshold={10 ** (thr / 20):.5f}:ratio={f(ratio)}:attack=5:release=100")
        gain = (p.get(f"Gain {solo}", 0.5) - 0.5) * 12.0
        if abs(gain) >= 0.1:
            out.append(f"volume={f(gain)}dB")
        notes.append(f"band {solo} was soloed in the preset, so only that band's range is kept")
        return out, notes, "close"
    ratios = [1.0 + 29.0 * p.get(f"Ratio {b}", 0.0) for b in range(1, 5)]
    thr = -60.0 + 60.0 * (sum(p.get(f"Threshold {b}", 1.0) for b in range(1, 5)) / 4)
    r = sum(ratios) / 4
    if r > 1.05:
        out.append(f"acompressor=threshold={10 ** (thr / 20):.5f}:ratio={f(r)}:attack=5:release=100")
    notes.append("bands are averaged into one compressor")
    return out, notes, "rough"


def _fft_filter(p):
    n = int(round(clamp(p.get("Number of X/Y pairs", 0.0), 0, 1) * 128))
    pts = [(p.get(f"X {k}", 0.0) * FREQ_MAX, p.get(f"Y {k}", 0.0)) for k in range(1, n + 1)]
    pts = [(clamp(x, 20.0, 23900.0), y) for x, y in pts]
    pts.sort()
    if len(pts) < 2:
        return [], ["no usable curve"], "rough"
    ymax = max(y for _, y in pts) or 1.0
    entries = ";".join(f"entry({f(x)},{f((y / ymax - 1.0) * 60.0)})" for x, y in pts)
    return [f"firequalizer=gain_entry='{entries}'"], ["curve height is mapped to 0 to -60 dB (assumed)"], "close"


def _distortion(p):
    thr = clamp(1.0 - 1.2 * p.get("dB Range", 0.5), 0.05, 1.0)
    return [f"asoftclip=type=tanh:threshold={f(thr)}"], ["soft clipping stands in for Premiere's smoothing curves"], "rough"


CONVERTERS = {
    "Parametric Equalizer": _parametric_eq,
    "Amplify": _amplify,
    "Hard Limiter": _hard_limiter,
    "Single-band Compressor": _compressor,
    "DeNoise": _denoise,
    "DeEsser": _deesser,
    "Dynamics Processing": _dynamics,
    "Pitch Shifter": _pitch,
    "Flanger": _flanger,
    "Studio Reverb": _reverb,
    "Multiband Compressor": _multiband,
    "FFT Filter": _fft_filter,
    "Distortion": _distortion,
}


# --------------------------------------------------------------------------
# Reading a .prfpset
# --------------------------------------------------------------------------
def _num(text: str | None) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _param_values(comp, by_id) -> dict:
    values: dict[str, float] = {}
    for ref in comp.iter("Param"):
        e = by_id.get(ref.get("ObjectRef"))
        if e is None:
            continue
        name = e.findtext("Name")
        if not name or name in SKIP_PARAMS or name in values:
            continue
        cur = e.find("CurrentValue")
        if cur is not None:
            raw = cur.text
        else:
            parts = (e.findtext("StartKeyframe") or "").split(",")
            raw = parts[1] if len(parts) > 1 else None
        v = _num(raw)
        if v is not None:
            values[name] = v
    return values


def read_prfpset(path: Path) -> list[dict]:
    """Parse a .prfpset into a list of preset dicts. Raises EditError if it is not one."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise EditError(f"{path.name} is not a readable Premiere preset file: {exc}")
    if root.tag != "PremiereData":
        raise EditError(f"{path.name} is not a Premiere preset file (root element is '{root.tag}')")
    by_id = {e.get("ObjectID"): e for e in root if e.get("ObjectID")}

    group_of: dict[str, str] = {}
    for b in root.iter("BinTreeItem"):
        label = b.findtext("TreeItemBase/Name") or ""
        for it in b.iter("Item"):
            group_of[it.get("ObjectRef")] = label

    presets = []
    for ti in root.iter("TreeItem"):
        name = (ti.findtext("TreeItemBase/Name") or "").strip()
        data_ref = ti.find("TreeItemBase/Data")
        item = by_id.get(data_ref.get("ObjectRef")) if data_ref is not None else None
        if not name or item is None or item.tag != "FilterPresetItem":
            continue
        variants = []
        for fp in item.iter("FilterPreset"):
            fobj = by_id.get(fp.get("ObjectRef"))
            comp = by_id.get(fobj.find("Component").get("ObjectRef")) if fobj is not None else None
            if comp is None or comp.find("AudioComponent/Component") is None:
                continue
            c = comp.find("AudioComponent/Component")
            variants.append({
                "channel": comp.findtext("AudioComponent/ChannelType") or "0",
                "effect": c.findtext("DisplayName") or "",
                "bypass": (c.findtext("Bypass") or "false") == "true",
                "params": _param_values(c, by_id),
            })
        # The same settings are stored per channel layout (mono, stereo, 5.1). Use stereo.
        effects = [v for v in variants if v["channel"] == "1"]
        if not effects:
            effects = [v for v in variants if v["channel"] == (variants[0]["channel"] if variants else "")]
        presets.append({
            "name": name,
            "group": group_of.get(ti.get("ObjectID"), ""),
            "effects": effects,
        })
    if not presets:
        raise EditError(f"no audio-effect presets found in {path.name}")
    return presets


def convert(preset: dict, source: str) -> dict:
    """Build the stored form: FFmpeg chain plus the original values and honest notes."""
    effects, chain = [], []
    for e in preset["effects"]:
        entry = {"effect": e["effect"], "bypassed": e["bypass"]}
        conv = CONVERTERS.get(e["effect"])
        if e["bypass"]:
            entry.update(filters=[], match="skipped", notes=["effect was bypassed in the preset"])
        elif not conv:
            entry.update(filters=[], match="unsupported",
                         notes=[f"'{e['effect']}' has no FFmpeg equivalent yet; it was skipped"])
        else:
            try:
                filters, notes, match = conv(e["params"])
            except KeyError as exc:
                filters, notes, match = [], [f"missing parameter {exc}; effect skipped"], "unsupported"
            entry.update(filters=filters, match=match, notes=notes)
        p = e["params"]
        if e["effect"] == "FFT Filter":
            n = int(round(clamp(p.get("Number of X/Y pairs", 0.0), 0, 1) * 128))
            p = {k: v for k, v in p.items() if not re.fullmatch(r"[XY] \d+", k) or int(k.split()[1]) <= n}
        entry["params"] = {k: round(v, 6) for k, v in p.items()}
        chain += entry["filters"]
        effects.append(entry)
    return {
        "name": preset["name"],
        "group": preset["group"],
        "source": source,
        "effects": effects,
        "chain": ",".join(chain),
    }


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "preset"


def import_prfpset(path: Path, dest: Path | None = None) -> list[dict]:
    """Extract every preset from a .prfpset into JSON files. Returns the stored presets."""
    dest = dest or USER_DIR
    dest.mkdir(parents=True, exist_ok=True)
    stored = []
    for pr in read_prfpset(path):
        data = convert(pr, path.name)
        (dest / f"{slug(data['name'])}.json").write_text(
            json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        stored.append(data)
    return stored


# --------------------------------------------------------------------------
# Using stored presets
# --------------------------------------------------------------------------
def load_all() -> dict[str, dict]:
    """name (lower case) -> preset. A preset in the user folder overrides a bundled one."""
    found: dict[str, dict] = {}
    for d in (BUNDLED_DIR, USER_DIR):
        for fp in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                found[data["name"].strip().lower()] = data
            except (json.JSONDecodeError, KeyError):
                continue
    return found


def get(name: str) -> dict:
    presets = load_all()
    key = (name or "").strip().lower()
    if key not in presets:
        known = ", ".join(sorted(presets)) or "none installed"
        raise EditError(f"no voice preset '{name}'. Available: {known}")
    return presets[key]


def chain_for(name: str | None) -> str:
    return get(name)["chain"] if name else ""
