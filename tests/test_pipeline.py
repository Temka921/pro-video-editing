"""End-to-end test with synthetic sounds whose hit times are known.

Run:  python3 -m unittest discover -s tests -v
Requires FFmpeg and ffprobe on PATH. No audio files are needed; they are generated.
"""
from __future__ import annotations

import array
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def run(*args, check=True):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(f"{args}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
    return r


def ff(*args):
    r = subprocess.run(["ffmpeg", "-y", "-nostdin", "-v", "error", *map(str, args)], capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(r.stderr)


def peak_time(path, sr=16000) -> float:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True,
    )
    a = array.array("h")
    a.frombytes(r.stdout)
    i = max(range(len(a)), key=lambda k: abs(a[k]))
    return i / sr


@unittest.skipUnless(HAVE_FFMPEG, "FFmpeg not installed")
class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pve_test_"))
        src = cls.tmp / "src" / "Impacts"
        src.mkdir(parents=True)
        (cls.tmp / "src" / "Whooshes").mkdir()
        (cls.tmp / "src" / "UI").mkdir()
        (cls.tmp / "src" / "Ambience").mkdir()

        # Impact: loud hit at 0.5 s, decays over 1 s.
        ff("-f", "lavfi", "-i",
           "aevalsrc=exprs='if(lt(t,0.5),0.0005*sin(2*PI*300*t),0.9*exp(-8*(t-0.5))*sin(2*PI*300*t))':d=1.5:s=44100",
           cls.tmp / "src" / "Impacts" / "Heavy_Impact_Boom_01.wav")
        # Whoosh: noise swell peaking at 0.8 s.
        ff("-f", "lavfi", "-i",
           "aevalsrc=exprs='0.8*exp(-pow(t-0.8,2)/0.02)*(random(0)*2-1)':d=1.4:s=44100",
           cls.tmp / "src" / "Whooshes" / "Whoosh_Fast_Airy_02.wav")
        # UI click: tiny, peak near 0.02 s.
        ff("-f", "lavfi", "-i",
           "aevalsrc=exprs='0.7*exp(-120*t)*sin(2*PI*1800*t)':d=0.15:s=44100",
           cls.tmp / "src" / "UI" / "UI_Click_Soft.wav")
        # Ambience bed: 8 s of steady pink noise.
        ff("-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:a=0.3:r=44100",
           cls.tmp / "src" / "Ambience" / "Ambience_Forest_Birds_Loop.wav")

        # Library as a zip, and as a folder-of-zips (like multi-part downloads).
        cls.zip_path = cls.tmp / "Test_Pack.zip"
        with zipfile.ZipFile(cls.zip_path, "w") as z:
            for f in (cls.tmp / "src").rglob("*.wav"):
                z.write(f, f.relative_to(cls.tmp / "src").as_posix())
            z.writestr("__MACOSX/._junk.wav", b"x")
        cls.multi = cls.tmp / "multi"
        cls.multi.mkdir()
        shutil.copy(cls.zip_path, cls.multi / "part1.zip")

        # 6 s test video with quiet audio.
        cls.video = cls.tmp / "in.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=6", "-f", "lavfi", "-i", "sine=f=220:d=6",
           "-af", "volume=0.05", "-c:v", "libx264" if _has_x264() else "mpeg4", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", cls.video)

        cls.catalog = cls.tmp / "catalog.json"
        run(SCRIPTS / "build_catalog.py", cls.zip_path, "--out", cls.catalog, "--jobs", "2")
        cls.cat = json.loads(cls.catalog.read_text())
        cls.by_name = {Path(s["path"]).stem: s for s in cls.cat["sounds"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- catalog ---------------------------------------------------------
    def test_catalog_contents(self):
        self.assertEqual(self.cat["count"], 4)  # __MACOSX junk skipped
        imp = self.by_name["Heavy_Impact_Boom_01"]
        self.assertAlmostEqual(imp["peak_s"], 0.5, delta=0.02)
        self.assertIn(imp["category"], ("impact", "boom"))
        wh = self.by_name["Whoosh_Fast_Airy_02"]
        self.assertEqual(wh["category"], "whoosh")
        self.assertAlmostEqual(wh["peak_s"], 0.8, delta=0.12)
        self.assertEqual(self.by_name["UI_Click_Soft"]["category"], "ui")
        self.assertEqual(self.by_name["Ambience_Forest_Birds_Loop"]["kind"], "bed")
        self.assertEqual(imp["kind"], "one-shot")

    def test_multi_zip_folder(self):
        out = self.tmp / "multi_catalog.json"
        run(SCRIPTS / "build_catalog.py", self.multi, "--out", out)
        cat = json.loads(out.read_text())
        self.assertEqual(cat["count"], 4)
        self.assertTrue(all(s.get("container") == "part1.zip" for s in cat["sounds"]))

    # ---- search ----------------------------------------------------------
    def test_search_meaning(self):
        r = run(SCRIPTS / "sfx_search.py", "swoosh", "--catalog", self.catalog, "--json")  # synonym of whoosh
        names = [h["name"] for h in json.loads(r.stdout)]
        self.assertEqual(names[0], "Whoosh_Fast_Airy_02")

    def test_search_filters_and_exclude(self):
        r = run(SCRIPTS / "sfx_search.py", "--category", "ui", "--max-dur", "1", "--catalog", self.catalog, "--json")
        self.assertEqual([h["name"] for h in json.loads(r.stdout)], ["UI_Click_Soft"])
        sid = self.by_name["UI_Click_Soft"]["id"]
        r = run(SCRIPTS / "sfx_search.py", "click", "--exclude", sid, "--catalog", self.catalog)
        self.assertIn("no matches", r.stdout)

    # ---- fetch -----------------------------------------------------------
    def test_fetch(self):
        out = self.tmp / "fetched"
        sid = self.by_name["UI_Click_Soft"]["id"]
        r = run(SCRIPTS / "sfx_fetch.py", sid, "--out", out, "--catalog", self.catalog, "--library", self.zip_path)
        self.assertTrue(Path(r.stdout.strip()).is_file())

    # ---- render: sync accuracy ------------------------------------------
    def _sheet(self, cues, **extra):
        p = self.tmp / "cues.json"
        p.write_text(json.dumps({"cues": cues, **extra}))
        return p

    def test_sync_peak_lands_on_time(self):
        sid = self.by_name["Heavy_Impact_Boom_01"]["id"]
        sheet = self._sheet([{"id": sid, "at": 3.0, "gain_db": -6}])
        stem = self.tmp / "stem.wav"
        run(SCRIPTS / "render_mix.py", self.video, sheet, "--sfx-only", stem,
            "--catalog", self.catalog, "--library", self.zip_path)
        self.assertAlmostEqual(peak_time(stem), 3.0, delta=0.02)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", stem],
                                   capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dur, 6.0, delta=0.1)  # stem is padded to the video length

    def test_sync_when_sound_must_start_before_zero(self):
        # Peak is 0.5 s into the file; landing it at 0.2 s needs the head trimmed.
        sid = self.by_name["Heavy_Impact_Boom_01"]["id"]
        sheet = self._sheet([{"id": sid, "at": 0.2}])
        stem = self.tmp / "stem2.wav"
        run(SCRIPTS / "render_mix.py", self.video, sheet, "--sfx-only", stem,
            "--catalog", self.catalog, "--library", self.zip_path)
        self.assertAlmostEqual(peak_time(stem), 0.2, delta=0.03)

    def test_sync_onset_mode_and_frame_cue(self):
        sid = self.by_name["Heavy_Impact_Boom_01"]["id"]
        # frame 75 at 25 fps = 3.0 s
        sheet = self._sheet([{"id": sid, "at_frame": 75, "sync": "peak"}], fps=25)
        stem = self.tmp / "stem3.wav"
        run(SCRIPTS / "render_mix.py", self.video, sheet, "--sfx-only", stem,
            "--catalog", self.catalog, "--library", self.zip_path)
        self.assertAlmostEqual(peak_time(stem), 3.0, delta=0.02)

    def test_pitched_cue_still_lands_on_time(self):
        # Regression: atempo + atrim=duration once moved pitched cues to the wrong time.
        sid = self.by_name["Heavy_Impact_Boom_01"]["id"]
        sheet = self._sheet([{"id": sid, "at": 3.0, "pitch_semitones": -5}], duck=False)
        stem = self.tmp / "stem_pitch.wav"
        run(SCRIPTS / "render_mix.py", self.video, sheet, "--sfx-only", stem,
            "--catalog", self.catalog, "--library", self.zip_path)
        self.assertAlmostEqual(peak_time(stem), 3.0, delta=0.03)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", stem],
                                   capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dur, 6.0, delta=0.1)

    def test_pitched_cue_in_full_render(self):
        silent = self.tmp / "silent_p.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=5", "-c:v", "libx264" if _has_x264() else "mpeg4",
           "-pix_fmt", "yuv420p", silent)
        sid = self.by_name["Heavy_Impact_Boom_01"]["id"]
        out = self.tmp / "pitched_full.mp4"
        run(SCRIPTS / "render_mix.py", silent, self._sheet([{"id": sid, "at": 3.0, "pitch_semitones": 4}]),
            "-o", out, "--catalog", self.catalog, "--library", self.zip_path)
        self.assertAlmostEqual(peak_time(out), 3.0, delta=0.05)

    def test_ducking_lowers_sfx_under_loud_audio(self):
        loud = self.tmp / "loud.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=6", "-f", "lavfi", "-i", "sine=f=1000:d=6",
           "-af", "volume=2", "-c:v", "libx264" if _has_x264() else "mpeg4", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", loud)
        sid = self.by_name["Heavy_Impact_Boom_01"]["id"]

        def hit_level(duck: bool) -> float:
            out = self.tmp / f"duck_{duck}.mp4"
            run(SCRIPTS / "render_mix.py", loud, self._sheet([{"id": sid, "at": 3.0, "gain_db": -3}], duck=duck),
                "-o", out, "--catalog", self.catalog, "--library", self.zip_path)
            r = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "info", "-ss", "3.0", "-t", "0.3", "-i", str(out), "-vn", "-af",
                 "bandpass=f=300:width_type=h:w=60,volumedetect", "-f", "null", "-"],
                capture_output=True, text=True)
            return float(r.stderr.split("max_volume:")[1].split("dB")[0])

        self.assertLess(hit_level(True), hit_level(False) - 6)  # ducked by well over 6 dB

    def test_full_render_keeps_video_and_adds_audio(self):
        a = self.by_name["Whoosh_Fast_Airy_02"]["id"]
        b = self.by_name["Heavy_Impact_Boom_01"]["id"]
        c = self.by_name["UI_Click_Soft"]["id"]
        sheet = self._sheet([
            {"id": a, "at": 1.0}, {"id": b, "at": 2.0, "gain_db": -6},
            {"id": c, "at": 4.0, "pitch_semitones": 3}, {"id": c, "at": 5.0},
        ])
        out = self.tmp / "out.mp4"
        r = run(SCRIPTS / "render_mix.py", self.video, sheet, "-o", out,
                "--catalog", self.catalog, "--library", self.zip_path)
        self.assertTrue(out.is_file())
        info = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,duration", "-of", "json", out],
            capture_output=True, text=True).stdout)
        kinds = {s["codec_type"]: s for s in info["streams"]}
        self.assertIn("video", kinds)
        self.assertIn("audio", kinds)
        self.assertAlmostEqual(float(kinds["audio"]["duration"]), 6.0, delta=0.2)
        # Quiet original (0.05) + SFX: the output must be clearly louder than the input alone.
        qc = run(SCRIPTS / "qc_audio.py", out, "--target", "-30", "--tolerance", "40", check=False)
        self.assertIn("integrated loudness", qc.stdout)
        self.assertIn("same sound" , r.stdout)  # the repeated click within 8 s is flagged

    def test_video_without_audio(self):
        silent = self.tmp / "silent.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=4", "-c:v", "libx264" if _has_x264() else "mpeg4",
           "-pix_fmt", "yuv420p", silent)
        sid = self.by_name["Heavy_Impact_Boom_01"]["id"]
        out = self.tmp / "silent_out.mp4"
        run(SCRIPTS / "render_mix.py", silent, self._sheet([{"id": sid, "at": 2.0}]), "-o", out,
            "--catalog", self.catalog, "--library", self.zip_path)
        self.assertTrue(out.is_file())

    # ---- lint ------------------------------------------------------------
    def test_check_reports_errors(self):
        sheet = self._sheet([{"id": "nope", "at": 1.0}, {"id": self.by_name["UI_Click_Soft"]["id"], "at": 99}])
        r = run(SCRIPTS / "render_mix.py", self.video, sheet, "--check", "--catalog", self.catalog, check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown sound id", r.stdout)
        self.assertIn("outside the video", r.stdout)

    def test_missing_library_gives_actionable_error(self):
        sid = self.by_name["UI_Click_Soft"]["id"]
        bad = json.loads(self.catalog.read_text())
        bad["packs"]["Test_Pack"]["path"] = str(self.tmp / "gone.zip")
        cat2 = self.tmp / "cat_missing.json"
        cat2.write_text(json.dumps(bad))
        r = run(SCRIPTS / "render_mix.py", self.video, self._sheet([{"id": sid, "at": 1.0}]), "-o", self.tmp / "x.mp4",
                "--catalog", cat2, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--library", r.stderr)


def _has_x264() -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return "libx264" in r.stdout


if __name__ == "__main__":
    unittest.main()
