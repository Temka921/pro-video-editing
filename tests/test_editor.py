"""Tests for the vedit editor: project ops, rendering, Premiere preset extraction, the web server.

Generates its own media with FFmpeg, so no assets are needed.
Run:  python3 -m unittest discover -s tests -v
"""
import array
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "editor"))

from veditlib import ops, presets, project as P, render, server  # noqa: E402
from veditlib.project import EditError  # noqa: E402

PRFPSET = ROOT / "presets" / "isaacverse-voice-presets.prfpset"
FFMPEG = shutil.which("ffmpeg") is not None


def ff(*args):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *map(str, args)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def decode_mono(path, ar=48000):
    """All samples of a media file's audio as floats in -1..1."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", str(ar),
                        "-f", "s16le", "-"], capture_output=True)
    a = array.array("h")
    a.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    return [x / 32768 for x in a]


def rms(samples):
    return (sum(x * x for x in samples) / max(1, len(samples))) ** 0.5


def tone_through(chain, freq, tmp, dur=2.0):
    """RMS (dB) of a sine after the filter chain, measured on the steady middle of the signal."""
    src, out = Path(tmp) / f"t{freq}.wav", Path(tmp) / f"o{freq}.wav"
    ff("-f", "lavfi", "-i", f"sine=f={freq}:d={dur}:r=48000", "-ar", "48000", "-ac", "2", src)
    ff("-i", src, "-af", chain, out)
    import math
    mid = decode_mono(out)
    mid = mid[int(len(mid) * 0.3): int(len(mid) * 0.9)]
    ref = decode_mono(src)
    ref = ref[int(len(ref) * 0.3): int(len(ref) * 0.9)]
    return 20 * math.log10(max(rms(mid), 1e-9) / rms(ref))


def zero_cross_freq(samples, sr=48000):
    s = samples[int(len(samples) * 0.3): int(len(samples) * 0.9)]
    crossings = sum(1 for a, b in zip(s, s[1:]) if a < 0 <= b)
    return crossings / (len(s) / sr)


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vedit_test_"))
        cls.media = cls.tmp / "media_src"
        cls.media.mkdir()
        ff("-f", "lavfi", "-i", "testsrc2=d=6:s=640x360:r=30", "-f", "lavfi", "-i", "sine=f=330:d=6", "-shortest",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", cls.media / "a.mp4")
        ff("-f", "lavfi", "-i", "smptebars=d=4:s=320x240:r=25", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           cls.media / "silent.mp4")
        ff("-f", "lavfi", "-i", "color=c=orange:s=200x200", "-frames:v", "1", cls.media / "logo.png")
        ff("-f", "lavfi", "-i", "sine=f=440:d=3", cls.media / "tone.wav")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def new(self, name=None, w=640, h=360, fps=30):
        folder = self.tmp / (name or self.id().split(".")[-1])
        return P.create(folder, w, h, fps).path

    def run_ops(self, path, *steps):
        results, proj = ops.apply(path, list(steps))
        return results, proj

    def imp(self, path, file):
        return self.run_ops(path, {"op": "import_asset", "path": str(self.media / file)})[0][0]["asset"]


class TestProjectOps(Base):
    def test_import_probes_media(self):
        path = self.new()
        r, proj = self.run_ops(path, {"op": "import_asset", "path": str(self.media / "a.mp4")},
                               {"op": "import_asset", "path": str(self.media / "logo.png")},
                               {"op": "import_asset", "path": str(self.media / "tone.wav")})
        self.assertEqual([x["kind"] for x in r], ["video", "image", "audio"])
        self.assertTrue(r[0]["has_audio"])
        self.assertAlmostEqual(r[0]["duration"], 6.0, delta=0.1)
        # importing the same file twice returns the same asset
        again, _ = self.run_ops(path, {"op": "import_asset", "path": str(self.media / "a.mp4")})
        self.assertEqual(again[0]["asset"], "a1")

    def test_not_media_is_rejected(self):
        path = self.new()
        bad = self.tmp / "notes.txt"
        bad.write_text("hello")
        with self.assertRaises(EditError):
            self.run_ops(path, {"op": "import_asset", "path": str(bad)})

    def test_add_defaults_and_append(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        r, _ = self.run_ops(path, {"op": "add", "asset": a, "dur": 2}, {"op": "add", "asset": a, "dur": 3})
        self.assertEqual((r[0]["start"], r[0]["end"]), (0, 2))
        self.assertEqual((r[1]["start"], r[1]["end"]), (2, 5))  # appended to the end of the track

    def test_add_checks_source_length(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        with self.assertRaises(EditError) as cm:
            self.run_ops(path, {"op": "add", "asset": a, "src_in": 5, "dur": 4})
        self.assertIn("only", str(cm.exception))

    def test_split_trim_move_remove(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        self.run_ops(path, {"op": "add", "asset": a, "dur": 4, "fade_in": 0.5, "fade_out": 0.5})
        r, proj = self.run_ops(path, {"op": "split", "clip": "c1", "at": 1.5})
        c1, c2 = proj.clip("c1"), proj.clip(r[0]["second"])
        self.assertEqual((c1["start"], c1["dur"], c1["src_in"]), (0, 1.5, 0))
        self.assertEqual((c2["start"], c2["dur"], c2["src_in"]), (1.5, 2.5, 1.5))
        self.assertEqual((c1["fade_in"], c1["fade_out"]), (0.5, 0))   # first half keeps the fade-in
        self.assertEqual((c2["fade_in"], c2["fade_out"]), (0, 0.5))   # second half keeps the fade-out
        # trimming the head moves the in-point so the picture stays in sync
        _, proj = self.run_ops(path, {"op": "trim", "clip": "c2", "start": 2.0})
        c2 = proj.clip("c2")
        self.assertEqual((c2["start"], c2["dur"], c2["src_in"]), (2.0, 2.0, 2.0))
        with self.assertRaises(EditError):  # nothing exists in the source before its start
            self.run_ops(path, {"op": "trim", "clip": "c1", "start": -1})
        _, proj = self.run_ops(path, {"op": "move", "clip": "c2", "at": 10, "track": "V2"})
        self.assertEqual((proj.clip("c2")["start"], proj.clip("c2")["track"]), (10, "V2"))
        _, proj = self.run_ops(path, {"op": "remove", "clip": "c2"})
        self.assertEqual([c["id"] for c in proj.clips], ["c1"])

    def test_split_outside_clip_is_rejected(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        self.run_ops(path, {"op": "add", "asset": a, "dur": 2})
        for t in (0, 2, 5):
            with self.assertRaises(EditError):
                self.run_ops(path, {"op": "split", "clip": "c1", "at": t})

    def test_ripple_remove_closes_gap_on_same_track_only(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        self.run_ops(path, {"op": "add", "asset": a, "dur": 2}, {"op": "add", "asset": a, "dur": 2},
                     {"op": "add", "asset": a, "dur": 2, "track": "V2", "at": 4})
        _, proj = self.run_ops(path, {"op": "remove", "clip": "c1", "ripple": True})
        self.assertEqual(proj.clip("c2")["start"], 0)
        self.assertEqual(proj.clip("c3")["start"], 4)  # other track untouched

    def test_set_validates(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        self.run_ops(path, {"op": "add", "asset": a, "dur": 2})
        _, proj = self.run_ops(path, {"op": "set", "clip": "c1", "volume_db": "-6", "mute": "yes", "preset": "phone call"})
        c = proj.clip("c1")
        self.assertEqual((c["volume_db"], c["mute"], c["preset"]), (-6.0, True, "phone call"))
        for bad in ({"speed": 9}, {"opacity": 3}, {"nonsense": 1}, {"preset": "no such preset"}, {"text": "x"}):
            with self.assertRaises(EditError, msg=str(bad)):
                self.run_ops(path, {"op": "set", "clip": "c1", **bad})

    def test_batch_is_all_or_nothing_and_undo_reverts_it(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        before = P.load(path).data["rev"]
        with self.assertRaises(EditError) as cm:
            self.run_ops(path, {"op": "add", "asset": a, "dur": 2}, {"op": "add", "asset": "zzz"})
        self.assertIn("step 2", str(cm.exception))
        self.assertEqual(P.load(path).clips, [])            # step 1 was not kept
        self.assertEqual(P.load(path).data["rev"], before)  # and nothing was saved
        self.run_ops(path, {"op": "add", "asset": a, "dur": 2}, {"op": "add", "asset": a, "dur": 1})
        self.assertEqual(len(P.load(path).clips), 2)
        P.undo(path)
        self.assertEqual(P.load(path).clips, [])            # one undo reverts the whole batch

    def test_undo_with_no_history(self):
        path = self.new()
        with self.assertRaises(EditError):
            P.undo(path)

    def test_check_reports_problems(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        self.run_ops(path, {"op": "add", "asset": a, "dur": 2, "at": 1})
        res = ops.check(P.load(path))
        self.assertTrue(any("black" in w for w in res["warnings"]))  # gap before the first clip
        (self.media / "gone.mp4").write_bytes((self.media / "a.mp4").read_bytes())
        self.imp(path, "gone.mp4")
        (self.media / "gone.mp4").unlink()
        self.assertTrue(any("missing" in e for e in ops.check(P.load(path))["errors"]))

    def test_text_clip_defaults_to_top_video_track(self):
        path = self.new()
        r, proj = self.run_ops(path, {"op": "add_text", "text": "Hi", "at": 0, "dur": 2})
        self.assertEqual(r[0]["track"], "V2")
        with self.assertRaises(EditError):
            self.run_ops(path, {"op": "add_text", "text": "  ", "at": 0, "dur": 2})


class TestRender(Base):
    def info(self, path):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                           capture_output=True, text=True)
        j = json.loads(r.stdout)
        v = next(s for s in j["streams"] if s["codec_type"] == "video")
        has_a = any(s["codec_type"] == "audio" for s in j["streams"])
        return float(j["format"]["duration"]), int(v["width"]), int(v["height"]), has_a

    def test_render_composites_and_keeps_length_size_and_audio(self):
        path = self.new(w=640, h=360)
        a, b, logo = self.imp(path, "a.mp4"), self.imp(path, "silent.mp4"), self.imp(path, "logo.png")
        self.run_ops(path,
                     {"op": "add", "asset": a, "dur": 3},
                     {"op": "add", "asset": b, "dur": 2, "fade_in": 0.3},
                     {"op": "add", "asset": logo, "track": "V2", "at": 1, "dur": 2, "box": "0.7,0.05,0.25,0.25"},
                     {"op": "add_text", "text": "Hello: 100% 'quoted'", "at": 0.5, "dur": 2})
        out = render.render(P.load(path), preview=False)
        dur, w, h, has_a = self.info(out)
        self.assertAlmostEqual(dur, 5.0, delta=0.1)
        self.assertEqual((w, h), (640, 360))
        self.assertTrue(has_a)

    def test_preview_is_smaller(self):
        path = self.new(w=1280, h=720)
        a = self.imp(path, "a.mp4")
        self.run_ops(path, {"op": "add", "asset": a, "dur": 1})
        out = render.render(P.load(path), preview=True)
        _, w, h, _ = self.info(out)
        self.assertEqual((w, h), (640, 360))

    def test_snapshot_shows_the_requested_time(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        self.run_ops(path, {"op": "add", "asset": a, "dur": 4})
        png = render.snapshot(P.load(path), 2.0)
        self.assertTrue(png.is_file() and png.stat().st_size > 1000)

    def test_clip_audio_lands_at_its_start_time(self):
        path = self.new()
        t = self.imp(path, "tone.wav")
        a = self.imp(path, "silent.mp4")
        self.run_ops(path, {"op": "add", "asset": a, "dur": 4},
                     {"op": "add", "asset": t, "track": "A1", "at": 2.0, "dur": 1.0, "volume_db": -6})
        samples = decode_mono(render.render(P.load(path), preview=True))
        sr = 48000
        loud = [i for i, x in enumerate(samples) if abs(x) > 0.02]
        self.assertAlmostEqual(loud[0] / sr, 2.0, delta=0.06)
        self.assertAlmostEqual(loud[-1] / sr, 3.0, delta=0.06)

    def test_speed_changes_length_not_pitch(self):
        path = self.new()
        t = self.imp(path, "tone.wav")
        self.run_ops(path, {"op": "add", "asset": t, "track": "A1", "dur": 1.5, "speed": 2})  # 3s source -> 1.5s
        self.assertEqual(P.load(path).duration(), 1.5)
        samples = decode_mono(render.render(P.load(path), preview=True))
        self.assertAlmostEqual(zero_cross_freq(samples), 440, delta=15)

    def test_sfx_track_ducks_under_dialogue(self):
        path = self.new()
        a = self.imp(path, "a.mp4")   # carries a 330 Hz tone
        t = self.imp(path, "tone.wav")
        self.run_ops(path, {"op": "add", "asset": a, "dur": 3}, {"op": "add", "asset": t, "track": "A2", "dur": 3, "volume_db": 0})
        ducked = decode_mono(render.render(P.load(path), preview=True))
        self.run_ops(path, {"op": "set_track", "id": "A2", "duck": False})
        flat = decode_mono(render.render(P.load(path), preview=True))
        mid = slice(48000, 96000)
        self.assertLess(rms(ducked[mid]), rms(flat[mid]) * 0.85)

    def test_render_refuses_a_broken_project_with_a_clear_message(self):
        path = self.new()
        with self.assertRaises(EditError):
            render.render(P.load(path))   # empty timeline
        (self.media / "tmp_copy.mp4").write_bytes((self.media / "a.mp4").read_bytes())
        c = self.imp(path, "tmp_copy.mp4")
        self.run_ops(path, {"op": "add", "asset": c, "dur": 1})
        (self.media / "tmp_copy.mp4").unlink()
        with self.assertRaises(EditError) as cm:
            render.render(P.load(path))
        self.assertIn("missing", str(cm.exception))


class TestSfxPlacement(Base):
    def test_sfx_peak_lands_on_the_requested_time(self):
        sfx_dir = self.tmp / "sfxsrc"
        sfx_dir.mkdir()
        # A click whose loudest sample is 0.3 s into the file.
        ff("-f", "lavfi", "-i", "aevalsrc='if(lt(t,0.3),0,exp(-(t-0.3)*40)*sin(2*PI*800*(t-0.3)))':d=1:s=48000",
           sfx_dir / "Door_Slam_Heavy.wav")
        zpath = self.tmp / "tiny_pack.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            z.write(sfx_dir / "Door_Slam_Heavy.wav", "Impacts/Door_Slam_Heavy.wav")
        cat = self.tmp / "tiny_catalog.json"
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_catalog.py"), str(zpath), "--out", str(cat),
                            "--pack", "tiny"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        sound = json.loads(cat.read_text())["sounds"][0]
        self.assertAlmostEqual(sound["peak_s"], 0.3, delta=0.01)

        path = self.new()
        v = self.imp(path, "silent.mp4")
        self.run_ops(path, {"op": "add", "asset": v, "dur": 4})
        res, proj = self.run_ops(path, {"op": "add_sfx", "sound": sound["id"], "at": 2.0, "gain_db": 0,
                                        "catalog": str(cat), "library": str(zpath)})
        clip = proj.clip(res[0]["clip"])
        self.assertAlmostEqual(clip["start"], 2.0 - sound["peak_s"], delta=0.002)
        self.assertEqual(clip["track"], "A2")
        self.assertTrue((proj.dir / "media" / "sfx" / f"{sound['id']}.wav").is_file())  # project is self-contained
        samples = decode_mono(render.render(proj, preview=True))
        peak_at = max(range(len(samples)), key=lambda i: abs(samples[i])) / 48000
        self.assertAlmostEqual(peak_at, 2.0, delta=0.03)

        # a hit near the start has its head trimmed so the peak still lands on time
        res, proj = self.run_ops(path, {"op": "add_sfx", "sound": sound["id"], "at": 0.1, "catalog": str(cat),
                                        "library": str(zpath)})
        c = proj.clip(res[0]["clip"])
        self.assertEqual(c["start"], 0)
        self.assertAlmostEqual(c["src_in"], 0.2, delta=0.01)

    def test_unknown_sound_id(self):
        path = self.new()
        with self.assertRaises(EditError):  # whether or not a catalog exists on this machine
            ops.OPS["add_sfx"](P.load(path), sound="deadbeef", at=1)


@unittest.skipUnless(PRFPSET.is_file(), "bundled prfpset missing")
class TestPrfpset(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dest = cls.tmp / "imported"
        cls.stored = {s["name"]: s for s in presets.import_prfpset(PRFPSET, cls.dest)}

    def test_extracts_every_preset_with_group_and_effects(self):
        expected = {"90s", "alien voice", "chipmunk", "inner monologue", "pilot voice", "robot voice",
                    "torture viewers", "villain voice", "deeper voice", "echo", "movie trailer voice",
                    "muffled voice", "noise remover", "phone call", "voice enhancer"}
        self.assertEqual(set(self.stored), expected)
        self.assertEqual(self.stored["phone call"]["group"], "useful")
        self.assertEqual(self.stored["robot voice"]["group"], "fun")
        self.assertEqual([e["effect"] for e in self.stored["robot voice"]["effects"]], ["Flanger", "Pitch Shifter"])
        self.assertEqual(len(list(self.dest.glob("*.json"))), 15)

    def test_original_parameters_are_kept(self):
        eq = self.stored["muffled voice"]["effects"][0]
        self.assertIn("High Shelf Frequency", eq["params"])
        self.assertTrue(0 <= eq["params"]["High Shelf Frequency"] <= 1)

    def test_every_chain_runs_in_ffmpeg(self):
        for name, s in self.stored.items():
            with self.subTest(preset=name):
                self.assertTrue(s["chain"], "preset produced no filters")
                out = self.tmp / "chain_out.wav"
                ff("-f", "lavfi", "-i", "sine=f=300:d=1:r=48000", "-ar", "48000", "-ac", "2", "-af", s["chain"], out)
                self.assertGreater(out.stat().st_size, 10000)

    def test_chains_do_what_their_names_say(self):
        chain = {n: s["chain"] for n, s in self.stored.items()}
        # pitch: chipmunk rises, deeper voice and villain fall
        self.assertGreater(zero_cross_freq(self.run_chain(chain["chipmunk"], 440)), 470)
        self.assertLess(zero_cross_freq(self.run_chain(chain["deeper voice"], 440)), 410)
        self.assertLess(zero_cross_freq(self.run_chain(chain["villain voice"], 440)), 380)
        # muffled voice cuts the highs but leaves the lows
        self.assertLess(tone_through(chain["muffled voice"], 10000, self.tmp), -10)
        self.assertGreater(tone_through(chain["muffled voice"], 200, self.tmp), -2)
        # phone call cuts deep lows, keeps the mids
        self.assertLess(tone_through(chain["phone call"], 60, self.tmp), -10)
        self.assertGreater(tone_through(chain["phone call"], 2000, self.tmp), -2)
        # pilot voice keeps only a mid band
        self.assertLess(tone_through(chain["pilot voice"], 200, self.tmp), -12)
        self.assertLess(tone_through(chain["pilot voice"], 9000, self.tmp), -12)
        self.assertGreater(tone_through(chain["pilot voice"], 2000, self.tmp), -6)
        # 90s keeps 130 Hz..3 kHz and drops the rest
        self.assertLess(tone_through(chain["90s"], 15000, self.tmp), -20)
        self.assertGreater(tone_through(chain["90s"], 1000, self.tmp), -6)

    def run_chain(self, chain, freq):
        out = self.tmp / f"rc{freq}.wav"
        src = self.tmp / f"rcs{freq}.wav"
        ff("-f", "lavfi", "-i", f"sine=f={freq}:d=2:r=48000", "-ar", "48000", "-ac", "2", src)
        ff("-i", src, "-af", chain, out)
        return decode_mono(out)

    def test_preset_on_a_clip_changes_the_render(self):
        path = self.new()
        t = self.imp(path, "tone.wav")
        self.run_ops(path, {"op": "add", "asset": t, "track": "A1", "dur": 2})
        plain = decode_mono(render.render(P.load(path), preview=True))
        self.run_ops(path, {"op": "set", "clip": "c1", "preset": "phone call"})
        self.run_ops(path, {"op": "set", "clip": "c1", "preset": "chipmunk"})
        shifted = decode_mono(render.render(P.load(path), preview=True))
        self.assertGreater(zero_cross_freq(shifted), zero_cross_freq(plain) * 1.15)

    def test_rejects_files_that_are_not_prfpset(self):
        junk = self.tmp / "junk.prfpset"
        junk.write_text("this is not xml")
        with self.assertRaises(EditError):
            presets.import_prfpset(junk, self.tmp / "x")
        other = self.tmp / "other.prfpset"
        other.write_text("<?xml version='1.0'?><Something/>")
        with self.assertRaises(EditError):
            presets.import_prfpset(other, self.tmp / "x")
        empty = self.tmp / "empty.prfpset"
        empty.write_text("<?xml version='1.0'?><PremiereData Version='3'></PremiereData>")
        with self.assertRaises(EditError):
            presets.import_prfpset(empty, self.tmp / "x")

    def test_unknown_effect_is_reported_not_hidden(self):
        pr = {"name": "x", "group": "", "effects": [{"effect": "Mystery Effect", "bypass": False, "params": {}, "channel": "1"}]}
        s = presets.convert(pr, "t.prfpset")
        self.assertEqual(s["effects"][0]["match"], "unsupported")
        self.assertEqual(s["chain"], "")


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class TestServer(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.path = P.create(cls.tmp / "served", 640, 360, 30).path
        ops.apply(cls.path, [{"op": "import_asset", "path": str(cls.media / "a.mp4")}, {"op": "add", "asset": "a1", "dur": 2}])
        cls.state = server.State(cls.path)
        cls.httpd = server.Server(("127.0.0.1", 0), server.make_handler(cls.state, 0))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def call(self, method, url, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = dict(headers or {})
        c.putrequest(method, url, skip_host=True)
        c.putheader("Host", host or f"127.0.0.1:{self.port}")
        for k, v in hdrs.items():
            c.putheader(k, v)
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else b"")
        c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw, dict(r.getheaders())

    def post(self, url, body):
        s, raw, _ = self.call("POST", url, body, {"X-Vedit": "1", "Content-Type": "application/json"})
        return s, json.loads(raw)

    def test_page_and_state(self):
        s, raw, _ = self.call("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn(b"vedit editor", raw)
        s, raw, _ = self.call("GET", "/api/state")
        d = json.loads(raw)
        self.assertEqual(d["project"]["clips"][0]["id"], "c1")
        self.assertIn("phone call", d["presets"])
        s, raw, _ = self.call("GET", f"/api/state?since={d['project']['rev']}")
        self.assertTrue(json.loads(raw)["unchanged"])

    def test_edit_through_api_is_seen_in_the_file(self):
        s, d = self.post("/api/op", {"steps": [{"op": "set", "clip": "c1", "volume_db": -3}]})
        self.assertEqual(s, 200)
        self.assertEqual(P.load(self.path).clip("c1")["volume_db"], -3)
        s, d = self.post("/api/op", {"steps": [{"op": "set", "clip": "c1", "speed": 99}]})
        self.assertEqual(s, 400)
        self.assertIn("error", d)
        s, d = self.post("/api/undo", {})
        self.assertEqual(s, 200)
        self.assertEqual(P.load(self.path).clip("c1").get("volume_db", 0), 0)

    def test_blocks_cross_site_and_foreign_hosts(self):
        s, _, _ = self.call("POST", "/api/op", {"steps": []}, {"Content-Type": "application/json"})
        self.assertEqual(s, 403)  # no X-Vedit header: a web page on another site cannot send this
        s, _, _ = self.call("GET", "/api/state", host="evil.example")
        self.assertEqual(s, 403)  # DNS rebinding
        s, _, _ = self.call("POST", "/api/op", {"steps": []}, {"X-Vedit": "1", "Origin": "https://evil.example"})
        self.assertEqual(s, 403)

    def test_cannot_read_files_outside_out(self):
        for url in ("/out/../project.json", "/out/..%2Fproject.json", "/out/.render.log", "/media/a.mp4", "/project.json"):
            s, _, _ = self.call("GET", url)
            self.assertEqual(s, 404, url)

    def test_render_job_and_range_requests(self):
        s, d = self.post("/api/render", {"preview": True})
        self.assertEqual(s, 200)
        import time
        for _ in range(100):
            time.sleep(0.2)
            state = json.loads(self.call("GET", "/api/state")[1])
            if state["job"]["state"] != "running":
                break
        self.assertEqual(state["job"]["state"], "done", state["job"])
        self.assertEqual(state["renders"]["preview"]["rev"], state["project"]["rev"])
        s, raw, h = self.call("GET", "/out/preview.mp4", headers={"Range": "bytes=0-99"})
        self.assertEqual((s, len(raw)), (206, 100))
        self.assertTrue(h["Content-Range"].startswith("bytes 0-99/"))
        s, raw, _ = self.call("GET", "/out/preview.mp4", headers={"Range": "bytes=999999999-"})
        self.assertEqual(s, 416)

    def test_upload_imports_media_and_rejects_junk(self):
        data = (self.media / "tone.wav").read_bytes()
        s, raw, _ = self.call("POST", "/api/upload?name=my%20tone.wav", data, {"X-Vedit": "1"})
        self.assertEqual(s, 200, raw)
        self.assertEqual(json.loads(raw)["kind"], "audio")
        self.assertTrue((P.load(self.path).dir / "media" / "my tone.wav").is_file())
        s, raw, _ = self.call("POST", "/api/upload?name=..%2F..%2Fevil.txt", b"not media", {"X-Vedit": "1"})
        self.assertEqual(s, 400)
        self.assertFalse((P.load(self.path).dir.parent / "evil.txt").exists())
        self.assertFalse(any(f.suffix == ".txt" for f in (P.load(self.path).dir / "media").iterdir()))


class TestCli(Base):
    def vedit(self, *args, ok=True):
        r = subprocess.run([sys.executable, "-I", str(ROOT / "editor" / "vedit.py"), *map(str, args)],
                           capture_output=True, text=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_full_flow_from_the_command_line(self):
        folder = self.tmp / "cli_flow"
        self.vedit("new", folder, "--size", "640x360")
        pf = ["-p", folder]
        a = json.loads(self.vedit("import", self.media / "a.mp4", *pf).stdout)["asset"]
        self.vedit("add", a, "--dur", "3", "--preset", "deeper voice", *pf)
        self.vedit("text", "Title", "--at", "0", "--dur", "2", *pf)
        self.vedit("split", "c1", "--at", "1", *pf)
        self.vedit("set", "c2", "volume_db=-4", "fade_out=0.5", *pf)
        r = self.vedit("check", *pf)
        self.assertEqual(json.loads(r.stdout)["errors"], [])
        out = json.loads(self.vedit("render", "--preview", *pf).stdout)["rendered"]
        self.assertTrue(Path(out).is_file())
        shown = json.loads(self.vedit("show", "--json", *pf).stdout)
        self.assertEqual(len(shown["clips"]), 3)
        self.vedit("undo", *pf)
        self.assertEqual(len(json.loads(self.vedit("show", "--json", *pf).stdout)["clips"]), 3)  # undid the set
        batch = self.tmp / "b.json"
        batch.write_text(json.dumps([{"op": "remove", "clip": "c3"}, {"op": "set", "clip": "c1", "volume_db": -2}]))
        self.vedit("batch", batch, *pf)
        self.assertEqual(len(json.loads(self.vedit("show", "--json", *pf).stdout)["clips"]), 2)

    def test_errors_are_plain_and_exit_nonzero(self):
        folder = self.tmp / "cli_err"
        self.vedit("new", folder)
        r = self.vedit("add", "a9", "-p", folder, ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("error:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_presets_commands(self):
        r = self.vedit("presets", "list")
        names = {p["name"] for p in json.loads(r.stdout)}
        self.assertIn("phone call", names)
        dest = self.tmp / "imp_cli"
        r = self.vedit("presets", "import", PRFPSET, "--dest", dest)
        self.assertEqual(len(json.loads(r.stdout)["imported"]), 15)

    def test_analyze_finds_silence(self):
        ff("-f", "lavfi", "-i", "sine=f=440:d=1", self.tmp / "s1.wav")
        ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1.5", self.tmp / "s2.wav")
        ff("-i", self.tmp / "s1.wav", "-i", self.tmp / "s2.wav", "-i", self.tmp / "s1.wav", "-filter_complex",
           "[0:a][1:a][2:a]concat=n=3:v=0:a=1[a]", "-map", "[a]", self.tmp / "gap.wav")
        res = json.loads(self.vedit("analyze", self.tmp / "gap.wav").stdout)
        self.assertEqual(len(res["silences"]), 1)
        self.assertAlmostEqual(res["silences"][0]["start"], 1.0, delta=0.1)
        self.assertAlmostEqual(res["silences"][0]["end"], 2.5, delta=0.1)


if __name__ == "__main__":
    unittest.main()
