"""Tests for moments.py, favorites.py and their MCP tools, using synthetic
sns.db / favorite.db files and a fake WeChat cache folder (no real data).

Run: python -m unittest discover -s tests
"""
import contextlib
import hashlib
import html.parser
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest

import zstandard
from Crypto.Cipher import AES

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import favorites as FV  # noqa: E402
import mcp_server as M  # noqa: E402
import moments as MO  # noqa: E402
import social_common as S  # noqa: E402
from test_chats import ALICE, BOB, DAVE, ROOM, SELF, Fixture  # noqa: E402

AES_KEY = "0123456789abcdef"
XOR_KEY = 0x42
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(256)) * 3 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x1cftypisom" + b"\x00" * 64

POST_TS = 1_700_000_000  # 2023-11-14


def v2_dat(payload, aes_size=64, xor_size=16):
    """Encrypt payload in WeChat's V2 .dat layout (inverse of image_decode)."""
    head = payload[:aes_size]
    pad = 16 - len(head) % 16
    enc = AES.new(AES_KEY.encode(), AES.MODE_ECB).encrypt(head + bytes([pad]) * pad)
    rest = payload[aes_size:]
    xor_size = min(xor_size, len(rest))
    middle, tail = rest[:len(rest) - xor_size], rest[len(rest) - xor_size:]
    return (b"\x07\x08V2\x08\x07" + struct.pack("<II", aes_size, xor_size) + b"\x00"
            + enc + middle + bytes(b ^ XOR_KEY for b in tail))


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def user_comment(username, nickname, ts, content=None, ref=None, deleted=0,
                 image=False, emoji=False):
    x = (f"<user_comment><username>{username}</username><nickname>{esc(nickname)}</nickname>"
         f"<create_time>{ts}</create_time><b_deleted>{deleted}</b_deleted>"
         f"<comment_id>{ts % 1000}</comment_id>")
    if content is not None:
        x += f"<content>{esc(content)}</content>"
    if ref:
        x += f"<ref_username>{ref}</ref_username>"
    x += "<imagelist>" + ("<imageinfo><md5>x</md5></imageinfo>" if image else "") + "</imagelist>"
    x += "<emojilist>" + ("<emojiinfo><md5>y</md5></emojiinfo>" if emoji else "") + "</emojilist>"
    return x + "</user_comment>"


def sns_xml(pid, username, nickname, ts, text="", ctype=1, media=(), title="", desc="",
            url="", location=None, likes=(), comments=(), private=0, source="",
            with_users=()):
    """A realistic <SnsDataItem> (same element names as WeChat 4.x)."""
    ms = ""
    for mid, mtype, live in media:
        ms += (f"<media><id>{mid}</id><type>{mtype}</type><sub_type>0</sub_type>"
               f'<url type="1" md5="abc" enc_idx="1" key="k" token="t">http://szmmsns.qpic.cn/'
               f'mmsns/AAAA{mid}/0</url><thumb type="1" enc_idx="1" key="k">http://szmmsns.qpic.cn/'
               f'mmsns/AAAA{mid}/150</thumb><size width="1440" height="1080" totalSize="1"/>'
               f"<videoDuration>{'12.5' if mtype == 6 else '0.0'}</videoDuration><enc>0</enc>"
               + ("<LivePhoto><liveMedia><id>l</id></liveMedia></LivePhoto>" if live else "")
               + "</media>")
    loc = '<location poiClassifyType="0" longitude="0.0" latitude="0.0" poiScale="0"></location>'
    if location:
        loc = (f'<location poiName="{esc(location)}" poiAddress="Addr 1" city="City" '
               f'country="CN" longitude="121.5" latitude="31.2" poiScale="0"></location>')
    return (
        "<SnsDataItem><TimelineObject>"
        f"<id>{pid}</id><username>{username}</username><createTime>{ts}</createTime>"
        f"<contentDescShowType>0</contentDescShowType><contentDescScene>0</contentDescScene>"
        f"<private>{private}</private><sightFolded>0</sightFolded><showFlag>0</showFlag>"
        f"<contentDesc>{esc(text)}</contentDesc>{loc}"
        f"<sourceNickName>{esc(source)}</sourceNickName>"
        f"<ContentObject><type>{ctype}</type><contentSubStyle>0</contentSubStyle>"
        f"<title>{esc(title)}</title><description>{esc(desc)}</description>"
        f"<contentUrl>{esc(url)}</contentUrl><mediaList>{ms}</mediaList></ContentObject>"
        "<statisticsData></statisticsData><canvasInfoXml></canvasInfoXml>"
        "</TimelineObject><LocalExtraInfo>"
        f"<nickname>{esc(nickname)}</nickname><tid>{pid}</tid><like_flag>0</like_flag>"
        + ("<like_user_list>" + "".join(likes) + "</like_user_list>" if likes else "")
        + ("<comment_user_list>" + "".join(comments) + "</comment_user_list>" if comments else "")
        + ("<with_user_list>" + "".join(user_comment(u, "", ts) for u in with_users)
           + "</with_user_list>" if with_users else "")
        + "</LocalExtraInfo></SnsDataItem>")


def signed(pid):
    return pid - (1 << 64) if pid >= (1 << 63) else pid


