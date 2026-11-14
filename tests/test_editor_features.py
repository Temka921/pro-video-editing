"""Tests for keyframes, color grading, chroma key, transitions, captions, loudness and the related UI plumbing.

Most tests render real frames and measure pixels (brightness and color of a region) or sound, so they
check what the viewer would see and hear, not just that a command ran.
Run:  python3 -m unittest discover -s tests -v
"""
import array
import json
import math
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "editor"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from test_editor import Base, ff, decode_mono, rms
except ImportError:  # run as tests.test_editor_features
    from tests.test_editor import Base, ff, decode_mono, rms

from veditlib import analyze, grade, keys, ops, project as P, render, server  # noqa: E402
from veditlib.project import EditError  # noqa: E402


def region(png, x=None, y=None, w=None, h=None):
    """Average Y, U, V (and max Y) of a region of an image, as measured by FFmpeg."""
    vf = "signalstats,metadata=print"
    if x is not None:
        vf = f"crop={w}:{h}:{x}:{y}," + vf
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(png), "-vf", vf, "-f", "null", "-"],
                       capture_output=True, text=True)
    v = {k: float(n) for k, n in re.findall(r"lavfi\.signalstats\.(\w+)=(-?[\d.]+)", r.stderr)}
    return {"Y": v["YAVG"], "U": v["UAVG"], "V": v["VAVG"], "YMAX": v["YMAX"], "YMIN": v["YMIN"]}


