"""Tests for wechat_send.py with a fake UI driver (no GUI, no real data)."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wechat_send as W  # noqa: E402

_RealSender = W.Sender

CONTACTS = {
    "wxid_alice": "Alice",
    "wxid_bob": "Bob",
    "wxid_bob2": "Bob",           # same display name -> ambiguous
    "wxid_carol": "Carol Smith",
    "filehelper": "文件传输助手",
    "111@chatroom": "Team",
}
CHAT_LIST = [
    {"username": "222@chatroom", "name": "222@chatroom", "is_group": True},  # unnamed
    {"username": "wxid_alice", "name": "Alice", "is_group": False},
]


class FakeDriver(W.UIDriver):
    def __init__(self, titles=None, input_after_paste=None, leftover="", existing=""):
        self.calls = []
        self.titles = {}            # query -> title shown after opening
        self.titles.update(titles or {})
        self.current_title = None
        self.box = existing
        self.input_after_paste = input_after_paste
        self.leftover = leftover

    def prepare(self):
        self.calls.append("prepare")
        return "token"

    def open_chat(self, query, expected_names):
        self.calls.append(("open", query))
        self.current_title = self.titles.get(query, query)

    def chat_title(self):
        return self.current_title

    def paste_into_input(self, text):
        self.calls.append("paste")
        self.box = self.input_after_paste if self.input_after_paste is not None else text

    def input_text(self):
        return self.box

    def clear_input(self):
        self.calls.append("clear")
        self.box = ""

    def press_send(self, send_key):
        self.calls.append(("enter", send_key))
        self.box = self.leftover

    def restore(self, token):
        self.calls.append(("restore", token))


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log_path = os.path.join(self.tmp, "logs", "send_log.jsonl")
        self.cfg = {"decrypted_dir": self.tmp, "send_log": self.log_path,
                    "send_rate_limit": {"min_interval_s": 3, "max_per_minute": 3}}
        self.clock = Clock()
        self.verified = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def sender(self, driver, verify_result=("sent", None)):
        def verifier(target, text, since, cfg, contacts):
            self.verified.append((target["username"], text, since))
            return verify_result
        return _RealSender(self.cfg, driver=driver,
                        data_loader=lambda cfg: (dict(CONTACTS), list(CHAT_LIST)),
                        verifier=verifier, clock=self.clock, sleep=self.clock.sleep)

    def log_entries(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path) as f:
            return [json.loads(line) for line in f]


class ResolveTests(unittest.TestCase):
    def test_exact_username(self):
        self.assertEqual(W.resolve_chat("wxid_bob2", CONTACTS, CHAT_LIST)["username"],
                         "wxid_bob2")

    def test_exact_name(self):
        c = W.resolve_chat("Alice", CONTACTS, CHAT_LIST)
        self.assertEqual((c["username"], c["is_group"]), ("wxid_alice", False))
        self.assertTrue(W.resolve_chat("Team", CONTACTS, CHAT_LIST)["is_group"])

    def test_ambiguous(self):
        with self.assertRaises(W.AmbiguousChat) as cm:
            W.resolve_chat("Bob", CONTACTS, CHAT_LIST)
        users = {c["username"] for c in cm.exception.extra["candidates"]}
        self.assertEqual(users, {"wxid_bob", "wxid_bob2"})

    def test_fuzzy_only_is_not_found(self):
        with self.assertRaises(W.ChatNotFound) as cm:
            W.resolve_chat("carol", CONTACTS, CHAT_LIST)
        self.assertEqual([c["username"] for c in cm.exception.extra["candidates"]],
                         ["wxid_carol"])

    def test_case_insensitive_is_not_exact(self):
        with self.assertRaises(W.ChatNotFound):
            W.resolve_chat("alice", CONTACTS, CHAT_LIST)

    def test_empty(self):
        with self.assertRaises(W.ChatNotFound):
            W.resolve_chat("  ", CONTACTS, CHAT_LIST)

    def test_chat_without_contact_row(self):
        self.assertEqual(W.resolve_chat("222@chatroom", CONTACTS, CHAT_LIST)["username"],
                         "222@chatroom")


class TitleTests(unittest.TestCase):
    def test_title_matches(self):
        self.assertTrue(W.title_matches("Alice", ["Alice"]))
        self.assertTrue(W.title_matches(" Alice ", ["Alice"]))
        self.assertFalse(W.title_matches("Alice2", ["Alice"]))
        self.assertFalse(W.title_matches("Alice (3)", ["Alice"]))
        self.assertTrue(W.title_matches("Team (3)", ["Team"], is_group=True))
        self.assertTrue(W.title_matches("Team（12）", ["Team"], is_group=True))
        self.assertFalse(W.title_matches("Team (x)", ["Team"], is_group=True))
        self.assertFalse(W.title_matches(None, ["Team"]))

    def test_filehelper_aliases(self):
        names = W.ui_names({"username": "filehelper", "name": "文件传输助手"})
        self.assertIn("File Transfer", names)
        self.assertEqual(names[0], "文件传输助手")


class SendFlowTests(Base):
    def test_send_happy_path(self):
        d = FakeDriver()
        res = self.sender(d).send_text("Alice", "hi\nthere")
        self.assertEqual(res["status"], "sent")
        self.assertEqual(res["chat"]["username"], "wxid_alice")
        self.assertEqual(d.calls, ["prepare", ("open", "Alice"), "paste",
                                   ("enter", "enter"), ("restore", "token")])
        self.assertEqual(self.verified[0][:2], ("wxid_alice", "hi\nthere"))
        entries = self.log_entries()
        self.assertEqual(entries[0]["status"], "sent")
        self.assertEqual(entries[0]["len"], len("hi\nthere"))
        self.assertEqual(entries[1]["verify_status"], "sent")
        with open(self.log_path) as f:
            self.assertNotIn("there", f.read())

    def test_filehelper_uses_ui_name(self):
        d = FakeDriver()
        res = self.sender(d).send_text("filehelper", "x")
        self.assertEqual(res["status"], "sent")
        self.assertIn(("open", "文件传输助手"), d.calls)

    def test_filehelper_english_title(self):
        d = FakeDriver(titles={"文件传输助手": "File Transfer"})
        self.assertEqual(self.sender(d).send_text("filehelper", "x")["status"], "sent")

    def test_wrong_title_aborts_before_typing(self):
        d = FakeDriver(titles={"Alice": "Somebody Else"})
        with self.assertRaises(W.UIError):
            self.sender(d).send_text("Alice", "hi")
        self.assertNotIn("paste", d.calls)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "enter" for c in d.calls))
        self.assertEqual(d.calls[-1], ("restore", "token"))
        self.assertEqual(self.log_entries()[-1]["status"], "failed")

    def test_unreadable_title_aborts(self):
        d = FakeDriver(titles={"Alice": None})
        with self.assertRaises(W.UIError):
            self.sender(d).send_text("Alice", "hi")
        self.assertNotIn("paste", d.calls)

    def test_input_mismatch_clears(self):
        d = FakeDriver(input_after_paste="h")
        with self.assertRaises(W.UIError):
            self.sender(d).send_text("Alice", "hi")
        self.assertIn("clear", d.calls)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "enter" for c in d.calls))
        self.assertEqual(d.box, "")

    def test_existing_draft_untouched(self):
        d = FakeDriver(existing="user draft")
        with self.assertRaises(W.UIError):
            self.sender(d).send_text("Alice", "hi")
        self.assertNotIn("paste", d.calls)
        self.assertNotIn("clear", d.calls)
        self.assertEqual(d.box, "user draft")

    def test_title_changes_after_paste(self):
        d = FakeDriver()
        orig = d.paste_into_input

        def paste(text):
            orig(text)
            d.current_title = "Other"
        d.paste_into_input = paste
        with self.assertRaises(W.UIError):
            self.sender(d).send_text("Alice", "hi")
        self.assertIn("clear", d.calls)

    def test_dry_run_never_presses_enter(self):
        d = FakeDriver()
        res = self.sender(d).send_text("Alice", "hi", dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertIn("clear", d.calls)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "enter" for c in d.calls))
        self.assertEqual(self.verified, [])
        self.assertEqual(self.log_entries()[0]["status"], "dry_run")

    def test_enter_did_not_send(self):
        d = FakeDriver(leftover="hi\n")
        with self.assertRaises(W.UIError):
            self.sender(d).send_text("Alice", "hi")
        self.assertEqual(d.calls[-2], "clear")

    def test_cmd_enter_config(self):
        self.cfg["send_key"] = "cmd_enter"
        d = FakeDriver()
        self.sender(d).send_text("Alice", "hi")
        self.assertIn(("enter", "cmd_enter"), d.calls)

    def test_unverified(self):
        d = FakeDriver()
        res = self.sender(d, verify_result=("unverified", "no keys")).send_text("Alice", "hi")
        self.assertEqual((res["status"], res["note"]), ("unverified", "no keys"))

    def test_no_verify(self):
        res = self.sender(FakeDriver()).send_text("Alice", "hi", verify=False)
        self.assertEqual(res["status"], "unverified")
        self.assertEqual(self.verified, [])

    def test_resolution_errors_do_not_touch_ui(self):
        d = FakeDriver()
        with self.assertRaises(W.AmbiguousChat):
            self.sender(d).send_text("Bob", "hi")
        with self.assertRaises(W.ChatNotFound):
            self.sender(d).send_text("Bo", "hi")
        with self.assertRaises(W.UnsupportedChat):
            self.sender(d).send_text("222@chatroom", "hi")
        self.assertEqual(d.calls, [])

    def test_text_validation(self):
        self.cfg["send_max_chars"] = 5
        d = FakeDriver()
        for bad in ("", "   ", "123456", "a\x00b"):
            with self.assertRaises(W.InvalidText):
                self.sender(d).send_text("Alice", bad)
        self.assertEqual(d.calls, [])

    def test_driver_exception_clears_and_restores(self):
        d = FakeDriver()

        def boom(send_key):
            raise RuntimeError("x")
        d.press_send = boom
        with self.assertRaises(RuntimeError):
            self.sender(d).send_text("Alice", "hi")
        self.assertIn("clear", d.calls)
        self.assertEqual(d.calls[-1], ("restore", "token"))
        self.assertEqual(self.log_entries()[-1]["error"], "RuntimeError")


class RateLimitTests(Base):
    def test_min_interval_and_per_minute(self):
        s = self.sender(FakeDriver())
        s.send_text("Alice", "1")
        with self.assertRaises(W.RateLimited) as cm:
            s.send_text("Alice", "2")
        self.assertGreater(cm.exception.extra["retry_after"], 0)
        self.clock.t += 5
        s.send_text("Alice", "2")
        self.clock.t += 5
        s.send_text("Alice", "3")
        self.clock.t += 5
        with self.assertRaises(W.RateLimited):
            s.send_text("Alice", "4")      # 3 per minute
        self.clock.t += 60
        s.send_text("Alice", "4")

    def test_dry_runs_and_failures_not_counted(self):
        s = self.sender(FakeDriver())
        s.send_text("Alice", "1", dry_run=True)
        s.send_text("Alice", "2", dry_run=True)
        self.assertEqual(s.send_text("Alice", "3")["status"], "sent")

    def test_rate_limited_does_not_touch_ui(self):
        d = FakeDriver()
        s = self.sender(d)
        s.send_text("Alice", "1")
        d.calls.clear()
        with self.assertRaises(W.RateLimited):
            s.send_text("Alice", "2")
        self.assertEqual(d.calls, [])


class ToolWrapperTests(Base):
    def test_errors_become_dicts(self):
        orig = W.Sender

        def fake_sender(cfg, driver=None):
            return self.sender(FakeDriver())
        W.Sender = fake_sender
        try:
            res = W.send_message_tool("Bob", "hi", cfg=self.cfg)
            self.assertEqual((res["status"], res["error"]), ("failed", "ambiguous_chat"))
            json.dumps(res)
            res = W.send_message_tool("Alice", "hi", cfg=self.cfg)
            self.assertEqual(res["status"], "sent")
            json.dumps(res)
        finally:
            W.Sender = orig


class VerifyTests(unittest.TestCase):
    def test_polls_until_found(self):
        clock = Clock()
        hits = iter([None, None, {"text": "x"}])
        status, note = W.verify_sent(
            {"username": "u"}, "x", 0, {}, {}, timeout=10,
            refresh=lambda cfg: (True, None), finder=lambda *a: next(hits),
            sleep=clock.sleep, clock=clock)
        self.assertEqual(status, "sent")

    def test_timeout(self):
        clock = Clock()
        status, note = W.verify_sent(
            {"username": "u"}, "x", 0, {}, {}, timeout=3,
            refresh=lambda cfg: (True, None), finder=lambda *a: None,
            sleep=clock.sleep, clock=clock)
        self.assertEqual(status, "unverified")
        self.assertIn("not found", note)

    def test_no_keys(self):
        status, note = W.verify_sent(
            {"username": "u"}, "x", 0, {}, {},
            refresh=lambda cfg: (False, "no keys"), finder=lambda *a: 1 / 0)
        self.assertEqual((status, note), ("unverified", "no keys"))

    def test_refresh_exception(self):
        def boom(cfg):
            raise OSError("x")
        status, _ = W.verify_sent({"username": "u"}, "x", 0, {}, {}, refresh=boom)
        self.assertEqual(status, "unverified")


class FindSentTests(unittest.TestCase):
    """find_sent_message against a tiny synthetic message DB."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "message"))
        db = sqlite3.connect(os.path.join(self.tmp, "message", "message_0.db"))
        db.execute("CREATE TABLE Name2Id (user_name TEXT)")
        db.executemany("INSERT INTO Name2Id(rowid, user_name) VALUES (?, ?)",
                       [(1, "wxid_self"), (2, "wxid_alice")])
        t = "Msg_" + hashlib.md5(b"wxid_alice").hexdigest()
        db.execute(f"""CREATE TABLE [{t}] (local_id INTEGER PRIMARY KEY, server_id INTEGER,
            local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER,
            create_time INTEGER, message_content TEXT, WCDB_CT_message_content INTEGER,
            packed_info_data BLOB)""")
        rows = [(1, 1, 100, 1, 100, "old hello", 0),
                (1, 2, 200, 2, 200, "hello\nworld", 0),     # from the other side
                (1, 3, 200, 1, 200, "hello\r\nworld  ", 0)]  # self
        db.executemany(f"""INSERT INTO [{t}] (local_type, sort_seq, create_time,
            real_sender_id, server_id, message_content, WCDB_CT_message_content)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", rows)
        db.commit()
        db.close()
        self.cfg = {"decrypted_dir": self.tmp, "self_wxid": "wxid_self"}
        self.chat = {"username": "wxid_alice", "name": "Alice", "is_group": False}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_found(self):
        rec = W.find_sent_message(self.chat, "hello\nworld", 150, self.cfg, {})
        self.assertIsNotNone(rec)
        self.assertTrue(rec["is_self"])

    def test_too_old_or_other_text(self):
        self.assertIsNone(W.find_sent_message(self.chat, "old hello", 150, self.cfg, {}))
        self.assertIsNone(W.find_sent_message(self.chat, "hello", 150, self.cfg, {}))

    def test_unknown_chat(self):
        chat = dict(self.chat, username="wxid_nobody")
        self.assertIsNone(W.find_sent_message(chat, "x", 0, self.cfg, {}))


class WalTests(unittest.TestCase):
    PAGE = 64

    def _frame(self, pgno, commit, salt, fill):
        return (pgno.to_bytes(4, "big") + commit.to_bytes(4, "big") + salt + b"\0" * 8
                + bytes([fill]) * self.PAGE)

    def test_apply_committed_frames_only(self):
        tmp = tempfile.mkdtemp()
        try:
            db = os.path.join(tmp, "x.db")
            with open(db, "wb") as f:
                f.write(b"\x01" * self.PAGE * 3)
            salt = b"SALTSALT"
            hdr = b"\x37\x7f\x06\x82" + b"\0" * 4 + self.PAGE.to_bytes(4, "big") \
                + b"\0" * 4 + salt + b"\0" * 8
            wal = hdr + self._frame(2, 0, salt, 0x22) + self._frame(4, 4, salt, 0x44) \
                + self._frame(3, 0, salt, 0x33) \
                + self._frame(1, 4, b"OLDSALT!", 0x55)
            with open(db + "-wal", "wb") as f:
                f.write(wal)
            n = W._apply_wal(db + "-wal", db, b"k", lambda k, p, n: p, self.PAGE)
            self.assertEqual(n, 2)
            with open(db, "rb") as f:
                data = f.read()
            pages = [data[i:i + self.PAGE] for i in range(0, len(data), self.PAGE)]
            self.assertEqual([p[0] for p in pages], [0x01, 0x22, 0x01, 0x44])
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
