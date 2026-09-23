"""Tests for decrypt_db.py with synthetic SQLCipher-4 databases and WAL files.

Builds plaintext SQLite databases (page_size 4096, reserve 80 like WeChat's),
encrypts them page by page with a known key, writes WAL files by hand
(committed / uncommitted transactions, stale salts, corrupted frames) and checks
that decrypt_database produces exactly the expected plaintext database.

Run: python -m unittest discover -s tests
"""
import contextlib
import hashlib
import hmac
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

from Crypto.Cipher import AES

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

if "decrypt_db" in sys.modules:
    D = sys.modules["decrypt_db"]
else:
    # decrypt_db reads config.json at import time; don't depend on a real one.
    with mock.patch("config.load_config", return_value={
            "db_dir": "/nonexistent", "decrypted_dir": "/nonexistent",
            "keys_file": "/nonexistent/keys.json"}):
        import decrypt_db as D  # noqa: E402

PAGE = D.PAGE_SZ
USABLE = PAGE - D.RESERVE_SZ
KEY = bytes(range(32))
SALT = bytes(range(100, 116))
MAC_KEY = D.derive_mac_key(KEY, SALT)


# -- building plaintext databases -------------------------------------------------

def new_plain_db(path):
    """Empty database with page_size 4096 and 80 reserved bytes per page."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("VACUUM")
    conn.close()
    with open(path, "r+b") as f:
        hdr = bytearray(f.read(PAGE))
        assert len(hdr) == PAGE and hdr[20] == 0
        hdr[20] = D.RESERVE_SZ                        # reserved bytes per page
        hdr[105:107] = struct.pack(">H", USABLE)      # empty page 1: content starts at usable end
        hdr[18] = hdr[19] = 1
        f.seek(0)
        f.write(hdr)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE msg(id INTEGER PRIMARY KEY, create_time INTEGER, body TEXT)")
    conn.execute("CREATE INDEX msg_time ON msg(create_time)")
    return conn


def add_rows(conn, start, n, size=300):
    conn.execute("BEGIN")
    for i in range(start, start + n):
        conn.execute("INSERT INTO msg VALUES (?,?,?)", (i, 1_700_000_000 + i, f"m{i}:" + "x" * size))
    conn.execute("COMMIT")


def snapshot(path):
    """List of plaintext pages of a (rollback-journal) database file."""
    with open(path, "rb") as f:
        data = f.read()
    assert len(data) % PAGE == 0
    return [data[i:i + PAGE] for i in range(0, len(data), PAGE)]


def read(path):
    with open(path, "rb") as f:
        return f.read()


def rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, create_time, body FROM msg ORDER BY id").fetchall()
    finally:
        conn.close()


# -- encryption / WAL writing -----------------------------------------------------

def encrypt_page(plain, pgno, key=KEY, mac_key=MAC_KEY):
    assert len(plain) == PAGE and plain[USABLE:] == b"\0" * D.RESERVE_SZ
    iv = hashlib.sha256(plain + struct.pack("<I", pgno)).digest()[:16]  # deterministic
    start = D.SALT_SZ if pgno == 1 else 0
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(plain[start:USABLE])
    mac = hmac.new(mac_key, enc + iv, hashlib.sha512)
    mac.update(struct.pack("<I", pgno))
    out = (SALT if pgno == 1 else b"") + enc + iv + mac.digest()
    assert len(out) == PAGE
    return out


def encrypt_db(pages, path):
    with open(path, "wb") as f:
        for i, p in enumerate(pages, 1):
            f.write(encrypt_page(p, i))


def cksum(data, s, big):
    return D._wal_checksum(data, s[0], s[1], big)


class WalWriter:
    """Writes a SQLite WAL whose frames carry encrypted pages."""

    def __init__(self, salts=(0x1234, 0xabcdef), big=False):
        self.big = big
        self.salts = salts
        magic = D.WAL_MAGIC_BE if big else D.WAL_MAGIC_LE
        hdr = struct.pack(">6I", magic, 3007000, PAGE, 7, *salts)
        self.s = cksum(hdr, (0, 0), big)
        self.data = bytearray(hdr + struct.pack(">2I", *self.s))

    def frame(self, pgno, plain, commit=0, salts=None, corrupt_hmac=False, raw=None):
        page = raw if raw is not None else encrypt_page(plain, pgno)
        if corrupt_hmac:
            page = page[:-1] + bytes([page[-1] ^ 1])
        fh8 = struct.pack(">2I", pgno, commit)
        s = cksum(fh8, self.s, self.big)
        s = cksum(page, s, self.big)
        self.s = s
        self.data += fh8 + struct.pack(">2I", *(salts or self.salts)) + struct.pack(">2I", *s) + page
        return self

    def txn(self, pages, changed=None, salts=None):
        """One transaction: the pages that differ, commit frame carries the db size."""
        idx = changed if changed is not None else range(1, len(pages) + 1)
        idx = list(idx)
        for n, pgno in enumerate(idx):
            last = n == len(idx) - 1
            self.frame(pgno, pages[pgno - 1], commit=len(pages) if last else 0, salts=salts)
        return self

    def write(self, path):
        with open(path, "wb") as f:
            f.write(self.data)


def diff(old, new):
    return [i for i in range(1, len(new) + 1) if i > len(old) or old[i - 1] != new[i - 1]]


def expected_bytes(pages):
    """Output decrypt_database should produce: the plaintext pages with page 1's
    header switched to rollback mode and the db size set."""
    p1 = bytearray(pages[0])
    p1[18] = p1[19] = 1
    p1[28:32] = struct.pack(">I", len(pages))
    p1[92:96] = p1[24:28]
    return bytes(p1) + b"".join(pages[1:])


# -- tests ------------------------------------------------------------------------

class WalDecryptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wal_test_")
        plain = os.path.join(cls.tmp, "plain.db")
        conn = new_plain_db(plain)
        add_rows(conn, 0, 40)
        cls.A = snapshot(plain)             # main file
        add_rows(conn, 40, 30)
        cls.B = snapshot(plain)             # + txn 1 (grows)
        conn.execute("UPDATE msg SET body='edited' WHERE id < 5 OR id = 65")
        cls.C = snapshot(plain)             # + txn 2 (rewrites pages already in txn 1)
        add_rows(conn, 70, 20)
        cls.E = snapshot(plain)             # + txn 3 (never committed in tests)
        conn.execute("DELETE FROM msg WHERE id >= 20")
        conn.execute("VACUUM")
        cls.F = snapshot(plain)             # shrunk
        conn.close()
        cls.rows = {}
        for name in "ABCEF":
            p = os.path.join(cls.tmp, name + ".db")
            with open(p, "wb") as f:
                f.write(b"".join(getattr(cls, name)))
            cls.rows[name] = rows(p)
            conn = sqlite3.connect(p)
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            assert ok == "ok", (name, ok)
        assert len(cls.B) > len(cls.A) and len(cls.F) < len(cls.C)
        assert set(diff(cls.B, cls.C)) & set(diff(cls.A, cls.B)) - {1}, "want page overrides"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=self.tmp)
        self.src = os.path.join(self.dir, "src.db")
        self.out = os.path.join(self.dir, "out", "src.db")
        encrypt_db(self.A, self.src)

    def decrypt(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return D.decrypt_database(self.src, self.out, KEY)

    def check(self, pages, name):
        sig = self.decrypt()
        self.assertTrue(sig)
        with open(self.out, "rb") as f:
            got = f.read()
        self.assertEqual(len(got), len(pages) * PAGE)
        self.assertEqual(got, expected_bytes(pages))
        conn = sqlite3.connect(self.out)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        conn.close()
        self.assertEqual(rows(self.out), self.rows[name])
        self.assertFalse(os.path.exists(self.out + "-wal"))
        self.assertFalse(os.path.exists(self.out + ".tmp"))
        return sig

    def wal(self):
        return self.src + "-wal"

    def test_no_wal(self):
        sig = self.check(self.A, "A")
        self.assertIsNone(sig["wal"])
        self.assertEqual(sig["wal_info"]["frames"], 0)

    def test_empty_and_header_only_wal(self):
        open(self.wal(), "wb").close()
        self.check(self.A, "A")
        WalWriter().write(self.wal())
        self.check(self.A, "A")

    def test_committed_transactions_with_overrides(self):
        w = WalWriter().txn(self.B, diff(self.A, self.B)).txn(self.C, diff(self.B, self.C))
        w.write(self.wal())
        sig = self.check(self.C, "C")
        self.assertEqual(sig["wal_info"]["commits"], 2)
        self.assertEqual(sig["wal_info"]["uncommitted"], 0)

    def test_big_endian_checksums(self):
        WalWriter(big=True).txn(self.B, diff(self.A, self.B)).write(self.wal())
        self.check(self.B, "B")

    def test_whole_db_in_wal(self):
        WalWriter().txn(self.C).write(self.wal())
        self.check(self.C, "C")

    def test_uncommitted_trailing_frames_ignored(self):
        w = WalWriter().txn(self.B, diff(self.A, self.B))
        for pgno in diff(self.B, self.E):         # txn 3 without its commit frame
            w.frame(pgno, self.E[pgno - 1])
        w.write(self.wal())
        sig = self.check(self.B, "B")
        self.assertEqual(sig["wal_info"]["uncommitted"], len(diff(self.B, self.E)))

    def test_partial_trailing_frame_ignored(self):
        w = WalWriter().txn(self.B, diff(self.A, self.B))
        w.data += b"\x01" * 1000                  # torn append
        w.write(self.wal())
        self.check(self.B, "B")

    def test_salt_mismatch_stops(self):
        # Frames of a previous WAL generation after a restart: different salts.
        w = WalWriter().txn(self.B, diff(self.A, self.B))
        w.txn(self.C, diff(self.B, self.C), salts=(0x1233, 0x55))
        w.write(self.wal())
        self.check(self.B, "B")
        # Only stale frames (what WeChat's WALs usually hold after a checkpoint).
        w = WalWriter(salts=(9, 9))
        w.txn(self.C, salts=(8, 8))
        w.write(self.wal())
        self.check(self.A, "A")

    def test_corrupted_hmac_stops(self):
        w = WalWriter().txn(self.B, diff(self.A, self.B))
        changed = diff(self.B, self.C)
        w.frame(changed[0], self.C[changed[0] - 1], corrupt_hmac=True)  # checksum still valid
        for pgno in changed[1:]:
            w.frame(pgno, self.C[pgno - 1], commit=len(self.C) if pgno == changed[-1] else 0)
        w.write(self.wal())
        sig = self.check(self.B, "B")
        self.assertEqual(sig["wal_info"]["stop"], "bad hmac")

    def test_bad_checksum_stops(self):
        w = WalWriter().txn(self.B, diff(self.A, self.B))
        mark = len(w.data)
        w.txn(self.C, diff(self.B, self.C))
        w.data[mark + 30] ^= 0xFF                 # flip a byte inside the first txn-2 page
        w.write(self.wal())
        sig = self.check(self.B, "B")
        self.assertEqual(sig["wal_info"]["stop"], "bad checksum")

    def test_wrong_key_frame_stops(self):
        w = WalWriter().txn(self.B, diff(self.A, self.B))
        other = D.derive_mac_key(bytes(32), SALT)
        for pgno in diff(self.B, self.C):
            w.frame(pgno, None, commit=len(self.C) if pgno == diff(self.B, self.C)[-1] else 0,
                    raw=encrypt_page(self.C[pgno - 1], pgno, key=bytes(32), mac_key=other))
        w.write(self.wal())
        self.check(self.B, "B")

    def test_shrinking_commit_truncates(self):
        w = WalWriter().txn(self.B, diff(self.A, self.B)).txn(self.C, diff(self.B, self.C))
        w.txn(self.F, diff(self.C, self.F))
        w.write(self.wal())
        self.check(self.F, "F")

    def test_main_file_page1_hmac_failure(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(D.decrypt_database(self.src, self.out, bytes(32)))
        self.assertFalse(os.path.exists(self.out))

    def test_state_tracks_db_and_wal(self):
        state_dir = os.path.join(self.dir, "out")
        sig = self.decrypt()
        D.save_state(state_dir, {"src.db": sig})
        state = D.load_state(state_dir)
        self.assertNotIn("wal_info", state["src.db"])
        self.assertTrue(D.is_current(state, "src.db", self.src, self.out))
        self.assertFalse(D.is_current(state, "other.db", self.src, self.out))
        # A WAL appears / grows while the .db is untouched -> not current.
        WalWriter().txn(self.B, diff(self.A, self.B)).write(self.wal())
        self.assertFalse(D.is_current(state, "src.db", self.src, self.out))
        D.save_state(state_dir, {"src.db": self.decrypt()})
        state = D.load_state(state_dir)
        self.assertTrue(D.is_current(state, "src.db", self.src, self.out))
        st = os.stat(self.wal())
        os.utime(self.wal(), ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
        self.assertFalse(D.is_current(state, "src.db", self.src, self.out))
        # Output deleted -> not current.
        D.save_state(state_dir, {"src.db": self.decrypt()})
        state = D.load_state(state_dir)
        os.remove(self.out)
        self.assertFalse(D.is_current(state, "src.db", self.src, self.out))
        # Corrupt state file -> empty state.
        with open(os.path.join(state_dir, D.STATE_FILE), "w") as f:
            f.write("{nope")
        self.assertEqual(D.load_state(state_dir), {})

    def test_retries_when_db_changes_during_read(self):
        real = D._decrypt_snapshot
        calls = []

        def racing(db_path, out_path, key):
            calls.append(1)
            res = real(db_path, out_path, key)
            if len(calls) == 1:  # WeChat checkpoints while we read
                encrypt_db(self.C, self.src)
                with open(self.wal(), "wb"):
                    pass
            return res

        with mock.patch.object(D, "_decrypt_snapshot", racing):
            WalWriter().txn(self.B, diff(self.A, self.B)).write(self.wal())
            sig = self.check(self.C, "C")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sig, dict(D.source_sig(self.src), wal_info=sig["wal_info"]))

    def test_gives_up_when_db_keeps_changing(self):
        real = D._decrypt_snapshot

        def always(db_path, out_path, key):
            res = real(db_path, out_path, key)
            st = os.stat(db_path)
            os.utime(db_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
            return res

        with mock.patch.object(D, "_decrypt_snapshot", always):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(D.decrypt_database(self.src, self.out, KEY))
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".tmp"))

    def test_never_writes_source_files(self):
        WalWriter().txn(self.B, diff(self.A, self.B)).write(self.wal())
        before = {p: (os.stat(p).st_mtime_ns, read(p)) for p in (self.src, self.wal())}
        self.check(self.B, "B")
        after = {p: (os.stat(p).st_mtime_ns, read(p)) for p in (self.src, self.wal())}
        self.assertEqual(before, after)
        self.assertEqual(sorted(os.listdir(self.dir)), ["out", "src.db", "src.db-wal"])


class MainAndKeysTest(unittest.TestCase):
    """main() incremental behavior and ensure_keys' stale-key rescan (sudo mocked)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="decrypt_main_")
        self.db_dir = os.path.join(self.tmp, "db_storage")
        self.out = os.path.join(self.tmp, "decrypted")
        self.keys_file = os.path.join(self.tmp, "all_keys.json")
        os.makedirs(os.path.join(self.db_dir, "message"))
        plain = os.path.join(self.tmp, "plain.db")
        conn = new_plain_db(plain)
        add_rows(conn, 0, 10)
        conn.close()
        self.pages = snapshot(plain)
        self.src = os.path.join(self.db_dir, "message", "message_0.db")
        encrypt_db(self.pages, self.src)
        self.patches = [mock.patch.object(D, "DB_DIR", self.db_dir),
                        mock.patch.object(D, "OUT_DIR", self.out),
                        mock.patch.object(D, "KEYS_FILE", self.keys_file)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def write_keys(self, key_hex, mtime=None):
        with open(self.keys_file, "w") as f:
            json.dump({"_db_dir": self.db_dir, "message/message_0.db": {"enc_key": key_hex}}, f)
        if mtime:
            os.utime(self.keys_file, (mtime, mtime))

    def run_main(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            D.main()
        return buf.getvalue()

    def test_incremental_runs(self):
        self.write_keys(KEY.hex(), mtime=time.time() + 100)
        out = self.run_main()
        self.assertIn("1 成功, 0 跳过", out)
        out = self.run_main()
        self.assertIn("0 成功, 1 跳过", out)
        # WAL written by WeChat, .db untouched -> re-decrypted with the WAL content.
        conn = sqlite3.connect(os.path.join(self.tmp, "plain.db"), isolation_level=None)
        add_rows(conn, 10, 5)
        conn.close()
        new = snapshot(os.path.join(self.tmp, "plain.db"))
        WalWriter().txn(new, diff(self.pages, new)).write(self.src + "-wal")
        out = self.run_main()
        self.assertIn("1 成功, 0 跳过", out)
        self.assertIn("从 WAL 合并了", out)
        self.assertEqual(len(rows(os.path.join(self.out, "message", "message_0.db"))), 15)
        self.assertIn("0 成功, 1 跳过", self.run_main())

    def test_stale_key_triggers_scanner(self):
        # Key file older than the DB and its key no longer validates -> rescan.
        self.write_keys("22" * 32, mtime=time.time() - 100)

        def fake_scanner():
            self.write_keys(KEY.hex())

        with mock.patch.object(D, "run_key_scanner", side_effect=fake_scanner) as scan:
            with contextlib.redirect_stdout(io.StringIO()):
                keys = D.ensure_keys()
        scan.assert_called_once()
        self.assertEqual(keys["message/message_0.db"]["enc_key"], KEY.hex())
        # Now valid: no rescan.
        with mock.patch.object(D, "run_key_scanner") as scan:
            D.ensure_keys()
        scan.assert_not_called()

    def test_scanner_failure_keeps_old_keys(self):
        self.write_keys("22" * 32, mtime=time.time() - 100)
        with mock.patch.object(D, "run_key_scanner", side_effect=RuntimeError("no sudo")):
            with contextlib.redirect_stdout(io.StringIO()):
                keys = D.ensure_keys()
        self.assertIn("message/message_0.db", keys)   # didn't exit


if __name__ == "__main__":
    unittest.main()
