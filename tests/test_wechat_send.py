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

    def press_send(self, send_key, expected_title, expected_input):
        # Like the helper: refuse unless state is exactly what was approved.
        if expected_title != self.current_title or expected_input != self.box:
            raise W.UIError("precondition_failed")
        self.calls.append(("enter", send_key))
        self.box = self.leftover
        return self.box

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

        def boom(send_key, expected_title, expected_input):
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


class FakeDecryptDb:
    """Stands in for decrypt_db's incremental-decrypt API."""
    PAGE_SZ = 16

    def __init__(self, keys, key_ok=True, decrypt_ok=True):
        self.keys, self.key_ok, self.decrypt_ok = keys, key_ok, decrypt_ok
        self.decrypted, self.saved = [], {}
        self.current = set()

    def load_keys(self):
        print("noise on stdout")      # must be routed to stderr
        return dict(self.keys)

    def load_state(self, out_dir):
        return {}

    def is_current(self, state, rel, db_path, out_path):
        return rel in self.current

    def page1_hmac_ok(self, page1, enc_key):
        return self.key_ok

    def decrypt_database(self, src, out, enc_key):
        self.decrypted.append(os.path.basename(src))
        return {"db": [1, 1], "wal": None} if self.decrypt_ok else False

    def save_state(self, out_dir, updates):
        self.saved.update(updates)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        msg = os.path.join(self.tmp, "src", "message")
        os.makedirs(msg)
        for f in ("message_0.db", "message_1.db", "message_fts.db", "message_0.db-wal"):
            with open(os.path.join(msg, f), "wb") as fh:
                fh.write(b"x" * 16)
        self.cfg = {"db_dir": os.path.join(self.tmp, "src"),
                    "decrypted_dir": os.path.join(self.tmp, "out")}
        self.keys = {"message/message_0.db": {"enc_key": "00" * 32},
                     "message/message_1.db": {"enc_key": "00" * 32}}

    def run_refresh(self, dd):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            res = W.refresh_message_dbs(self.cfg, dd=dd)
        self.assertEqual(out.getvalue(), "")   # nothing on stdout (MCP-safe)
        return res

    def test_decrypts_changed_message_dbs_only(self):
        dd = FakeDecryptDb(self.keys)
        dd.current = {"message/message_1.db"}
        self.assertEqual(self.run_refresh(dd), (True, None))
        self.assertEqual(dd.decrypted, ["message_0.db"])
        self.assertEqual(list(dd.saved), ["message/message_0.db"])

    def test_no_keys(self):
        ok, note = self.run_refresh(FakeDecryptDb({}))
        self.assertFalse(ok)
        self.assertIn("keys", note)

    def test_stale_key(self):
        dd = FakeDecryptDb(self.keys, key_ok=False)
        ok, note = self.run_refresh(dd)
        self.assertFalse(ok)
        self.assertIn("stale", note)
        self.assertEqual(dd.decrypted, [])

    def test_decrypt_failure(self):
        ok, note = self.run_refresh(FakeDecryptDb(self.keys, decrypt_ok=False))
        self.assertFalse(ok)
        self.assertIn("decrypt failed", note)

class FakeHelperServer:
    """Unix-socket server speaking the helper protocol from a handler table."""

    def __init__(self, handlers):
        import socket
        import threading
        self.dir = tempfile.mkdtemp(prefix="wsh")
        self.path = os.path.join(self.dir, "s.sock")
        self.handlers = handlers
        self.requests = []
        self.connections = 0
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.connections += 1
            f = c.makefile("rwb")
            for line in f:
                req = json.loads(line)
                self.requests.append(req)
                h = self.handlers.get(req["op"])
                if h is None:
                    resp = {"ok": False, "error": {"code": "unknown_op", "message": "x"}}
                elif h == "hang":
                    continue
                elif h == "close":
                    break
                else:
                    resp = h(req)
                    wrong_id = resp.pop("wrong_id", False)
                    if resp.get("ok") is None:
                        resp = {"ok": True, "result": resp}
                    if wrong_id:
                        resp["id"] = -1
                resp.setdefault("id", req.get("id"))
                f.write((json.dumps(resp) + "\n").encode())
                f.flush()
            f.close()
            c.close()

    def ops(self):
        return [r["op"] for r in self.requests]

    def close(self):
        self.srv.close()
        shutil.rmtree(self.dir)


def _err(code):
    return lambda req: {"ok": False, "error": {"code": code, "message": code}}