class FeatBase(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        m = cls.media
        def img(name, graph):
            ff("-f", "lavfi", "-i", graph, "-frames:v", "1", m / name)
        img("box.png", "color=c=black:s=640x360,drawbox=x=420:y=120:w=120:h=120:color=white:t=fill")
        img("gray.png", "color=c=0x808080:s=640x360")
        img("light.png", "color=c=0xc0c0c0:s=640x360")
        img("red.png", "color=c=0xd02020:s=640x360")
        img("blue.png", "color=c=0x2020d0:s=640x360")
        img("rbox.png", "color=c=black:s=640x360,drawbox=x=400:y=160:w=80:h=40:color=white:t=fill")
        img("lbox.png", "color=c=black:s=640x360,drawbox=x=40:y=120:w=120:h=120:color=white:t=fill")
        img("greenbox.png", "color=c=0x00ff00:s=640x360,drawbox=x=240:y=100:w=160:h=160:color=orange:t=fill")
        ff("-f", "lavfi", "-i", "sine=f=440:d=8", m / "tone8.wav")
        (m / "invert.cube").write_text("LUT_3D_SIZE 2\n1 1 1\n0 1 1\n1 0 1\n0 0 1\n1 1 0\n0 1 0\n1 0 0\n0 0 0\n")

    _n = 0

    def new(self, name=None, w=640, h=360, fps=30):
        """A fresh project; every call gets its own folder, even within one test."""
        FeatBase._n += 1
        return super().new(f"{name or self.id().split('.')[-1]}_{FeatBase._n}", w, h, fps)

    def clip_of(self, path, image, dur=4.0, **extra):
        a = self.imp(path, image)
        res, _ = ops.apply(path, [{"op": "add", "asset": a, "dur": dur, **extra}])
        return res[0]["clip"]

    def frame(self, path, t):
        """A frame of the edit as its own file, so two frames at the same time never overwrite each other."""
        FeatBase._n += 1
        return render.snapshot(P.load(path), t, P.load(path).out_dir / f"test_frame_{FeatBase._n}.png")

    def key(self, path, clip, prop, at, value, ease="linear"):
        ops.apply(path, [{"op": "key", "clip": clip, "prop": prop, "at": at, "value": value, "ease": ease}])


# ---------------------------------------------------------------------------------------------
class TestKeyframeMath(FeatBase):
    def test_ffmpeg_expression_matches_python_curve_for_every_ease(self):
        pts = keys.clean("scale", [[0.5, 1.0, "linear"], [1.5, 2.0, "in"], [2.5, 0.5, "out"], [3.0, 1.5, "inout"],
                                   [3.5, 0.25, "hold"], [3.8, 2.0]])
        expr = keys.expr(pts, "t")
        out = self.tmp / "curve.f32"
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=100:d=4.5",
                            "-f", "f32le", str(out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        a = array.array("f")
        a.frombytes(out.read_bytes())
        self.assertGreater(len(a), 400)
        worst = max(abs(a[i] - keys.value_at(pts, i / 100)) for i in range(len(a)))
        self.assertLess(worst, 2e-3, f"FFmpeg and Python disagree by {worst}")

    def test_holds_before_first_and_after_last_key(self):
        pts = keys.clean("opacity", [[1, 0.2], [2, 0.8]])
        self.assertEqual(keys.value_at(pts, 0), 0.2)
        self.assertEqual(keys.value_at(pts, 9), 0.8)

    def test_split_keeps_the_value_at_the_cut_and_shift_keeps_the_picture(self):
        pts = keys.clean("scale", [[0, 1.0], [4, 2.0, "inout"]])
        first, second = keys.split_points(pts, 1.0)
        v = keys.value_at(pts, 1.0)
        self.assertAlmostEqual(keys.value_at(first, 1.0), v, places=4)
        self.assertAlmostEqual(keys.value_at(second, 0.0), v, places=4)
        for t in (0.5, 2.0, 3.0):  # the second half draws the same curve, shifted
            self.assertAlmostEqual(keys.value_at(second, t), keys.value_at(pts, t + 1.0), places=3)
        shifted = keys.shift_points(pts, -1.0)  # head trimmed by one second
        for t in (0.0, 1.0, 2.5):
            self.assertAlmostEqual(keys.value_at(shifted, t), keys.value_at(pts, t + 1.0), places=3)

    def test_validation(self):
        for prop, pts in (("scale", [[0, 99]]), ("scale", [[-1, 1]]), ("scale", [[0, 1, "wobble"]]), ("nonsense", [[0, 1]]),
                          ("scale", []), ("scale", [[0]]), ("opacity", [[0, "x"]])):
            with self.assertRaises(EditError, msg=f"{prop} {pts}"):
                keys.clean(prop, pts)
        keys.clean("scale", [[i / 100, 1] for i in range(keys.MAX_POINTS)])   # the limit itself is fine
        with self.assertRaises(EditError):
            keys.clean("scale", [[i / 100, 1] for i in range(keys.MAX_POINTS + 1)])


class TestKeyOps(FeatBase):
    def test_key_uses_timeline_time_and_stays_with_the_clip(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 4)
        ops.apply(path, [{"op": "move", "clip": c, "at": 10}])
        self.key(path, c, "opacity", 11.0, 0.5)
        self.assertEqual(P.load(path).clip(c)["keys"]["opacity"], [[1.0, 0.5, "linear"]])  # stored from the clip's start
        ops.apply(path, [{"op": "move", "clip": c, "at": 2}])
        self.assertEqual(P.load(path).clip(c)["keys"]["opacity"][0][0], 1.0)  # moving the clip moved the key with it

    def test_key_without_value_freezes_the_current_value(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 4)
        self.key(path, c, "scale", 0, 1.0)
        self.key(path, c, "scale", 4, 2.0)
        ops.apply(path, [{"op": "key", "clip": c, "prop": "scale", "at": 2}])
        pts = P.load(path).clip(c)["keys"]["scale"]
        self.assertEqual([p[0] for p in pts], [0, 2, 4])
        self.assertAlmostEqual(pts[1][1], 1.5, places=3)

    def test_key_errors(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 4)
        t = ops.apply(path, [{"op": "add_text", "text": "Hi", "at": 0, "dur": 2}])[0][0]["clip"]
        for step in ({"clip": c, "prop": "scale", "at": 9}, {"clip": c, "prop": "volume_db", "at": 1, "value": 0},
                     {"clip": c, "prop": "wobble", "at": 1, "value": 1}, {"clip": t, "prop": "scale", "at": 1, "value": 1},
                     {"clip": c, "prop": "opacity", "at": 1, "value": 7}):
            with self.assertRaises(EditError, msg=str(step)):
                ops.apply(path, [{"op": "key", **step}])
        ops.apply(path, [{"op": "key", "clip": t, "prop": "opacity", "at": 0.5, "value": 0.3}])  # text may key opacity

    def test_unkey_and_set_keys(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 4)
        ops.apply(path, [{"op": "set_keys", "clip": c, "prop": "x", "points": [[0, 0], [2, 0.4, "out"], [4, 0]]}])
        ops.apply(path, [{"op": "unkey", "clip": c, "prop": "x", "at": 2}])
        self.assertEqual(len(P.load(path).clip(c)["keys"]["x"]), 2)
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "unkey", "clip": c, "prop": "x", "at": 3}])
        ops.apply(path, [{"op": "unkey", "clip": c, "prop": "x"}])
        self.assertNotIn("keys", P.load(path).clip(c))
        ops.apply(path, [{"op": "set_keys", "clip": c, "prop": "x", "points": []}])  # clearing nothing is fine

    def test_split_carries_keys_and_halves_do_not_share_state(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 4)
        self.key(path, c, "scale", 0, 1.0)
        self.key(path, c, "scale", 4, 2.0)
        ops.apply(path, [{"op": "grade", "clip": c, "saturation": 0.5}])
        res, proj = ops.apply(path, [{"op": "split", "clip": c, "at": 1.0}])
        a, b = proj.clip(c), proj.clip(res[0]["second"])
        self.assertAlmostEqual(keys.value_at(a["keys"]["scale"], 1.0), keys.value_at(b["keys"]["scale"], 0.0), places=4)
        ops.apply(path, [{"op": "grade", "clip": b["id"], "saturation": 1.5}])
        self.assertEqual(P.load(path).clip(c)["color"]["saturation"], 0.5)  # the first half is untouched

    def test_trim_head_keeps_keys_on_the_same_picture(self):
        path = self.new()
        a = self.imp(path, "tone8.wav")
        res, _ = ops.apply(path, [{"op": "add", "asset": a, "track": "A1", "dur": 6}])
        c = res[0]["clip"]
        ops.apply(path, [{"op": "set_keys", "clip": c, "prop": "volume_db", "points": [[0, 0], [6, -30]]}])
        before = keys.value_at(P.load(path).clip(c)["keys"]["volume_db"], 3.0)  # value at timeline second 3
        ops.apply(path, [{"op": "trim", "clip": c, "start": 2.0}])
        proj = P.load(path)
        self.assertAlmostEqual(keys.value_at(proj.clip(c)["keys"]["volume_db"], 3.0 - 2.0), before, places=2)


