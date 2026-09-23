"""Tests for mcp_server.py using the synthetic fixture from test_chats.py.

Run: python -m unittest discover -s tests   (or: pytest tests)
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import mcp_server as M  # noqa: E402
from test_chats import (ALICE, BOB, DAVE, IMAGE_MD5, ROOM, SELF, Fixture,  # noqa: E402
                        tbl)
from test_media import HAVE_SILK, make_media_db, silk_sample  # noqa: E402


def make_data(root, **cfg_extra):
    cfg = {"decrypted_dir": os.path.join(root, "decrypted"), "self_wxid": SELF,
           "wechat_base_dir": os.path.join(root, "wechat"),
           "mcp_index_path": os.path.join(root, "index", "idx.db"),
           "mcp_auto_refresh_minutes": 0}
    cfg.update(cfg_extra)
    return M.WeChatData(cfg)


def add_msg(db_path, username, local_type, sender, ts, content):
    conn = sqlite3.connect(db_path)
    sid = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (sender,)).fetchone()[0]
    cur = conn.execute(
        f"INSERT INTO {tbl(username)}(server_id, local_type, sort_seq, real_sender_id, "
        f"create_time, message_content) VALUES (?,?,?,?,?,?)",
        (ts, local_type, ts * 1000, sid, ts, content))
    conn.commit()
    conn.close()
    bump(db_path)
    return cur.lastrowid


def bump(path):
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 5))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcp_test_")
        self.fx = Fixture(os.path.join(self.tmp, "decrypted"))
        self.data = make_data(self.tmp)

    def tearDown(self):
        if self.data._index:
            self.data._index.close()
        shutil.rmtree(self.tmp)

    def err(self, fn, *a, **kw):
        with self.assertRaises(M.ToolError) as cm:
            fn(*a, **kw)
        return cm.exception.payload


class HelpersTest(unittest.TestCase):
    def test_parse_time(self):
        p = M.parse_time
        self.assertIsNone(p(None))
        self.assertIsNone(p(""))
        self.assertEqual(p(1700000000), 1700000000)
        self.assertEqual(p("1700000000"), 1700000000)
        import datetime as dt
        day = int(dt.datetime(2024, 3, 5).timestamp())
        self.assertEqual(p("2024-03-05"), day)
        self.assertEqual(p("2024/03/05"), day)
        self.assertEqual(p("2024-03-05", end=True), day + 86399)
        self.assertEqual(p("2024-03-05 10:30"), day + 10 * 3600 + 1800)
        self.assertEqual(p("2024-03-05T10:30:00", end=True), day + 10 * 3600 + 1800)
        self.assertEqual(p("7d", now=1_000_000), 1_000_000 - 7 * 86400)
        self.assertEqual(p("12h", now=1_000_000), 1_000_000 - 12 * 3600)
        with self.assertRaises(M.ToolError):
            p("last tuesday")

    def test_clamp_and_snippet(self):
        self.assertEqual(M.clamp(10_000, 1, M.MAX_LIMIT), M.MAX_LIMIT)
        self.assertEqual(M.clamp(-5, 1, 10), 1)
        self.assertEqual(M.clamp("x", 1, 10), 10)
        long = "a" * 200 + "NEEDLE" + "b" * 200
        s = M._snippet(long, ["needle"])
        self.assertIn("NEEDLE", s)
        self.assertTrue(s.startswith("…") and s.endswith("…"))
        self.assertLess(len(s), 140)


class ToolsTest(Base):
    # -- list_chats / resolution --------------------------------------------
    def test_list_chats(self):
        r = self.data.list_chats()
        self.assertEqual([c["username"] for c in r["chats"]], [ROOM, ALICE])
        self.assertEqual(r["chats"][1], {"name": "Alice R", "username": ALICE, "is_group": False,
                                         "msg_count": 8,
                                         "last_time": M.iso(205)})
        self.assertEqual([c["username"] for c in self.data.list_chats(type="group")["chats"]], [ROOM])
        self.assertEqual([c["username"] for c in self.data.list_chats(type="single")["chats"]], [ALICE])
        # filter matches the nickname too, not only the display (remark) name
        self.assertEqual([c["username"] for c in self.data.list_chats(filter="ALICE NICK")["chats"]],
                         [ALICE])
        r = self.data.list_chats(limit=1)
        self.assertEqual((r["total"], r["count"]), (2, 1))
        self.assertEqual(self.data.list_chats(limit=10_000)["count"], 2)
        self.err(self.data.list_chats, type="bogus")

    def test_resolve_chat(self):
        r = self.data.resolve_chat
        self.assertEqual(r(ALICE)["username"], ALICE)
        self.assertEqual(r("Alice R")["username"], ALICE)
        self.assertEqual(r("alice r")["username"], ALICE)
        self.assertEqual(r("alice nick")["username"], ALICE)   # nickname
        self.assertEqual(r("grou")["username"], ROOM)          # unique fragment
        p = self.err(r, "e")                                   # in both names
        self.assertEqual(p["error"], "ambiguous")
        self.assertEqual({c["username"] for c in p["candidates"]}, {ALICE, ROOM})
        self.assertIn("no chat", self.err(r, "zzz")["error"])
        self.err(r, "")

    def test_unnamed_group_gets_member_name(self):
        conn = sqlite3.connect(os.path.join(self.fx.root, "contact", "contact.db"))
        conn.execute("UPDATE contact SET nick_name='' WHERE username=?", (ROOM,))
        conn.commit()
        conn.close()
        room = [c for c in self.data.list_chats()["chats"] if c["username"] == ROOM][0]
        self.assertEqual(room["name"], "Bobby、Dave Stranger、carol_custom")

    # -- get_messages -------------------------------------------------------
    def test_get_messages_default(self):
        r = self.data.get_messages("Alice R")
        self.assertEqual(r["count"], 7)
        self.assertEqual([m["text"] for m in r["messages"]][:3],
                         ["hi old", "hello old", "compressed hi"])
        self.assertEqual(r["messages"][0], {"id": "1:1", "time": M.iso(100),
                                            "sender": "Alice R", "text": "hi old"})
        self.assertEqual(r["messages"][3]["kind"], "image")
        self.assertEqual(r["messages"][2]["id"], "0:1")   # same local_id, other DB
        self.assertFalse(r["has_more_before"] or r["has_more_after"])
        self.assertEqual(r["chat"]["username"], ALICE)

    def test_pagination_backward_and_forward(self):
        full = [m["id"] for m in self.data.get_messages(ROOM)["messages"]]
        self.assertEqual(len(full), 8)
        got, cursor = [], None
        while True:
            r = self.data.get_messages(ROOM, limit=3, before=cursor)
            got = [m["id"] for m in r["messages"]] + got
            if not r["has_more_before"]:
                break
            cursor = r["before_cursor"]
        self.assertEqual(got, full)
        got, cursor = [], None
        r = self.data.get_messages(ROOM, limit=3, from_start=True)
        while True:
            got += [m["id"] for m in r["messages"]]
            if not r["has_more_after"]:
                break
            r = self.data.get_messages(ROOM, limit=3, after=r["after_cursor"])
        self.assertEqual(got, full)
        r = self.data.get_messages(ROOM, limit=3, newest_first=True)
        self.assertEqual([m["id"] for m in r["messages"]], list(reversed(full[-3:])))
        self.assertIn("bad message id", self.err(self.data.get_messages, ROOM, before="x")["error"])
        self.assertIn("not found", self.err(self.data.get_messages, ROOM, before="9:9")["error"])

    def test_since_until(self):
        r = self.data.get_messages(ALICE, since=200, until=202)
        self.assertEqual([m["text"] for m in r["messages"]],
                         ["compressed hi", "[图片]", "[系统消息] Alice recalled"])
        r = self.data.get_messages(ALICE, since=200, limit=2, from_start=True)
        self.assertEqual([m["text"] for m in r["messages"]], ["compressed hi", "[图片]"])
        self.assertTrue(r["has_more_after"])
        self.assertFalse(r["has_more_before"])  # nothing before within the range

    def test_limit_cap_and_text_cap(self):
        add_msg(self.fx.dbs[0], ALICE, 1, ALICE, 600, "x" * (M.MAX_TEXT + 50))
        r = self.data.get_messages(ALICE, limit=100_000)
        self.assertEqual(r["count"], 8)
        self.assertTrue(r["messages"][-1]["text"].endswith("[+50 chars]"))

    # -- search -------------------------------------------------------------
    def test_search(self):
        s = self.data.search_messages
        r = s("compressed")                  # trigram MATCH path
        self.assertEqual(r["count"], 1)
        hit = r["results"][0]
        self.assertEqual({k: hit[k] for k in ("chat", "chat_username", "id", "sender", "snippet")},
                         {"chat": "Alice R", "chat_username": ALICE, "id": "0:1",
                          "sender": "Alice R", "snippet": "compressed hi"})
        self.assertEqual(s("COMPRESSED")["count"], 1)            # case-insensitive
        self.assertEqual([h["id"] for h in s("hi")["results"]],  # short -> LIKE, newest first
                         ["0:1", "1:1"])
        self.assertEqual(s("from", chat=ROOM)["count"], 4)
        self.assertEqual(s("from", chat=ROOM, sender="Bob")["count"], 1)
        self.assertEqual(s("old", since=102)["count"], 1)       # "old group msg"
        self.assertEqual(s("old", until=150)["count"], 3)
        self.assertEqual(s("from me")["count"], 1)               # AND of terms
        self.assertEqual(s("图片")["count"], 0)                   # placeholders not searchable
        self.assertEqual(s("from", limit=2)["has_more"], True)
        self.err(s, "  ")

    def test_search_chinese_and_incremental(self):
        idx_stats = self.data.sync_index()
        self.assertEqual(idx_stats["messages_added"], 15)
        self.assertEqual(self.data.sync_index()["tables_checked"], 0)  # unchanged files skipped
        add_msg(self.fx.dbs[0], ALICE, 1, ALICE, 700, "我们明天去吃火锅吧")
        add_msg(self.fx.dbs[0], ROOM, 1, BOB, 701, f"{BOB}:\n火锅店在哪里")
        st = self.data.sync_index()
        self.assertEqual((st["tables_appended"], st["tables_reindexed"], st["messages_added"]),
                         (2, 0, 2))
        r = self.data.search_messages("吃火锅")               # 3 chars: FTS
        self.assertEqual([h["chat_username"] for h in r["results"]], [ALICE])
        r = self.data.search_messages("火锅")                 # 2 chars: LIKE
        self.assertEqual([h["sender"] for h in r["results"]], ["Bob in group", "Alice R"])
        # A deleted row forces a re-index of that table.
        conn = sqlite3.connect(self.fx.dbs[0])
        conn.execute(f"DELETE FROM {tbl(ALICE)} WHERE message_content LIKE '%火锅%'")
        conn.commit()
        conn.close()
        bump(self.fx.dbs[0])
        st = self.data.sync_index()
        self.assertEqual(st["tables_reindexed"], 1)
        self.assertEqual(self.data.search_messages("吃火锅")["count"], 0)
        self.assertEqual(self.data.get_messages(ALICE)["count"], 7)
        # A recalled message (type changes in place) is also detected.
        conn = sqlite3.connect(self.fx.dbs[0])
        conn.execute(f"UPDATE {tbl(ROOM)} SET local_type=10000, message_content='recalled' "
                     "WHERE message_content LIKE '%火锅%'")
        conn.commit()
        conn.close()
        bump(self.fx.dbs[0])
        self.assertEqual(self.data.sync_index()["tables_reindexed"], 1)
        self.assertEqual(self.data.search_messages("火锅")["count"], 0)

    def test_index_persists_and_version_reset(self):
        self.data.sync_index()
        self.data._index.close()
        d2 = make_data(self.tmp)
        self.assertEqual(d2.sync_index()["messages_added"], 0)
        d2._index.close()
        d3 = make_data(self.tmp, self_wxid="wxid_other")   # other account -> rebuilt
        self.assertEqual(d3.sync_index()["messages_added"], 15)
        d3._index.close()

    def test_get_messages_syncs_only_that_chat(self):
        self.data.get_messages(ALICE)
        n = self.data.index().conn.execute("SELECT count(DISTINCT chat) FROM msgs").fetchone()[0]
        self.assertEqual(n, 1)
        add_msg(self.fx.dbs[0], ALICE, 1, SELF, 800, "brand new")
        self.assertEqual(self.data.get_messages(ALICE)["messages"][-1]["text"], "brand new")
        self.assertEqual(self.data.search_messages("brand new")["count"], 1)

    # -- context ------------------------------------------------------------
    def test_context(self):
        r = self.data.get_message_context(ROOM, message_id="0:3", before=1, after=2)
        self.assertEqual([m["text"] for m in r["messages"]],
                         ["from me", "from dave", "from carol", "[视频]"])
        self.assertEqual([m.get("anchor", False) for m in r["messages"]],
                         [False, True, False, False])
        self.assertEqual(r["anchor_id"], "0:3")
        r = self.data.get_message_context(ROOM, time=302.5, before=0, after=0)
        self.assertEqual(r["anchor_id"], "0:3")
        r = self.data.get_message_context(ROOM, time=1, before=0, after=1)  # before 1st -> 1st
        self.assertEqual([m["text"] for m in r["messages"]], ["old group msg", "from bob"])
        r = self.data.get_message_context(ROOM, message_id="0:3", before=10_000, after=10_000)
        self.assertEqual(r["count"], 8)
        self.err(self.data.get_message_context, ROOM)

    # -- contacts -----------------------------------------------------------
    def test_get_contact_person(self):
        r = self.data.get_contact("Alice R")
        self.assertEqual(r["username"], ALICE)
        self.assertEqual((r["remark"], r["nickname"], r["is_friend"], r["is_group"]),
                         ("Alice R", "alice nick", True, False))
        self.assertEqual(r["chat"], {"msg_count": 7, "first_time": M.iso(100),
                                     "last_time": M.iso(204), "sent_by_me": 3, "sent_by_them": 4})
        self.assertEqual(r["shared_groups"], [])
        r = self.data.get_contact("Bobby")
        self.assertEqual(r["shared_group_count"], 1)
        g = r["shared_groups"][0]
        self.assertEqual((g["username"], g["name"], g["their_group_nickname"], g["member_count"]),
                         (ROOM, "Test Group", "Bob in group", 4))
        self.assertNotIn("chat", r)          # no 1-on-1 chat with Bob
        self.assertFalse(self.data.get_contact(DAVE)["is_friend"])   # stranger

    def test_get_contact_group_and_ambiguity(self):
        r = self.data.get_contact("Test Group")
        self.assertTrue(r["is_group"])
        self.assertEqual(r["member_count"], 4)
        self.assertIn({"name": "Bob in group", "username": BOB}, r["members"])
        self.assertEqual(r["chat"]["msg_count"], 8)
        p = self.err(self.data.get_contact, "o")      # Bobby, carol, Group, Dave Stranger...
        self.assertEqual(p["error"], "ambiguous")
        self.assertGreater(len(p["candidates"]), 1)
        self.assertIn("no contact", self.err(self.data.get_contact, "nobody here")["error"])

    # -- images -------------------------------------------------------------
    def _write_dat(self, suffix=""):
        d = os.path.join(self.tmp, "wechat", "msg", "attach", hashlib.md5(ALICE.encode()).hexdigest(),
                         time.strftime("%Y-%m", time.localtime(201)), "Img")
        os.makedirs(d, exist_ok=True)
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x11" * 64 + b"\xff\xd9"
        with open(os.path.join(d, IMAGE_MD5 + suffix + ".dat"), "wb") as f:
            f.write(bytes(b ^ 0x37 for b in jpeg))    # legacy XOR format
        return jpeg

    def test_get_image(self):
        self.data._image_keys = (None, None)
        self.assertIn("folder not found", self.err(self.data.get_image, ALICE, "0:2")["error"])
        os.makedirs(os.path.join(self.tmp, "wechat"))
        img_id = [m for m in self.data.get_messages(ALICE)["messages"] if m.get("kind") == "image"][0]["id"]
        self.assertIn("not on disk", self.err(self.data.get_image, ALICE, img_id)["error"])
        jpeg = self._write_dat("_t")
        data, ext, meta = self.data.get_image(ALICE, img_id)
        self.assertEqual((data, ext, meta["variant"], meta["id"]), (jpeg, "jpg", "thumbnail", img_id))
        self.assertIn("not an image", self.err(self.data.get_image, ALICE, "0:1")["error"])
        self.assertIn("not found", self.err(self.data.get_image, ALICE, "0:999")["error"])
        self.assertIn("not found", self.err(self.data.get_image, ALICE, "7:1")["error"])
        self.assertIn("bad message id", self.err(self.data.get_image, ALICE, "abc")["error"])


class PolicyTest(Base):
    def test_blocklist(self):
        d = make_data(self.tmp, mcp_blocklist=["Alice R"])
        self.assertEqual([c["username"] for c in d.list_chats()["chats"]], [ROOM])
        self.assertIn("no chat", self.err(d.resolve_chat, ALICE)["error"])
        self.assertIn("no chat", self.err(d.get_messages, "Alice")["error"])
        self.assertEqual(d.search_messages("hi")["count"], 0)
        self.assertEqual(d.search_messages("from")["count"], 4)
        self.err(d.get_contact, ALICE)
        self.err(d.get_image, ALICE, "0:2")
        # "e" is no longer ambiguous: only the group is visible.
        self.assertEqual(d.resolve_chat("e")["username"], ROOM)
        d._index.close()

    def test_blocklist_by_username_hides_shared_group(self):
        d = make_data(self.tmp, mcp_blocklist=[ROOM])
        self.assertEqual(d.get_contact("Bobby")["shared_groups"], [])
        self.assertEqual(d.search_messages("from")["count"], 0)

    def test_allowlist(self):
        d = make_data(self.tmp, mcp_allowlist=[ROOM])
        self.assertEqual([c["username"] for c in d.list_chats()["chats"]], [ROOM])
        self.assertEqual(d.search_messages("hi")["count"], 0)
        self.assertEqual(d.search_messages("from bob")["count"], 1)
        self.err(d.get_contact, "Bobby")          # not in the allowlist
        self.assertEqual(d.get_contact("Test Group")["member_count"], 4)
        d._index.close()


class DispatchTest(Base):
    def test_access_log_has_no_content(self):
        log_path = os.path.join(self.tmp, "logs", "access.jsonl")
        log = M.AccessLog(log_path)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = M.call_tool(self.data, log, "search_messages", {"query": "compressed"},
                              lambda: self.data.search_messages("compressed"))
            err = M.call_tool(self.data, log, "get_messages", {"chat": "e"},
                              lambda: self.data.get_messages("e"))
            M.call_tool(self.data, log, "boom", {}, lambda: 1 / 0)
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(res["count"], 1)
        self.assertEqual(err["error"], "ambiguous")
        with open(log_path) as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual([x["tool"] for x in lines], ["search_messages", "get_messages", "boom"])
        self.assertEqual(lines[0]["count"], 1)
        self.assertEqual(lines[0]["args"], {"query": "compressed"})
        self.assertEqual(lines[1]["error"], "ambiguous")
        self.assertEqual(lines[2]["error"], "ZeroDivisionError")
        self.assertTrue(set(lines[0]) >= {"time", "tool", "args", "count", "ms"})
        with open(log_path) as f:
            self.assertNotIn("compressed hi", f.read())

    def test_mcp_in_process(self):
        try:
            from mcp import Client
        except ImportError:
            self.skipTest("mcp 2.x client not available")
        import anyio
        log = M.AccessLog(os.path.join(self.tmp, "logs", "a.jsonl"))
        srv = M.build_server(self.data, log)
        self.data._image_keys = (None, None)
        ToolsTest._write_dat(self)

        async def go():
            async with Client(srv) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                self.assertLessEqual({"list_chats", "get_messages", "search_messages",
                                         "get_message_context", "get_contact", "get_image",
                                         "get_voice", "refresh"}, tools)
                r = await client.call_tool("list_chats", {"type": "single"})
                payload = json.loads(r.content[0].text)
                self.assertEqual(payload["chats"][0]["username"], ALICE)
                r = await client.call_tool("get_messages", {"chat": "Alice R", "limit": 2})
                self.assertEqual(json.loads(r.content[0].text)["count"], 2)
                r = await client.call_tool("get_image", {"chat": ALICE, "message_id": "0:2"})
                self.assertEqual(r.content[0].type, "image")
                self.assertEqual(r.content[0].mime_type, "image/jpeg")
                self.assertEqual(json.loads(r.content[1].text)["variant"], "full")
                r = await client.call_tool("get_image", {"chat": ALICE, "message_id": "0:1"})
                self.assertIn("not an image", json.loads(r.content[0].text)["error"])
                if voice_id:
                    r = await client.call_tool("get_voice", {"chat": ALICE,
                                                             "message_id": voice_id})
                    self.assertEqual(r.content[0].type, "audio")
                    self.assertIn(r.content[0].mime_type, ("audio/mp4", "audio/wav"))
                    self.assertEqual(json.loads(r.content[1].text)["duration"], 3)
        voice_id = _add_voice(self.fx, self.tmp) if HAVE_SILK else None
        anyio.run(go)


def _add_voice(fx, root, ts=260, voicelength=2600):
    """Voice message in ALICE's chat + its SILK blob in media_0.db -> message id."""
    lid = add_msg(fx.dbs[0], ALICE, 34, ALICE, ts,
                  f'<msg><voicemsg voicelength="{voicelength}" /></msg>')
    make_media_db(os.path.join(root, "decrypted", "message", "media_0.db"),
                  [(ALICE, ts, lid, ts, silk_sample(2.0))])
    return f"0:{lid}"


