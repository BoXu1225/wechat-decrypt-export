"""Owner-only permissions: config.py helpers, the startup migration, and that
decrypt / export / MCP outputs end up 0700 (dirs) / 0600 (files).

Synthetic data in temp dirs only. Every test starts from umask 022 (the macOS
default, which is what left decrypted/ 755 and the keys 644) and restores the
process umask afterwards.

Run: ./venv/bin/python -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import config  # noqa: E402
import export_chat as E  # noqa: E402
import mcp_server as M  # noqa: E402
import test_decrypt_db as TD  # noqa: E402  (also imports decrypt_db with a mocked config)
from test_chats import SELF, Fixture  # noqa: E402

D = TD.D


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class PermTestCase(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o022)
        self.addCleanup(os.umask, self.old_umask)
        self.tmp = tempfile.mkdtemp(prefix="perms_test_")
        self.addCleanup(shutil.rmtree, self.tmp)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def make(self, rel, fmode=0o644, text="x"):
        path = self.p(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, fmode)
        return path

    def assertPrivateTree(self, top):
        """Every dir under top is 0700 and every regular file 0600 (symlinks ignored)."""
        self.assertEqual(mode(top), 0o700 if os.path.isdir(top) else 0o600, top)
        for root, dirs, files in os.walk(top):
            for name in dirs + files:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    continue
                want = 0o700 if os.path.isdir(path) else 0o600
                self.assertEqual(mode(path), want, f"{path}: {oct(mode(path))}")


class HelperTest(PermTestCase):
    def test_private_dir_creates_and_tightens(self):
        new = self.p("a", "b")
        self.assertEqual(config.private_dir(new), new)
        self.assertEqual(mode(new), 0o700)
        old = self.p("old")
        os.mkdir(old, 0o755)
        os.chmod(old, 0o755)
        config.private_dir(old)
        self.assertEqual(mode(old), 0o700)

    def test_write_private(self):
        path = self.make("k.json", 0o644, "old")
        config.write_private(path, '{"a": 1}')
        self.assertEqual(mode(path), 0o600)
        with open(path) as f:
            self.assertEqual(json.load(f), {"a": 1})
        new = self.p("new.json")
        config.write_private(new, "{}")
        self.assertEqual(mode(new), 0o600)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["k.json", "new.json"])  # no temp left

    def test_private_opener(self):
        path = self.p("log.jsonl")
        with open(path, "a", opener=config.private_opener) as f:
            f.write("x\n")
        self.assertEqual(mode(path), 0o600)

    def test_chmod_private_keeps_owner_bits(self):
        exe = self.make("run.sh", 0o755)
        ro = self.make("ro.txt", 0o444)
        self.assertTrue(config.chmod_private(exe))
        self.assertTrue(config.chmod_private(ro))
        self.assertEqual(mode(exe), 0o700)
        self.assertEqual(mode(ro), 0o400)
        self.assertFalse(config.chmod_private(exe))            # already private
        self.assertFalse(config.chmod_private(self.p("nope")))  # missing

    def test_tighten_tree_skips_symlinks(self):
        outside = self.make("outside/secret.txt", 0o644)
        os.chmod(self.p("outside"), 0o755)
        self.make("tree/sub/a.db", 0o644)
        self.make("tree/b.txt", 0o664)
        os.chmod(self.p("tree", "sub"), 0o755)
        os.chmod(self.p("tree"), 0o755)
        os.symlink(outside, self.p("tree", "link.txt"))
        os.symlink(self.p("outside"), self.p("tree", "linkdir"))
        self.assertEqual(config.tighten_tree(self.p("tree")), 4)
        self.assertPrivateTree(self.p("tree"))
        self.assertEqual(mode(outside), 0o644)           # symlink targets untouched
        self.assertEqual(mode(self.p("outside")), 0o755)

    def test_tighten_tree_only_walks_when_top_is_open(self):
        self.make("tree/inner.txt", 0o644)
        os.chmod(self.p("tree"), 0o700)
        self.assertEqual(config.tighten_tree(self.p("tree")), 0)
        self.assertEqual(mode(self.p("tree", "inner.txt")), 0o644)


class MigrationTest(PermTestCase):
    """secure_outputs() on a fake project root, as left behind by older versions."""

    def setUp(self):
        super().setUp()
        self.root = self.p("repo")
        self.wechat = self.p("xwechat_files", "wxid_x_1a2b")
        for rel in ("repo/config.json", "repo/all_keys.json", "repo/decrypted/.decrypt_state.json",
                    "repo/decrypted/message/message_0.db", "repo/decrypted/mcp_index.db",
                    "repo/export/Alice.txt", "repo/export/Alice_files/1.jpg",
                    "repo/logs/mcp_access.jsonl", "repo/logs/send_log.jsonl",
                    "repo/chats.py", "xwechat_files/wxid_x_1a2b/db_storage/message/message_0.db"):
            self.make(rel, 0o644)
        for root, dirs, _ in os.walk(self.tmp):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
        self.cfg = {"db_dir": os.path.join(self.wechat, "db_storage"),
                    "wechat_base_dir": self.wechat,
                    "keys_file": os.path.join(self.root, "all_keys.json"),
                    "decrypted_dir": os.path.join(self.root, "decrypted"),
                    "decoded_image_dir": os.path.join(self.root, "decoded_images")}
        for p in (mock.patch.object(config, "PROJECT_ROOT", self.root),
                  mock.patch.object(config, "CONFIG_FILE", os.path.join(self.root, "config.json"))):
            p.start()
            self.addCleanup(p.stop)

    def run_secure(self, cfg):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            n = config.secure_outputs(cfg)
        return n, err.getvalue()

    def test_migration(self):
        n, msg = self.run_secure(self.cfg)
        self.assertEqual(os.umask(0o077), 0o077)   # entry points leave umask 077
        self.assertGreater(n, 0)
        self.assertIn("已将", msg)
        for top in ("config.json", "all_keys.json", "decrypted", "export", "logs"):
            self.assertPrivateTree(os.path.join(self.root, top))
        # Not ours to touch: the source tree and WeChat's own files.
        self.assertEqual(mode(self.root), 0o755)
        self.assertEqual(mode(os.path.join(self.root, "chats.py")), 0o644)
        wx_db = os.path.join(self.wechat, "db_storage", "message", "message_0.db")
        self.assertEqual(mode(wx_db), 0o644)
        self.assertEqual(mode(os.path.join(self.wechat, "db_storage")), 0o755)
        # Second run: nothing to do, silent.
        self.assertEqual(self.run_secure(self.cfg), (0, ""))

    def test_refuses_wechat_and_project_dirs(self):
        cfg = dict(self.cfg, decrypted_dir=self.wechat,          # misconfigured
                   decoded_image_dir=self.root,
                   keys_file=os.path.join(self.wechat, "db_storage", "message", "message_0.db"))
        self.run_secure(cfg)
        wx_db = os.path.join(self.wechat, "db_storage", "message", "message_0.db")
        self.assertEqual(mode(wx_db), 0o644)
        self.assertEqual(mode(self.wechat), 0o755)
        self.assertEqual(mode(self.root), 0o755)
        self.assertEqual(mode(os.path.join(self.root, "chats.py")), 0o644)

    def test_relative_mcp_paths_resolve_against_project(self):
        self.make("repo/idx/i.db", 0o644)
        self.make("repo/idx/i.db-wal", 0o644)
        self.run_secure({"mcp_index_path": "idx/i.db", "mcp_access_log": "logs/other.jsonl"})
        self.assertEqual(mode(os.path.join(self.root, "idx", "i.db")), 0o600)
        self.assertEqual(mode(os.path.join(self.root, "idx", "i.db-wal")), 0o600)


class DecryptOutputTest(PermTestCase):
    """decrypt_db writes private files even when the process umask is 022."""

    def setUp(self):
        super().setUp()
        self.db_dir = self.p("db_storage")
        self.out = self.p("decrypted")
        self.keys_file = self.p("all_keys.json")
        os.makedirs(os.path.join(self.db_dir, "message"))
        plain = self.p("plain.db")
        conn = TD.new_plain_db(plain)
        TD.add_rows(conn, 0, 10)
        conn.close()
        self.src = os.path.join(self.db_dir, "message", "message_0.db")
        TD.encrypt_db(TD.snapshot(plain), self.src)
        os.chmod(self.src, 0o644)

    def test_decrypt_database_and_state(self):
        out = os.path.join(self.out, "message", "message_0.db")
        with contextlib.redirect_stdout(io.StringIO()):
            sig = D.decrypt_database(self.src, out, TD.KEY)
        self.assertTrue(sig)
        D.save_state(self.out, {"message/message_0.db": sig})
        self.assertPrivateTree(self.out)
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "message"))), ["message_0.db"])

    def test_main_tightens_old_output_and_writes_private(self):
        # Output left by an older version: 755 dirs, 644 files, 644 keys.
        stale = os.path.join(self.out, "contact", "contact.db")
        os.makedirs(os.path.dirname(stale))
        with open(stale, "wb") as f:
            f.write(b"old")
        os.chmod(stale, 0o644)
        os.chmod(self.out, 0o755)
        os.chmod(os.path.dirname(stale), 0o755)
        with open(self.keys_file, "w") as f:
            json.dump({"message/message_0.db": {"enc_key": TD.KEY.hex()}}, f)
        os.chmod(self.keys_file, 0o644)
        os.utime(self.keys_file, (time.time() + 100,) * 2)
        cfg = {"db_dir": self.db_dir, "decrypted_dir": self.out, "keys_file": self.keys_file}
        with mock.patch.object(D, "DB_DIR", self.db_dir), mock.patch.object(D, "OUT_DIR", self.out), \
                mock.patch.object(D, "KEYS_FILE", self.keys_file), mock.patch.object(D, "_cfg", cfg), \
                mock.patch.object(config, "PROJECT_ROOT", self.p("repo")), \
                mock.patch.object(config, "CONFIG_FILE", self.p("repo", "config.json")), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            D.main()
        self.assertIn("1 成功", out.getvalue())
        self.assertPrivateTree(self.out)
        self.assertEqual(mode(self.keys_file), 0o600)
        self.assertEqual(mode(self.src), 0o644)          # WeChat's file untouched

    def test_ensure_keys_rewrites_keys_private(self):
        # Scanner wrote a world-readable file (old scanner / root umask 022).
        def fake_scanner():
            with open(self.keys_file, "w") as f:
                json.dump({"message/message_0.db": {"enc_key": TD.KEY.hex()}}, f)
            os.chmod(self.keys_file, 0o644)

        with mock.patch.object(D, "DB_DIR", self.db_dir), \
                mock.patch.object(D, "KEYS_FILE", self.keys_file), \
                mock.patch.object(D, "run_key_scanner", side_effect=fake_scanner), \
                contextlib.redirect_stdout(io.StringIO()):
            keys = D.ensure_keys()
        self.assertIn("message/message_0.db", keys)
        self.assertEqual(mode(self.keys_file), 0o600)


class ExportOutputTest(PermTestCase):
    def setUp(self):
        super().setUp()
        self.dec = self.p("decrypted")
        Fixture(self.dec)
        self.out = self.p("export")
        cfg = {"decrypted_dir": self.dec, "self_wxid": SELF,
               "wechat_base_dir": self.p("wechat_base")}
        for p in (mock.patch.object(E, "get_config", return_value=cfg),
                  mock.patch.object(config, "PROJECT_ROOT", self.p("repo")),
                  mock.patch.object(config, "CONFIG_FILE", self.p("repo", "config.json"))):
            p.start()
            self.addCleanup(p.stop)

    def test_export_outputs_private(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(E.main(["--all", "--no-decrypt", "--export-dir", self.out]), 0)
            self.assertEqual(E.main(["--all", "--no-decrypt", "--export-dir", self.out,
                                     "--format", "html"]), 0)
            target = self.p("single", "alice.md")
            self.assertEqual(E.main(["Alice", "--no-decrypt", "-o", target,
                                     "--format", "md"]), 0)
        self.assertTrue(os.listdir(self.out))
        self.assertPrivateTree(self.out)
        self.assertEqual(mode(target), 0o600)
        self.assertEqual(mode(os.path.dirname(target)), 0o700)

    def test_default_export_dir_migrated(self):
        old = self.make("repo/export/Old.txt", 0o644)
        os.chmod(self.p("repo", "export"), 0o755)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            E.main(["--list", "--no-decrypt"])
        self.assertEqual(mode(old), 0o600)
        self.assertEqual(mode(self.p("repo", "export")), 0o700)


class McpOutputTest(PermTestCase):
    def test_index_and_access_log_private(self):
        Fixture(self.p("decrypted"))
        data = M.WeChatData({"decrypted_dir": self.p("decrypted"), "self_wxid": SELF,
                             "wechat_base_dir": self.p("wechat"),
                             "mcp_index_path": self.p("index", "idx.db"),
                             "mcp_auto_refresh_minutes": 0})
        with contextlib.redirect_stdout(io.StringIO()):
            data.sync_index()
        self.addCleanup(lambda: data._index and data._index.close())
        log = M.AccessLog(self.p("logs", "mcp_access.jsonl"))
        log.write("list_chats", {}, 1, 3)
        self.assertPrivateTree(self.p("index"))
        self.assertPrivateTree(self.p("logs"))


if __name__ == "__main__":
    unittest.main()