# ---------------------------------------------------------------------------------------------
class TestKeyframedPicture(FeatBase):
    def test_zoom_in_over_time(self):
        path = self.new()
        c = self.clip_of(path, "box.png", 4)
        self.key(path, c, "scale", 0, 1.0)
        self.key(path, c, "scale", 4, 2.0)
        spot = (430, 150, 60, 50)  # inside the white box at normal size; black once zoomed to 2x
        start, mid, end = (region(self.frame(path, t), *spot)["Y"] for t in (0.05, 2.0, 3.95))
        self.assertGreater(start, 200)
        self.assertLess(end, 40)
        self.assertTrue(end + 15 < mid < start - 15, f"zoom should progress smoothly: {start} {mid} {end}")

    def test_move_right_and_down(self):
        path = self.new()
        c = self.clip_of(path, "box.png", 4)
        self.key(path, c, "x", 0, 0.0)
        self.key(path, c, "x", 4, 0.5)   # 0.5 of the frame width to the right: the box leaves the picture
        self.assertGreater(region(self.frame(path, 0.05), 430, 150, 60, 50)["Y"], 200)
        self.assertLess(region(self.frame(path, 3.95), 430, 150, 60, 50)["Y"], 40)
        path = self.new("down")
        c = self.clip_of(path, "box.png", 4)
        self.key(path, c, "y", 0, 0.0)
        self.key(path, c, "y", 4, 0.5)   # half the frame height down: the box lands at y 300..420
        self.assertLess(region(self.frame(path, 0.05), 430, 310, 60, 40)["Y"], 40)
        self.assertGreater(region(self.frame(path, 3.95), 430, 310, 60, 40)["Y"], 200)

    def test_opacity_fades_the_clip_in_over_black(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 4)
        self.key(path, c, "opacity", 0, 0.0)
        self.key(path, c, "opacity", 2, 1.0)
        y0, y1, y3 = (region(self.frame(path, t))["Y"] for t in (0.05, 1.0, 3.5))
        self.assertLess(y0, 30)
        self.assertTrue(y0 + 20 < y1 < y3 - 20, f"{y0} {y1} {y3}")

    def test_rotation_turns_the_picture_clockwise(self):
        path = self.new()
        c = self.clip_of(path, "rbox.png", 4)   # a small white bar to the right of the frame centre
        self.key(path, c, "rotation", 0, 0)
        self.key(path, c, "rotation", 4, 90)
        self.assertGreater(region(self.frame(path, 0.05), 410, 165, 60, 30)["Y"], 200)
        end = self.frame(path, 3.98)
        self.assertLess(region(end, 410, 165, 60, 30)["Y"], 40)      # gone from the right
        self.assertGreater(region(end, 305, 275, 30, 50)["Y"], 150)  # now below the centre (clockwise)

    def test_text_can_fade_in_with_an_opacity_key(self):
        path = self.new()
        self.clip_of(path, "gray.png", 4)
        t = ops.apply(path, [{"op": "add_text", "text": "HELLO", "at": 0, "dur": 4, "fade_in": 0, "fade_out": 0}])[0][0]["clip"]
        self.key(path, t, "opacity", 0, 0.0)
        self.key(path, t, "opacity", 1, 1.0)
        self.assertLess(region(self.frame(path, 0.05), 160, 280, 320, 70)["YMAX"], 160)   # nothing but gray yet
        self.assertGreater(region(self.frame(path, 3.0), 160, 280, 320, 70)["YMAX"], 220)  # white text

    def test_keys_work_with_speed_changes_and_picture_in_picture(self):
        path = self.new()
        c = self.clip_of(path, "box.png", 4, box=[0.5, 0.0, 0.5, 0.5])
        self.key(path, c, "opacity", 0, 1.0)
        self.key(path, c, "scale", 0, 1.0)
        self.key(path, c, "scale", 4, 1.5)
        render.snapshot(P.load(path), 2.0)   # builds and runs without error