STATUS_OK = {"trusted": True, "screen_locked": False, "wechat_running": True}


class HelperDriverTests(unittest.TestCase):
    def make(self, **handlers):
        base = {"status": lambda r: dict(STATUS_OK),
                "activate": lambda r: {"previous_pid": 42},
                "restore": lambda r: {"restored": True}}
        base.update(handlers)
        self.server = FakeHelperServer(base)
        self.addCleanup(self.server.close)
        return W.HelperDriver(W.HelperClient(self.server.path))

    def test_missing_socket(self):
        d = W.HelperDriver(W.HelperClient("/nonexistent/dir/s.sock"))
        with self.assertRaises(W.HelperUnavailable) as cm:
            d.prepare()
        self.assertEqual(cm.exception.code, "helper_unavailable")
        self.assertIn("install.sh", str(cm.exception))

    def test_stale_socket_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "s.sock")
        import socket
        s = socket.socket(socket.AF_UNIX)
        s.bind(path)
        s.close()   # file exists, nobody listening
        with self.assertRaises(W.HelperUnavailable):
            W.HelperDriver(W.HelperClient(path)).prepare()

    def test_prepare_checks_status(self):
        for status, exc in ((dict(STATUS_OK, trusted=False), W.AccessibilityDenied),
                            (dict(STATUS_OK, screen_locked=True), W.ScreenLocked),
                            (dict(STATUS_OK, wechat_running=False), W.WeChatNotRunning)):
            d = self.make(status=lambda r, st=status: st)
            with self.assertRaises(exc):
                d.prepare()
            self.assertNotIn("activate", self.server.ops())

    def test_error_code_mapping(self):
        d = self.make(chat_title=_err("not_trusted"), input_text=_err("screen_locked"),
                      clear_input=_err("wechat_not_running"), paste_input=_err("no_input"))
        with self.assertRaises(W.AccessibilityDenied) as cm:
            d.chat_title()
        self.assertIn("Accessibility", str(cm.exception))
        with self.assertRaises(W.ScreenLocked):
            d.input_text()
        with self.assertRaises(W.WeChatNotRunning):
            d.clear_input()
        with self.assertRaises(W.UIError) as cm:
            d.paste_into_input("x")
        self.assertEqual(cm.exception.extra["helper_error"], "no_input")

    def test_full_send_over_socket(self):
        state = {"title": None, "box": ""}

        def click(req):
            state["title"] = "Alice"
            return {"clicked": True}

        def paste(req):
            state["box"] = req["text"]
            return {"text": state["box"]}

        def send(req):
            if req["expected_title"] != state["title"] or req["expected_input"] != state["box"]:
                return {"ok": False, "error": {"code": "precondition_failed", "message": ""}}
            state["box"] = ""
            return {"pressed": True, "leftover": ""}

        d = self.make(
            open_search=lambda r: {"value_matches": True},
            search_results=lambda r: {"results": [
                {"text": "Chat History", "y": 90, "x": 0},
                {"text": "Alice", "y": 200, "x": 10},
                {"text": "Alice", "y": 120, "x": 10}]},
            click_result=click,
            chat_title=lambda r: {"title": state["title"]},
            input_text=lambda r: {"text": state["box"]},
            paste_input=paste, send=send)
        cfg = {"decrypted_dir": self.server.dir,
               "send_log": os.path.join(self.server.dir, "log.jsonl")}
        clock = Clock()
        sender = _RealSender(cfg, driver=d,
                             data_loader=lambda c: (dict(CONTACTS), list(CHAT_LIST)),
                             verifier=lambda *a: ("sent", None), clock=clock,
                             sleep=clock.sleep)
        res = sender.send_text("Alice", "hello")
        self.assertEqual(res["status"], "sent")
        self.assertEqual(self.server.ops(), [
            "status", "activate", "open_search", "search_results", "click_result",
            "chat_title", "input_text", "paste_input", "chat_title", "input_text",
            "send", "restore"])
        reqs = {r["op"]: r for r in self.server.requests}
        self.assertEqual(reqs["click_result"]["text"], "Alice")
        self.assertEqual(reqs["open_search"]["query"], "Alice")
        self.assertEqual(reqs["send"]["expected_title"], "Alice")
        self.assertEqual(reqs["send"]["expected_input"], "hello")
        self.assertEqual(reqs["send"]["key"], "enter")
        self.assertEqual(reqs["restore"]["pid"], 42)
        self.assertEqual(self.server.connections, 1)   # one connection per send

    def test_no_exact_result_uses_search_enter(self):
        d = self.make(open_search=lambda r: {}, search_enter=lambda r: {},
                      search_results=lambda r: {"results": [{"text": "Alicia", "y": 1}]})
        d.open_chat("Alice", ["Alice"], sleep=lambda s: None)
        self.assertEqual(self.server.ops()[-1], "search_enter")

    def test_send_precondition_failure_is_ui_error(self):
        d = self.make(send=_err("precondition_failed"))
        with self.assertRaises(W.UIError) as cm:
            d.press_send("enter", "t", "x")
        self.assertFalse(cm.exception.extra["maybe_sent"])

    def test_timeout_marks_maybe_sent(self):
        d = self.make(send="hang")
        orig = W._HELPER_TIMEOUTS["send"]
        W._HELPER_TIMEOUTS["send"] = 0.3
        try:
            with self.assertRaises(W.UIError) as cm:
                d.press_send("enter", "t", "x")
        finally:
            W._HELPER_TIMEOUTS["send"] = orig
        self.assertTrue(cm.exception.extra["maybe_sent"])

    def test_connection_closed(self):
        d = self.make(chat_title="close")
        with self.assertRaises(W.HelperUnavailable):
            d.chat_title()

    def test_response_id_mismatch(self):
        d = self.make(chat_title=lambda r: {"title": "x", "wrong_id": True})
        with self.assertRaises(W.UIError):
            d.chat_title()

    def test_helper_check(self):
        self.make(status=lambda r: dict(STATUS_OK, trusted=False, version="1"))
        info = W.helper_check(W.HelperClient(self.server.path), launchctl=lambda: True)
        self.assertTrue(info["running"])
        self.assertFalse(info["trusted"])
        self.assertTrue(info["launch_agent_loaded"])
        self.assertIn("Accessibility", info["hint"])
        info = W.helper_check(W.HelperClient("/nonexistent/s.sock"), launchctl=lambda: False)
        self.assertFalse(info["running"])
        self.assertEqual(info["error"], "helper_unavailable")

    def test_make_driver(self):
        self.assertIsInstance(W.make_driver({}), W.HelperDriver)
        with self.assertRaises(W.SendError):
            W.make_driver({"send_driver": "bogus"})


