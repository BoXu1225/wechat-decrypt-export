"""Tests for media_decode.py, media rendering in formatters.py and the
export_chat.py --voice / --media options (synthetic data only).

Run: ./venv/bin/python -m unittest discover -s tests
"""
import contextlib
import csv
import io
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from datetime import datetime
from html.parser import HTMLParser
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import chats  # noqa: E402
import export_chat as E  # noqa: E402
import formatters as F  # noqa: E402
import media_decode as M  # noqa: E402
from test_chats import ALICE, SELF, Fixture, tbl  # noqa: E402

try:
    import pysilk  # noqa: F401
    HAVE_SILK = True
except ImportError:
    HAVE_SILK = False

VIDEO_MD5 = "fedcba9876543210fedcba9876543210"
T0 = int(datetime(2026, 3, 1, 9, 5, 7).timestamp())


def sine_pcm(seconds=1.0, rate=24000):
    n = int(seconds * rate)
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                    for i in range(n))


def silk_sample(seconds=1.0):
    """A real WeChat-style SILK blob (0x02 + #!SILK_V3 ...) encoded with pysilk."""
    out = io.BytesIO()
    pysilk.encode(io.BytesIO(sine_pcm(seconds)), out, 24000, 24000, tencent=True)
    return out.getvalue()


def _box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def fake_mp4(seconds=35, timescale=1000):
    mvhd = _box(b"mvhd", b"\x00" * 4 + struct.pack(">IIII", 0, 0, timescale,
                                                    seconds * timescale) + b"\x00" * 80)
    return (_box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41") + _box(b"free", b"")
            + _box(b"mdat", b"\x00" * 100) + _box(b"moov", mvhd))