# post ids (unsigned); the first is >= 2**63 so SnsTimeLine.tid is negative
P_ALICE_IMG = 14872190000000000001
P_ALICE_LINK = 14872190000000000002
P_BOB_TEXT = 14872190000000000003
P_SELF_VIDEO = 14872190000000000004
P_DAVE = 14872190000000000005
P_ZSTD = 14872190000000000006
P_BAD = 14872190000000000007


def build_sns(root):
    os.makedirs(os.path.join(root, "sns"), exist_ok=True)
    conn = sqlite3.connect(os.path.join(root, "sns", "sns.db"))
    conn.execute("CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY DESC, user_name TEXT, "
                 "content TEXT, pack_info_buf TEXT)")
    conn.execute("CREATE TABLE SnsMessage_tmp3(local_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "create_time INTEGER, type INTEGER, feed_id INTEGER, from_username TEXT)")
    rows = [
        (P_ALICE_IMG, ALICE, sns_xml(
            P_ALICE_IMG, ALICE, "alice at the time", POST_TS,
            text="Sunny day <script>alert(1)</script>", ctype=54,
            media=[("m1", 2, True), ("m2", 2, False), ("m3", 2, False)], location="West Lake",
            likes=[user_comment(BOB, "bob nick", POST_TS + 10),
                   user_comment(SELF, "me nick", POST_TS + 11)],
            comments=[user_comment(BOB, "bob nick", POST_TS + 20, "nice!"),
                      user_comment(ALICE, "alice", POST_TS + 30, "thanks", ref=BOB),
                      user_comment(DAVE, "dave nick", POST_TS + 25, "deleted", deleted=1),
                      user_comment(SELF, "me nick", POST_TS + 40, "", emoji=True)],
            with_users=[BOB])),
        (P_ALICE_LINK, ALICE, sns_xml(
            P_ALICE_LINK, ALICE, "alice", POST_TS + 100, text="read this", ctype=3,
            media=[("c1", 0, False)], title="An article", desc="about things",
            url="javascript:alert(1)", source="Some Account")),
        (P_BOB_TEXT, BOB, sns_xml(P_BOB_TEXT, BOB, "Bobby old", POST_TS + 200,
                                  text="just words\nsecond line", ctype=2, private=1)),
        (P_SELF_VIDEO, SELF, sns_xml(P_SELF_VIDEO, SELF, "me nick", POST_TS + 300,
                                     text="my video", ctype=15, media=[("v1", 6, False)],
                                     title="", url="https://example.com/v")),
        (P_DAVE, DAVE, sns_xml(P_DAVE, DAVE, "dave nick", POST_TS - 86400 * 400,
                               text="old post from dave", ctype=1, media=[("d1", 2, False)])),
        (P_ZSTD, BOB, zstandard.ZstdCompressor().compress(sns_xml(
            P_ZSTD, BOB, "Bobby", POST_TS + 50, text="compressed post", ctype=2).encode())),
        (P_BAD, BOB, "<SnsDataItem><TimelineObject><id>1"),
    ]
    conn.executemany("INSERT INTO SnsTimeLine(tid, user_name, content, pack_info_buf) "
                     "VALUES (?,?,?,x'0a00')", [(signed(p), u, c) for p, u, c in rows])
    conn.commit()
    conn.close()


def build_sns_cache(base):
    """Fake <wechat_base_dir>/cache: full+thumb for m1, square only for m2,
    nothing for m3, live clip for m1, video for v1 (+ a partial .tmp)."""
    def put(kind, key, data, ext="", month="2026-07"):
        d = os.path.join(base, "cache", month, "Sns", kind, key[:2])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, key[2:] + ext), "wb") as f:
            f.write(data)
    k = MO.cache_key
    put("Img", k(P_ALICE_IMG, "m1", 2), v2_dat(JPEG))
    put("Img", k(P_ALICE_IMG, "m1", 1), v2_dat(JPEG[:200] + b"\xff\xd9"), month="2026-08")
    put("Video", k(P_ALICE_IMG, "m1", 3), MP4, ".mp4")
    put("Img", k(P_ALICE_IMG, "m2", 6), v2_dat(JPEG))
    put("Video", k(P_SELF_VIDEO, "v1", 3), MP4, ".mp4")
    put("Img", k(P_SELF_VIDEO, "v1", 1), v2_dat(JPEG))
    put("Video", k(P_DAVE, "d1", 3), MP4[:10], ".tmp")


# ---------------------------------------------------------------- favorites

def fav_xml(ftype, body):
    return f'<favitem type="{ftype}">{body}<taglist></taglist><tagidlist></tagidlist></favitem>'


def src(fromusr, tousr=SELF, realchat=None, ct=None, link=None, stype=1):
    x = f'<source sourcetype="{stype}" sourceid="123"><fromusr>{fromusr}</fromusr><tousr>{tousr}</tousr>'
    if realchat:
        x += f"<realchatname>{realchat}</realchatname>"
    if ct:
        x += f"<createtime>{ct}</createtime>"
    if link:
        x += f"<link>{esc(link)}</link>"
    return x + "<msgid>9</msgid></source>"


FAV_IMG_MD5 = hashlib.md5(JPEG).hexdigest()
FAV_THUMB = PNG + b"thumb"
FAV_THUMB_MD5 = hashlib.md5(FAV_THUMB).hexdigest()
FAV_FILE = b"%PDF-1.4 fake pdf"
FAV_FILE_MD5 = hashlib.md5(FAV_FILE).hexdigest()

