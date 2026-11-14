"""Tests for speed ramps, reverse, freeze frames, masks, motion tracking and stabilization.

The footage is made so the result can be measured: one clip's brightness encodes its source time (so a frame
reveals which moment of the source is showing), another has a textured patch moving along a known path, another
shakes. Every check looks at rendered frames or sound.
Run:  python3 -m unittest discover -s tests -v
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "editor"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from test_editor_features import FeatBase, region
    from test_editor import decode_mono, rms
except ImportError:  # run as tests.test_editor_advanced
    from tests.test_editor_features import FeatBase, region
    from tests.test_editor import decode_mono, rms

from veditlib import keys, masks, ops, project as P, render, stabilize, timemap, tracking  # noqa: E402
from veditlib.project import EditError  # noqa: E402

try:
    from test_editor import ff
except ImportError:
    from tests.test_editor import ff

SRC_LEN = 6.0


class AdvBase(FeatBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        m = cls.media
        # Luma climbs steadily with source time: Y = 16 + 219 * t / 6, so a frame tells you its source second.
        ff("-f", "lavfi", "-i", f"color=c=black:s=320x180:r=30:d={SRC_LEN},format=yuv420p,geq=lum='16+219*T/{SRC_LEN}':cb=128:cr=128",
           "-f", "lavfi", "-i", f"sine=f=330:d={SRC_LEN}", "-shortest", "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p",
           "-c:a", "aac", m / "timecode.mp4")
        ff("-f", "lavfi", "-i", "aevalsrc='exp(-3*t)*sin(2*PI*440*t)':d=2:s=48000", m / "decay.wav")
        ff("-f", "lavfi", "-i", "color=c=black:s=640x360,format=yuv420p,geq=lum='255*mod(floor(X/6),2)':cb=128:cr=128",
           "-frames:v", "1", m / "stripes.png")
        ff("-f", "lavfi", "-i", "color=c=0x808080:s=320x240", "-frames:v", "1", m / "gray43.png")
        # A textured patch drifting across a dark noisy background along a known path.
        ff("-f", "lavfi", "-i", "nullsrc=s=640x360,geq=random(1)*70:128:128", "-frames:v", "1", m / "bg.png")
        ff("-f", "lavfi", "-i", "nullsrc=s=96x96,geq=40+random(7)*190:128:128", "-frames:v", "1", m / "patch.png")
        for name, xexpr in (("move", "40+160*t"), ("exit", "40+400*t")):
            ff("-loop", "1", "-framerate", "30", "-t", "3", "-i", m / "bg.png", "-loop", "1", "-framerate", "30", "-i", m / "patch.png",
               "-filter_complex", f"[0][1]overlay=x='{xexpr}':y='60+60*t'", "-t", "3", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "12",
               m / f"{name}.mp4")
        # A static scene filmed by a shaky camera.
        ff("-f", "lavfi", "-i", "nullsrc=s=720x420,geq=random(3)*255:128:128", "-frames:v", "1", m / "scene.png")
        ff("-loop", "1", "-framerate", "30", "-t", "4", "-i", m / "scene.png", "-vf",
           "crop=w=640:h=360:x='40+12*sin(n*0.9)+7*sin(n*2.3)':y='30+10*sin(n*1.3)+6*cos(n*2.9)'", "-pix_fmt", "yuv420p",
           "-c:v", "libx264", "-crf", "12", m / "shaky.mp4")

    def tcproject(self):
        path = self.new(w=320, h=180)
        a = self.imp(path, "timecode.mp4")
        return path, a

    def source_second(self, path, t):
        """Which second of the source the picture at timeline time t is showing (decoded from its brightness)."""
        y = region(self.frame(path, t))["Y"]
        return (y - 16) * SRC_LEN / 219

    def clip(self, path, asset, **kw):
        return ops.apply(path, [{"op": "add", "asset": asset, **kw}])[0][0]["clip"]


# -------------------------------------------------------------------------------------------
class TestTimeModel(AdvBase):
    def test_constant_speed_reads_the_right_part_of_the_source(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=2, speed=2)
        for t, want in ((0.5, 1.0), (1.5, 3.0)):
            self.assertAlmostEqual(self.source_second(path, t), want, delta=0.15)

    def test_speed_ramp_follows_the_integral_of_the_speed_curve(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=2)
        ops.apply(path, [{"op": "set_keys", "clip": c, "prop": "speed", "points": [[0, 1], [2, 3]]}])
        proj = P.load(path)
        self.assertAlmostEqual(timemap.source_span(proj.clip(c)), 4.0, delta=0.02)     # 1 -> 3 over 2 s: t + t^2/2
        for t, want in ((1.0, 1.5), (1.5, 2.625), (1.95, 3.85)):
            self.assertAlmostEqual(self.source_second(path, t), want, delta=0.2, msg=f"at {t}s")

    def test_ramp_that_needs_more_source_than_exists_is_refused(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=4)
        with self.assertRaises(EditError) as cm:
            ops.apply(path, [{"op": "set_keys", "clip": c, "prop": "speed", "points": [[0, 2], [4, 4]]}])  # would use 12 s
        self.assertIn("speed ramp", str(cm.exception))
        self.assertNotIn("keys", P.load(path).clip(c))          # and nothing was saved
        ops.apply(path, [{"op": "key", "clip": c, "prop": "speed", "at": 0, "value": 1.0}])   # a gentle ramp is fine
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "key", "clip": c, "prop": "speed", "at": 4, "value": 6.0}])

    def test_speed_keys_only_on_video_files_and_warn_about_sound(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=3)
        ops.apply(path, [{"op": "key", "clip": c, "prop": "speed", "at": 0, "value": 1.0},
                         {"op": "key", "clip": c, "prop": "speed", "at": 3, "value": 2.0}])
        warns = ops.check(P.load(path))["warnings"]
        self.assertTrue(any("mutes" in w for w in warns), warns)
        sound = decode_mono(render.render(P.load(path), preview=True))
        self.assertLess(rms(sound), 0.005)   # a ramped clip is silent
        img = ops.apply(path, [{"op": "add_text", "text": "x", "at": 0, "dur": 1}])[0][0]["clip"]
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "key", "clip": img, "prop": "speed", "at": 0.5, "value": 1.5}])

    def test_reverse_plays_the_clip_backwards(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=6)
        before = [self.source_second(path, t) for t in (0.1, 5.9)]
        self.assertAlmostEqual(before[0], 0.1, delta=0.15)
        ops.apply(path, [{"op": "set", "clip": c, "reverse": True}])
        after = [self.source_second(path, t) for t in (0.1, 3.0, 5.9)]
        for got, want in zip(after, (5.9, 3.0, 0.1)):
            self.assertAlmostEqual(got, want, delta=0.2)
        self.assertEqual(P.load(path).duration(), 6.0)

    def test_reverse_audio(self):
        path = self.new()
        a = self.imp(path, "decay.wav")
        c = ops.apply(path, [{"op": "add", "asset": a, "track": "A1", "dur": 2}])[0][0]["clip"]
        sr = 48000
        loud_first = lambda s: rms(s[:sr // 5]) > rms(s[-sr // 5:])  # noqa: E731
        self.assertTrue(loud_first(decode_mono(render.render(P.load(path), preview=True))))     # fades out normally
        ops.apply(path, [{"op": "set", "clip": c, "reverse": True}])
        self.assertFalse(loud_first(decode_mono(render.render(P.load(path), preview=True))))    # swells instead

    def test_reverse_is_refused_on_text(self):
        path = self.new()
        t = ops.apply(path, [{"op": "add_text", "text": "x", "at": 0, "dur": 1}])[0][0]["clip"]
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "set", "clip": t, "reverse": True}])

    def test_split_and_trim_keep_a_reversed_clip_continuous(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=6)
        ops.apply(path, [{"op": "set", "clip": c, "reverse": True}])
        res, proj = ops.apply(path, [{"op": "split", "clip": c, "at": 2.0}])
        first, second = proj.clip(c), proj.clip(res[0]["second"])
        self.assertEqual((first["src_in"], second["src_in"]), (4.0, 0.0))   # the first half plays the END of the source
        for t, want in ((1.95, 4.05), (2.05, 3.95)):
            self.assertAlmostEqual(self.source_second(path, t), want, delta=0.2)
        ops.apply(path, [{"op": "trim", "clip": second["id"], "start": 3.0}])       # drop the head: picture must not move
        self.assertAlmostEqual(self.source_second(path, 3.5), 6 - 3.5, delta=0.2)
        ops.apply(path, [{"op": "trim", "clip": second["id"], "end": 5.0}])         # drop the tail
        proj = P.load(path)
        self.assertAlmostEqual(self.source_second(path, 4.5), 6 - 4.5, delta=0.2)
        self.assertEqual(proj.clip(second["id"])["dur"], 2.0)

    def test_split_a_ramped_clip_is_seamless(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=2)
        ops.apply(path, [{"op": "set_keys", "clip": c, "prop": "speed", "points": [[0, 1], [2, 3]]}])
        ops.apply(path, [{"op": "split", "clip": c, "at": 1.0}])
        proj = P.load(path)
        lo, hi = sorted(proj.clips, key=lambda x: x["start"])
        self.assertAlmostEqual(lo["src_in"] + timemap.source_span(lo), hi["src_in"], delta=0.02)   # no gap, no overlap
        for t, want in ((0.95, 0.95 + 0.95 ** 2 / 2), (1.05, 1.05 + 1.05 ** 2 / 2)):
            self.assertAlmostEqual(self.source_second(path, t), want, delta=0.2)

    def test_window_math(self):
        c = {"src_in": 1.0, "dur": 4.0, "speed": 1.0, "reverse": True}
        self.assertEqual(timemap.source_time_at(c, 0.0), 5.0)
        self.assertEqual(timemap.source_time_at(c, 4.0), 1.0)
        self.assertEqual(timemap.split_windows(c, 1.0), (4.0, 1.0))
        self.assertEqual(timemap.trimmed_src_in(c, 1.0, 2.0), 2.0)   # keep output 1..3 = source 4..2


class TestFreezeFrame(AdvBase):
    def test_freeze_holds_one_frame_then_carries_on(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=6)
        res, proj = ops.apply(path, [{"op": "freeze", "clip": c, "at": 2.0, "dur": 1.5}])
        self.assertEqual(proj.duration(), 7.5)
        hold = proj.clip(res[0]["hold"])
        self.assertTrue(hold["hold"] and hold["mute"] and hold["start"] == 2.0 and hold["dur"] == 1.5)
        for t in (2.2, 2.8, 3.4):
            self.assertAlmostEqual(self.source_second(path, t), 2.0, delta=0.12, msg=f"frozen at {t}s")
        self.assertAlmostEqual(self.source_second(path, 3.9), 2.4, delta=0.15)   # playback resumes where it stopped
        self.assertAlmostEqual(self.source_second(path, 1.5), 1.5, delta=0.15)

    def test_freeze_is_silent_and_later_clips_move(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=6)
        later = self.clip(path, a, dur=1)
        ops.apply(path, [{"op": "freeze", "clip": c, "at": 2.0, "dur": 1.5}])
        proj = P.load(path)
        self.assertEqual(proj.clip(later)["start"], 7.5)
        s = decode_mono(render.render(proj, preview=True))
        sr = 48000
        self.assertGreater(rms(s[int(0.5 * sr):int(1.5 * sr)]), 0.03)
        self.assertLess(rms(s[int(2.2 * sr):int(3.3 * sr)]), 0.005)

    def test_freeze_at_the_start_and_the_end(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=3)
        ops.apply(path, [{"op": "freeze", "clip": c, "at": 0.0, "dur": 1.0}])
        self.assertAlmostEqual(self.source_second(path, 0.5), 0.0, delta=0.12)
        self.assertAlmostEqual(self.source_second(path, 1.5), 0.5, delta=0.15)
        path, a = self.tcproject()
        c = self.clip(path, a, dur=3)
        res, proj = ops.apply(path, [{"op": "freeze", "clip": c, "at": 3.0, "dur": 1.0}])
        self.assertEqual(proj.duration(), 4.0)
        self.assertAlmostEqual(self.source_second(path, 3.5), 3.0, delta=0.2)

    def test_freeze_a_reversed_clip_and_errors(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=6)
        ops.apply(path, [{"op": "set", "clip": c, "reverse": True}])
        res, _ = ops.apply(path, [{"op": "freeze", "clip": c, "at": 1.0, "dur": 1.0}])
        self.assertAlmostEqual(self.source_second(path, 1.5), 5.0, delta=0.2)   # reversed: second 1 of output = source 5
        hold = res[0]["hold"]
        for bad in ({"clip": hold, "at": 1.2}, {"clip": c, "at": 99}, {"clip": c, "at": 1, "dur": 0}, {"clip": c, "at": 1, "dur": 500}):
            with self.assertRaises(EditError, msg=str(bad)):
                ops.apply(path, [{"op": "freeze", **bad}])
        t = ops.apply(path, [{"op": "add_text", "text": "x", "at": 0, "dur": 1}])[0][0]["clip"]
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "freeze", "clip": t, "at": 0.5}])

    def test_freeze_keeps_animation_values_steady(self):
        path, a = self.tcproject()
        c = self.clip(path, a, dur=4)
        ops.apply(path, [{"op": "set_keys", "clip": c, "prop": "scale", "points": [[0, 1.0], [4, 2.0]]}])
        res, proj = ops.apply(path, [{"op": "freeze", "clip": c, "at": 2.0, "dur": 1.0}])
        hold = proj.clip(res[0]["hold"])
        self.assertEqual(len(hold["keys"]["scale"]), 1)
        self.assertAlmostEqual(hold["keys"]["scale"][0][1], 1.5, places=2)


# -------------------------------------------------------------------------------------------
class TestMasks(AdvBase):
    def masked(self, image, t=0.5, fit=None, **mask):
        path = self.new()
        c = self.clip_of(path, image, 4)
        if fit:
            ops.apply(path, [{"op": "set", "clip": c, "fit": fit}])
        m = ops.apply(path, [{"op": "mask", "clip": c, **mask}])[0][0]["mask"]
        return path, c, m, self.frame(path, t)

    @staticmethod
    def spread(png, *box):
        r = region(png, *box)
        return r["YMAX"] - r["YMIN"]

    def test_blur_inside_the_ellipse_only(self):
        _, _, _, f = self.masked("stripes.png", shape="ellipse", x=0.5, y=0.5, w=0.4, h=0.5, feather=0.1, effect="blur", amount=0.9)
        self.assertLess(self.spread(f, 300, 160, 40, 40), 90)      # stripes smeared inside
        self.assertGreater(self.spread(f, 0, 0, 60, 40), 200)      # crisp outside

    def test_pixelate(self):
        _, _, _, f = self.masked("stripes.png", shape="rect", x=0.5, y=0.5, w=0.4, h=0.5, feather=0.02, effect="pixelate", amount=0.8)
        self.assertLess(self.spread(f, 290, 150, 60, 60), 60)
        self.assertGreater(self.spread(f, 0, 0, 60, 40), 200)

    def test_darken_and_desaturate(self):
        _, _, _, f = self.masked("gray.png", shape="ellipse", w=0.4, h=0.5, effect="darken", amount=1.0)
        self.assertLess(region(f, 300, 160, 40, 40)["Y"], 50)
        self.assertGreater(region(f, 0, 0, 60, 40)["Y"], 110)
        _, _, _, f = self.masked("red.png", shape="ellipse", w=0.4, h=0.5, effect="desaturate", amount=1.0)
        self.assertLess(abs(region(f, 300, 160, 40, 40)["V"] - 128), 10)
        self.assertGreater(abs(region(f, 0, 0, 60, 40)["V"] - 128), 40)

    def test_alpha_mask_shows_the_clip_only_inside_and_invert_flips_it(self):
        _, _, m, f = self.masked("gray.png", shape="ellipse", w=0.4, h=0.5, effect="none", feather=0.05)
        self.assertGreater(region(f, 300, 160, 40, 40)["Y"], 100)
        self.assertLess(region(f, 0, 0, 60, 40)["Y"], 30)
        _, _, _, f = self.masked("gray.png", shape="ellipse", w=0.4, h=0.5, effect="none", feather=0.05, invert=True)
        self.assertLess(region(f, 300, 160, 40, 40)["Y"], 30)
        self.assertGreater(region(f, 0, 0, 60, 40)["Y"], 100)

    def test_feather_softens_the_edge(self):
        edge = (452, 170, 14, 20)   # just inside the right edge of a 320 px wide ellipse centred at 320
        _, _, _, crisp = self.masked("gray.png", shape="ellipse", w=0.5, h=0.5, effect="none", feather=0.0)
        _, _, _, soft = self.masked("gray.png", shape="ellipse", w=0.5, h=0.5, effect="none", feather=0.9)
        self.assertGreater(region(crisp, *edge)["Y"], region(soft, *edge)["Y"] + 20)

    def test_mask_position_is_relative_to_the_picture_not_the_canvas(self):
        path = self.new()
        c = self.clip_of(path, "gray43.png", 4)    # 4:3 picture on a 16:9 canvas: bars, picture spans x 80..560
        ops.apply(path, [{"op": "mask", "clip": c, "shape": "rect", "x": 0.25, "y": 0.5, "w": 0.5, "h": 1.0, "feather": 0.0,
                          "effect": "darken", "amount": 1.0}])
        f = self.frame(path, 0.5)
        self.assertLess(region(f, 200, 100, 100, 100)["Y"], 50)       # left half of the picture
        self.assertGreater(region(f, 340, 100, 100, 100)["Y"], 100)   # right half
        self.assertLess(region(f, 296, 150, 16, 60)["Y"], 60)         # the split sits at the picture's middle (x 320)
        self.assertGreater(region(f, 330, 150, 16, 60)["Y"], 100)

    def test_mask_stays_on_its_part_of_the_picture_when_zoomed(self):
        path = self.new()
        c = self.clip_of(path, "gray43.png", 4)
        ops.apply(path, [{"op": "mask", "clip": c, "shape": "rect", "x": 0.25, "y": 0.5, "w": 0.5, "h": 1.0, "feather": 0.0,
                          "effect": "darken", "amount": 1.0}, {"op": "set", "clip": c, "scale": 2.0}])
        f = self.frame(path, 0.5)
        self.assertLess(region(f, 200, 100, 80, 100)["Y"], 50)
        self.assertGreater(region(f, 350, 100, 80, 100)["Y"], 100)

    def test_keyframed_mask_moves(self):
        path = self.new()
        c = self.clip_of(path, "stripes.png", 4)
        m = ops.apply(path, [{"op": "mask", "clip": c, "shape": "ellipse", "w": 0.3, "h": 0.6, "effect": "blur", "amount": 1.0,
                              "x": 0.2, "y": 0.5}])[0][0]["mask"]
        ops.apply(path, [{"op": "key", "clip": c, "prop": f"{m}.x", "at": 0, "value": 0.2},
                         {"op": "key", "clip": c, "prop": f"{m}.x", "at": 4, "value": 0.8}])
        early, late = self.frame(path, 0.05), self.frame(path, 3.95)
        left, right = (130, 160, 20, 40), (500, 160, 20, 40)    # around x = 0.2 and 0.8 of 640
        self.assertLess(self.spread(early, *left), 90)
        self.assertGreater(self.spread(early, *right), 200)
        self.assertGreater(self.spread(late, *left), 200)
        self.assertLess(self.spread(late, *right), 90)

    def test_masks_on_a_picture_in_picture_box_of_a_different_shape(self):
        # a 4:3 picture in a 16:9 box leaves the layer smaller than the box; opacity keys and masks must match its real size
        path = self.new()
        c = self.clip_of(path, "gray43.png", 4, box=[0.5, 0.0, 0.5, 0.5])
        ops.apply(path, [{"op": "mask", "clip": c, "shape": "ellipse", "w": 0.6, "h": 0.6, "effect": "none", "feather": 0.1},
                         {"op": "key", "clip": c, "prop": "opacity", "at": 0, "value": 0.0},
                         {"op": "key", "clip": c, "prop": "opacity", "at": 1, "value": 1.0}])
        late, early = self.frame(path, 3.0), self.frame(path, 0.02)
        self.assertGreater(region(late, 440, 60, 40, 40)["Y"], 90)   # picture centre (it sits at x 360..600, y 0..180)
        self.assertLess(region(early, 440, 60, 40, 40)["Y"], 30)

    def test_validation_and_removal(self):
        path = self.new()
        c = self.clip_of(path, "gray.png", 3)
        for bad in ({"shape": "star"}, {"effect": "sparkle"}, {"x": 5}, {"w": 0}, {"feather": 2}, {"amount": -1}):
            with self.assertRaises(EditError, msg=str(bad)):
                ops.apply(path, [{"op": "mask", "clip": c, **bad}])
        for _ in range(masks.MAX_MASKS):
            ops.apply(path, [{"op": "mask", "clip": c}])
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "mask", "clip": c}])
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "mask", "clip": c, "id": "m9", "x": 0.4}])
        ops.apply(path, [{"op": "key", "clip": c, "prop": "m2.x", "at": 0, "value": 0.1}, {"op": "key", "clip": c, "prop": "m2.x", "at": 2, "value": 0.9}])
        ops.apply(path, [{"op": "unmask", "clip": c, "id": "m2"}])
        proj = P.load(path)
        self.assertNotIn("m2.x", proj.clip(c).get("keys", {}))
        self.assertEqual(len(proj.clip(c)["masks"]), masks.MAX_MASKS - 1)
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "key", "clip": c, "prop": "m2.x", "at": 1, "value": 0.5}])   # no such mask any more
        t = ops.apply(path, [{"op": "add_text", "text": "x", "at": 0, "dur": 1}])[0][0]["clip"]
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "mask", "clip": t}])

    def test_split_keeps_masks_with_both_halves(self):
        path = self.new()
        c = self.clip_of(path, "stripes.png", 4)
        ops.apply(path, [{"op": "mask", "clip": c, "effect": "blur"}])
        res, proj = ops.apply(path, [{"op": "split", "clip": c, "at": 2.0}])
        self.assertEqual(len(proj.clip(res[0]["second"])["masks"]), 1)
        render.snapshot(proj, 3.0)


# -------------------------------------------------------------------------------------------
def at_time(points, t):
    """The tracked (x, y) at time t, interpolated between the stored points."""
    pts = sorted(points)
    for (ta, xa, ya), (tb, xb, yb) in zip(pts, pts[1:]):
        if ta <= t <= tb:
            u = (t - ta) / (tb - ta) if tb != ta else 0
            return xa + (xb - xa) * u, ya + (yb - ya) * u
    return (pts[0][1], pts[0][2]) if t < pts[0][0] else (pts[-1][1], pts[-1][2])


def truth(t):
    """Where the patch centre really is at time t in the 'move' clip, as picture fractions."""
    return (40 + 160 * t + 48) / 640, (60 + 60 * t + 48) / 360


class TestTracking(AdvBase):
    def setup_move(self, name="move.mp4"):
        path = self.new()
        a = self.imp(path, name)
        c = self.clip(path, a, dur=3)
        return path, c

    def test_tracker_follows_a_known_path(self):
        path, c = self.setup_move()
        x0, y0 = truth(0.0)
        res, proj = ops.apply(path, [{"op": "track", "clip": c, "at": 0.0, "x": x0, "y": y0, "size": 0.2}])
        self.assertGreater(res[0]["found"], 0.85)
        pts = proj.data["trackers"][res[0]["tracker"]]["points"]
        self.assertLess(len(pts), 160)                                    # simplified, not one point per frame
        for t in (0.4, 1.0, 1.7, 2.5, 2.9):
            tx, ty = truth(t)
            gx, gy = at_time(pts, t)
            self.assertLess(abs(gx - tx), 0.02, f"x at {t}s")              # within 2% of the frame width (13 px)
            self.assertLess(abs(gy - ty), 0.03, f"y at {t}s")

    def test_tracking_from_the_middle_goes_both_ways(self):
        path, c = self.setup_move()
        x, y = truth(1.5)
        res, proj = ops.apply(path, [{"op": "track", "clip": c, "at": 1.5, "x": x, "y": y, "size": 0.2}])
        pts = proj.data["trackers"][res[0]["tracker"]]["points"]
        self.assertLess(pts[0][0], 0.3)            # reached the beginning of the clip
        self.assertGreater(pts[-1][0], 2.7)        # and the end
        for t in (0.3, 2.7):
            tx, ty = truth(t)
            self.assertLess(abs(at_time(pts, t)[0] - tx), 0.025)

    def test_a_blur_mask_follows_the_target(self):
        path, c = self.setup_move()
        x0, y0 = truth(0.0)
        ops.apply(path, [{"op": "track", "clip": c, "at": 0.0, "x": x0, "y": y0, "size": 0.2}])
        ops.apply(path, [{"op": "mask", "clip": c, "shape": "ellipse", "w": 0.22, "h": 0.4, "feather": 0.1, "effect": "blur", "amount": 1.0},
                         {"op": "follow", "clip": c, "mask": "m1", "tracker": "t1"}])
        cx, cy = (int(v) for v in (truth(2.4)[0] * 640, truth(2.4)[1] * 360))
        spot = (cx - 25, cy - 15, 50, 30)
        blurred = self.frame(path, 2.4)
        ops.apply(path, [{"op": "unmask", "clip": c, "id": "m1"}])
        plain = self.frame(path, 2.4)
        s = lambda png: region(png, *spot)["YMAX"] - region(png, *spot)["YMIN"]  # noqa: E731
        self.assertLess(s(blurred), s(plain) * 0.5, f"{s(blurred)} vs {s(plain)}")

    def test_a_title_attaches_to_the_target(self):
        path, c = self.setup_move()
        x0, y0 = truth(0.0)
        ops.apply(path, [{"op": "track", "clip": c, "at": 0.0, "x": x0, "y": y0, "size": 0.2}])
        a = self.imp(path, "red.png")
        tag = ops.apply(path, [{"op": "add", "asset": a, "track": "V2", "at": 0, "dur": 3, "box": [0.45, 0.45, 0.1, 0.1]}])[0][0]["clip"]
        ops.apply(path, [{"op": "attach", "clip": tag, "tracker": "t1"}])
        for t in (0.5, 1.5, 2.5):
            tx, ty = truth(t)
            f = self.frame(path, t)
            self.assertGreater(region(f, int(tx * 640) - 12, int(ty * 360) - 8, 24, 16)["V"], 175, f"red tag on target at {t}s")
        # and it really moved: at t=2.5 the starting spot is plain background again
        self.assertLess(region(self.frame(path, 2.5), int(x0 * 640) - 12, int(y0 * 360) - 8, 24, 16)["V"], 150)
        text = ops.apply(path, [{"op": "add_text", "text": "HERE", "at": 0, "dur": 3, "size": 30}])[0][0]["clip"]
        ops.apply(path, [{"op": "attach", "clip": text, "tracker": "t1", "dy": -0.2}])
        self.assertEqual(P.load(path).clip(text)["pos"], "center")

    def test_attach_survives_a_split_of_the_footage(self):
        path, c = self.setup_move()
        x0, y0 = truth(0.0)
        ops.apply(path, [{"op": "track", "clip": c, "at": 0.0, "x": x0, "y": y0, "size": 0.2}])
        res, _ = ops.apply(path, [{"op": "split", "clip": c, "at": 1.5}])
        second = res[0]["second"]
        ops.apply(path, [{"op": "mask", "clip": second, "effect": "blur", "w": 0.22, "h": 0.4}])
        r, _ = ops.apply(path, [{"op": "follow", "clip": second, "mask": "m1", "tracker": "t1"}])    # source time maps through src_in
        self.assertGreater(r[0]["keyframes"], 2)

    def test_lost_target_is_reported(self):
        path, c = self.setup_move("exit.mp4")     # the patch leaves the picture partway through
        x0 = (40 + 48) / 640
        res, _ = ops.apply(path, [{"op": "track", "clip": c, "at": 0.0, "x": x0, "y": (60 + 48) / 360, "size": 0.2}])
        self.assertLess(res[0]["found"], 0.7)
        self.assertIn("warning", res[0])

    def test_bad_requests_are_explained(self):
        path, c = self.setup_move()
        x0, y0 = truth(0.0)
        for bad, word in (({"x": 0.01, "y": 0.5}, "off the picture"), ({"x": 0.98, "y": 0.98}, "off the picture"),
                          ({"x": x0, "y": y0, "at": 9}, "outside"), ({"x": x0, "y": y0, "size": 0.9}, "size")):
            with self.assertRaises(EditError, msg=str(bad)) as cm:
                ops.apply(path, [{"op": "track", "clip": c, "at": 0.0, "size": 0.2, **bad}])
            self.assertIn(word.lower(), str(cm.exception).lower())
        ops.apply(path, [{"op": "set", "clip": c, "reverse": True}])
        with self.assertRaises(EditError) as cm:
            ops.apply(path, [{"op": "track", "clip": c, "at": 1.0, "x": 0.5, "y": 0.5}])
        self.assertIn("reversing", str(cm.exception))
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "follow", "clip": c, "mask": "m1", "tracker": "t9"}])

    def test_flat_areas_cannot_be_tracked(self):
        path = self.new()
        a = self.imp(path, "timecode.mp4")   # brightness ramps smoothly, no detail anywhere
        c = self.clip(path, a, dur=3)
        with self.assertRaises(EditError) as cm:
            ops.apply(path, [{"op": "track", "clip": c, "at": 1.0, "x": 0.5, "y": 0.5, "size": 0.2}])
        self.assertIn("flat", str(cm.exception))

    def test_a_hand_made_path_can_drive_a_mask(self):
        path, c = self.setup_move()
        ops.apply(path, [{"op": "set_tracker", "id": "mine", "clip": c, "points": [[0, 0.2, 0.3], [1, 0.5, 0.4], [3, 0.8, 0.7]]},
                         {"op": "mask", "clip": c, "effect": "darken"}, {"op": "follow", "clip": c, "mask": "m1", "tracker": "mine", "dx": 0.05}])
        ks = P.load(path).clip(c)["keys"]
        self.assertAlmostEqual(keys.value_at(ks["m1.x"], 1.0), 0.55, places=2)
        self.assertAlmostEqual(keys.value_at(ks["m1.y"], 3.0), 0.7, places=2)

    def test_path_simplification_keeps_the_shape(self):
        path = [[i / 30, 0.1 + 0.5 * i / 90, 0.5 + 0.2 * (1 if (i // 20) % 2 else -1) * 0.0] for i in range(91)]
        out = tracking.simplify(path)
        self.assertLessEqual(len(out), 5)       # a straight line needs almost no points
        wobbly = [[i / 30, 0.5 + 0.2 * ((i % 40) / 40), 0.5] for i in range(121)]
        self.assertGreater(len(tracking.simplify(wobbly)), 6)


# -------------------------------------------------------------------------------------------
def frame_difference(path):
    """Average difference between consecutive frames: lower means steadier."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-vf",
                        "tblend=all_mode=difference,signalstats,metadata=print", "-f", "null", "-"], capture_output=True, text=True)
    vals = [float(v) for v in re.findall(r"YAVG=(-?[\d.]+)", r.stderr)]
    return sum(vals) / len(vals)


