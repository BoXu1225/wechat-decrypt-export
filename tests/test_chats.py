"""Tests for chats.py using small synthetic databases (no real data).

Run: python -m unittest discover -s tests   (or: pytest tests)
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

import zstandard

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chats  # noqa: E402

SELF = "wxid_self"
ALICE = "wxid_alice"      # friend with remark
BOB = "wxid_bob"          # friend with nickname only
CAROL = "carol_custom"    # non-wxid personal id, no contact name -> username
DAVE = "wxid_dave"        # stranger (group member)
ROOM = "12345@chatroom"
OFFICIAL = "gh_news"
FILEHELPER = "filehelper"

MSG_SCHEMA = """(local_id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER,
    local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
    status INTEGER, upload_status INTEGER, download_status INTEGER, server_seq INTEGER,
    origin_source INTEGER, source TEXT, message_content TEXT, compress_content TEXT,
    packed_info_data BLOB, WCDB_CT_message_content INTEGER DEFAULT NULL,
    WCDB_CT_source INTEGER DEFAULT NULL)"""

CONTACT_COLS = ("id INTEGER PRIMARY KEY, username TEXT, local_type INTEGER, alias TEXT, "
                "encrypt_username TEXT, flag INTEGER, delete_flag INTEGER, verify_flag INTEGER, "
                "remark TEXT, nick_name TEXT, extra_buffer BLOB")


def tbl(username):
    return "Msg_" + hashlib.md5(username.encode()).hexdigest()


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(num, data):
    if isinstance(data, str):
        data = data.encode()
    return _varint((num << 3) | 2) + _varint(len(data)) + data


def _vfield(num, val):
    return _varint(num << 3) + _varint(val)


def room_ext_buffer(members):
    """members: list of (username, group_nickname or None)."""
    buf = b""
    for user, nick in members:
        m = _field(1, user)
        if nick:
            m += _field(2, nick)
        m += _vfield(3, 1) + _field(4, SELF)
        buf += _field(1, m)
    return buf + _vfield(3, 0) + _vfield(4, 7)


def zstd(text):
    return zstandard.ZstdCompressor().compress(text.encode())


class Fixture:
    """Builds decrypted/{contact,message} with two message DBs."""

    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "contact"))
        os.makedirs(os.path.join(root, "message"))
        self._make_contacts()
        self.dbs = {}
        # message_1.db = older year, message_0.db = newer year
        self._make_msg_db(1, [
            (ALICE, [
                (1, ALICE, 100, "hi old", None),
                (1, SELF, 101, "hello old", None),
            ]),
            (ROOM, [
                (1, BOB, 150, f"{BOB}:\nold group msg", None),
            ]),
        ])
        self._make_msg_db(0, [
            (ALICE, [
                (1, ALICE, 200, zstd("compressed hi"), 4),
                (3, SELF, 201, None, None),
                (10000, "", 202, "<revokemsg>Alice recalled</revokemsg>", None),
                (49 | (57 << 32), ALICE, 203,
                 "<msg><appmsg><title>quoted reply</title><type>57</type></appmsg></msg>", None),
                (49 | (5 << 32), SELF, 204,
                 "<msg><appmsg><title>a link</title><type>5</type></appmsg></msg>", None),
                (11000, ALICE, 205, "dropped", None),  # formats to None
            ]),
            (ROOM, [
                (1, BOB, 300, f"{BOB}:\nfrom bob", None),
                (1, SELF, 301, "from me", None),
                (1, DAVE, 302, zstd(f"{DAVE}:\nfrom dave"), 4),
                (1, CAROL, 303, f"{CAROL}:\nfrom carol", None),
                (43, "", 304, f'<msg><videomsg length="1" fromusername = "{DAVE}" /></msg>', None),
                (10000, "", 305, f"{ROOM}:\n<sysmsg>joined</sysmsg>", None),
                (47, ALICE, 306, f"{ALICE}:\n<emoji/>", None),
            ]),
            (OFFICIAL, [(1, OFFICIAL, 400, "promo", None)]),
            (FILEHELPER, [(1, SELF, 500, "note", None)]),
        ])

    def _make_contacts(self):
        conn = sqlite3.connect(os.path.join(self.root, "contact", "contact.db"))
        conn.execute(f"CREATE TABLE contact({CONTACT_COLS})")
        conn.execute(f"CREATE TABLE stranger({CONTACT_COLS})")
        conn.execute("CREATE TABLE chat_room(id INTEGER PRIMARY KEY, username TEXT, "
                     "owner TEXT, ext_buffer BLOB)")
        rows = [
            (SELF, 1, 0, "", "Me Myself"),
            (ALICE, 1, 0, " Alice R ", "alice nick"),
            (BOB, 1, 0, "", "Bobby"),
            (CAROL, 1, 0, "", ""),
            (ROOM, 2, 0, "", "Test Group"),
            (OFFICIAL, 1, 24, "", "News Account"),
            (FILEHELPER, 1, 0, "", "File Transfer"),
        ]
        conn.executemany(
            "INSERT INTO contact(username, local_type, verify_flag, remark, nick_name) "
            "VALUES (?,?,?,?,?)", rows)
        conn.execute("INSERT INTO stranger(username, local_type, verify_flag, remark, nick_name) "
                     "VALUES (?,3,0,'','Dave Stranger')", (DAVE,))
        conn.execute("INSERT INTO chat_room(username, owner, ext_buffer) VALUES (?,?,?)",
                     (ROOM, SELF, room_ext_buffer(
                         [(SELF, None), (BOB, "Bob in group"), (DAVE, None), (CAROL, None)])))
        conn.commit()
        conn.close()

    def _make_msg_db(self, n, chats_rows):
        path = os.path.join(self.root, "message", f"message_{n}.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
        # Different rowid layout per DB to make sure ids are resolved per DB.
        users = [""] + [SELF, ALICE, BOB, CAROL, DAVE, ROOM, OFFICIAL, FILEHELPER]
        if n == 0:
            users = list(reversed(users))
        conn.executemany("INSERT INTO Name2Id(user_name, is_session) VALUES (?,0)",
                         [(u,) for u in users])
        ids = {u: r for r, u in conn.execute("SELECT rowid, user_name FROM Name2Id")}
        conn.execute("CREATE TABLE TimeStamp(timestamp INTEGER)")
        for username, msgs in chats_rows:
            t = tbl(username)
            conn.execute(f"CREATE TABLE {t}{MSG_SCHEMA}")
            conn.execute(f"CREATE INDEX {t}_SENDERID ON {t}(real_sender_id)")
            for i, (lt, sender, ts, content, ct) in enumerate(msgs):
                conn.execute(
                    f"INSERT INTO {t}(server_id, local_type, sort_seq, real_sender_id, "
                    f"create_time, message_content, WCDB_CT_message_content) "
                    f"VALUES (?,?,?,?,?,?,?)",
                    (ts * 10 + i, lt, ts * 1000, ids[sender], ts, content, ct))
        # A table whose hash matches no known user must be ignored.
        conn.execute(f"CREATE TABLE {tbl('nobody')}{MSG_SCHEMA}")
        conn.execute(f"INSERT INTO {tbl('nobody')}(local_type, create_time) VALUES (1, 1)")
        conn.commit()
        conn.close()
        self.dbs[n] = path


class ChatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="chats_test_")
        cls.dir = os.path.join(cls.tmp, "decrypted")
        cls.fx = Fixture(cls.dir)
        cls.contacts = chats.load_contacts(cls.dir)
        cls.chats = chats.list_chats(cls.dir, contacts=cls.contacts)
        cls.by_user = {c["username"]: c for c in cls.chats}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    # -- contacts -----------------------------------------------------------
    def test_load_contacts_priority(self):
        c = self.contacts
        self.assertEqual(c[ALICE], "Alice R")        # remark, stripped
        self.assertEqual(c[BOB], "Bobby")            # nickname
        self.assertEqual(c[CAROL], CAROL)            # username fallback
        self.assertEqual(c[DAVE], "Dave Stranger")   # stranger table
        self.assertEqual(c[ROOM], "Test Group")

    def test_load_contacts_missing_db(self):
        self.assertEqual(chats.load_contacts(os.path.join(self.tmp, "nope")), {})

    def test_parse_group_nicknames(self):
        gn = chats.load_group_nicknames(self.dir)
        self.assertEqual(gn, {ROOM: {BOB: "Bob in group"}})
        members = chats.parse_chat_room_members(room_ext_buffer([(BOB, "x"), (DAVE, None)]))
        self.assertEqual(members, {BOB: "x", DAVE: ""})
        self.assertEqual(chats.parse_chat_room_members(b"\xff\xff"), {})
        self.assertEqual(chats.parse_chat_room_members(None), {})

    # -- list_chats ---------------------------------------------------------
    def test_md5_table_name(self):
        self.assertEqual(chats.table_name_for(ALICE),
                         "Msg_" + hashlib.md5(ALICE.encode()).hexdigest())

    def test_list_chats_basic(self):
        self.assertEqual(set(self.by_user), {ALICE, ROOM})
        alice = self.by_user[ALICE]
        self.assertFalse(alice["is_group"])
        self.assertEqual(alice["name"], "Alice R")
        self.assertEqual(alice["msg_count"], 8)
        self.assertEqual(alice["last_ts"], 205)
        # Spans both DBs, oldest first.
        self.assertEqual(alice["tables"], [(self.fx.dbs[1], tbl(ALICE)),
                                           (self.fx.dbs[0], tbl(ALICE))])
        room = self.by_user[ROOM]
        self.assertTrue(room["is_group"])
        self.assertEqual(room["msg_count"], 8)
        self.assertEqual(room["last_ts"], 306)
        # Sorted by last activity desc.
        self.assertEqual([c["username"] for c in self.chats], [ROOM, ALICE])

    def test_list_chats_include_system(self):
        allc = {c["username"] for c in chats.list_chats(self.dir, include_system=True)}
        self.assertEqual(allc, {ALICE, ROOM, OFFICIAL, FILEHELPER})

    def test_is_system_username(self):
        self.assertTrue(chats.is_system_username("gh_abc"))
        self.assertTrue(chats.is_system_username("weixin"))
        self.assertTrue(chats.is_system_username("someofficial", verify_flag=8))
        self.assertFalse(chats.is_system_username("wxid_x", verify_flag=8))
        self.assertFalse(chats.is_system_username("custom_id"))
        self.assertFalse(chats.is_system_username("123@chatroom"))
        self.assertFalse(chats.is_system_username("123@openim"))

    def test_list_chats_empty_dir(self):
        self.assertEqual(chats.list_chats(os.path.join(self.tmp, "nope")), [])

    # -- find_chats ---------------------------------------------------------
    def test_find_chats(self):
        fake = [
            {"username": "wxid_1", "name": "Ann Smith"},
            {"username": "wxid_2", "name": "ann"},
            {"username": "wxid_3", "name": "Ann"},
            {"username": "wxid_ann", "name": "Zed"},
            {"username": "wxid_4", "name": "Bob"},
        ]
        names = [c["name"] for c in chats.find_chats("Ann", fake)]
        self.assertEqual(names, ["Ann", "ann", "Ann Smith", "Zed"])
        self.assertEqual(chats.find_chats("zzz", fake), [])
        self.assertEqual([c["name"] for c in chats.find_chats("wxid_4", fake)], ["Bob"])

    # -- iter_messages ------------------------------------------------------
    def msgs(self, username):
        return list(chats.iter_messages(self.by_user[username], self.dir, SELF, self.contacts))

    def test_iter_messages_one_on_one(self):
        recs = self.msgs(ALICE)
        got = [(r["ts"], r["sender"], r["is_self"], r["kind"], r["text"]) for r in recs]
        self.assertEqual(got, [
            (100, "Alice R", False, "text", "hi old"),
            (101, "我", True, "text", "hello old"),
            (200, "Alice R", False, "text", "compressed hi"),
            (201, "我", True, "image", "[图片]"),
            (202, "系统", False, "system", "[系统消息] Alice recalled"),
            (203, "Alice R", False, "quote", "quoted reply"),
            (204, "我", True, "link", "[链接] a link"),
        ])
        r = recs[2]
        self.assertEqual(set(r), {"ts", "sender", "is_self", "kind", "text", "local_type",
                                  "local_id", "server_id", "create_time"})
        self.assertEqual(r["create_time"], 200)
        self.assertEqual(r["server_id"], 2000)
        self.assertEqual(r["local_type"], 1)
        self.assertEqual(recs[5]["local_type"], 49 | (57 << 32))

    def test_iter_messages_group(self):
        got = [(r["ts"], r["sender"], r["is_self"], r["kind"], r["text"])
               for r in self.msgs(ROOM)]
        self.assertEqual(got, [
            (150, "Bob in group", False, "text", "old group msg"),
            (300, "Bob in group", False, "text", "from bob"),       # group nickname
            (301, "我", True, "text", "from me"),
            (302, "Dave Stranger", False, "text", "from dave"),     # stranger nickname
            (303, CAROL, False, "text", "from carol"),              # username fallback
            (304, "Dave Stranger", False, "video", "[视频]"),        # fromusername in XML
            (305, "系统", False, "system", "[系统消息] joined"),
            (306, "Alice R", False, "emoji", "[表情]"),
        ])

    def test_group_nicknames_param(self):
        recs = list(chats.iter_messages(self.by_user[ROOM], self.dir, SELF, self.contacts,
                                        group_nicknames={}))
        self.assertEqual(recs[0]["sender"], "Bobby")

    def test_message_kind(self):
        k = chats.message_kind
        self.assertEqual(k(1, "x"), "text")
        self.assertEqual(k(34, None), "voice")
        self.assertEqual(k(48, None), "location")
        self.assertEqual(k(10000, None), "system")
        self.assertEqual(k(49, "<appmsg><type>6</type></appmsg>"), "file")
        self.assertEqual(k(49, "<appmsg><type>33</type></appmsg>"), "miniprogram")
        self.assertEqual(k(49, "<appmsg><type>8</type></appmsg>"), "emoji")
        self.assertEqual(k(49 | (57 << 32), None), "quote")
        self.assertEqual(k(49 | (2001 << 32), None), "other")
        self.assertEqual(k(42, None), "other")

    def test_decompress(self):
        self.assertEqual(chats.decompress_if_needed(zstd("abc"), 4), "abc")
        self.assertEqual(chats.decompress_if_needed("plain", None), "plain")
        self.assertIsNone(chats.decompress_if_needed(b"garbage", 4))
        self.assertIsNone(chats.decompress_if_needed(b"bytes", None))


if __name__ == "__main__":
    unittest.main()
