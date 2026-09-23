"""Tests for wxgf alpha handling in image_decode.py (synthetic data only).

Run: ./venv/bin/python -m unittest discover -s tests
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import image_decode as I  # noqa: E402
import wxgf_fixtures as F  # noqa: E402

HAVE_SIPS = sys.platform == "darwin" and shutil.which("sips") is not None


def boxes(data, start=0, end=None):
    return list(F._boxes(data, start, end))


def child(data, parent, typ, skip=0):
    """(start, end) of the first `typ` box inside parent (start, end)."""
    return next((s, e) for t, s, e in F._boxes(data, parent[0] + skip, parent[1]) if t == typ)


def png_alpha(png):
    """Alpha samples of an 8-bit RGBA / GA PNG (tiny images only)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    i, idat, ihdr = 8, b"", None
    while i < len(png):
        ln, typ = struct.unpack(">I4s", png[i:i + 8])
        body = png[i + 8:i + 8 + ln]
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat += body
        i += 12 + ln
    w, h, depth, ctype = ihdr[:4]
    if ctype not in (4, 6) or depth != 8:
        return None
    bpp = 4 if ctype == 6 else 2
    raw = zlib.decompress(idat)
    stride = w * bpp
    prev = bytearray(stride)
    alpha = []
    p = 0
    for _ in range(h):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[x] = (line[x] + pr) & 255
        alpha += line[bpp - 1::bpp]
        prev = line
    return alpha


class WxgfStreamsTest(unittest.TestCase):
    def test_parse_synthetic_sps(self):
        sps = I._parse_sps(F.make_sps(330, 218, chroma=0))
        self.assertEqual((sps["width"], sps["height"], sps["chroma"]), (330, 218, 0))

    def test_single_stream(self):
        color = F.fake_stream(64, 48)
        c, a = I.wxgf_streams(F.make_wxgf(color))
        self.assertEqual(c, color)
        self.assertIsNone(a)
        self.assertFalse(I.wxgf_has_alpha(F.make_wxgf(color)))

    def test_alpha_stream_first_and_monochrome(self):
        color = F.fake_stream(64, 48, slice_payload=b"\x80" + b"\x55" * 200)
        alpha = F.fake_stream(64, 48, chroma=0)
        c, a = I.wxgf_streams(F.make_wxgf(color, alpha))
        self.assertEqual((c, a), (color, alpha))
        self.assertTrue(I.wxgf_has_alpha(F.make_wxgf(color, alpha)))

    def test_second_stream_ignored_if_not_alpha(self):
        color = F.fake_stream(64, 48, slice_payload=b"\x80" + b"\x55" * 200)
        # different size -> not an alpha plane
        self.assertIsNone(I.wxgf_streams(F.make_wxgf(color, F.fake_stream(32, 24, 0)))[1])
        # colour (4:2:0) second stream -> not an alpha plane
        self.assertIsNone(I.wxgf_streams(F.make_wxgf(color, F.fake_stream(64, 48, 1)))[1])


class HeicAlphaStructureTest(unittest.TestCase):
    def setUp(self):
        self.color = F.fake_stream(64, 48, slice_payload=b"\x80COLOR")
        self.alpha = F.fake_stream(64, 48, chroma=0, slice_payload=b"\x80ALPHA")

    def meta(self, heic):
        s, e = next((s, e) for t, s, e in boxes(heic) if t == b"meta")
        return {t: (a, b) for t, a, b in F._boxes(heic, s + 4, e)}

    def test_without_alpha_single_item(self):
        heic = I.hevc_to_heic(self.color)
        m = self.meta(heic)
        self.assertNotIn(b"iref", m)
        s, _ = m[b"iinf"]
        self.assertEqual(struct.unpack(">H", heic[s + 4:s + 6])[0], 1)

    def test_alpha_item_layout(self):
        heic = I.hevc_to_heic(self.color, self.alpha)
        m = self.meta(heic)
        # two hvc1 items
        s, _ = m[b"iinf"]
        self.assertEqual(struct.unpack(">H", heic[s + 4:s + 6])[0], 2)
        # iref: auxl from item 2 to item 1
        auxl = child(heic, m[b"iref"], b"auxl", skip=4)
        self.assertEqual(struct.unpack(">HHH", heic[auxl[0]:auxl[0] + 6]), (2, 1, 1))
        # auxC property with the HEVC alpha URN
        ipco = child(heic, m[b"iprp"], b"ipco")
        props = [(t, heic[a:b]) for t, a, b in F._boxes(heic, *ipco)]
        self.assertEqual([t for t, _ in props], [b"hvcC", b"ispe", b"hvcC", b"ispe", b"auxC"])
        self.assertEqual(props[4][1][4:], I.HEVC_ALPHA_URN + b"\0")
        self.assertEqual(props[2][1][17] & 3, 0)  # alpha hvcC: chroma_format_idc 0
        # ipma associates alpha item with hvcC#3, ispe#4, auxC#5
        ipma = child(heic, m[b"iprp"], b"ipma")
        body = heic[ipma[0] + 4:ipma[1]]
        self.assertEqual(body, struct.pack(">I", 2) + struct.pack(">HB", 1, 2) + bytes([0x81, 2])
                         + struct.pack(">HB", 2, 3) + bytes([0x83, 4, 0x85]))
        # iloc extents point at each item's length-prefixed slice data
        s, _ = m[b"iloc"]
        (n,) = struct.unpack(">H", heic[s + 6:s + 8])
        self.assertEqual(n, 2)
        ext = [struct.unpack(">HHHII", heic[s + 8 + 14 * i:s + 22 + 14 * i]) for i in range(2)]
        self.assertEqual(heic[ext[0][3] + 4:ext[0][3] + ext[0][4]][2:], b"\x80COLOR")
        self.assertEqual(heic[ext[1][3] + 4:ext[1][3] + ext[1][4]][2:], b"\x80ALPHA")