class TestGrading(FeatBase):
    def graded(self, image, t=0.5, **grade_args):
        path = self.new()
        c = self.clip_of(path, image, 2)
        if grade_args:
            ops.apply(path, [{"op": "grade", "clip": c, **grade_args}])
        return region(self.frame(path, t)), path, c

    def test_brightness_and_contrast(self):
        base, *_ = self.graded("gray.png")
        bright, *_ = self.graded("gray.png", brightness=0.25)
        self.assertGreater(bright["Y"], base["Y"] + 35)
        light, *_ = self.graded("light.png")
        punchy, *_ = self.graded("light.png", contrast=1.6)
        self.assertGreater(punchy["Y"], light["Y"] + 15)

    def test_saturation_zero_removes_color(self):
        base, *_ = self.graded("red.png")
        self.assertGreater(abs(base["V"] - 128), 40)
        grey, *_ = self.graded("red.png", saturation=0)
        self.assertLess(abs(grey["U"] - 128), 3)
        self.assertLess(abs(grey["V"] - 128), 3)

    def test_warmth_and_tint_move_color_the_right_way(self):
        base, *_ = self.graded("gray.png")
        warm, *_ = self.graded("gray.png", temperature=0.8)
        cool, *_ = self.graded("gray.png", temperature=-0.8)
        self.assertGreater(warm["V"], base["V"] + 6)   # more red
        self.assertLess(warm["U"], base["U"] - 6)      # less blue
        self.assertGreater(cool["U"], base["U"] + 6)
        self.assertLess(cool["V"], base["V"] - 6)
        magenta, *_ = self.graded("gray.png", tint=0.9)
        self.assertGreater(abs(magenta["U"] - 128) + abs(magenta["V"] - 128), 6)

    def test_looks(self):
        bw, *_ = self.graded("red.png", look="black-white")
        self.assertLess(abs(bw["U"] - 128) + abs(bw["V"] - 128), 6)
        warm, *_ = self.graded("gray.png", look="warm")
        self.assertGreater(warm["V"], 130)
        for look in grade.LOOK_NAMES:  # every look renders
            self.graded("gray.png", look=look)

    def test_explicit_fields_override_the_look(self):
        a, *_ = self.graded("red.png", look="black-white", saturation=1.0)
        self.assertGreater(abs(a["V"] - 128), 30)

    def test_vignette_darkens_the_corners_only(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 2)
        ops.apply(path, [{"op": "grade", "clip": c, "vignette": 1.0}])
        f = self.frame(path, 0.5)
        corner, centre = region(f, 0, 0, 40, 30)["Y"], region(f, 300, 165, 40, 30)["Y"]
        self.assertLess(corner + 40, centre, f"corner {corner}, centre {centre}")
        ops.apply(path, [{"op": "grade", "clip": c, "vignette": 0.3}])   # a light vignette is lighter than a heavy one
        light = region(self.frame(path, 0.5), 0, 0, 40, 30)["Y"]
        self.assertTrue(corner < light < centre, f"{corner} {light} {centre}")

    def test_lut_is_applied(self):
        path = self.new()
        c = self.clip_of(path, "box.png", 2)
        ops.apply(path, [{"op": "grade", "clip": c, "lut": str(self.media / "invert.cube")}])
        f = self.frame(path, 0.5)
        self.assertLess(region(f, 440, 150, 60, 50)["Y"], 60)    # the white box turned dark
        self.assertGreater(region(f, 20, 20, 60, 50)["Y"], 180)  # black turned white

    def test_keyframed_brightness_ramps(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 4)
        self.key(path, c, "brightness", 0, 0.0)
        self.key(path, c, "brightness", 4, 0.4)
        early, late = (region(self.frame(path, t))["Y"] for t in (0.05, 3.9))
        self.assertGreater(late, early + 35)

    def test_grade_errors_and_clearing(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 2)
        for bad in ({"look": "sepia-dream"}, {"brightness": 5}, {"saturation": "x"}, {"lut": str(self.tmp / "none.cube")}, {"wobble": 1}):
            with self.assertRaises(EditError, msg=str(bad)):
                ops.apply(path, [{"op": "grade", "clip": c, **bad}])
        ops.apply(path, [{"op": "grade", "clip": c, "look": "warm", "blur": 3}])
        ops.apply(path, [{"op": "grade", "clip": c, "look": "none"}])
        self.assertEqual(P.load(path).clip(c)["color"], {"blur": 3.0})
        ops.apply(path, [{"op": "grade", "clip": c, "reset": True}])
        self.assertNotIn("color", P.load(path).clip(c))
        t = ops.apply(path, [{"op": "add_text", "text": "x", "at": 0, "dur": 1}])[0][0]["clip"]
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "grade", "clip": t, "saturation": 0}])

    def test_text_clips_keep_their_font_colour_field(self):
        path = self.new()
        t = ops.apply(path, [{"op": "add_text", "text": "x", "at": 0, "dur": 1, "color": "yellow"}])[0][0]["clip"]
        self.assertEqual(P.load(path).clip(t)["color"], "yellow")
        self.assertEqual(ops.check(P.load(path))["errors"], [])