class TestStabilization(AdvBase):
    def shaky_project(self):
        path = self.new()
        a = self.imp(path, "shaky.mp4")
        c = self.clip(path, a, dur=4)
        return path, c

    @unittest.skipUnless(stabilize.available(), "this FFmpeg has no libvidstab")
    def test_stabilizing_reduces_frame_to_frame_motion(self):
        path, c = self.shaky_project()
        raw = frame_difference(render.render(P.load(path), preview=True))
        res, _ = ops.apply(path, [{"op": "stabilize", "clip": c, "strength": 0.6}])
        self.assertGreater(res[0]["smoothing_frames"], 5)
        calm = frame_difference(render.render(P.load(path), preview=True))
        self.assertLess(calm, raw * 0.8, f"shaky {raw:.1f} -> stabilized {calm:.1f}")

    @unittest.skipUnless(stabilize.available(), "this FFmpeg has no libvidstab")
    def test_analysis_is_cached_and_stabilization_can_be_removed(self):
        path, c = self.shaky_project()
        ops.apply(path, [{"op": "stabilize", "clip": c}])
        render.render(P.load(path), preview=True)
        files = list((P.load(path).dir / ".cache").glob("stab_*.trf"))
        self.assertEqual(len(files), 1)
        stamp = files[0].stat().st_mtime_ns
        render.render(P.load(path), preview=True)
        self.assertEqual(files[0].stat().st_mtime_ns, stamp)           # not analysed again
        ops.apply(path, [{"op": "stabilize", "clip": c, "strength": "off"}])
        self.assertNotIn("stabilize", P.load(path).clip(c))

    def test_without_vidstab_it_says_so_instead_of_pretending(self):
        path, c = self.shaky_project()
        original = stabilize.available
        stabilize.available = lambda: False
        try:
            with self.assertRaises(EditError) as cm:
                ops.apply(path, [{"op": "stabilize", "clip": c}])
            self.assertIn("libvidstab", str(cm.exception))
            P.load(path).clip(c)["stabilize"] = {"strength": 0.5}
            proj = P.load(path)
            proj.clip(c)["stabilize"] = {"strength": 0.5}
            self.assertTrue(any("libvidstab" in e for e in ops.check(proj)["errors"]))
        finally:
            stabilize.available = original

    def test_validation(self):
        path, c = self.shaky_project()
        if stabilize.available():
            with self.assertRaises(EditError):
                ops.apply(path, [{"op": "stabilize", "clip": c, "strength": 3}])
        t = ops.apply(path, [{"op": "add_text", "text": "x", "at": 0, "dur": 1}])[0][0]["clip"]
        with self.assertRaises(EditError):
            ops.apply(path, [{"op": "stabilize", "clip": t}])