FAVS = [
    # local_id, type, update_time, xml
    (1, 1, POST_TS, fav_xml(1, "<desc>remember the milk &lt;b&gt;</desc>"
                            + src(ROOM, realchat=BOB, ct=POST_TS - 5)
                            + '<datalist count="1"><dataitem datatype="1" dataid="a">'
                            "<datadesc>remember the milk &lt;b&gt;</datadesc></dataitem></datalist>")),
    (2, 5, POST_TS + 10, fav_xml(5, "<title>Great article</title>"
                                 + src(ALICE, link="https://example.com/a?x=1")
                                 + '<datalist count="1"><dataitem datatype="5" dataid="b">'
                                 "<datatitle>Great article</datatitle><datadesc>summary</datadesc>"
                                 "<stream_weburl>https://example.com/a?x=1</stream_weburl></dataitem>"
                                 "</datalist><weburlitem><pagedesc>summary</pagedesc>"
                                 "<pagetitle>Great article</pagetitle></weburlitem>")),
    (3, 2, POST_TS + 20, fav_xml(2, src(SELF, tousr=ALICE)
                                 + '<datalist count="1"><dataitem datatype="2" dataid="c">'
                                 f"<fullmd5>{FAV_IMG_MD5.upper()}</fullmd5><thumbfullmd5>{FAV_THUMB_MD5}"
                                 "</thumbfullmd5><fullsize>999</fullsize></dataitem></datalist>")),
    (4, 14, POST_TS + 30, fav_xml(14, "<title>Chat with Alice</title>" + src(ALICE)
                                  + '<datalist count="3">'
                                  '<dataitem datatype="1" dataid="d1"><datadesc>hello there</datadesc>'
                                  "<datasrcname>Alice</datasrcname><datasrctime>2023-11-14 10:00"
                                  "</datasrctime><dataitemsource><fromusr>" + ALICE
                                  + "</fromusr></dataitemsource></dataitem>"
                                  '<dataitem datatype="2" dataid="d2"><datasrcname>Me</datasrcname>'
                                  "<datasrctime>2023-11-14 10:01</datasrctime></dataitem>"
                                  '<dataitem datatype="1" dataid="d3"><datadesc>see you &lt;script&gt;'
                                  "</datadesc><datasrcname>Dave</datasrcname><dataitemsource><fromusr>"
                                  + DAVE + "</fromusr></dataitemsource></dataitem></datalist>")),
    (5, 6, POST_TS + 40, fav_xml(6, src(BOB) + "<locitem><lat>31.2</lat><lng>121.5</lng>"
                                 "<label>Somewhere road</label><poiname>Cafe</poiname></locitem>")),
    (6, 8, POST_TS + 50, fav_xml(8, src(SELF) + '<datalist count="1"><dataitem datatype="8" '
                                 'dataid="e"><datatitle>report.pdf</datatitle><datafmt>pdf</datafmt>'
                                 f"<fullmd5>{FAV_FILE_MD5}</fullmd5></dataitem></datalist>")),
    (7, 18, POST_TS + 60, fav_xml(18, src(SELF) + '<datalist count="2">'
                                  '<dataitem datatype="1" htmlid="WeNoteHtmlFile"><datadesc>'
                                  "note body text</datadesc></dataitem>"
                                  '<dataitem datatype="2"><fullmd5>ffff</fullmd5></dataitem>'
                                  "</datalist>")),
    (8, 99, POST_TS + 70, fav_xml(99, "<title>mystery</title>" + src(SELF))),
    (9, 1, POST_TS + 80, "<favitem type='1'><desc>broken"),
]


def build_favorites(root):
    os.makedirs(os.path.join(root, "favorite"), exist_ok=True)
    conn = sqlite3.connect(os.path.join(root, "favorite", "favorite.db"))
    conn.execute("CREATE TABLE fav_db_item(local_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "server_id INTEGER, type INTEGER, update_seq INTEGER, flag INTEGER, "
                 "update_time INTEGER, version INTEGER, content TEXT, source_id TEXT, "
                 "fromusr TEXT, realchatname TEXT, ext_buf TEXT)")
    conn.execute("CREATE TABLE fav_tag_db_item(local_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "server_id INTEGER, name TEXT, seq INTEGER)")
    conn.execute("CREATE TABLE fav_bind_tag_db_item(tag_local_id INTEGER, tag_server_id INTEGER, "
                 "fav_local_id INTEGER, fav_server_id INTEGER, op_code INTEGER)")
    conn.executemany("INSERT INTO fav_db_item(local_id, server_id, type, update_time, content) "
                     "VALUES (?,?,?,?,?)", [(i, 1000 + i, t, ts, c) for i, t, ts, c in FAVS])
    conn.execute("INSERT INTO fav_tag_db_item(local_id, name) VALUES (1, 'work')")
    conn.execute("INSERT INTO fav_bind_tag_db_item(tag_local_id, fav_local_id) VALUES (1, 2)")
    conn.commit()
    conn.close()


def build_fav_files(base):
    def put(sub, name, data):
        d = os.path.join(base, "business", "favorite", sub, name[:2])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
    put("data", "a" * 32, v2_dat(JPEG))
    put("thumb", "b" * 32, v2_dat(FAV_THUMB))
    put("data", "c" * 32, FAV_FILE)            # plain (not .dat encrypted)
    put("mid", "d" * 32, v2_dat(PNG + b"unrelated"))


