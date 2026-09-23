"""
emoticon.py - 获取自定义表情 / 贴纸图片（消息 local_type 47）

Where sticker images can come from (WeChat 4.x, macOS):

1. Message XML.  Every sticker message carries
       <emoji md5=".." len=".." cdnurl=".." encrypturl=".." aeskey=".."
              externurl=".." externmd5=".." width=".." height=".." productid="..">
   * cdnurl     - plain image (GIF/PNG/JPEG/wxgf), md5(body) == md5 attr
   * encrypturl - AES-128-CBC(key = iv = bytes.fromhex(aeskey)), PKCS7;
                  decrypts to the same bytes as cdnurl
   Old links (e.g. emoji.qpic.cn) often answer 403; some messages have no URL.

2. emoticon/emoticon.db  kNonStoreEmoticonTable (favourited stickers):
   md5, aes_key, cdn_url, encrypt_url, extern_url, extern_md5, thumb_url ...
   Used as a fallback source of URLs.

3. Local caches under the account folder
       cache/<YYYY-MM>/Emoticon/<md5[:2]>/<md5>
       business/emoticon/{Persist,PersistStore,Thumb,ThumbStore}/<md5[:2]>/<md5>[.thumb]
   These files are encrypted (whole file, 16-byte aligned; one account- or
   app-wide key: different stickers share the same first cipher block).  The
   key is not the image .dat key, the per-sticker aeskey, nor a DB key; this
   module only uses a cached file if it is already a plain image.

Decoded stickers are cached in <decrypted_dir>/emoji_cache/<md5>.<ext>.
Downloading is opt-in (download=True / export_chat.py --download-emoji); only
http(s) URLs on Tencent CDN hosts are fetched, with a timeout and size limit.
Failures are remembered for a week (<md5>.fail) so re-exports do not retry
dead links every time.
"""
import glob
import html
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

import image_decode

CACHE_DIRNAME = "emoji_cache"
ALLOWED_HOST_SUFFIXES = (".qq.com", ".qpic.cn", ".wechat.com", ".weixin.qq.com")
MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = 15
FAIL_TTL = 7 * 86400
USER_AGENT = "Mozilla/5.0"
_EXTS = ("gif", "png", "jpg", "webp", "bmp", "tif", "heic")

_EMOJI_TAG_RE = re.compile(r"<emoji\b([^>]*)>", re.S)
_ATTR_RE = re.compile(r"([A-Za-z_][\w-]*)\s*=\s*\"([^\"]*)\"")
_MD5_TAG_RE = re.compile(r"<(?:emoticonmd5|md5)>\s*([0-9a-fA-F]{32})\s*<")
_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class EmojiError(Exception):
    pass


# --------------------------------------------------------------------------
# parsing / lookup
# --------------------------------------------------------------------------

def parse_emoji_xml(text):
    """Parse a sticker message's content -> dict (md5, cdnurl, encrypturl,
    aeskey, externurl, externmd5, width, height, len, productid, ...) or None.

    Also accepts app messages (type 49/8) that only carry <emoticonmd5>."""
    if not text:
        return None
    m = _EMOJI_TAG_RE.search(text)
    if m:
        info = {k.lower(): html.unescape(v) for k, v in _ATTR_RE.findall(m.group(1))}
    else:
        m = _MD5_TAG_RE.search(text)
        if not m:
            return None
        info = {"md5": m.group(1)}
    md5 = (info.get("md5") or "").lower()
    if not _HEX32_RE.match(md5):
        return None
    info["md5"] = md5
    return info


def load_emoticon_db(decrypted_dir):
    """md5 -> {cdnurl, encrypturl, aeskey, externurl, externmd5, thumburl}
    from emoticon/emoticon.db (kNonStoreEmoticonTable)."""
    path = os.path.join(decrypted_dir, "emoticon", "emoticon.db")
    out = {}
    if not os.path.exists(path):
        return out
    con = sqlite3.connect(path)
    try:
        rows = con.execute("SELECT md5, cdn_url, encrypt_url, aes_key, extern_url, "
                           "extern_md5, thumb_url FROM kNonStoreEmoticonTable").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for md5, cdn, enc, key, ext, ext_md5, thumb in rows:
        if md5:
            out[md5.lower()] = {"cdnurl": cdn or "", "encrypturl": enc or "",
                                "aeskey": key or "", "externurl": ext or "",
                                "externmd5": ext_md5 or "", "thumburl": thumb or ""}
    return out


def merge_info(msg_info, db_info):
    """Fill empty URL/key fields of a message's info from emoticon.db."""
    info = dict(msg_info or {})
    for k, v in (db_info or {}).items():
        if v and not info.get(k):
            info[k] = v
    return info