class TestLongKeyframeCurves(AdvBase):
    def test_hundreds_of_keyframes_render(self):
        path = self.new()
        c = self.clip_of(path, "box.png", 4)
        pts = [[round(i * 4 / 299, 4), 1.0 + 0.5 * i / 299] for i in range(300)]
        ops.apply(path, [{"op": "set_keys", "clip": c, "prop": "scale", "points": pts}])
        end = self.frame(path, 3.95)
        self.assertLess(region(end, 430, 150, 60, 50)["Y"], 150)    # zoomed in: the box has drifted out of this spot
        self.assertGreater(region(self.frame(path, 0.05), 430, 150, 60, 50)["Y"], 200)


class TestVeryLongGraphs(AdvBase):
    def test_a_graph_too_long_for_the_command_line_is_passed_as_a_file(self):
        path = self.new()
        c = self.clip_of(path, "box.png", 4)
        pts = [[round(i * 4 / 399, 4), 0.5 * i / 399] for i in range(400)]
        ops.apply(path, [{"op": "set_keys", "clip": c, "prop": "x", "points": pts},
                         {"op": "set_keys", "clip": c, "prop": "y", "points": pts}])
        plan = render.Plan(P.load(path), preview=True)
        graph = plan.build()[0]
        self.assertGreater(len(graph), render.INLINE_GRAPH)
        out = render.render(P.load(path), preview=True)
        self.assertTrue(out.is_file())
        self.assertTrue((P.load(path).out_dir / ".graph.txt").is_file())
        end = self.frame(path, 3.95)   # shifted right and down by half the frame: the box has left its spot and the bottom-right now holds black
        self.assertLess(region(end, 430, 150, 60, 50)["Y"], 40)