class AlphaDecisionTest(unittest.TestCase):
    def test_bmp_min_sample_ignores_row_padding(self):
        bmp = F.make_bmp(3, 2, lambda x, y: (255, 255, 254))  # 9-byte rows + 3 pad bytes
        self.assertEqual(I.bmp_min_sample(bmp), 254)
        bmp = F.make_bmp(3, 2, lambda x, y: (255, 10, 255) if (x, y) == (2, 1) else (255,) * 3)
        self.assertEqual(I.bmp_min_sample(bmp), 10)
        with self.assertRaises(I.ImageDecodeError):
            I.bmp_min_sample(b"GIF89a" + b"\0" * 60)

    def test_auto_picks_png_only_for_real_transparency(self):
        color = F.fake_stream(64, 48, slice_payload=b"\x80" + b"\x55" * 200)
        alpha = F.fake_stream(64, 48, chroma=0)
        wx = F.make_wxgf(color, alpha)
        calls = []

        def fake_convert(heic, fmt="jpg"):
            calls.append((fmt, heic.count(b"auxC")))
            return b"IMG"

        with mock.patch.object(I, "heic_convert", side_effect=fake_convert), \
                mock.patch.object(I, "alpha_is_opaque", return_value=True):
            self.assertEqual(I.wxgf_convert(wx, "auto"), (b"IMG", "jpg"))
        with mock.patch.object(I, "heic_convert", side_effect=fake_convert), \
                mock.patch.object(I, "alpha_is_opaque", return_value=False):
            self.assertEqual(I.wxgf_convert(wx, "auto"), (b"IMG", "png"))
            self.assertEqual(I.wxgf_convert(F.make_wxgf(color), "auto"), (b"IMG", "jpg"))
            self.assertEqual(I.wxgf_convert(wx, "jpg"), (b"IMG", "jpg"))
            self.assertEqual(I.wxgf_convert(wx, "png"), (b"IMG", "png"))
        self.assertEqual(calls, [("jpg", 0), ("png", 1), ("jpg", 0), ("jpg", 0), ("png", 1)])
        heic, ext = I.wxgf_convert(wx, "heic")
        self.assertEqual((ext, heic.count(b"auxC")), ("heic", 1))

    def test_png_falls_back_to_opaque_when_alpha_rejected(self):
        color = F.fake_stream(64, 48, slice_payload=b"\x80" + b"\x55" * 200)
        wx = F.make_wxgf(color, F.fake_stream(64, 48, chroma=0))

        def fake_convert(heic, fmt="jpg"):
            if b"auxC" in heic:
                raise I.ImageDecodeError("sips failed")
            return b"OPAQUE"

        with mock.patch.object(I, "heic_convert", side_effect=fake_convert):
            self.assertEqual(I.wxgf_convert(wx, "png"), (b"OPAQUE", "png"))


@unittest.skipUnless(HAVE_SIPS, "needs macOS sips")
class SipsEndToEndTest(unittest.TestCase):
    """Real HEVC streams (encoded by sips from synthetic BMPs) combined into
    an alpha HEIC and converted back: the PNG must carry the mask."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wxgf_alpha_test_")

        def encode(name, pixel):
            src = os.path.join(cls.tmp, name + ".bmp")
            dst = os.path.join(cls.tmp, name + ".heic")
            with open(src, "wb") as f:
                f.write(F.make_bmp(64, 64, pixel))
            subprocess.run(["sips", "-s", "format", "heic", src, "--out", dst],
                           capture_output=True, check=True)
            with open(dst, "rb") as f:
                return F.heic_to_annexb(f.read())
        cls.color = encode("color", lambda x, y: (200, x * 4, y * 4))
        cls.mask = encode("mask", lambda x, y: (0,) * 3 if x < 32 else (255,) * 3)
        cls.white = encode("white", lambda x, y: (255,) * 3)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_png_has_alpha_mask(self):
        png = I.heic_convert(I.hevc_to_heic(self.color, self.mask), "png")
        alpha = png_alpha(png)
        self.assertIsNotNone(alpha, "PNG has no alpha channel")
        self.assertEqual(len(alpha), 64 * 64)
        left = [alpha[y * 64 + x] for y in range(64) for x in range(4, 28)]
        right = [alpha[y * 64 + x] for y in range(64) for x in range(36, 60)]
        self.assertLess(max(left), 16)
        self.assertGreater(min(right), 240)

    def test_alpha_is_opaque(self):
        self.assertTrue(I.alpha_is_opaque(self.white))
        self.assertFalse(I.alpha_is_opaque(self.mask))

    def test_auto_end_to_end(self):
        wx = F.make_wxgf(self.color, self.mask)
        with mock.patch.object(I, "_hevc_is_monochrome",
                               side_effect=lambda s: s != self.color):
            data, ext = I.wxgf_convert(wx, "auto")
            self.assertEqual(ext, "png")
            self.assertLess(min(png_alpha(data)), 16)
            data, ext = I.wxgf_convert(F.make_wxgf(self.color, self.white), "auto")
        self.assertEqual(ext, "jpg")
        self.assertTrue(data.startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
