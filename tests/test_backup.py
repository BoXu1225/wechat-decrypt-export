"""Tests for backup.py: config parsing, lock, no-sudo decryption, export orchestration,
run summary, notifications and LaunchAgent plist generation. No real data, no launchctl.

Run: ./venv/bin/python -m unittest discover -s tests
"""
import contextlib
import datetime as dt
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import backup as B  # noqa: E402
from test_decrypt_db import D, KEY, add_rows, encrypt_db, new_plain_db, rows, snapshot  # noqa: E402

HELP_BASE = "usage: export_chat.py [-h] [-i] [-f {txt,md,html,json,csv}] [--all] " \
            "[--images] [--export-dir EXPORT_DIR] [--no-decrypt]\n"
SECRET_NAME = "张三的秘密群"


def read(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class FakeRunner:
    """Stands in for subprocess.run: answers --help, fakes export runs, records everything."""

    def __init__(self, help_text=HELP_BASE, export_rc=0, export_out=None):
        self.help_text = help_text
        self.export_rc = export_rc
        self.export_out = export_out if export_out is not None else (
            f"[+] 共 3 个聊天\n  [1/3] {SECRET_NAME} [群]: +5\n  [2/3] 李四: +2\n"
            "[+] 完成: 2 个聊天有新消息，共新增 7 条\n")
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if "--help" in cmd:
            return SimpleNamespace(returncode=0, stdout=self.help_text, stderr="")
        if cmd[0].endswith("osascript"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=self.export_rc, stdout=self.export_out,
                               stderr="Traceback: boom" if self.export_rc else "")

    def exports(self):
        return [c for c in self.calls if "--all" in c]

    def notifications(self):
        return [c for c in self.calls if c[0].endswith("osascript")]


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = B.load_backup_config({}, root="/repo")
        self.assertEqual(cfg["formats"], ["html", "txt"])
        self.assertTrue(cfg["media"])
        self.assertEqual(cfg["dir"], "/repo/export")
        self.assertEqual((cfg["hour"], cfg["minute"], cfg["time"]), (3, 30, "03:30"))
        self.assertEqual(B.load_backup_config({"backup": None}, root="/repo")["dir"], "/repo/export")

    def test_custom(self):
        cfg = B.load_backup_config({"db_dir": "x", "backup": {
            "formats": ["TXT", "md", "txt"], "media": False, "dir": "~/bk", "time": "4:05"}},
            root="/repo")
        self.assertEqual(cfg["formats"], ["txt", "md"])
        self.assertFalse(cfg["media"])
        self.assertEqual(cfg["dir"], os.path.expanduser("~/bk"))
        self.assertEqual(cfg["time"], "04:05")
        self.assertEqual(B.load_backup_config({"backup": {"formats": "json", "dir": "/abs/d"}},
                                              root="/r")["formats"], ["json"])
        self.assertEqual(B.load_backup_config({"backup": {"dir": "a/../b"}}, root="/r")["dir"], "/r/b")

    def test_invalid(self):
        for bad in ({"formats": ["pdf"]}, {"formats": []}, {"formats": 3}, {"time": "25:00"},
                    {"time": "3:5"}, {"time": "noon"}, {"media": "yes"}, {"dir": ""},
                    {"dir": 5}, {"colour": "red"}):
            with self.subTest(bad=bad), self.assertRaises(B.ConfigError):
                B.load_backup_config({"backup": bad})
        with self.assertRaises(B.ConfigError):
            B.load_backup_config({"backup": ["html"]})

    def test_read_raw_config(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, "config.json")
        self.assertEqual(B.read_raw_config(p), {})
        with open(p, "w") as f:
            f.write("{broken")
        with self.assertRaises(B.ConfigError):
            B.read_raw_config(p)

    def test_overrides(self):
        args = B.build_parser().parse_args([])
        raw = {"backup": {"dir": "/from/config", "formats": ["md"]}}
        self.assertEqual(B.resolve_config(args, raw, env={})["dir"], "/from/config")
        self.assertEqual(B.resolve_config(args, raw, env={"WECHAT_BACKUP_DIR": "/env"})["dir"], "/env")
        args = B.build_parser().parse_args(["--dir", "/flag", "--formats", "txt,csv", "--no-media"])
        cfg = B.resolve_config(args, raw, env={"WECHAT_BACKUP_DIR": "/env"})
        self.assertEqual((cfg["dir"], cfg["formats"], cfg["media"]), ("/flag", ["txt", "csv"], False))
        self.assertEqual(raw, {"backup": {"dir": "/from/config", "formats": ["md"]}})  # not mutated
        # No "backup" section in config.json at all: flags / env still apply.
        self.assertEqual(B.resolve_config(args, {"db_dir": "x"}, env={})["dir"], "/flag")
        args = B.build_parser().parse_args([])
        self.assertEqual(B.resolve_config(args, {}, env={"WECHAT_BACKUP_DIR": "/env"})["dir"], "/env")

    def test_main_config_error_exit_code(self):
        with mock.patch.object(B, "read_raw_config", return_value={"backup": {"time": "99:99"}}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(B.main([]), B.EXIT_CONFIG)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.lock = os.path.join(self.tmp, "logs", "backup.lock")

    def test_exclusive_and_released(self):
        with B.run_lock(self.lock):
            with self.assertRaises(B.Locked):
                with B.run_lock(self.lock):
                    pass
        with B.run_lock(self.lock):   # released after the first run
            pass

    def test_held_by_other_process(self):
        os.makedirs(os.path.dirname(self.lock))
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT);"
             "fcntl.flock(fd, fcntl.LOCK_EX); print('ok', flush=True); time.sleep(30)", self.lock],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "ok")
        summary = os.path.join(self.tmp, "logs", "backup.jsonl")
        with mock.patch.object(B, "LOCK_FILE", self.lock), \
                mock.patch.object(B, "SUMMARY_FILE", summary), \
                mock.patch.object(B, "read_raw_config", return_value={}), \
                mock.patch.object(B, "run_backup") as run, quiet():
            self.assertEqual(B.main(["--no-notify"]), B.EXIT_LOCKED)
        run.assert_not_called()
        self.assertEqual(B.last_summary(summary)["status"], "locked")