class HTMLCheck(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.hrefs, self.srcs, self.scripts = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if tag == "script":
            self.scripts += 1
        if a.get("href"):
            self.hrefs.append(a["href"])
        if a.get("src"):
            self.srcs.append(a["src"])


def check_html(testcase, text, base_dir):
    p = HTMLCheck()
    p.feed(text)
    testcase.assertEqual(p.scripts, 0)
    for h in p.hrefs:
        testcase.assertFalse(h.lower().startswith(("javascript:", "data:")), h)
    for s in p.srcs:
        testcase.assertFalse(os.path.isabs(s), s)
        from urllib.parse import unquote
        testcase.assertTrue(os.path.exists(os.path.join(base_dir, unquote(s))), s)
    return p


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="social_test_")
        self.dd = os.path.join(self.tmp, "decrypted")
        Fixture(self.dd)
        build_sns(self.dd)
        build_favorites(self.dd)
        self.base = os.path.join(self.tmp, "wechat")
        build_sns_cache(self.base)
        build_fav_files(self.base)
        self.cfg = {"decrypted_dir": self.dd, "self_wxid": SELF, "wechat_base_dir": self.base,
                    "image_aes_key": AES_KEY, "image_xor_key": XOR_KEY}
        import chats
        self.contacts = chats.load_contacts(self.dd)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def posts(self):
        return MO.resolve_names(MO.load_posts(self.dd), self.contacts, SELF)

    def favs(self):
        return FV.resolve_names(FV.load_favorites(self.dd), self.contacts, SELF)


# ---------------------------------------------------------------- moments

class MomentsParseTest(Env):
    def test_load_counts_order_and_failures(self):
        st = {}
        posts = MO.load_posts(self.dd, stats=st)
        self.assertEqual(st, {"rows": 7, "parsed": 6, "failed": 1})
        self.assertEqual([p["id"] for p in posts],
                         [str(P_SELF_VIDEO), str(P_BOB_TEXT), str(P_ALICE_LINK), str(P_ZSTD),
                          str(P_ALICE_IMG), str(P_DAVE)])

    def test_image_post_fields(self):
        p = next(x for x in self.posts() if x["id"] == str(P_ALICE_IMG))
        self.assertEqual((p["username"], p["ts"], p["kind"], p["type"]), (ALICE, POST_TS, "image", 54))
        self.assertEqual(p["author"], "Alice R")           # contact remark wins
        self.assertEqual(p["nickname"], "alice at the time")
        self.assertIn("<script>", p["text"])
        self.assertEqual([m["id"] for m in p["media"]], ["m1", "m2", "m3"])
        self.assertTrue(p["media"][0]["live_photo"])
        self.assertEqual((p["media"][0]["width"], p["media"][0]["height"]), (1440, 1080))
        self.assertTrue(p["media"][0]["url"].startswith("http://"))
        self.assertEqual(p["location"]["name"], "West Lake")
        self.assertEqual(p["location"]["latitude"], 31.2)
        self.assertEqual([x["name"] for x in p["likes"]], ["Bobby", "Me Myself"])
        # deleted comment dropped, sorted by time, reply + emoji handled
        self.assertEqual([(c["name"], c["text"]) for c in p["comments"]],
                         [("Bobby", "nice!"), ("Alice R", "thanks"), ("Me Myself", "[表情]")])
        self.assertEqual(p["comments"][1]["reply_to_name"], "Bobby")
        self.assertEqual(p["with_names"], ["Bobby"])
        self.assertFalse(p["private"])

    def test_other_kinds(self):
        by = {p["id"]: p for p in self.posts()}
        link = by[str(P_ALICE_LINK)]
        self.assertEqual((link["kind"], link["title"], link["description"], link["source"]),
                         ("link", "An article", "about things", "Some Account"))
        self.assertEqual(link["media"][0]["type"], "cover")
        self.assertIsNone(link["location"])               # 0,0 and no names
        text = by[str(P_BOB_TEXT)]
        self.assertEqual((text["kind"], text["private"], text["media"]), ("text", True, []))
        self.assertEqual(by[str(P_SELF_VIDEO)]["media"][0]["type"], "video")
        self.assertEqual(by[str(P_SELF_VIDEO)]["media"][0]["duration"], 12.5)
        self.assertTrue(by[str(P_SELF_VIDEO)]["is_self"])
        self.assertEqual(by[str(P_ZSTD)]["text"], "compressed post")
        self.assertEqual(by[str(P_DAVE)]["author"], "Dave Stranger")

    def test_parse_post_edge_cases(self):
        self.assertIsNone(MO.parse_post(None))
        self.assertIsNone(MO.parse_post("not xml"))
        self.assertIsNone(MO.parse_post("<SnsDataItem/>"))
        p = MO.parse_post("<TimelineObject><username>u</username><createTime>5</createTime>"
                          "<contentDesc>a\x01b</contentDesc></TimelineObject>", tid=-1)
        self.assertEqual((p["id"], p["text"], p["kind"]), (str((1 << 64) - 1), "ab", "other"))
        self.assertEqual(MO.unsigned_id(-2), str((1 << 64) - 2))

    def test_display_name_fallbacks(self):
        self.assertEqual(MO.display_name("x", "nick", {}), "nick")
        self.assertEqual(MO.display_name("x", "", {}), "x")
        self.assertEqual(MO.display_name("x", "nick", {"x": "x"}), "nick")

    def test_match_and_filter(self):
        import chats
        posts = self.posts()
        rows = chats.load_contact_rows(self.dd)
        self.assertEqual(MO.match_authors("Alice R", posts, rows), [ALICE])
        self.assertEqual(MO.match_authors("alice at the", posts, rows), [ALICE])  # old nickname
        self.assertEqual(MO.match_authors("我", posts, rows, SELF), [SELF])
        self.assertEqual(MO.match_authors("b", posts, rows), [BOB])               # "Bobby"
        self.assertEqual(set(MO.match_authors("e", posts, rows)), {ALICE, DAVE, SELF})
        self.assertEqual(MO.match_authors("zzz", posts, rows), [])
        self.assertEqual([p["id"] for p in MO.filter_posts(posts, [ALICE])],
                         [str(P_ALICE_LINK), str(P_ALICE_IMG)])
        self.assertEqual([p["id"] for p in MO.filter_posts(posts, query="NICE west")],
                         [str(P_ALICE_IMG)])                                    # comment + location
        got = MO.filter_posts(posts, since=POST_TS + 50, until=POST_TS + 200)
        self.assertEqual([p["id"] for p in got], [str(P_ALICE_LINK), str(P_ZSTD)])

    def test_missing_db(self):
        os.remove(MO.sns_db_path(self.dd))
        st = {}
        self.assertEqual(MO.load_posts(self.dd, stats=st), [])
        self.assertEqual(st["rows"], 0)