class TestPictureTools(FeatBase):
    def test_chroma_key_removes_the_background(self):
        path = self.new()
        c = self.clip_of(path, "greenbox.png", 2)
        before = self.frame(path, 0.5)
        ops.apply(path, [{"op": "set", "clip": c, "chromakey": "green"}])
        after = self.frame(path, 0.5)
        self.assertGreater(region(before, 20, 20, 60, 40)["Y"], 120)   # green
        self.assertLess(region(after, 20, 20, 60, 40)["Y"], 40)        # keyed out: the black background shows
        self.assertGreater(region(after, 300, 150, 40, 40)["Y"], 90)   # the orange subject stays

    def test_chroma_key_validation(self):
        path = self.new()
        c = self.clip_of(path, "greenbox.png", 2)
        for bad in ("not a colour!", "green,5", "green,0.2,9"):
            with self.assertRaises(EditError, msg=bad):
                ops.apply(path, [{"op": "set", "clip": c, "chromakey": bad}])

    def test_flip_and_crop(self):
        path = self.new()
        c = self.clip_of(path, "lbox.png", 2)
        self.assertLess(region(self.frame(path, 0.5), 500, 150, 60, 60)["Y"], 40)
        ops.apply(path, [{"op": "set", "clip": c, "flip": "h"}])
        flipped = self.frame(path, 0.5)
        self.assertGreater(region(flipped, 500, 150, 60, 60)["Y"], 200)   # the box moved to the right
        self.assertLess(region(flipped, 60, 150, 60, 60)["Y"], 40)
        path = self.new("crop")
        c = self.clip_of(path, "box.png", 2)
        self.assertLess(region(self.frame(path, 0.5), 280, 150, 60, 60)["Y"], 40)
        ops.apply(path, [{"op": "set", "clip": c, "crop": "0.5,0,0,0"}])  # keep the right half, which holds the box
        self.assertGreater(region(self.frame(path, 0.5), 280, 150, 60, 60)["Y"], 200)
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "set", "clip": c, "crop": "0.6,0,0.6,0"}])

    def test_picture_fields_are_refused_on_wrong_clips(self):
        path = self.new()
        t = ops.apply(path, [{"op": "add_text", "text": "x", "at": 0, "dur": 1}])[0][0]["clip"]
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "set", "clip": t, "scale": 2}])
        a = self.imp(path, "tone8.wav")
        s = ops.apply(path, [{"op": "add", "asset": a, "track": "A1", "dur": 1}])[0][0]["clip"]
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "set", "clip": s, "flip": "h"}])


class TestKeyframedSound(FeatBase):
    def test_volume_ramps_down(self):
        path = self.new()
        a = self.imp(path, "tone8.wav")
        res, _ = ops.apply(path, [{"op": "add", "asset": a, "track": "A1", "dur": 4}])
        c = res[0]["clip"]
        self.key(path, c, "volume_db", 0, 0)
        self.key(path, c, "volume_db", 4, -40)
        s = decode_mono(render.render(P.load(path), preview=True))
        sr = 48000
        early, late = rms(s[int(0.1 * sr):int(0.6 * sr)]), rms(s[int(3.4 * sr):int(3.9 * sr)])
        self.assertGreater(20 * math.log10(early / max(late, 1e-9)), 25)

    def test_sound_only_fade_leaves_the_picture_alone(self):
        path = self.new()
        a = self.imp(path, "a.mp4")
        res, _ = ops.apply(path, [{"op": "add", "asset": a, "dur": 3}])
        ops.apply(path, [{"op": "set", "clip": res[0]["clip"], "afade_out": 1.5}])
        s = decode_mono(render.render(P.load(path), preview=True))
        sr = 48000
        self.assertGreater(rms(s[int(0.5 * sr):int(1.0 * sr)]), 0.05)
        self.assertLess(rms(s[int(2.8 * sr):int(2.95 * sr)]), 0.01)
        self.assertGreater(region(self.frame(path, 2.9))["Y"], 20)  # the picture is still there at the end


