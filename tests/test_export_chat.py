"""Tests for the export_chat.py CLI on synthetic databases (no real data).

Run: ./venv/bin/python -m unittest discover -s tests
"""
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import export_chat as E  # noqa: E402
from test_chats import ALICE, BOB, IMAGE_MD5, ROOM, SELF, Fixture, tbl  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"


def ts_str(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.imgs = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.imgs.append(dict(attrs)["src"])


class ExportCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="export_cli_test_")
        self.dec = os.path.join(self.tmp, "decrypted")
        self.fx = Fixture(self.dec)
        self.out = os.path.join(self.tmp, "export")
        self.base = os.path.join(self.tmp, "wechat_base")
        cfg = {"decrypted_dir": self.dec, "self_wxid": SELF, "wechat_base_dir": self.base,
               "image_aes_key": "0123456789abcdef", "image_xor_key": 0}
        p = mock.patch.object(E, "get_config", return_value=cfg)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args, stdin=None):
        buf = io.StringIO()
        argv = list(args) + ["--no-decrypt", "--export-dir", self.out]
        with contextlib.redirect_stdout(buf), \
                mock.patch("builtins.input", side_effect=stdin or []):
            rc = E.main(argv)
        return rc, buf.getvalue()

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return f.read()

    def add_msg(self, username, local_type, sender, ts, content, db=0):
        conn = sqlite3.connect(self.fx.dbs[db])
        sid = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (sender,)).fetchone()[0]
        conn.execute(f"INSERT INTO {tbl(username)}(local_type, sort_seq, real_sender_id, "
                     "create_time, message_content) VALUES (?,?,?,?,?)",
                     (local_type, ts * 1000 + 999, sid, ts, content))
        conn.commit()
        conn.close()

    # -- list / pick ----------------------------------------------------------
    def test_list_columns_and_type_filter(self):
        rc, out = self.run_cli("--list")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("名称"))
        self.assertRegex(lines[1], r"^Test Group\s+\[群\]\s+8\s+19(70-01-01|69-12-31)$")
        self.assertRegex(lines[2], r"^Alice R\s+\[单\]\s+8\s+19(70-01-01|69-12-31)$")
        self.assertIn("共 2 个聊天（单聊 1，群聊 1）", out)
        _, out = self.run_cli("--list", "--type", "group")
        self.assertNotIn("Alice", out)
        _, out = self.run_cli("--list", "alice")
        self.assertNotIn("Test Group", out)

    def test_list_names_unnamed_group_like_mcp(self):
        conn = sqlite3.connect(os.path.join(self.dec, "contact", "contact.db"))
        conn.execute("UPDATE contact SET nick_name='' WHERE username=?", (ROOM,))
        conn.commit()
        conn.close()
        rc, out = self.run_cli("--list")
        self.assertEqual(rc, 0)
        self.assertRegex(out.splitlines()[1], r"^Bobby、Dave Stranger、carol_custom\s+\[群\]\s+8")
        rc, out = self.run_cli("--list", "dave stranger")
        self.assertIn("共 1 个聊天（单聊 0，群聊 1）", out)

    def test_pick_shows_group_marker(self):
        # "e" matches both "Alice R" and "Test Group"
        rc, out = self.run_cli("e", stdin=["x", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("1. Test Group [群]", out)
        self.assertIn("2. Alice R\n", out)
        self.assertIn("请输入 1 到 2 之间的数字", out)
        self.assertTrue(os.path.exists(os.path.join(self.out, "Test Group_chat.txt")))

    def test_system_chat_fallback(self):
        rc, out = self.run_cli("News Account")
        self.assertEqual(rc, 0)
        self.assertIn("promo", self.read("News Account_chat.txt"))
        rc, out = self.run_cli("nomatch")
        self.assertEqual(rc, 1)
        self.assertIn("[!] 未找到匹配「nomatch」的聊天", out)

    # -- txt ------------------------------------------------------------------
    def test_plain_txt_legacy_path_and_content(self):
        rc, out = self.run_cli("Alice R")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read("Alice R_chat.txt").splitlines(), [
            f"[{ts_str(100)}] Alice R: hi old",
            f"[{ts_str(101)}] 我: hello old",
            f"[{ts_str(200)}] Alice R: compressed hi",
            f"[{ts_str(201)}] 我: [图片]",
            f"[{ts_str(202)}] 系统: [系统消息] Alice recalled",
            f"[{ts_str(203)}] Alice R: quoted reply",
            f"[{ts_str(204)}] 我: [链接] a link",
        ])
        self.assertIn("已导出 7 条消息", out)

    def test_output_option_and_since_until(self):
        target = os.path.join(self.tmp, "custom", "o.txt")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            day = datetime.fromtimestamp(150).strftime("%Y-%m-%d")
            E.main(["Test Group", "-o", target, "--no-decrypt", "--since", day, "--until", day])
        with open(target, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 8)
        rc, out = self.run_cli("Test Group", "--since", "2030-01-01")
        self.assertIn("没有可导出的消息", out)

    def test_incremental_txt_boundary_tie(self):
        self.run_cli("Alice R", "-i")
        path = os.path.join(self.out, "Alice R.txt")
        first = self.read("Alice R.txt")
        self.assertEqual(len(first.splitlines()), 7)
        _, out = self.run_cli("Alice R", "-i")
        self.assertIn("没有新消息", out)
        self.assertEqual(self.read("Alice R.txt"), first)
        # Same second as the last exported message + a later one.
        self.add_msg(ALICE, 1, ALICE, 204, "same second")
        self.add_msg(ALICE, 1, SELF, 210, "later")
        _, out = self.run_cli("Alice R", "-i")
        self.assertIn("追加 2 条消息", out)
        self.assertEqual(self.read("Alice R.txt"), first + "".join([
            f"[{ts_str(204)}] Alice R: same second\n",
            f"[{ts_str(210)}] 我: later\n"]))
        self.assertTrue(os.path.exists(path))

    # -- formats --------------------------------------------------------------
    def test_all_formats_valid_and_incremental_stable(self):
        for fmt in E.FORMATS:
            rc, _ = self.run_cli("Test Group", "-i", "--format", fmt)
            self.assertEqual(rc, 0)
            path = os.path.join(self.out, f"Test Group.{fmt}")
            with open(path, "rb") as f:
                before = f.read()
            _, out = self.run_cli("Test Group", "-i", "--format", fmt)
            self.assertIn("没有新消息", out, fmt)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), before, fmt)
        with open(os.path.join(self.out, "Test Group.json"), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(len(doc["messages"]), 8)
        self.assertTrue(doc["chat"]["is_group"])
        self.assertNotIn("packed_info_data", doc["messages"][0])
        with open(os.path.join(self.out, "Test Group.csv"), encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["time", "sender", "is_self", "kind", "text"])
        self.assertEqual(len(rows), 9)
        HTMLParser().feed(self.read("Test Group.html"))
        md = self.read("Test Group.md")
        self.assertTrue(md.startswith("# Test Group\n"))
        self.assertRegex(md, r"\n## \d{4}-\d{2}-\d{2}\n")

    def test_incremental_append_formats(self):
        for fmt in ("md", "csv", "html", "json"):
            self.run_cli("Test Group", "-i", "-f", fmt)
        self.add_msg(ROOM, 1, BOB, 306, f"{BOB}:\ntie <b>")
        self.add_msg(ROOM, 1, SELF, 400, "newer")
        for fmt in ("md", "csv", "html", "json"):
            _, out = self.run_cli("Test Group", "-i", "-f", fmt)
            self.assertRegex(out, r"(追加 2 条|新增 2 条)", fmt)
            _, out = self.run_cli("Test Group", "-i", "-f", fmt)
            self.assertIn("没有新消息", out, fmt)
        md = self.read("Test Group.md")
        self.assertEqual(md.count("<!-- wechat-export:"), 1)
        self.assertTrue(md.endswith("<!-- wechat-export: last=400 n=1 -->\n"))
        self.assertIn("tie &lt;b>", md)
        self.assertEqual(md.count("## "), len(set(re.findall(r"^## (.+)$", md, re.M))))
        # Same as a fresh non-incremental render (minus the state comment).
        self.run_cli("Test Group", "-f", "md")
        fresh = self.read("Test Group_chat.md")
        self.assertEqual(md[:md.rindex("<!--")], fresh)
        with open(os.path.join(self.out, "Test Group.csv"), encoding="utf-8-sig", newline="") as f:
            self.assertEqual(len(list(csv.reader(f))), 11)
        with open(os.path.join(self.out, "Test Group.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["messages"]), 10)

    def test_all_type_group(self):
        rc, out = self.run_cli("--all", "--type", "group")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(os.listdir(self.out)), ["Test Group.txt"])
        self.assertIn("[1/1] Test Group [群]: +8", out)
        _, out = self.run_cli("--all")
        self.assertEqual(sorted(os.listdir(self.out)), ["Alice R.txt", "Test Group.txt"])
        self.assertIn("1 个聊天有新消息，共新增 7 条", out)

    def test_all_with_output_is_error(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            E.main(["--all", "-o", "x.txt", "--no-decrypt"])

    # -- naming ---------------------------------------------------------------
    def test_safe_filename_and_duplicates(self):
        self.assertEqual(E.safe_filename('a/b:c*?"<>|d'), "a_b_c______d")
        self.assertEqual(E.safe_filename(" .. "), "unnamed")
        chat = {"name": "Same", "username": "wxid_a/b"}
        self.assertEqual(E.chat_filename(chat, {"wxid_a/b": "Same", "x": "Same"}), "Same_wxid_a_b")
        self.assertEqual(E.chat_filename(chat, {"wxid_a/b": "Same"}), "Same")

    # -- legacy layout migration ------------------------------------------------
    def test_legacy_migration(self):
        legacy = os.path.join(self.out, "Alice R")
        os.makedirs(legacy)
        parts = {
            0: f"[{ts_str(100)}] Alice R: hi old\n",
            2: f"[{ts_str(200)}] Alice R: compressed hi\n",
            10: f"[{ts_str(201)}] 我: [图片]",  # no trailing newline
        }
        # Order must be numeric (0, 2, 10), not lexicographic.
        for i, text in parts.items():
            with open(os.path.join(legacy, f"output_{i}.txt"), "w", encoding="utf-8") as f:
                f.write(text)
        _, out = self.run_cli("Alice R", "-i")
        self.assertIn("合并到", out)
        self.assertIn("可以删除", out)
        self.assertIn("追加 3 条消息", out)
        self.assertEqual(self.read("Alice R.txt").splitlines(), [
            f"[{ts_str(100)}] Alice R: hi old",
            f"[{ts_str(200)}] Alice R: compressed hi",
            f"[{ts_str(201)}] 我: [图片]",
            f"[{ts_str(202)}] 系统: [系统消息] Alice recalled",
            f"[{ts_str(203)}] Alice R: quoted reply",
            f"[{ts_str(204)}] 我: [链接] a link",
        ])
        self.assertTrue(os.path.isdir(legacy))  # left in place
        _, out = self.run_cli("Alice R", "-i")
        self.assertNotIn("合并到", out)
        self.assertIn("没有新消息", out)

    # -- images ---------------------------------------------------------------
    def put_dat(self, username, create_time, suffix=""):
        month = time.strftime("%Y-%m", time.localtime(create_time))
        d = os.path.join(self.base, "msg", "attach", hashlib.md5(username.encode()).hexdigest(),
                         month, "Img")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{IMAGE_MD5}{suffix}.dat"), "wb") as f:
            f.write(bytes(b ^ 0x37 for b in JPEG))  # legacy single-byte XOR format

    def test_images_html_and_rerun(self):
        self.put_dat(ALICE, 201)
        _, out = self.run_cli("Alice R", "-i", "-f", "html", "--images")
        self.assertIn("新解码 1", out)
        img = os.path.join(self.out, "Alice R_files", f"{IMAGE_MD5}.jpg")
        with open(img, "rb") as f:
            self.assertEqual(f.read(), JPEG)
        tags = _Tags()
        tags.feed(self.read("Alice R.html"))
        self.assertEqual(tags.imgs, [f"Alice%20R_files/{IMAGE_MD5}.jpg"])
        _, out = self.run_cli("Alice R", "-i", "-f", "html", "--images")
        self.assertIn("新解码 0", out)
        self.assertIn("已存在 1", out)
        # Re-render without --images keeps the reference.
        _, out = self.run_cli("Alice R", "-i", "-f", "html")
        self.assertIn("没有新消息", out)
        # md embeds, txt keeps the label.
        self.run_cli("Alice R", "-f", "md", "--images")
        self.assertIn(f"![[图片]](Alice%20R_files/{IMAGE_MD5}.jpg)", self.read("Alice R_chat.md"))
        _, out = self.run_cli("Alice R", "--images")
        self.assertIn("txt 格式不嵌入图片", out)
        self.assertIn("我: [图片]", self.read("Alice R_chat.txt"))

    def test_images_missing_and_thumbnail_upgrade(self):
        _, out = self.run_cli("Alice R", "-f", "md", "--images")
        self.assertIn("缺失 1", out)
        self.assertIn("[!] 1 张图片未能导出", out)
        self.assertIn("**我** ", self.read("Alice R_chat.md"))
        self.put_dat(ALICE, 201, "_t")
        _, out = self.run_cli("Alice R", "-f", "md", "--images")
        self.assertIn("仅缩略图 1", out)
        files = os.path.join(self.out, "Alice R_files")
        self.assertEqual(os.listdir(files), [f"{IMAGE_MD5}_t.jpg"])
        self.put_dat(ALICE, 201)
        _, out = self.run_cli("Alice R", "-f", "md", "--images")
        self.assertIn("新解码 1（其中仅缩略图 0）", out)
        self.assertEqual(os.listdir(files), [f"{IMAGE_MD5}.jpg"])


if __name__ == "__main__":
    unittest.main()