class DecryptTestBase(unittest.TestCase):
    """A synthetic encrypted message DB in a temp db_storage, decrypt_db pointed at it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup_test_")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db_dir = os.path.join(self.tmp, "db_storage")
        self.out = os.path.join(self.tmp, "decrypted")
        self.keys_file = os.path.join(self.tmp, "all_keys.json")
        os.makedirs(os.path.join(self.db_dir, "message"))
        plain = os.path.join(self.tmp, "plain.db")
        conn = new_plain_db(plain)
        add_rows(conn, 0, 10)
        conn.close()
        self.src = os.path.join(self.db_dir, "message", "message_0.db")
        encrypt_db(snapshot(plain), self.src)
        for p in (mock.patch.object(D, "DB_DIR", self.db_dir),
                  mock.patch.object(D, "OUT_DIR", self.out),
                  mock.patch.object(D, "KEYS_FILE", self.keys_file),
                  mock.patch.object(B, "SUMMARY_FILE", os.path.join(self.tmp, "logs", "backup.jsonl")),
                  mock.patch.object(B, "LOCK_FILE", os.path.join(self.tmp, "logs", "backup.lock"))):
            p.start()
            self.addCleanup(p.stop)
        # No-sudo guarantee: any real subprocess call, and the key scanner, are traps.
        self.scanner = mock.MagicMock(name="run_key_scanner")
        self.ensure = mock.MagicMock(name="ensure_keys")
        self.real_subprocess = mock.MagicMock(name="subprocess.run")
        for p in (mock.patch.object(D, "run_key_scanner", self.scanner),
                  mock.patch.object(D, "ensure_keys", self.ensure),
                  mock.patch("subprocess.run", self.real_subprocess),
                  mock.patch("subprocess.Popen", self.real_subprocess),
                  mock.patch("os.system", self.real_subprocess)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.scanner.assert_not_called()
        self.ensure.assert_not_called()
        self.real_subprocess.assert_not_called()

    def write_keys(self, key_hex, mtime):
        with open(self.keys_file, "w") as f:
            json.dump({"_db_dir": self.db_dir, "message/message_0.db": {"enc_key": key_hex}}, f)
        os.utime(self.keys_file, (mtime, mtime))

    def cfg(self, **kw):
        return B.load_backup_config({"backup": {"dir": os.path.join(self.tmp, "export"), **kw}})

    def run_backup(self, runner, **kw):
        with quiet():
            return B.run_backup(self.cfg(**kw), runner=runner, decrypt_module=D)

    def assert_no_sudo(self, runner):
        for c in runner.calls:
            self.assertNotIn("sudo", c)
            self.assertFalse(any("find_all_keys" in a for a in c), c)


class DecryptChangedTest(DecryptTestBase):
    def test_decrypts_then_unchanged(self):
        self.write_keys(KEY.hex(), time.time() + 100)
        res = B.decrypt_changed(D)
        self.assertEqual((res["decrypted"], res["unchanged"], res["failed"], res["needs_key"]),
                         (1, 0, [], []))
        self.assertEqual(len(rows(os.path.join(self.out, "message", "message_0.db"))), 10)
        res = B.decrypt_changed(D)
        self.assertEqual((res["decrypted"], res["unchanged"]), (0, 1))

    def test_stale_key_skipped_not_scanned(self):
        self.write_keys("22" * 32, time.time() - 100)   # older than the DB, wrong key
        res = B.decrypt_changed(D)
        self.assertEqual(res["needs_key"], ["message/message_0.db"])
        self.assertEqual(res["decrypted"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.out, "message", "message_0.db")))

    def test_missing_keys_file(self):
        res = B.decrypt_changed(D)
        self.assertEqual(res["needs_key"], ["message/message_0.db"])

    def test_keyless_db_unchanged_since_scan_is_not_an_alert(self):
        with open(self.keys_file, "w") as f:
            json.dump({"other.db": {"enc_key": "00" * 32}}, f)
        os.utime(self.keys_file, (time.time() + 100,) * 2)
        res = B.decrypt_changed(D)
        self.assertEqual((res["needs_key"], res["no_key"]), ([], ["message/message_0.db"]))

    def test_scanner_is_trapped_during_decrypt(self):
        with B.no_key_scanner(D):
            with self.assertRaises(RuntimeError):
                D.run_key_scanner()
            with self.assertRaises(RuntimeError):
                D.ensure_keys()
        self.assertIs(D.run_key_scanner, self.scanner)   # restored


class RunBackupTest(DecryptTestBase):
    def test_success(self):
        self.write_keys(KEY.hex(), time.time() + 100)
        runner = FakeRunner()
        code, entry = self.run_backup(runner)
        self.assertEqual(code, B.EXIT_OK)
        self.assert_no_sudo(runner)
        exports = runner.exports()
        self.assertEqual([c[c.index("-f") + 1] for c in exports], ["html", "txt"])
        for c in exports:
            for flag in ("--all", "-i", "--no-decrypt", "--images"):
                self.assertIn(flag, c)
            self.assertEqual(c[c.index("--export-dir") + 1], os.path.join(self.tmp, "export"))
        self.assertEqual(runner.notifications(), [])      # quiet on success
        self.assertEqual((entry["chats_updated"], entry["messages_added"]), (2, 7))
        self.assertEqual(entry["decrypt"]["decrypted"], 1)
        # Summary on disk, one line per run, counts only.
        with open(B.SUMMARY_FILE) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        saved = json.loads(lines[0])
        for k in ("time", "duration_s", "chats_updated", "messages_added", "skipped_dbs",
                  "errors", "status", "exit_code", "exports"):
            self.assertIn(k, saved)
        self.assertEqual(saved["status"], "ok")
        self.assertNotIn(SECRET_NAME, lines[0])
        self.assertNotIn("李四", lines[0])
        # Second run: DB unchanged.
        _, entry2 = self.run_backup(FakeRunner(export_out="[+] 完成: 0 个聊天有新消息，共新增 0 条\n"))
        self.assertEqual((entry2["decrypt"]["unchanged"], entry2["messages_added"]), (1, 0))
        self.assertEqual(len(read(B.SUMMARY_FILE).splitlines()), 2)

    def test_media_flag_detection(self):
        self.write_keys(KEY.hex(), time.time() + 100)
        runner = FakeRunner(help_text=HELP_BASE + "  --media  导出媒体\n")
        _, entry = self.run_backup(runner, formats=["txt"])
        self.assertEqual(entry["media"], "--media")
        self.assertIn("--media", runner.exports()[0])
        self.assertNotIn("--images", runner.exports()[0])
        runner = FakeRunner()
        _, entry = self.run_backup(runner, formats=["txt"], media=False)
        self.assertIsNone(entry["media"])
        self.assertFalse({"--media", "--images"} & set(runner.exports()[0]))
        runner = FakeRunner(help_text="usage: [--all] [-i] [--export-dir D] [--no-decrypt]")
        _, entry = self.run_backup(runner, formats=["txt"])
        self.assertIsNone(entry["media"])   # neither flag supported -> none passed

    def test_needs_keys_notifies_and_exit_4(self):
        self.write_keys("22" * 32, time.time() - 100)
        runner = FakeRunner()
        code, entry = self.run_backup(runner)
        self.assertEqual(code, B.EXIT_NEEDS_KEYS)
        self.assertEqual(entry["status"], "needs_keys")
        self.assertEqual(entry["skipped_dbs"], ["message/message_0.db"])
        self.assertEqual(len(runner.exports()), 2)          # still exports existing data
        notes = runner.notifications()
        self.assertEqual(len(notes), 1)
        self.assertIn("./wechat decrypt", notes[0][-1])
        self.assert_no_sudo(runner)

    def test_export_failure(self):
        self.write_keys(KEY.hex(), time.time() + 100)
        runner = FakeRunner(export_rc=1, export_out="")
        with contextlib.redirect_stderr(io.StringIO()):
            code, entry = self.run_backup(runner)
        self.assertEqual(code, B.EXIT_FAIL)
        self.assertEqual(entry["status"], "error")
        self.assertEqual(len(entry["errors"]), 2)
        self.assertEqual(len(runner.notifications()), 1)
        self.assertIn("备份失败", runner.notifications()[0][-1])
        self.assertNotIn("Traceback", read(B.SUMMARY_FILE))

    def test_refuses_export_without_no_decrypt(self):
        # An export CLI that would decrypt (and possibly sudo) itself must never be run.
        self.write_keys(KEY.hex(), time.time() + 100)
        runner = FakeRunner(help_text="usage: [--all] [-i] [--export-dir D]")
        code, entry = self.run_backup(runner)
        self.assertEqual(code, B.EXIT_FAIL)
        self.assertEqual(runner.exports(), [])

    def test_notify_disabled(self):
        self.write_keys("22" * 32, time.time() - 100)
        runner = FakeRunner()
        with quiet():
            B.run_backup(self.cfg(), runner=runner, decrypt_module=D, notify_enabled=False)
        self.assertEqual(runner.notifications(), [])

    def test_decrypt_crash_is_recorded(self):
        self.write_keys(KEY.hex(), time.time() + 100)
        with mock.patch.object(B, "decrypt_changed", side_effect=OSError("disk gone")):
            code, entry = self.run_backup(FakeRunner())
        self.assertEqual(code, B.EXIT_FAIL)
        self.assertTrue(any("disk gone" in e for e in entry["errors"]))


class ParseOutputTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(B.parse_export_output("x\n[+] 完成: 3 个聊天有新消息，共新增 12 条\n"), (3, 12))
        self.assertEqual(B.parse_export_output("  [1/9] a: +4\n  [5/9] b [群]: +1\n"), (2, 5))
        self.assertEqual(B.parse_export_output("[!] 没有找到聊天记录\n"), (0, 0))
        self.assertEqual(B.parse_export_output("???"), (None, None))

    def test_notification_text(self):
        self.assertIsNone(B.notification_text({"status": "ok", "errors": [], "needs_key": []}))
        t = B.notification_text({"status": "error", "errors": ["a"], "needs_key": ["x", "y"]})
        self.assertIn("备份失败", t)
        self.assertIn("2 个数据库", t)

    def test_applescript_escaping(self):
        runner = FakeRunner()
        B.notify('a "b" \\ c', runner=runner)
        self.assertEqual(runner.calls[0][-1],
                         'display notification "a \\"b\\" \\\\ c" with title "微信聊天备份"')

    def test_real_export_cli_capabilities(self):
        caps = B.export_capabilities()
        for flag in ("--all", "--no-decrypt", "--export-dir", "--format"):
            self.assertIn(flag, caps)


class PlistTest(unittest.TestCase):
    def test_plist(self):
        pl = plistlib.loads(B.build_plist(3, 30, python="/r/venv/bin/python", root="/r"))
        self.assertEqual(pl["Label"], "local.wechat-decrypt-export.backup")
        self.assertEqual(pl["ProgramArguments"], ["/r/venv/bin/python", "/r/backup.py", "--scheduled"])
        self.assertEqual(pl["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        self.assertEqual(pl["ProcessType"], "Background")
        self.assertGreater(pl["Nice"], 0)
        self.assertTrue(pl["LowPriorityIO"])
        self.assertFalse(pl["RunAtLoad"])
        self.assertEqual(pl["WorkingDirectory"], "/r")
        log = os.path.expanduser("~/Library/Logs/wechat-decrypt-export/backup.log")
        self.assertEqual((pl["StandardOutPath"], pl["StandardErrorPath"]), (log, log))
        self.assertNotIn("--dir", pl["ProgramArguments"])
        pl = plistlib.loads(B.build_plist(23, 5, export_dir="/x y/bk", python="p", root="/r"))
        self.assertEqual(pl["ProgramArguments"][-2:], ["--dir", "/x y/bk"])
        self.assertEqual(pl["StartCalendarInterval"], {"Hour": 23, "Minute": 5})

    def test_install_uses_launchctl_idempotently(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        plist = os.path.join(tmp, "LaunchAgents", "x.plist")
        calls = []
        loaded = {"v": True}

        def fake_launchctl(*args):
            calls.append(args)
            if args[0] == "print":
                return SimpleNamespace(returncode=0 if loaded["v"] else 113, stdout="", stderr="")
            if args[0] == "bootout":
                loaded["v"] = False
            if args[0] == "bootstrap":
                loaded["v"] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        cfg = B.load_backup_config({})
        with mock.patch.object(B, "PLIST_PATH", plist), \
                mock.patch.object(B, "LAUNCHD_LOG_DIR", os.path.join(tmp, "Logs")), \
                mock.patch.object(B, "_launchctl", fake_launchctl), quiet():
            self.assertEqual(B.install(cfg), 0)
            self.assertEqual(B.install(cfg), 0)
            verbs = [c[0] for c in calls if c[0] != "print"]
            self.assertEqual(verbs, ["bootout", "bootstrap", "bootout", "bootstrap"])
            self.assertEqual(plistlib.loads(read(plist, "rb"))["StartCalendarInterval"],
                             {"Hour": 3, "Minute": 30})
            B.uninstall()
            self.assertFalse(os.path.exists(plist))
            self.assertFalse(loaded["v"])

    def test_next_run(self):
        now = dt.datetime(2026, 9, 23, 2, 0)
        self.assertEqual(B.next_run(3, 30, now), dt.datetime(2026, 9, 23, 3, 30))
        now = dt.datetime(2026, 9, 23, 3, 30)
        self.assertEqual(B.next_run(3, 30, now), dt.datetime(2026, 9, 24, 3, 30))


if __name__ == "__main__":
    unittest.main()