class TestTransitions(FeatBase):
    def two(self, name, kind, dur=1.0, **kw):
        path = self.new(name)
        a, b = self.clip_of(path, "red.png", 3), self.clip_of(path, "blue.png", 3)
        res, proj = ops.apply(path, [{"op": "transition", "clip_a": a, "clip_b": b, "type": kind, "dur": dur, **kw}])
        return path, a, b, res[0]

    def test_slide_overlaps_ripples_and_shows_both_pictures_midway(self):
        path, a, b, r = self.two("slide", "slide-left")
        proj = P.load(path)
        self.assertEqual(proj.clip(b)["start"], 2.0)       # pulled back by the length of the transition
        self.assertEqual(proj.duration(), 5.0)
        mid = self.frame(path, 2.5)
        self.assertGreater(region(mid, 20, 150, 60, 60)["V"], 200)   # red (A) still on the left
        self.assertGreater(region(mid, 400, 150, 60, 60)["U"], 200)  # blue (B) sliding in from the right
        self.assertGreater(region(self.frame(path, 4.5), 20, 150, 60, 60)["U"], 200)  # afterwards only B

    def test_push_moves_both_clips_with_a_clean_edge(self):
        path, a, b, r = self.two("push", "push-left")
        mid = self.frame(path, 2.5)   # halfway: A has left by half a frame, B has arrived by half a frame
        self.assertGreater(region(mid, 280, 150, 30, 60)["V"], 200)   # red just left of the middle
        self.assertGreater(region(mid, 335, 150, 30, 60)["U"], 200)   # blue just right of it

    def test_dissolve_blends(self):
        path, a, b, r = self.two("dissolve", "dissolve")
        mid = region(self.frame(path, 2.5), 300, 150, 40, 40)
        red, blue = region(self.frame(path, 1.0), 300, 150, 40, 40), region(self.frame(path, 4.0), 300, 150, 40, 40)
        self.assertTrue(min(red["U"], blue["U"]) + 15 < mid["U"] < max(red["U"], blue["U"]) - 15, f"{red} {mid} {blue}")

    def test_dip_goes_through_black(self):
        path, a, b, r = self.two("dip", "dip")
        proj = P.load(path)
        self.assertEqual(proj.clip(b)["start"], 3.0)     # a dip does not overlap
        self.assertEqual(proj.duration(), 6.0)
        solid = region(self.frame(path, 1.0))["Y"]
        self.assertLess(region(self.frame(path, 2.98))["Y"], solid - 25)   # nearly black at the cut

    def test_zoom_transition_renders(self):
        path, a, b, r = self.two("zoomt", "zoom")
        self.assertIn("scale", P.load(path).clip(b)["keys"])
        render.snapshot(P.load(path), 2.4)

    def test_crossfade_sound_keeps_the_level_up_through_the_overlap(self):
        path = self.new("xfade")
        t = self.imp(path, "a.mp4")  # 330 Hz tone with picture
        a = ops.apply(path, [{"op": "add", "asset": t, "dur": 3}])[0][0]["clip"]
        b = ops.apply(path, [{"op": "add", "asset": t, "dur": 3}])[0][0]["clip"]
        ops.apply(path, [{"op": "transition", "clip_a": a, "clip_b": b, "type": "dissolve", "dur": 1.0}])
        s = decode_mono(render.render(P.load(path), preview=True))
        sr = 48000
        steady, middle = rms(s[int(0.5 * sr):int(1.5 * sr)]), rms(s[int(2.4 * sr):int(2.6 * sr)])
        self.assertGreater(middle, steady * 0.5)   # no hole in the middle of the overlap

    def test_without_ripple_other_clips_stay_put(self):
        path = self.new("noripple")
        a, b, c = (self.clip_of(path, f, 3) for f in ("red.png", "blue.png", "gray.png"))
        ops.apply(path, [{"op": "transition", "clip_a": a, "clip_b": b, "type": "dissolve", "dur": 1, "ripple": False}])
        proj = P.load(path)
        self.assertEqual((proj.clip(b)["start"], proj.clip(c)["start"]), (2.0, 6.0))
        path = self.new("ripple")
        a, b, c = (self.clip_of(path, f, 3) for f in ("red.png", "blue.png", "gray.png"))
        ops.apply(path, [{"op": "transition", "clip_a": a, "clip_b": b, "type": "dissolve", "dur": 1}])
        self.assertEqual(P.load(path).clip(c)["start"], 5.0)

    def test_errors(self):
        path = self.new()
        a, b = self.clip_of(path, "red.png", 3), self.clip_of(path, "blue.png", 3)
        far = self.clip_of(path, "gray.png", 3, at=20)
        other = ops.apply(path, [{"op": "add", "asset": self.imp(path, "red.png"), "track": "V2", "at": 0, "dur": 1}])[0][0]["clip"]
        for step in ({"clip_a": a, "clip_b": far}, {"clip_a": a, "clip_b": other}, {"clip_a": b, "clip_b": a},
                     {"clip_a": a, "clip_b": b, "type": "spin"}, {"clip_a": a, "clip_b": b, "dur": 9}, {"clip_a": a, "clip_b": b, "dur": 0.01}):
            with self.assertRaises(EditError, msg=str(step)):
                ops.apply(path, [{"op": "transition", **step}])

    def test_reapplying_replaces_the_previous_transition(self):
        path, a, b, r = self.two("again", "slide-left")
        ops.apply(path, [{"op": "transition", "clip_a": a, "clip_b": b, "type": "dissolve", "dur": 1.0}])
        c = P.load(path).clip(b)
        self.assertNotIn("x", c.get("keys", {}))
        self.assertEqual(c["transition"]["type"], "dissolve")
        self.assertEqual(P.load(path).duration(), 5.0)   # the overlap that already existed was reused, not doubled