def local_cache_paths(wechat_base_dir, md5):
    """WeChat's own cached files for a sticker md5 (full image first)."""
    if not wechat_base_dir or not md5:
        return []
    sub = md5[:2]
    paths = sorted(glob.glob(os.path.join(wechat_base_dir, "cache", "*", "Emoticon", sub, md5)),
                   reverse=True)
    biz = os.path.join(wechat_base_dir, "business", "emoticon")
    for d, name in (("Persist", md5), ("PersistStore", md5)):
        paths.append(os.path.join(biz, d, sub, name))
    return [p for p in paths if os.path.isfile(p)]


def cached_file(cache_dir, md5):
    for ext in _EXTS:
        p = os.path.join(cache_dir, f"{md5}.{ext}")
        if os.path.exists(p):
            return p
    return None


# --------------------------------------------------------------------------
# decoding / downloading
# --------------------------------------------------------------------------

def decrypt_encrypturl_body(data, aeskey):
    """AES-128-CBC with key = iv = bytes.fromhex(aeskey), PKCS7 padded."""
    from Crypto.Cipher import AES
    try:
        key = bytes.fromhex(aeskey)
    except (TypeError, ValueError):
        raise EmojiError("bad aeskey")
    if len(key) != 16 or not data or len(data) % 16:
        raise EmojiError("bad aeskey or ciphertext length")
    plain = AES.new(key, AES.MODE_CBC, iv=key).decrypt(data)
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        raise EmojiError("bad padding (wrong aeskey?)")
    return plain[:-pad]


def to_image(data):
    """Validate/convert sticker bytes -> (bytes, ext). wxgf becomes png/jpg
    (first frame only for animated wxgf)."""
    ext = image_decode.detect_ext(data)
    if ext is None:
        raise EmojiError("not an image")
    if ext == "wxgf":
        try:
            return image_decode.wxgf_convert(data, "auto")
        except image_decode.ImageDecodeError as e:
            raise EmojiError(str(e))
    return data, ext


def url_allowed(url):
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(
        host == s[1:] or host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)


def fetch(url, timeout=TIMEOUT, max_bytes=MAX_BYTES):
    if not url_allowed(url):
        raise EmojiError("URL not allowed")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise EmojiError(f"HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise EmojiError(type(e).__name__)
    if len(data) > max_bytes:
        raise EmojiError("too large")
    return data


def download(info, fetcher=fetch):
    """Download a sticker using its cdnurl, else encrypturl+aeskey.
    Returns (bytes, ext); raises EmojiError. The body must be an image
    (for real stickers md5(body) equals the message's md5 attribute)."""
    errors = []
    if info.get("cdnurl"):
        try:
            return to_image(fetcher(info["cdnurl"]))
        except EmojiError as e:
            errors.append(f"cdn: {e}")
    if info.get("encrypturl") and info.get("aeskey"):
        try:
            return to_image(decrypt_encrypturl_body(fetcher(info["encrypturl"]), info["aeskey"]))
        except EmojiError as e:
            errors.append(f"enc: {e}")
    raise EmojiError("; ".join(errors) or "no URL")


# --------------------------------------------------------------------------
# resolver
# --------------------------------------------------------------------------

class EmojiResolver:
    """Resolve sticker md5 -> local image file in <decrypted_dir>/emoji_cache/.

    Order: emoji_cache hit -> WeChat's local cache (only if plain image)
    -> download (only if download=True). Counters in .stats."""

    def __init__(self, decrypted_dir, wechat_base_dir=None, download=False,
                 cache_dir=None, fetcher=None):
        self.decrypted_dir = decrypted_dir
        self.wechat_base_dir = wechat_base_dir
        self.download = download
        self.cache_dir = cache_dir or os.path.join(decrypted_dir, CACHE_DIRNAME)
        self.fetcher = fetcher
        self._db = None
        self.stats = {"cached": 0, "local": 0, "local_encrypted": 0,
                      "downloaded": 0, "failed": 0, "skipped_failed": 0,
                      "unavailable": 0}

    @property
    def db(self):
        if self._db is None:
            self._db = load_emoticon_db(self.decrypted_dir)
        return self._db

    def _fail_marker(self, md5):
        return os.path.join(self.cache_dir, md5 + ".fail")

    def _recently_failed(self, md5):
        try:
            return time.time() - os.path.getmtime(self._fail_marker(md5)) < FAIL_TTL
        except OSError:
            return False

    def _store(self, md5, data, ext):
        os.makedirs(self.cache_dir, exist_ok=True)
        path = os.path.join(self.cache_dir, f"{md5}.{ext}")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        try:
            os.remove(self._fail_marker(md5))
        except OSError:
            pass
        return path

    def resolve(self, info):
        """info: parse_emoji_xml() result. Returns a file path or None."""
        md5 = (info or {}).get("md5")
        if not md5:
            self.stats["unavailable"] += 1
            return None
        p = cached_file(self.cache_dir, md5)
        if p:
            self.stats["cached"] += 1
            return p
        encrypted = False
        for lp in local_cache_paths(self.wechat_base_dir, md5):
            try:
                with open(lp, "rb") as f:
                    data = f.read(MAX_BYTES + 1)
                data, ext = to_image(data)
            except (OSError, EmojiError):
                encrypted = True
                continue
            self.stats["local"] += 1
            return self._store(md5, data, ext)
        if encrypted:
            self.stats["local_encrypted"] += 1
        if not self.download:
            self.stats["unavailable"] += 1
            return None
        if self._recently_failed(md5):
            self.stats["skipped_failed"] += 1
            return None
        full = merge_info(info, self.db.get(md5))
        try:
            data, ext = download(full, self.fetcher or fetch)
        except EmojiError:
            self.stats["failed"] += 1
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self._fail_marker(md5), "w") as f:
                f.write(str(int(time.time())))
            return None
        self.stats["downloaded"] += 1
        return self._store(md5, data, ext)