def make_media_db(path, rows):
    """rows: (username, create_time, local_id, svr_id, blob[, data_index])."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE VoiceInfo(chat_name_id INTEGER, create_time INTEGER, "
                "local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index TEXT DEFAULT '0')")
    con.execute("INSERT INTO Name2Id VALUES ('someone_else')")
    for r in rows:
        user = r[0]
        con.execute("INSERT OR IGNORE INTO Name2Id VALUES (?)", (user,))
        (cid,) = con.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (user,)).fetchone()
        con.execute("INSERT INTO VoiceInfo VALUES (?,?,?,?,?,?)",
                    (cid, r[1], r[2], r[3], r[4], r[5] if len(r) > 5 else "0"))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# media_decode
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_SILK, "silk-python not installed")
class SilkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="media_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_decode_with_and_without_prefix(self):
        blob = silk_sample(1.0)
        self.assertEqual(blob[:10], b"\x02#!SILK_V3")
        self.assertEqual(M.silk_frame_count(blob), 50)  # 20 ms frames
        pcm = M.silk_to_pcm(blob)
        self.assertEqual(len(pcm), 24000 * 2)
        self.assertEqual(len(M.silk_to_pcm(blob[1:])), 24000 * 2)

    def test_bad_input(self):
        for bad in (b"", b"garbage data", b"#!AMR\n1234"):
            with self.assertRaises(M.MediaDecodeError):
                M.silk_to_pcm(bad)

    def test_wav_fallback(self):
        with mock.patch.object(M, "afconvert_path", return_value=None):
            path, dur = M.voice_to_file(silk_sample(2.0), os.path.join(self.tmp, "v"))
        self.assertTrue(path.endswith("v.wav"))
        self.assertAlmostEqual(dur, 2.0, places=2)
        self.assertAlmostEqual(M.audio_file_duration(path), 2.0, places=2)
        self.assertEqual(M.existing_audio(os.path.join(self.tmp, "v")), path)
        self.assertEqual([f for f in os.listdir(self.tmp)], ["v.wav"])  # no temp files left

    @unittest.skipUnless(M.afconvert_path(), "afconvert (macOS) not available")
    def test_m4a_via_afconvert(self):
        path, dur = M.voice_to_file(silk_sample(3.0), os.path.join(self.tmp, "sub", "123"))
        self.assertTrue(path.endswith("123.m4a"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(8)[4:8], b"ftyp")
        self.assertAlmostEqual(M.mp4_duration(path), 3.0, delta=0.2)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["123.m4a"])


class Mp4AndFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="media_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_mp4_duration(self):
        p = os.path.join(self.tmp, "a.mp4")
        with open(p, "wb") as f:
            f.write(fake_mp4(35, 600))
        self.assertAlmostEqual(M.mp4_duration(p), 35.0)
        self.assertTrue(M.is_mp4(p))
        with open(p, "wb") as f:
            f.write(b"\x00" * 64)
        self.assertIsNone(M.mp4_duration(p))
        self.assertFalse(M.is_mp4(p))
        self.assertIsNone(M.mp4_duration(os.path.join(self.tmp, "missing.mp4")))

    def test_seconds(self):
        self.assertEqual(M.seconds(0.2), 1)
        self.assertEqual(M.seconds(6.637), 7)
        self.assertEqual(M.seconds(6.4), 6)
        self.assertIsNone(M.seconds(None))

    def test_voice_stem(self):
        self.assertEqual(M.voice_stem(987, 100, 5), "987")
        self.assertEqual(M.voice_stem(None, 100, 5), "100_5")

    def test_copy_file(self):
        src = os.path.join(self.tmp, "src.mp4")
        with open(src, "wb") as f:
            f.write(fake_mp4())
        dst = M.copy_file(src, os.path.join(self.tmp, "out", "dst.mp4"))
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), fake_mp4())
        self.assertNotEqual(os.stat(src).st_ino, os.stat(dst).st_ino)  # never a hardlink
        self.assertEqual(os.listdir(os.path.dirname(dst)), ["dst.mp4"])

    def test_find_video(self):
        month = time.strftime("%Y-%m", time.localtime(T0))
        d = os.path.join(self.tmp, "msg", "video", month)
        os.makedirs(d)
        packed = b"\x12\x20" + VIDEO_MD5.encode()
        self.assertEqual(M.find_video_for_message(self.tmp, packed, T0), (None, None))
        with open(os.path.join(d, VIDEO_MD5 + "_thumb.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff")
        mp4 = os.path.join(d, VIDEO_MD5 + ".mp4")
        with open(mp4, "wb") as f:
            f.write(b"\x00" * 32)  # incomplete download
        self.assertEqual(M.find_video_for_message(self.tmp, packed, T0),
                         (None, os.path.join(d, VIDEO_MD5 + "_thumb.jpg")))
        with open(mp4, "wb") as f:
            f.write(fake_mp4())
        self.assertEqual(M.find_video_for_message(self.tmp, packed, T0)[0], mp4)
        # wrong month hint -> slow path still finds it
        self.assertEqual(M.find_video_for_message(self.tmp, md5=VIDEO_MD5,
                                                  create_time=T0 - 400 * 86400)[0], mp4)
        self.assertEqual(M.find_video_for_message(self.tmp, None, T0), (None, None))


class VoiceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="media_test_")
        make_media_db(os.path.join(self.tmp, "message", "media_0.db"), [
            (ALICE, 100, 7, 555, b"A"),
            (ALICE, 101, 7, 0, b"B"),     # no server id: (create_time, local_id)
            ("room@chatroom", 100, 7, 555, b"C"),
            (ALICE, 200, 9, 777, b"-2", "1"),
            (ALICE, 200, 9, 777, b"part1", "0"),
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_lookup(self):
        with M.VoiceStore(self.tmp) as s:
            self.assertEqual(s.get(ALICE, 555), b"A")
            self.assertEqual(s.get(ALICE, None, 101, 7), b"B")
            self.assertEqual(s.get(ALICE, 999, 101, 7), b"B")  # svr miss -> fallback
            self.assertEqual(s.get("room@chatroom", 555), b"C")
            self.assertEqual(s.get(ALICE, 777), b"part1-2")  # chunks by data_index
            self.assertIsNone(s.get(ALICE, 999))
            self.assertIsNone(s.get("nobody", 555))
        with M.VoiceStore(os.path.join(self.tmp, "nope")) as s:
            self.assertIsNone(s.get(ALICE, 555))


# ---------------------------------------------------------------------------
# formatters
# ---------------------------------------------------------------------------

class _Media(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class FormatterMediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        files = os.path.join(self.dir, "我 的_files")
        os.makedirs(files)
        self.audio = os.path.join(files, "123.m4a")
        self.video = os.path.join(files, f"{VIDEO_MD5}.mp4")
        self.poster = os.path.join(files, f"{VIDEO_MD5}_thumb.jpg")
        self.records = [
            {"ts": T0, "sender": "张三", "is_self": False, "kind": "voice", "text": "[语音]",
             "audio_path": self.audio, "duration": 12},
            {"ts": T0 + 1, "sender": "我", "is_self": True, "kind": "video", "text": "[视频]",
             "video_path": self.video, "poster_path": self.poster, "duration": 95},
            {"ts": T0 + 2, "sender": "张三", "is_self": False, "kind": "video", "text": "[视频]",
             "poster_path": self.poster, "duration": 3725},
            {"ts": T0 + 3, "sender": "张三", "is_self": False, "kind": "voice", "text": "[语音]",
             "duration": 3},  # audio missing
            {"ts": T0 + 4, "sender": "张三", "is_self": False, "kind": "voice", "text": "[语音]"},
        ]
        self.meta = {"name": "<g>", "is_group": True}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, fmt, name=None):
        p = os.path.join(self.dir, "sub", name or f"c.{fmt}")
        F.write(self.records, self.meta, p, fmt)
        with open(p, encoding="utf-8-sig", newline="") as f:
            return f.read()

    def test_labels(self):
        self.assertEqual(F.fmt_duration("voice", 12), "12″")
        self.assertEqual(F.fmt_duration("video", 35), "0:35")
        self.assertEqual(F.fmt_duration("video", 3725), "1:02:05")
        self.assertEqual([F.label(r) for r in self.records],
                         ["[语音 12″]", "[视频 1:35]", "[视频 1:02:05]", "[语音 3″]", "[语音]"])
        self.assertEqual(F.label({"kind": "text", "text": "x", "duration": 5}), "x")

    def test_txt_csv(self):
        t = self.write("txt")
        self.assertIn("张三: [语音 12″]\n", t)
        self.assertIn("我: [视频 1:35]\n", t)
        self.assertTrue(t.endswith("张三: [语音]\n"))
        rows = list(csv.reader(io.StringIO(self.write("csv"))))
        self.assertEqual([r[4] for r in rows[1:3]], ["[语音 12″]", "[视频 1:35]"])

    def test_html(self):
        s = self.write("html")
        p = _Media()
        p.feed(s)
        audio = [a for t, a in p.tags if t == "audio"]
        video = [a for t, a in p.tags if t == "video"]
        imgs = [a for t, a in p.tags if t == "img"]
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]["src"], "../%E6%88%91%20%E7%9A%84_files/123.m4a")
        self.assertEqual(audio[0]["preload"], "none")
        self.assertIn("controls", audio[0])
        self.assertEqual(len(video), 1)
        self.assertEqual(video[0]["preload"], "none")
        self.assertEqual(video[0]["src"], f"../%E6%88%91%20%E7%9A%84_files/{VIDEO_MD5}.mp4")
        self.assertEqual(video[0]["poster"],
                         f"../%E6%88%91%20%E7%9A%84_files/{VIDEO_MD5}_thumb.jpg")
        self.assertEqual(len(imgs), 1)  # poster-only video shows its thumbnail
        self.assertIn('<span class="dur">12″</span>', s)
        self.assertIn('<span class="dur">1:35</span>', s)
        self.assertIn(">[语音 3″]</div>", s)
        self.assertNotIn("<g>", s)

    def test_html_escapes_paths(self):
        evil = os.path.join(self.dir, 'x" onerror="alert(1).m4a')
        self.records = [dict(self.records[0], audio_path=evil)]
        s = self.write("html")
        p = _Media()
        p.feed(s)
        (tag, attrs), = [t for t in p.tags if t[0] == "audio"]
        self.assertNotIn("onerror", attrs)
        self.assertIn("%22%20onerror", attrs["src"])

    def test_markdown(self):
        s = self.write("md")
        self.assertIn("[▶ 语音 12″](../%E6%88%91%20%E7%9A%84_files/123.m4a)", s)
        self.assertIn(f"[![▶ 视频 1:35](../%E6%88%91%20%E7%9A%84_files/{VIDEO_MD5}_thumb.jpg)]"
                      f"(../%E6%88%91%20%E7%9A%84_files/{VIDEO_MD5}.mp4)", s)
        self.assertIn(f"![[视频 1:02:05]](../%E6%88%91%20%E7%9A%84_files/{VIDEO_MD5}_thumb.jpg)", s)
        self.assertIn("  [语音 3″]\n", s)

    def test_json(self):
        d = json.loads(self.write("json"))
        m = d["messages"]
        self.assertEqual(m[0]["audio_path"], "../我 的_files/123.m4a")
        self.assertEqual(m[0]["duration"], 12)
        self.assertEqual(m[1]["video_path"], f"../我 的_files/{VIDEO_MD5}.mp4")
        self.assertEqual(m[1]["poster_path"], f"../我 的_files/{VIDEO_MD5}_thumb.jpg")
        self.assertEqual(m[0]["text"], "[语音]")
        self.assertNotIn("duration", m[4])


# ---------------------------------------------------------------------------
# chats + export_chat
# ---------------------------------------------------------------------------

class ChatsMediaTest(unittest.TestCase):
    def test_media_duration(self):
        self.assertEqual(chats.media_duration('<voicemsg voicelength = "6637" />', "voice"), 6.637)
        self.assertEqual(chats.media_duration('<videomsg playlength="35" />', "video"), 35.0)
        self.assertIsNone(chats.media_duration('<videomsg length="35" />', "video"))
        self.assertIsNone(chats.media_duration(None, "voice"))


class ExportMediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="export_media_test_")
        self.dec = os.path.join(self.tmp, "decrypted")
        self.fx = Fixture(self.dec)
        self.out = os.path.join(self.tmp, "export")
        self.base = os.path.join(self.tmp, "wechat_base")
        cfg = {"decrypted_dir": self.dec, "self_wxid": SELF, "wechat_base_dir": self.base,
               "image_aes_key": "0123456789abcdef", "image_xor_key": 0}
        p = mock.patch.object(E, "get_config", return_value=cfg)
        p.start()
        self.addCleanup(p.stop)
        # voice with a server id and stored audio, voice without stored audio,
        # a downloaded video, and a video with only its thumbnail.
        self.v1 = self.add_msg(34, ALICE, 210, '<msg><voicemsg voicelength="2400" /></msg>',
                               server_id=4242)
        self.add_msg(34, SELF, 211, '<msg><voicemsg voicelength="5000" /></msg>', server_id=4343)
        self.add_msg(43, ALICE, 212, '<msg><videomsg playlength = "35" /></msg>',
                     packed=b"\x12\x20" + VIDEO_MD5.encode())
        self.add_msg(43, SELF, 213, '<msg><videomsg playlength="7" /></msg>',
                     packed=b"\x12\x20" + b"1" * 32)
        if HAVE_SILK:
            make_media_db(os.path.join(self.dec, "message", "media_0.db"),
                          [(ALICE, 210, self.v1, 4242, silk_sample(2.0))])
        vdir = os.path.join(self.base, "msg", "video", time.strftime("%Y-%m", time.localtime(212)))
        os.makedirs(vdir)
        with open(os.path.join(vdir, VIDEO_MD5 + ".mp4"), "wb") as f:
            f.write(fake_mp4(35))
        for md5 in (VIDEO_MD5, "1" * 32):
            with open(os.path.join(vdir, md5 + "_thumb.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0jpeg")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def add_msg(self, local_type, sender, ts, content, server_id=None, packed=None):
        conn = sqlite3.connect(self.fx.dbs[0])
        sid = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (sender,)).fetchone()[0]
        cur = conn.execute(f"INSERT INTO {tbl(ALICE)}(server_id, local_type, sort_seq, "
                           "real_sender_id, create_time, message_content, packed_info_data) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (server_id, local_type, ts * 1000, sid, ts, content, packed))
        conn.commit()
        conn.close()
        return cur.lastrowid

    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = E.main(list(args) + ["--no-decrypt", "--export-dir", self.out])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return f.read()

    def files(self):
        d = os.path.join(self.out, "Alice R_files")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_txt_without_flags_unchanged(self):
        self.run_cli("Alice R")
        t = self.read("Alice R_chat.txt")
        self.assertIn("Alice R: [语音]\n", t)
        self.assertIn("Alice R: [视频]\n", t)
        self.assertEqual(self.files(), [])

    @unittest.skipUnless(HAVE_SILK, "silk-python not installed")
    def test_voice_only(self):
        out = self.run_cli("Alice R", "-i", "-f", "html", "--voice")
        self.assertIn("新转换 1，已存在 0，缺失 1，转换失败 0", out)
        self.assertNotIn("[+] 视频", out)
        ext = ".m4a" if M.afconvert_path() else ".wav"
        self.assertEqual(self.files(), [f"4242{ext}"])
        s = self.read("Alice R.html")
        self.assertIn(f'<audio controls preload="none" src="Alice%20R_files/4242{ext}"', s)
        self.assertIn('<span class="dur">2″</span>', s)
        self.assertIn(">[语音 5″]</div>", s)  # missing audio keeps the stated length
        self.assertEqual(s.count("<video"), 0)
        # rerun: nothing converted again, file untouched
        out = self.run_cli("Alice R", "-i", "-f", "html", "--voice")
        self.assertIn("新转换 0，已存在 1", out)
        self.assertIn("没有新消息", out)
        # re-render without flags keeps the audio reference
        out = self.run_cli("Alice R", "-i", "-f", "html")
        self.assertIn("没有新消息", out)
        # txt with --voice gets labels, and a json export carries the fields
        self.run_cli("Alice R", "--voice")
        t = self.read("Alice R_chat.txt")
        self.assertIn("Alice R: [语音 2″]\n", t)
        self.assertIn("我: [语音 5″]\n", t)
        self.assertIn("Alice R: [视频 0:35]\n", t)  # stated length, file not copied
        self.run_cli("Alice R", "-f", "json", "--voice")
        msgs = json.loads(self.read("Alice R_chat.json"))["messages"]
        v = [m for m in msgs if m["kind"] == "voice"]
        self.assertEqual(v[0]["audio_path"], f"Alice R_files/4242{ext}")
        self.assertEqual([m["duration"] for m in v], [2, 5])
        self.assertTrue(all("media_duration" not in m and "packed_info_data" not in m
                            for m in msgs))

    def test_media_video(self):
        out = self.run_cli("Alice R", "-i", "-f", "html", "--media")
        self.assertIn("新复制 1，已存在 0，仅封面 1（视频未下载），缺失 0", out)
        self.assertIn(f"{VIDEO_MD5}.mp4", self.files())
        self.assertIn(f"{VIDEO_MD5}_thumb.jpg", self.files())
        self.assertIn(f"{'1' * 32}_thumb.jpg", self.files())
        s = self.read("Alice R.html")
        self.assertIn(f'<video controls preload="none" poster="Alice%20R_files/{VIDEO_MD5}_thumb.jpg"'
                      f' src="Alice%20R_files/{VIDEO_MD5}.mp4"', s)
        self.assertIn('<span class="dur">0:35</span>', s)
        self.assertIn(f'<img loading="lazy" src="Alice%20R_files/{"1" * 32}_thumb.jpg"', s)
        out = self.run_cli("Alice R", "-i", "-f", "html", "--media")
        self.assertIn("新复制 0，已存在 1，仅封面 1", out)
        self.run_cli("Alice R", "-f", "md", "--media")
        md = self.read("Alice R_chat.md")
        self.assertIn(f"[![▶ 视频 0:35](Alice%20R_files/{VIDEO_MD5}_thumb.jpg)]"
                      f"(Alice%20R_files/{VIDEO_MD5}.mp4)", md)
        out = self.run_cli("Alice R", "-f", "csv", "--media")
        self.assertIn("csv 格式不嵌入图片/语音/视频", out)
        self.assertIn("[视频 0:07]", self.read("Alice R_chat.csv"))


if __name__ == "__main__":
    unittest.main()