# ---------------------------------------------------------------------------------------------
class TestCaptions(FeatBase):
    SRT = ("1\n00:00:01,000 --> 00:00:02,500\nHello <i>there</i>\n\n"
           "2\n00:00:03,000 --> 00:00:04,000\nTwo lines\nof text\n\n"
           "3\n00:01:00,250 --> 00:01:01,000\nLate one\n")
    VTT = "WEBVTT\n\n00:01.500 --> 00:03.000\nShort form\n\n1:02:03.004 --> 1:02:04.000\nHours\n"

    def test_parse_srt_and_vtt(self):
        cues = ops.parse_captions(self.SRT)
        self.assertEqual(cues[0], (1.0, 2.5, "Hello there"))   # tags removed
        self.assertEqual(cues[1][2], "Two lines\nof text")
        self.assertAlmostEqual(cues[2][0], 60.25)
        v = ops.parse_captions(self.VTT)
        self.assertEqual((v[0][0], v[0][1]), (1.5, 3.0))
        self.assertAlmostEqual(v[1][0], 3723.004)

    def test_import_creates_one_text_clip_per_cue_and_honours_offset(self):
        path = self.new()
        res, proj = ops.apply(path, [{"op": "import_captions", "srt": self.SRT, "offset": 0.5}])
        self.assertEqual(res[0]["captions"], 3)
        starts = sorted(c["start"] for c in proj.clips)
        self.assertEqual(starts[:2], [1.5, 3.5])
        self.assertEqual({c["track"] for c in proj.clips}, {"V2"})
        self.assertEqual(proj.clips[1]["text"], "Two lines\nof text")

    def test_import_from_file_and_errors(self):
        path = self.new()
        f = self.tmp / "subs.srt"
        f.write_text("\ufeff" + self.SRT, encoding="utf-8")
        self.assertEqual(ops.apply(path, [{"op": "import_captions", "path": str(f)}])[0][0]["captions"], 3)
        for bad in ({}, {"srt": "nothing here"}, {"path": str(self.tmp / "missing.srt")}, {"srt": self.SRT, "path": str(f)}):
            with self.assertRaises(EditError, msg=str(bad)):
                ops.apply(path, [{"op": "import_captions", **bad}])

    def test_caption_shows_only_while_it_is_on(self):
        path = self.new()
        self.clip_of(path, "gray.png", 5)
        ops.apply(path, [{"op": "import_captions", "srt": "1\n00:00:01,000 --> 00:00:02,000\nHELLO WORLD\n"}])
        inside = region(self.frame(path, 1.5), 100, 270, 440, 80)
        outside = region(self.frame(path, 3.5), 100, 270, 440, 80)
        self.assertGreater(inside["YMAX"], 220)
        self.assertLess(outside["YMAX"], 150)


# ---------------------------------------------------------------------------------------------
def integrated_lufs(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)[-1])


class TestProjectSettings(FeatBase):
    def test_master_loudness_reaches_the_target(self):
        path = self.new()
        a = self.imp(path, "tone8.wav")
        ops.apply(path, [{"op": "add", "asset": a, "track": "A1", "dur": 8}, {"op": "add", "asset": self.imp(path, "gray.png"), "dur": 8}])
        plain = integrated_lufs(render.render(P.load(path), preview=True))
        for target in (-14, -23):
            ops.apply(path, [{"op": "set_settings", "master_lufs": target}])
            got = integrated_lufs(render.render(P.load(path), preview=True))
            self.assertAlmostEqual(got, target, delta=2.0, msg=f"asked for {target}, got {got} (untouched was {plain})")
        ops.apply(path, [{"op": "set_settings", "master_lufs": "off"}])
        self.assertNotIn("master_lufs", P.load(path).settings)
        for bad in (5, -80, "loud"):
            with self.assertRaises((EditError, ValueError)):
                ops.apply(path, [{"op": "set_settings", "master_lufs": bad}])

    def test_canvas_presets(self):
        path = self.new()
        self.clip_of(path, "gray.png", 1)
        ops.apply(path, [{"op": "set_settings", "preset": "vertical"}])
        s = P.load(path).settings
        self.assertEqual((s["width"], s["height"]), (1080, 1920))
        size = lambda f: tuple(int(x) for x in subprocess.run(  # noqa: E731
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(f)],
            capture_output=True, text=True).stdout.strip().split("x"))
        self.assertEqual(size(render.render(P.load(path), preview=True)), (360, 640))
        self.assertEqual(size(render.render(P.load(path))), (1080, 1920))
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "set_settings", "preset": "cinemascope"}])

    def test_track_mute_and_hide(self):
        path = self.new()
        self.clip_of(path, "gray.png", 3)
        a = self.imp(path, "tone8.wav")
        ops.apply(path, [{"op": "add", "asset": a, "track": "A1", "dur": 3}])
        loud = rms(decode_mono(render.render(P.load(path), preview=True)))
        ops.apply(path, [{"op": "set_track", "id": "A1", "mute": True}])
        self.assertLess(rms(decode_mono(render.render(P.load(path), preview=True))), loud * 0.01)
        ops.apply(path, [{"op": "set_track", "id": "A1", "mute": False}])
        self.assertNotIn("mute", P.load(path).track("A1"))
        shown = region(self.frame(path, 1.0))["Y"]
        ops.apply(path, [{"op": "set_track", "id": "V1", "hide": True}])
        self.assertLess(region(self.frame(path, 1.0))["Y"], shown - 40)
        for bad in ({"id": "A1", "hide": True}, {"id": "V1", "duck": True}):
            with self.assertRaises(EditError):
                ops.apply(path, [{"op": "set_track", **bad}])