def export_file(src, files_dir, md5):
    """Copy a cached sticker into an export's <name>_files/ as emoji_<md5>.<ext>."""
    import shutil
    ext = os.path.splitext(src)[1]
    dst = os.path.join(files_dir, f"emoji_{md5}{ext}")
    if not os.path.exists(dst):
        os.makedirs(files_dir, exist_ok=True)
        shutil.copyfile(src, dst)
    return dst


def existing_export_file(files_dir, md5):
    for ext in _EXTS:
        p = os.path.join(files_dir, f"emoji_{md5}.{ext}")
        if os.path.exists(p):
            return p
    return None


# --------------------------------------------------------------------------
# CLI: coverage report
# --------------------------------------------------------------------------

def _iter_emoji_contents(decrypted_dir):
    import chats
    for db in chats.message_db_paths(decrypted_dir):
        con = sqlite3.connect(db)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for t in tables:
                for content, ct in con.execute(
                        f"SELECT message_content, WCDB_CT_message_content FROM [{t}] "
                        "WHERE (local_type & 4294967295) = 47"):
                    yield chats.decompress_if_needed(content, ct)
        finally:
            con.close()


def coverage(decrypted_dir, wechat_base_dir):
    """Counts only: how many sticker messages / distinct md5s are available
    from emoji_cache, WeChat's cache (plain / encrypted), or have a URL."""
    cache_dir = os.path.join(decrypted_dir, CACHE_DIRNAME)
    db = load_emoticon_db(decrypted_dir)
    msgs = 0
    md5s = {}
    for text in _iter_emoji_contents(decrypted_dir):
        msgs += 1
        info = parse_emoji_xml(text)
        if info:
            md5s[info["md5"]] = merge_info(info, db.get(info["md5"]))
    c = {"messages": msgs, "distinct": len(md5s), "emoji_cache": 0,
         "wechat_cache_plain": 0, "wechat_cache_encrypted": 0,
         "has_cdnurl": 0, "has_encrypturl": 0, "no_url": 0}
    for md5, info in md5s.items():
        if cached_file(cache_dir, md5):
            c["emoji_cache"] += 1
        plain = enc = False
        for lp in local_cache_paths(wechat_base_dir, md5):
            with open(lp, "rb") as f:
                head = f.read(16)
            if image_decode.detect_ext(head):
                plain = True
            else:
                enc = True
        c["wechat_cache_plain"] += plain
        c["wechat_cache_encrypted"] += enc and not plain
        c["has_cdnurl"] += bool(info.get("cdnurl"))
        c["has_encrypturl"] += bool(info.get("encrypturl") and info.get("aeskey"))
        c["no_url"] += not (info.get("cdnurl") or info.get("encrypturl"))
    return c


def main():
    import argparse
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import load_config
    ap = argparse.ArgumentParser(
        description="统计表情（贴纸）图片的可用情况：本地缓存 / 可下载（只输出数量）")
    ap.parse_args()
    cfg = load_config()
    for k, v in coverage(cfg["decrypted_dir"], cfg.get("wechat_base_dir")).items():
        print(f"{k:24s} {v}")


if __name__ == "__main__":
    main()