class TestCliAdvanced(AdvBase):
    def vedit(self, *args, ok=True):
        r = subprocess.run([sys.executable, "-I", str(ROOT / "editor" / "vedit.py"), *map(str, args)], capture_output=True, text=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_cli_flow(self):
        folder = self.tmp / "cli_adv"
        self.vedit("new", folder, "--size", "320x180")
        p = ["-p", folder]
        a = json.loads(self.vedit("import", self.media / "timecode.mp4", *p).stdout)["asset"]
        self.vedit("add", a, "--dur", "4", *p)
        self.vedit("reverse", "c1", *p)
        self.vedit("reverse", "c1", "--off", *p)
        r = json.loads(self.vedit("freeze", "c1", "--at", "1", "--dur", "0.5", *p).stdout)
        self.assertTrue(r["hold"])
        self.vedit("key", "c1", "speed", "--at", "0", "--value", "1", *p)
        self.vedit("key", "c1", "speed", "--at", "1", "--value", "2", *p)
        self.vedit("mask", "c1", "--shape", "ellipse", "--effect", "blur", "--w", "0.3", *p)
        self.vedit("mask", "c1", "--id", "m1", "--x", "0.4", "--invert", *p)
        shown = self.vedit("show", *p).stdout
        for word in ("frozen", "speed-ramp", "masks=m1:blur"):
            self.assertIn(word, shown)
        self.vedit("unmask", "c1", "m1", *p)
        self.assertEqual(json.loads(self.vedit("check", *p).stdout)["errors"], [])
        self.assertTrue(Path(json.loads(self.vedit("render", "--preview", *p).stdout)["rendered"]).is_file())

    def test_tracker_commands(self):
        folder = self.tmp / "cli_track"
        self.vedit("new", folder, "--size", "640x360")
        p = ["-p", folder]
        a = json.loads(self.vedit("import", self.media / "move.mp4", *p).stdout)["asset"]
        self.vedit("add", a, "--dur", "3", *p)
        x, y = truth(0.0)
        out = json.loads(self.vedit("tracker", "add", "c1", "--at", "0", "--x", x, "--y", y, "--size", "0.2", "--name", "face", *p).stdout)
        self.assertEqual(out["tracker"], "face")
        self.vedit("mask", "c1", "--effect", "pixelate", *p)
        self.vedit("follow", "c1", "m1", "face", *p)
        self.assertIn("tracker face", self.vedit("show", *p).stdout)
        r = self.vedit("tracker", "add", "c1", "--at", "0", *p, ok=False)
        self.assertIn("--x", r.stderr)
        self.vedit("tracker", "remove", "face", *p)
        r = self.vedit("follow", "c1", "m1", "face", *p, ok=False)
        self.assertIn("no tracker", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_looks_lists_the_new_vocabulary(self):
        info = json.loads(self.vedit("looks").stdout)
        self.assertIn("speed", info["keyframe_properties"])
        self.assertEqual(set(info["mask_shapes"]), {"rect", "ellipse"})
        self.assertIn("pixelate", info["mask_effects"])


if __name__ == "__main__":
    unittest.main()