class TestMeasure(FeatBase):
    def test_measure_reports_numbers_and_hints(self):
        def png(name, color):
            ff("-f", "lavfi", "-i", f"color=c={color}:s=320x180", "-frames:v", "1", self.tmp / name)
            return analyze.measure_frame(self.tmp / name)
        dark, white, red, gray = png("k.png", "black"), png("w.png", "white"), png("r.png", "0xd02020"), png("g.png", "0x808080")
        self.assertLess(dark["luma_avg"], 25)
        self.assertTrue(any("dark" in h for h in dark["hints"]))
        self.assertTrue(any("bright" in h for h in white["hints"]) and any("clipped" in h for h in white["hints"]))
        self.assertTrue(any("warm" in h for h in red["hints"]))
        self.assertTrue(any("colorless" in h for h in gray["hints"]))
        self.assertGreater(white["luma_avg"], dark["luma_avg"] + 150)


class TestCliAndServer(FeatBase):
    def vedit(self, *args, ok=True):
        r = subprocess.run([sys.executable, "-I", str(ROOT / "editor" / "vedit.py"), *map(str, args)], capture_output=True, text=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_cli_flow_for_the_new_commands(self):
        folder = self.tmp / "cli_feat"
        self.vedit("new", folder, "--size", "640x360")
        p = ["-p", folder]
        a = json.loads(self.vedit("import", self.media / "red.png", *p).stdout)["asset"]
        b = json.loads(self.vedit("import", self.media / "blue.png", *p).stdout)["asset"]
        self.vedit("add", a, "--dur", "3", *p)
        self.vedit("add", b, "--dur", "3", *p)
        self.vedit("key", "c1", "scale", "--at", "0", "--value", "1", *p)
        self.vedit("key", "c1", "scale", "--at", "2", "--value", "1.4", "--ease", "inout", *p)
        self.vedit("keys", "c1", "opacity", '[[0,1],[2,0.6]]', *p)
        self.vedit("unkey", "c1", "opacity", *p)
        self.vedit("grade", "c1", "--look", "warm", "--vignette", "0.4", *p)
        self.vedit("transition", "c1", "c2", "--type", "push-left", "--dur", "0.8", *p)
        srt = self.tmp / "c.srt"
        srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHi\n")
        self.assertEqual(json.loads(self.vedit("captions", srt, *p).stdout)["captions"], 1)
        self.vedit("settings", "--preset", "square", "--lufs", "-16", *p)
        self.vedit("track", "set", "A2", "--mute", "on", *p)
        self.assertEqual(json.loads(self.vedit("check", *p).stdout)["errors"], [])
        shown = json.loads(self.vedit("show", "--json", *p).stdout)
        self.assertEqual(shown["settings"]["master_lufs"], -16)
        self.assertEqual(shown["settings"]["width"], 1080)
        m = json.loads(self.vedit("measure", "--at", "1", *p).stdout)
        self.assertIn("luma_avg", m)
        info = json.loads(self.vedit("looks").stdout)
        self.assertIn("cinematic", info["looks"])
        self.assertIn("volume_db", info["keyframe_properties"])
        self.assertIn("slide-left", info["transitions"])

    def test_cli_reports_bad_values_plainly(self):
        folder = self.tmp / "cli_bad"
        self.vedit("new", folder)
        self.vedit("import", self.media / "red.png", "-p", folder)
        self.vedit("add", "a1", "--dur", "2", "-p", folder)
        for args in (("key", "c1", "scale", "--at", "99", "--value", "1"), ("grade", "c1", "--look", "nope"),
                     ("keys", "c1", "scale", "not json"), ("transition", "c1", "c9")):
            r = self.vedit(*args, "-p", folder, ok=False)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("error:", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_server_waveform_thumbnail_and_metadata(self):
        import http.client
        path = P.create(self.tmp / "served_feat", 640, 360, 30).path
        ops.apply(path, [{"op": "import_asset", "path": str(self.media / "a.mp4")},
                         {"op": "import_asset", "path": str(self.media / "silent.mp4")},
                         {"op": "import_asset", "path": str(self.media / "tone8.wav")}])
        httpd = server.Server(("127.0.0.1", 0), server.make_handler(server.State(path), 0))
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def get(url):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            c.request("GET", url)
            r = c.getresponse()
            data = r.read()
            c.close()
            return r.status, data
        try:
            s, d = get("/api/wave?asset=a1")
            self.assertEqual((s, d[:4]), (200, b"\x89PNG"))
            s, d = get("/api/thumb?asset=a2")
            self.assertEqual((s, d[:2]), (200, b"\xff\xd8"))
            self.assertEqual(get("/api/wave?asset=a2")[0], 404)         # no sound in that clip
            self.assertEqual(get("/api/wave?asset=a99")[0], 404)
            self.assertEqual(get("/api/wave?asset=../x")[0], 400)
            self.assertEqual(get("/api/thumb?asset=a1&x=1")[0], 200)
            s, d = get("/api/state")
            meta = json.loads(d)["meta"]
            self.assertIn("scale", meta["keyprops"])
            self.assertIn("teal-orange", meta["looks"])
            self.assertIn("vertical", meta["canvas_presets"])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
