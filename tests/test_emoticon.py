"""Tests for emoticon.py (sticker lookup / download) with synthetic data.

No network access: downloads go through a fake fetcher.
Run: ./venv/bin/python -m unittest discover -s tests
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Crypto.Cipher import AES  # noqa: E402

import emoticon  # noqa: E402

MD5 = "a" * 32
MD5_DB = "b" * 32
GIF = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 20 + b";"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 30
AESKEY = "00112233445566778899aabbccddeeff"


def encrypt(data, aeskey=AESKEY):
    key = bytes.fromhex(aeskey)
    pad = 16 - len(data) % 16
    return AES.new(key, AES.MODE_CBC, iv=key).encrypt(data + bytes([pad]) * pad)


def emoji_xml(md5=MD5, **attrs):
    a = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return f'<msg><emoji fromusername="x" md5="{md5.upper()}" len="10" {a} ></emoji></msg>'


def make_emoticon_db(decrypted_dir, rows):
    d = os.path.join(decrypted_dir, "emoticon")
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(os.path.join(d, "emoticon.db"))
    con.execute("CREATE TABLE kNonStoreEmoticonTable(type INTEGER, md5 TEXT, caption TEXT, "
                "product_id TEXT, aes_key TEXT, thumb_url TEXT, tp_url TEXT, auth_key TEXT, "
                "cdn_url TEXT, extern_url TEXT, extern_md5 TEXT, encrypt_url TEXT)")
    for md5, cdn, enc, key in rows:
        con.execute("INSERT INTO kNonStoreEmoticonTable(md5, cdn_url, encrypt_url, aes_key) "
                    "VALUES (?,?,?,?)", (md5, cdn, enc, key))
    con.commit()
    con.close()


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        r = self.responses.get(url)
        if r is None or isinstance(r, Exception):
            raise r or emoticon.EmojiError("HTTP 403")
        return r


class ParseTest(unittest.TestCase):
    def test_parse_emoji_xml(self):
        info = emoticon.parse_emoji_xml(emoji_xml(
            cdnurl="http://wxapp.tc.qq.com/a?m=1&amp;b=2", aeskey=AESKEY, width="120"))
        self.assertEqual(info["md5"], MD5)
        self.assertEqual(info["cdnurl"], "http://wxapp.tc.qq.com/a?m=1&b=2")
        self.assertEqual(info["width"], "120")

    def test_parse_appmsg_and_invalid(self):
        self.assertEqual(emoticon.parse_emoji_xml(
            f"<msg><appmsg><emoticonmd5>{MD5}</emoticonmd5></appmsg></msg>")["md5"], MD5)
        self.assertIsNone(emoticon.parse_emoji_xml("<emoji/>"))
        self.assertIsNone(emoticon.parse_emoji_xml('<emoji md5="zz"/>'))
        self.assertIsNone(emoticon.parse_emoji_xml(None))

    def test_url_allowed(self):
        ok = ["http://wxapp.tc.qq.com/x", "https://emoji.qpic.cn/x", "http://vweixinf.tc.qq.com/x",
              "http://snsvideo.c2c.wechat.com/x", "https://qpic.cn/x"]
        bad = ["file:///etc/passwd", "http://192.168.1.1/x", "http://evilqq.com/x",
               "http://qq.com.evil.net/x", "ftp://wxapp.tc.qq.com/x", ""]
        for u in ok:
            self.assertTrue(emoticon.url_allowed(u), u)
        for u in bad:
            self.assertFalse(emoticon.url_allowed(u), u)

    def test_fetch_refuses_disallowed_url(self):
        with mock.patch("urllib.request.urlopen") as op:
            with self.assertRaises(emoticon.EmojiError):
                emoticon.fetch("http://127.0.0.1/x")
            op.assert_not_called()

    def test_decrypt_encrypturl_body(self):
        self.assertEqual(emoticon.decrypt_encrypturl_body(encrypt(GIF), AESKEY), GIF)
        with self.assertRaises(emoticon.EmojiError):
            emoticon.decrypt_encrypturl_body(encrypt(GIF), "ff" * 16)
        with self.assertRaises(emoticon.EmojiError):
            emoticon.decrypt_encrypturl_body(b"123", AESKEY)


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="emoticon_test_")
        self.dec = os.path.join(self.tmp, "decrypted")
        self.base = os.path.join(self.tmp, "wechat_base")
        os.makedirs(self.dec)
        make_emoticon_db(self.dec, [(MD5_DB, "http://wxapp.tc.qq.com/db", "", ""),
                                    (MD5, "", "http://wxapp.tc.qq.com/dbenc", AESKEY)])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def resolver(self, download=False, responses=None):
        self.fetcher = FakeFetcher(responses or {})
        return emoticon.EmojiResolver(self.dec, self.base, download=download,
                                      fetcher=self.fetcher)

    def put_local(self, rel, data):
        p = os.path.join(self.base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def test_load_emoticon_db(self):
        db = emoticon.load_emoticon_db(self.dec)
        self.assertEqual(db[MD5_DB]["cdnurl"], "http://wxapp.tc.qq.com/db")
        self.assertEqual(db[MD5]["aeskey"], AESKEY)
        self.assertEqual(emoticon.load_emoticon_db(self.tmp), {})

    def test_no_download_by_default(self):
        r = self.resolver(responses={"http://wxapp.tc.qq.com/c": GIF})
        self.assertIsNone(r.resolve({"md5": MD5, "cdnurl": "http://wxapp.tc.qq.com/c"}))
        self.assertEqual(self.fetcher.calls, [])
        self.assertEqual(r.stats["unavailable"], 1)

    def test_local_plain_cache_used(self):
        self.put_local(f"cache/2026-01/Emoticon/{MD5[:2]}/{MD5}", PNG)
        r = self.resolver()
        p = r.resolve({"md5": MD5})
        self.assertEqual(p, os.path.join(self.dec, "emoji_cache", f"{MD5}.png"))
        with open(p, "rb") as f:
            self.assertEqual(f.read(), PNG)
        self.assertEqual(r.resolve({"md5": MD5}), p)
        self.assertEqual((r.stats["local"], r.stats["cached"]), (1, 1))

    def test_local_encrypted_cache_counted_not_used(self):
        self.put_local(f"business/emoticon/Persist/{MD5[:2]}/{MD5}", os.urandom(64))
        r = self.resolver()
        self.assertIsNone(r.resolve({"md5": MD5}))
        self.assertEqual(r.stats["local_encrypted"], 1)

    def test_download_cdn(self):
        r = self.resolver(True, {"http://wxapp.tc.qq.com/c": GIF})
        p = r.resolve(emoticon.parse_emoji_xml(emoji_xml(cdnurl="http://wxapp.tc.qq.com/c")))
        self.assertTrue(p.endswith(f"{MD5}.gif"))
        self.assertEqual(r.stats["downloaded"], 1)

    def test_download_encrypturl_fallback_from_db(self):
        # message has a dead cdnurl; emoticon.db supplies encrypturl + aeskey
        r = self.resolver(True, {"http://wxapp.tc.qq.com/dbenc": encrypt(GIF)})
        p = r.resolve({"md5": MD5, "cdnurl": "http://wxapp.tc.qq.com/dead"})
        self.assertTrue(p.endswith(".gif"))
        self.assertEqual(self.fetcher.calls, ["http://wxapp.tc.qq.com/dead",
                                              "http://wxapp.tc.qq.com/dbenc"])

    def test_download_url_from_db_only(self):
        r = self.resolver(True, {"http://wxapp.tc.qq.com/db": PNG})
        self.assertTrue(r.resolve({"md5": MD5_DB}).endswith(f"{MD5_DB}.png"))

    def test_failure_marker_skips_retry(self):
        r = self.resolver(True)
        self.assertIsNone(r.resolve({"md5": MD5_DB}))
        self.assertEqual(r.stats["failed"], 1)
        n = len(self.fetcher.calls)
        self.assertIsNone(r.resolve({"md5": MD5_DB}))
        self.assertEqual(r.stats["skipped_failed"], 1)
        self.assertEqual(len(self.fetcher.calls), n)
        # expired marker -> retried
        marker = os.path.join(self.dec, "emoji_cache", MD5_DB + ".fail")
        old = time.time() - emoticon.FAIL_TTL - 10
        os.utime(marker, (old, old))
        self.fetcher.responses["http://wxapp.tc.qq.com/db"] = GIF
        self.assertIsNotNone(r.resolve({"md5": MD5_DB}))
        self.assertFalse(os.path.exists(marker))

    def test_non_image_body_rejected(self):
        r = self.resolver(True, {"http://wxapp.tc.qq.com/db": b"<html>nope</html>"})
        self.assertIsNone(r.resolve({"md5": MD5_DB}))

    def test_export_file(self):
        src = os.path.join(self.tmp, "x.gif")
        with open(src, "wb") as f:
            f.write(GIF)
        files = os.path.join(self.tmp, "out", "Chat_files")
        dst = emoticon.export_file(src, files, MD5)
        self.assertEqual(os.path.basename(dst), f"emoji_{MD5}.gif")
        self.assertEqual(emoticon.existing_export_file(files, MD5), dst)
        self.assertIsNone(emoticon.existing_export_file(files, MD5_DB))


if __name__ == "__main__":
    unittest.main()