class ChooseResultTests(unittest.TestCase):
    def test_topmost_exact(self):
        rows = [{"text": "Alice ", "y": 300}, {"text": "Alice", "y": 100, "x": 5},
                {"text": "Alice2", "y": 50}]
        self.assertEqual(W.choose_result(rows, ["Alice"])["y"], 100)

    def test_none(self):
        self.assertIsNone(W.choose_result([{"text": "alice", "y": 1}], ["Alice"]))
        self.assertIsNone(W.choose_result([], ["Alice"]))

    def test_aliases(self):
        r = W.choose_result([{"text": "File Transfer", "y": 1}], ["文件传输助手", "File Transfer"])
        self.assertEqual(r["text"], "File Transfer")


class FakeVisionClient:
    """Scripted helper for VisionDriver: a 1400x868 WeChat window."""

    W, H = 1400, 868

    def __init__(self, title="Alice", popup=None, input_text=""):
        self.title = title
        self.input = input_text
        self.popup = popup if popup is not None else [
            {"text": "Contacts", "x": 30, "y": 10, "w": 60, "h": 10},
            {"text": "Alice", "x": 70, "y": 30, "w": 40, "h": 14},
        ]
        self.calls = []
        self.screen_capture = True

    def _window_items(self):
        items = [{"text": self.title, "x": 315, "y": 22, "w": 40, "h": 18},
                 {"text": "Search", "x": 101, "y": 28, "w": 43, "h": 11}]
        # chat list: names left, right-aligned times ending at x=291
        for k in range(5):
            items.append({"text": "name", "x": 117, "y": 80 + 70 * k, "w": 50, "h": 16})
            items.append({"text": "12:00", "x": 262, "y": 82 + 70 * k, "w": 29, "h": 11})
        # the other person's messages start close to the pane edge
        items.append({"text": "hi there", "x": 360, "y": 300, "w": 150, "h": 18})
        return items

    def call(self, op, **a):
        self.calls.append((op, a))
        if op == "v_ocr":
            if a.get("popup"):
                return {"window": [368, 404], "items": self.popup, "text": ""}
            rect = a.get("rect")
            items = self._window_items()
            if rect and rect[1] > self.H / 2:  # input box
                return {"window": [self.W, self.H], "items": [], "text": self.input}
            if rect:
                x, y, w, h = rect
                items = [i for i in items if x <= i["x"] < x + w and y <= i["y"] < y + h]
            return {"window": [self.W, self.H], "items": items,
                    "text": "\n".join(i["text"] for i in items)}
        if op == "status":
            return {"trusted": True, "screen_capture": self.screen_capture,
                    "screen_locked": False, "wechat_running": True}
        if op == "v_paste":
            self.input = a["text"]
        if op == "v_clear":
            self.input = ""
        if op == "v_send":
            if a["expected_input"] != self.input:
                raise W.UIError("mismatch")
            self.input = ""
            return {"leftover": ""}
        return {}

    def close(self):
        pass