class MomentsMediaTest(Env):
    def test_cache_lookup(self):
        c = MO.MediaCache(self.base)
        m1 = c.lookup(str(P_ALICE_IMG), "m1")
        self.assertTrue(m1["image"] and m1["thumb"] and m1["video"])
        m2 = c.lookup(str(P_ALICE_IMG), "m2")
        self.assertEqual((m2["image"], m2["video"]), (None, None))
        self.assertTrue(m2["thumb"])                     # square variant
        self.assertEqual(c.lookup(str(P_ALICE_IMG), "m3"), {"image": None, "thumb": None, "video": None})
        self.assertIsNone(c.lookup(str(P_DAVE), "d1")["video"])   # .tmp ignored
        posts = c.annotate(self.posts())
        p = next(x for x in posts if x["id"] == str(P_ALICE_IMG))
        self.assertEqual([m["cached"] for m in p["media"]], [["image", "thumb", "video"], ["thumb"], []])
        self.assertEqual(MO.cache_key(1, "a", 2), hashlib.md5(b"1_a_2").hexdigest())
        self.assertEqual(MO.MediaCache(os.path.join(self.tmp, "nope")).lookup("1", "a")["image"], None)

    def test_exporter(self):
        files = os.path.join(self.tmp, "out", "moments_files")
        ex = MO.MediaExporter(MO.MediaCache(self.base), (AES_KEY, XOR_KEY), files)
        posts = ex.attach(self.posts())
        p = next(x for x in posts if x["id"] == str(P_ALICE_IMG))
        with open(p["media"][0]["image_path"], "rb") as f:
            self.assertEqual(f.read(), JPEG)
        self.assertTrue(p["media"][0]["live_path"].endswith(".mp4"))
        self.assertNotIn("video_path", p["media"][0])
        self.assertTrue(p["media"][1]["image_path"].endswith(".jpg"))
        self.assertNotIn("image_path", p["media"][2])
        v = next(x for x in posts if x["id"] == str(P_SELF_VIDEO))["media"][0]
        self.assertTrue(v["video_path"].endswith(".mp4") and v["image_path"].endswith(".jpg"))
        self.assertEqual((ex.images, ex.thumbs, ex.videos, ex.live, ex.missing, ex.failed),
                         (1, 2, 1, 1, 2, 0))
        ex2 = MO.MediaExporter(MO.MediaCache(self.base), (AES_KEY, XOR_KEY), files)
        ex2.attach(self.posts())
        self.assertEqual(ex2.reused, 3)
        bad = MO.MediaExporter(MO.MediaCache(self.base), ("f" * 16, XOR_KEY),
                               os.path.join(self.tmp, "bad"))
        bad.attach(self.posts())
        self.assertEqual(bad.failed, 3)