class VoiceTest(Base):
    @unittest.skipUnless(HAVE_SILK, "silk-python not installed")
    def test_get_voice(self):
        vid = _add_voice(self.fx, self.tmp)
        data, fmt, meta = self.data.get_voice(ALICE, vid)
        self.assertEqual(meta["duration"], 3)  # stated voicelength 2.6 s
        self.assertEqual(meta["id"], vid)
        self.assertEqual(meta["bytes"], len(data))
        if fmt == "mp4":
            self.assertEqual(data[4:8], b"ftyp")
        else:
            self.assertEqual(data[:4], b"RIFF")
        self.assertIn("not a voice", self.err(self.data.get_voice, ALICE, "0:1")["error"])
        vid2 = add_msg(self.fx.dbs[0], ALICE, 34, ALICE, 270, "<msg><voicemsg /></msg>")
        self.assertIn("not found", self.err(self.data.get_voice, ALICE, f"0:{vid2}")["error"])


class FakeDecryptor(types.SimpleNamespace):
    """Stands in for decrypt_db: 'decrypts' by copying, prints like the real one,
    and has no key-scanner attributes (so any sudo path would raise)."""
    PAGE_SZ = 4096

    def __init__(self, good_key):
        super().__init__()
        self.good_key = good_key
        self.calls = []

    def page1_hmac_ok(self, page1, key):
        return key == self.good_key

    # Change tracking like decrypt_db's state file, kept in memory.
    def load_state(self, out_dir):
        return dict(self.__dict__.setdefault("state", {}))

    def save_state(self, out_dir, updates):
        self.state.update(updates)

    def is_current(self, state, rel, src, out):
        return os.path.exists(out) and state.get(rel) == os.stat(src).st_mtime_ns

    def decrypt_database(self, src, out, key):
        print("decrypting", src)             # must not reach real stdout
        self.calls.append(src)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copyfile(src, out)
        return os.stat(src).st_mtime_ns


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcp_refresh_")
        self.db_dir = os.path.join(self.tmp, "db_storage")
        self.out = os.path.join(self.tmp, "decrypted")
        for rel in ("message/message_0.db", "contact/contact.db", "general/general.db"):
            p = os.path.join(self.db_dir, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(os.urandom(4096 * 2))
        self.key = "11" * 32
        self.keys_file = os.path.join(self.tmp, "keys.json")
        self.write_keys({"message/message_0.db": self.key, "contact/contact.db": self.key,
                         "general/general.db": self.key})
        self.fake = FakeDecryptor(bytes.fromhex(self.key))
        self.data = M.WeChatData({"decrypted_dir": self.out, "db_dir": self.db_dir,
                                  "keys_file": self.keys_file, "self_wxid": SELF,
                                  "mcp_index_path": os.path.join(self.tmp, "idx.db"),
                                  "mcp_auto_refresh_minutes": 5},
                                 decryptor=self.fake)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_keys(self, keys):
        with open(self.keys_file, "w") as f:
            json.dump({"_db_dir": self.db_dir, **{k: {"enc_key": v} for k, v in keys.items()}}, f)

    def test_refresh_flow(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = self.data.refresh()
        self.assertEqual(buf.getvalue(), "")  # decryptor prints went to stderr
        self.assertEqual(r["updated"], ["contact/contact.db", "message/message_0.db"])  # not general
        self.assertNotIn("action_required", r)
        self.assertFalse(any(f.endswith(".mcp_tmp") for _, _, fs in os.walk(self.out) for f in fs))
        r = self.data.refresh()
        self.assertEqual((r["updated"], r["unchanged"]), ([], 2))
        r = self.data.refresh(all_dbs=True)
        self.assertEqual(r["updated"], ["general/general.db"])

        # Changed DB whose key no longer validates -> stale, told to run ./wechat decrypt.
        src = os.path.join(self.db_dir, "message", "message_0.db")
        os.utime(src, (time.time() + 100, time.time() + 100))
        self.write_keys({"message/message_0.db": "22" * 32, "contact/contact.db": self.key})
        r = self.data.refresh()
        self.assertEqual(r["stale_keys"], ["message/message_0.db"])
        self.assertIn("./wechat decrypt", r["action_required"])
        # New DB without any key.
        p = os.path.join(self.db_dir, "message", "message_1.db")
        with open(p, "wb") as f:
            f.write(os.urandom(4096))
        r = self.data.refresh()
        self.assertEqual(r["missing_keys"], ["message/message_1.db"])

    def test_auto_refresh_throttled(self):
        self.assertIsNone(self.data.maybe_auto_refresh())
        n = len(self.fake.calls)
        self.assertEqual(n, 2)
        self.data.maybe_auto_refresh()
        self.assertEqual(len(self.fake.calls), n)       # within the interval: no-op
        src = os.path.join(self.db_dir, "contact", "contact.db")
        os.utime(src, (time.time() + 100, time.time() + 100))
        self.data._last_refresh -= 301
        self.data.maybe_auto_refresh()
        self.assertEqual(len(self.fake.calls), n + 1)

    def test_auto_refresh_notice_and_disabled(self):
        self.write_keys({})
        self.assertIn("./wechat decrypt", self.data.maybe_auto_refresh())
        d = M.WeChatData({"decrypted_dir": self.out, "mcp_auto_refresh_minutes": 0})
        self.assertIsNone(d.maybe_auto_refresh())


if __name__ == "__main__":
    unittest.main()