class VisionDriverTests(unittest.TestCase):
    def driver(self, **kw):
        c = FakeVisionClient(**kw)
        return W.VisionDriver(c, sleep=lambda s: None), c

    def test_layout_finds_title_despite_messages_near_the_list(self):
        d, _ = self.driver()
        d._layout()
        self.assertEqual(d.chat_title(), "Alice")
        self.assertLess(d.title_rect[0], 315)
        self.assertGreater(d.input_rect[1], 868 / 2)

    def test_open_chat_clicks_exact_contact_row(self):
        d, c = self.driver()
        d.open_chat("Alice", ["Alice"])
        click = [a for op, a in c.calls if op == "v_click_popup"]
        self.assertEqual(click, [{"x": 90, "y": 37}])
        self.assertNotIn("v_search_enter", [op for op, _ in c.calls])

    def test_open_chat_ignores_non_chat_sections(self):
        popup = [{"text": "Internet search results", "x": 60, "y": 19, "w": 200, "h": 12},
                 {"text": "Alice", "x": 38, "y": 50, "w": 40, "h": 14},
                 {"text": "Chat History", "x": 35, "y": 220, "w": 90, "h": 12},
                 {"text": "Alice", "x": 80, "y": 257, "w": 40, "h": 14}]
        d, c = self.driver(popup=popup)
        with self.assertRaises(W.UIError):
            d.open_chat("Alice", ["Alice"])
        self.assertNotIn("v_click_popup", [op for op, _ in c.calls])
        self.assertIn("escape", [op for op, _ in c.calls])

    def test_pick_needs_exact_text(self):
        items = [{"text": "Group Chats", "x": 30, "y": 5, "w": 60, "h": 10},
                 {"text": "Alice Fans", "x": 70, "y": 30, "w": 60, "h": 14},
                 {"text": "Alice", "x": 70, "y": 60, "w": 40, "h": 14}]
        self.assertEqual(W.VisionDriver.pick_search_result(items, ["Alice"])["y"], 60)
        self.assertIsNone(W.VisionDriver.pick_search_result(items, ["Bob"]))

    def test_ocr_matches(self):
        msg = "[wechat_send test] 4: a longer message 中文，测试！"
        self.assertTrue(W.ocr_matches(msg.replace(" message", "\nmessage"), msg))
        self.assertTrue(W.ocr_matches(msg.replace("，", ","), msg))  # NFKC folding
        self.assertTrue(W.ocr_matches(msg.replace("longer", "1onger"), msg))  # one misread
        self.assertFalse(W.ocr_matches(msg[:20], msg))  # truncated read-back
        self.assertFalse(W.ocr_matches("hi", "ho"))  # short texts must be exact
        self.assertFalse(W.ocr_matches("", msg))

    def test_full_send_flow(self):
        d, c = self.driver()
        base = Base("setUp")
        base.setUp()
        try:
            res = base.sender(d).send_text("Alice", "hello there, this is a test")
        finally:
            base.tearDown()
        self.assertEqual(res["status"], "sent")
        send = [a for op, a in c.calls if op == "v_send"][0]
        self.assertEqual(send["expected_title"], "Alice")
        self.assertEqual(send["expected_input"], "hello there, this is a test")

    def test_needs_screen_capture(self):
        d, c = self.driver()
        c.screen_capture = False
        with self.assertRaises(W.AccessibilityDenied):
            d.prepare()

    def test_make_driver(self):
        self.assertIsInstance(W.make_driver({}), W.VisionDriver)
        d = W.make_driver({"send_driver": "helper_ax"})
        self.assertIs(type(d), W.HelperDriver)


if __name__ == "__main__":
    unittest.main()