class MomentsWriteTest(Env):
    def export(self, fmt, *extra):
        out = os.path.join(self.tmp, "export")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = MO.main(["--no-decrypt", "-f", fmt, "--export-dir", out, *extra], cfg=self.cfg)
        self.assertEqual(rc, 0)
        return out, buf.getvalue()

    def test_html(self):
        out, log = self.export("html", "--images")
        path = os.path.join(out, "moments.html")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        p = check_html(self, text, out)
        self.assertEqual(p.tags.count("article"), 6)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertNotIn("javascript:alert", text)
        self.assertIn("moments_files/", text)
        self.assertIn("prefers-color-scheme:dark", text)
        self.assertIn('name="viewport"', text)
        self.assertIn("<video", text)
        self.assertIn(">LIVE<", text)
        self.assertIn("未缓存", text)
        self.assertIn("Alice R", text)
        self.assertIn("回复", text)
        self.assertNotIn("http://szmmsns", text)            # never hotlink CDN media
        self.assertIn("导出 6 条", log)

    def test_name_filter_and_formats(self):
        out, log = self.export("json", "Alice", "--since", "2023-11-14", "--until", "2023-11-14")
        with open(os.path.join(out, "moments_Alice.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual([p["id"] for p in d["posts"]], [str(P_ALICE_LINK), str(P_ALICE_IMG)])
        self.assertEqual(d["meta"]["count"], 2)
        self.assertIn("time", d["posts"][0])
        out, _ = self.export("json", "--images", "-o", os.path.join(self.tmp, "j", "m.json"))
        with open(os.path.join(self.tmp, "j", "m.json"), encoding="utf-8") as f:
            d = json.load(f)
        paths = [m["image_path"] for p in d["posts"] for m in p["media"] if m.get("image_path")]
        self.assertTrue(paths and all(x.startswith("m_files/") for x in paths))
        out, _ = self.export("md", "--images")
        with open(os.path.join(out, "moments.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("&lt;script>", md)
        self.assertIn("](moments_files/", md)
        self.assertNotIn("javascript:", md)
        out, _ = self.export("txt", "我")
        with open(os.path.join(out, "moments_我.txt"), encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("my video", txt)
        self.assertNotIn("Sunny", txt)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(MO.main(["--no-decrypt", "nobody-here", "--export-dir", out],
                                     cfg=self.cfg), 1)

    def test_writers_empty(self):
        for fmt in MO.WRITERS:
            path = os.path.join(self.tmp, f"e.{fmt}")
            MO.write([], {"title": "t"}, path, fmt)
            self.assertEqual(os.path.getsize(path) > 0, fmt != "txt")
        with self.assertRaises(ValueError):
            MO.write([], {}, os.path.join(self.tmp, "x.csv"), "csv")


# ---------------------------------------------------------------- favorites

class FavoritesTest(Env):
    def test_load_and_parse(self):
        st = {}
        favs = FV.load_favorites(self.dd, stats=st)
        self.assertEqual(st, {"rows": 9, "parsed": 8, "failed": 1})
        self.assertEqual([f["id"] for f in favs], [8, 7, 6, 5, 4, 3, 2, 1])
        by = {f["id"]: f for f in FV.resolve_names(favs, self.contacts, SELF)}
        self.assertEqual({i: f["kind"] for i, f in by.items()},
                         {1: "text", 2: "link", 3: "image", 4: "chat_record", 5: "location",
                          6: "file", 7: "note", 8: "other"})
        t = by[1]
        self.assertEqual(t["desc"], "remember the milk <b>")
        self.assertEqual((t["source"]["chat_name"], t["source"]["sender_name"]), ("Test Group", "Bobby"))
        self.assertEqual(t["ts"], POST_TS)
        link = by[2]
        self.assertEqual((link["title"], link["desc"], link["url"]),
                         ("Great article", "summary", "https://example.com/a?x=1"))
        self.assertEqual(link["tags"], ["work"])              # from the tag tables
        self.assertEqual(link["source"]["sender_name"], "Alice R")
        img = by[3]
        self.assertEqual(img["items"][0]["fullmd5"], FAV_IMG_MD5)   # lower-cased
        self.assertEqual(img["source"]["sender_name"], "我")
        self.assertTrue(img["is_self"])
        rec = by[4]
        self.assertEqual(rec["title"], "Chat with Alice")
        self.assertEqual([i.get("sender_name") for i in rec["items"]], ["Alice", "Me", "Dave"])
        self.assertEqual(rec["items"][1]["kind"], "image")
        self.assertEqual(by[5]["location"]["name"], "Cafe")
        self.assertEqual(by[6]["items"][0]["fmt"], "pdf")
        self.assertEqual(by[7]["desc"], "")
        self.assertEqual(FV.summary_text(by[7]), "note body text")
        self.assertEqual(FV.related_usernames(rec), {ALICE, DAVE})
        self.assertEqual(FV.related_usernames(t), {ROOM, BOB})

    def test_search(self):
        favs = self.favs()
        ids = lambda fs: [f["id"] for f in fs]  # noqa: E731
        self.assertEqual(ids(FV.search(favs, "milk")), [1])
        self.assertEqual(ids(FV.search(favs, "GREAT summary")), [2])
        self.assertEqual(ids(FV.search(favs, "see you")), [4])      # inside a chat record
        self.assertEqual(ids(FV.search(favs, "bobby")), [5, 1])     # source names
        self.assertEqual(ids(FV.search(favs, kind="link")), [2])
        self.assertEqual(ids(FV.search(favs, kind="6")), [5])
        self.assertEqual(ids(FV.search(favs, tag="work")), [2])
        self.assertEqual(ids(FV.search(favs, since=POST_TS + 20, until=POST_TS + 40)), [4, 3])
        self.assertEqual(FV.search(favs, "nothing-matches"), [])

    def test_file_index_and_export(self):
        idx = FV.FileIndex(self.base, (AES_KEY, XOR_KEY))
        self.assertEqual(idx.get(FAV_IMG_MD5)[1], "data")
        self.assertEqual(idx.get(FAV_THUMB_MD5)[1], "thumb")
        self.assertEqual(idx.get(FAV_FILE_MD5.upper())[1], "data")
        self.assertIsNone(idx.get("ffff"))
        favs = idx.annotate(self.favs())
        by = {f["id"]: f for f in favs}
        self.assertEqual(by[3]["items"][0]["local"], ["data", "thumb"])
        ex = FV.FileExporter(idx, os.path.join(self.tmp, "ff"))
        ex.attach(favs)
        with open(by[3]["items"][0]["image_path"], "rb") as f:
            self.assertEqual(f.read(), JPEG)
        self.assertTrue(by[6]["items"][0]["file_path"].endswith(".pdf"))
        self.assertEqual((ex.written, ex.missing), (2, 2))   # note image + record image missing
        # wrong key: encrypted files are skipped, plain file still indexed
        bad = FV.FileIndex(self.base, ("f" * 16, XOR_KEY))
        self.assertIsNone(bad.get(FAV_IMG_MD5))
        self.assertIsNotNone(bad.get(FAV_FILE_MD5))

    def test_cli_all_formats(self):
        out = os.path.join(self.tmp, "export")
        for fmt in ("html", "md", "json", "txt"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = FV.main(["--no-decrypt", "-f", fmt, "--images", "--export-dir", out],
                             cfg=self.cfg)
            self.assertEqual(rc, 0)
            self.assertIn("导出 8 条", buf.getvalue())
        with open(os.path.join(out, "favorites.html"), encoding="utf-8") as f:
            text = f.read()
        p = check_html(self, text, out)
        self.assertEqual(p.tags.count("article"), 8)
        self.assertIn("remember the milk &lt;b&gt;", text)
        self.assertIn("see you &lt;script&gt;", text)
        self.assertIn('href="https://example.com/a?x=1"', text)
        self.assertIn("favorites_files/", text)
        with open(os.path.join(out, "favorites.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(len(d["favorites"]), 8)
        self.assertTrue(any(i.get("image_path", "").startswith("favorites_files/")
                            for x in d["favorites"] for i in x["items"]))
        with open(os.path.join(out, "favorites.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("&lt;script>", md)
        with open(os.path.join(out, "favorites.txt"), encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("hello there", txt)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            FV.main(["--no-decrypt", "-f", "json", "milk", "--export-dir", out], cfg=self.cfg)
        with open(os.path.join(out, "favorites.json"), encoding="utf-8") as f:
            self.assertEqual([x["id"] for x in json.load(f)["favorites"]], [1])


class CommonTest(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(S.safe_url(" https://a.b/c "), "https://a.b/c")
        for bad in ("javascript:alert(1)", "data:text/html,x", "JaVaScRiPt:x", "", None, "//x"):
            self.assertEqual(S.safe_url(bad), "")
        self.assertEqual(S.blob_text(zstandard.ZstdCompressor().compress("中".encode())), "中")
        self.assertEqual(S.blob_text(b"\xff"), "�")
        self.assertEqual(S.rel_path("/a/b/c d.jpg", "/a"), "b/c%20d.jpg")
        self.assertEqual(S.safe_filename('a/b:c'), "a_b_c")
        self.assertTrue(S.in_range(5, 5, 6) and not S.in_range(6, 5, 6))


# ---------------------------------------------------------------- MCP

class MCPSocialTest(Env):
    def setUp(self):
        super().setUp()
        self.data = self.make()

    def make(self, **extra):
        cfg = dict(self.cfg, mcp_index_path=os.path.join(self.tmp, "index", "idx.db"),
                   mcp_auto_refresh_minutes=0, **extra)
        return M.WeChatData(cfg)

    def err(self, fn, *a, **kw):
        with self.assertRaises(M.ToolError) as cm:
            fn(*a, **kw)
        return cm.exception.payload

    def test_get_moments(self):
        r = M.moments_query(self.data)
        self.assertEqual((r["total"], r["count"], r["has_more"]), (6, 6, False))
        first = r["posts"][0]
        self.assertEqual((first["id"], first["author"], first["kind"]),
                         (str(P_SELF_VIDEO), "Me Myself", "video"))
        self.assertEqual(first["media"], {"images": 0, "videos": 1, "cached_locally": 1})
        img = next(p for p in r["posts"] if p["id"] == str(P_ALICE_IMG))
        self.assertNotIn("kind", img)
        self.assertEqual(img["likes"], ["Bobby", "Me Myself"])
        self.assertEqual(img["comments"][1], {"name": "Alice R", "time": img["comments"][1]["time"],
                                              "text": "thanks", "reply_to": "Bobby"})
        self.assertEqual(img["location"], "West Lake")
        self.assertEqual(img["media"]["cached_locally"], 2)
        r = M.moments_query(self.data, author="Alice", limit=1)
        self.assertEqual((r["total"], r["count"], r["has_more"]), (2, 1, True))
        self.assertEqual(M.moments_query(self.data, author="我")["total"], 1)
        self.assertEqual(M.moments_query(self.data, author=BOB, query="compressed")["total"], 1)
        self.assertEqual(M.moments_query(self.data, since="2023-11-14", until="2023-11-14")["total"], 5)
        self.assertEqual(M.moments_query(self.data, until="2023-01-01")["total"], 1)
        e = self.err(M.moments_query, self.data, author="e")
        self.assertEqual(e["error"], "ambiguous")
        self.assertEqual({c["username"] for c in e["candidates"]}, {ALICE, DAVE, SELF})
        self.assertIn("no Moments author", self.err(M.moments_query, self.data, author="zz")["error"])
        self.assertIn("cannot parse", self.err(M.moments_query, self.data, since="junk")["error"])

    def test_moments_cache_refreshes(self):
        self.assertEqual(M.moments_query(self.data)["total"], 6)
        conn = sqlite3.connect(MO.sns_db_path(self.dd))
        conn.execute("DELETE FROM SnsTimeLine WHERE user_name=?", (DAVE,))
        conn.commit()
        conn.close()
        st = os.stat(MO.sns_db_path(self.dd))
        os.utime(MO.sns_db_path(self.dd), (st.st_atime, st.st_mtime + 5))
        self.assertEqual(M.moments_query(self.data)["total"], 5)

    def test_moments_policy(self):
        d = self.make(mcp_blocklist=["Alice R"])
        r = M.moments_query(d)
        self.assertNotIn(ALICE, {p["author_username"] for p in r["posts"]})
        self.assertEqual(r["total"], 4)
        self.assertIn("no Moments author", self.err(M.moments_query, d, author=ALICE)["error"])
        d = self.make(mcp_allowlist=["Bobby"])
        self.assertEqual({p["author_username"] for p in M.moments_query(d)["posts"]}, {BOB, SELF})
        d = self.make(mcp_blocklist=["Me Myself"])
        self.assertNotIn(SELF, {p["author_username"] for p in M.moments_query(d)["posts"]})
        self.assertIn("no Moments author", self.err(M.moments_query, d, author="我")["error"])

    def test_favorites_tools(self):
        r = M.favorites_search(self.data)
        self.assertEqual((r["total"], r["count"]), (8, 8))
        link = next(f for f in r["favorites"] if f["id"] == 2)
        self.assertEqual((link["type"], link["title"], link["url"], link["tags"]),
                         ("link", "Great article", "https://example.com/a?x=1", ["work"]))
        self.assertEqual(link["source"]["sender_name"], "Alice R")
        r = M.favorites_search(self.data, query="milk")
        self.assertEqual(r["favorites"][0]["snippet"], "remember the milk <b>")
        self.assertEqual(r["favorites"][0]["source"]["chat_name"], "Test Group")
        self.assertEqual(M.favorites_search(self.data, type="chat_record")["total"], 1)
        self.assertEqual(M.favorites_search(self.data, limit=2)["has_more"], True)
        self.assertEqual(M.favorites_search(self.data, since=str(POST_TS + 60))["total"], 2)
        self.assertIn("unknown type", self.err(M.favorites_search, self.data, type="nope")["error"])
        f = M.favorite_get(self.data, 4)
        self.assertEqual([i.get("sender_name") for i in f["items"]], ["Alice", "Me", "Dave"])
        self.assertEqual(f["items"][0]["desc"], "hello there")
        self.assertIn("not found", self.err(M.favorite_get, self.data, 999)["error"])
        self.assertIn("bad favorite id", self.err(M.favorite_get, self.data, "x")["error"])

    def test_favorites_policy(self):
        d = self.make(mcp_blocklist=["Dave Stranger"])
        ids = {f["id"] for f in M.favorites_search(d)["favorites"]}
        self.assertNotIn(4, ids)                  # chat record contains Dave's message
        self.assertIn("not found", self.err(M.favorite_get, d, 4)["error"])
        d = self.make(mcp_blocklist=["Test Group"])
        self.assertNotIn(1, {f["id"] for f in M.favorites_search(d)["favorites"]})
        d = self.make(mcp_allowlist=["Alice R"])
        # own favorites + those only involving Alice
        self.assertEqual({f["id"] for f in M.favorites_search(d)["favorites"]}, {2, 3, 6, 7, 8})

    def test_wanted_db_regex(self):
        for rel in ("sns/sns.db", "favorite/favorite.db", "message/message_0.db",
                    "contact/contact.db"):
            self.assertTrue(M._WANTED_DB_RE.match(rel), rel)
        for rel in ("favorite/favorite_fts.db", "sns/sns.db.bak", "xsns/sns.db"):
            self.assertFalse(M._WANTED_DB_RE.match(rel), rel)

    def test_mcp_in_process_and_log(self):
        try:
            from mcp import Client
        except ImportError:
            self.skipTest("mcp 2.x client not available")
        import anyio
        log_path = os.path.join(self.tmp, "logs", "a.jsonl")
        srv = M.build_server(self.data, M.AccessLog(log_path))

        async def go():
            async with Client(srv) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                self.assertLessEqual({"get_moments", "search_favorites", "get_favorite"}, tools)
                r = await client.call_tool("get_moments", {"author": "Alice", "limit": 1})
                self.assertFalse(r.is_error)
                self.assertTrue(r.content[0].text.startswith("1 of "))
                r = await client.call_tool("search_favorites", {"query": "milk"})
                self.assertTrue(r.content[0].text.split("\n")[2].startswith("#1 | "))
                r = await client.call_tool("get_favorite", {"id": 4})
                self.assertEqual(r.content[0].text.count("\n- ["), 3)
                r = await client.call_tool("get_moments", {"author": "e"})
                self.assertEqual(json.loads(r.content[0].text)["error"], "ambiguous")
        anyio.run(go)
        with open(log_path, encoding="utf-8") as f:
            raw = f.read()
        lines = [json.loads(x) for x in raw.splitlines()]
        self.assertEqual([x["tool"] for x in lines],
                         ["get_moments", "search_favorites", "get_favorite", "get_moments"])
        self.assertEqual(lines[0]["count"], 1)
        self.assertEqual(lines[3]["error"], "ambiguous")
        for secret in ("Sunny", "remember the milk", "hello there"):
            self.assertNotIn(secret, raw)


if __name__ == "__main__":
    unittest.main()
