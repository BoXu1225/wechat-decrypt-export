"""Shared helpers for the Moments (moments.py) and Favorites (favorites.py)
exporters: XML/text utilities, safe HTML bits, output paths and CLI plumbing.

No database access (stdlib + zstandard).
"""
import argparse
import html
import os
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

import zstandard

TIME_FMT = "%Y-%m-%d %H:%M:%S"
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXPORT_DIR = os.path.join(PROJECT_ROOT, "export")
FORMATS = ("html", "md", "json", "txt")

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_BAD_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_zstd = zstandard.ZstdDecompressor()


# ---------------------------------------------------------------- decoding

def blob_text(value):
    """DB TEXT/BLOB column -> str. zstd-compressed blobs are decompressed;
    undecodable bytes are replaced. None stays None."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.encode("utf-8", "surrogatepass")
    if value.startswith(_ZSTD_MAGIC):
        try:
            value = _zstd.decompress(value)
        except zstandard.ZstdError:
            pass
    return value.decode("utf-8", errors="replace")


def parse_xml(text):
    """ElementTree root for text, tolerating control characters; None on failure."""
    import xml.etree.ElementTree as ET
    if not text:
        return None
    for attempt in (text, _BAD_XML_CHARS.sub("", text)):
        try:
            return ET.fromstring(attempt)
        except ET.ParseError:
            continue
    return None


def xtext(elem, path, default=""):
    """Stripped text at path under elem ('' / default if missing)."""
    if elem is None:
        return default
    v = elem.findtext(path)
    return v.strip() if v and v.strip() else default


def to_int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def to_float(v):
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f


# ---------------------------------------------------------------- time

def fmt_time(ts, fmt=TIME_FMT):
    return datetime.fromtimestamp(ts).strftime(fmt) if ts else ""


def iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def in_range(ts, since=None, until=None):
    """since inclusive, until exclusive (unix seconds; None = open)."""
    return (since is None or ts >= since) and (until is None or ts < until)


# ---------------------------------------------------------------- html

esc = html.escape


def safe_url(url):
    """url if it is http(s), else '' (never emit javascript:/data: links)."""
    if not url:
        return ""
    url = url.strip()
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in ("http", "https") else ""


def url_host(url):
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def rel_path(path, base_dir):
    """File path relative to base_dir, POSIX separators, URL-quoted (for src/href)."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base_dir or "."))
    return quote(rel.replace(os.sep, "/"))


def rel_plain(path, base_dir):
    """File path relative to base_dir with POSIX separators (for JSON)."""
    return os.path.relpath(os.path.abspath(path),
                           os.path.abspath(base_dir or ".")).replace(os.sep, "/")


def md_escape(s):
    """Neutralise raw HTML in Markdown output (see formatters._md_escape)."""
    return (s or "").replace("<", "&lt;")


BASE_CSS = """
:root{--bg:#f2f2f2;--card:#fff;--fg:#111;--muted:#888;--line:#e5e5e5;--link:#576b95;--soft:#f5f5f5;--accent:#07c160}
@media (prefers-color-scheme:dark){:root{--bg:#111;--card:#1c1c1c;--fg:#e6e6e6;--muted:#8a8a8a;--line:#2c2c2c;--link:#8fa3c9;--soft:#262626;--accent:#3eb575}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px;border-bottom:1px solid var(--line);text-align:center;z-index:1}
header h1{font-size:16px;margin:0}
header .sub{font-size:12px;color:var(--muted)}
main{max-width:680px;margin:0 auto;padding:8px 12px 40px}
a{color:var(--link);text-decoration:none}
.card{background:var(--card);border-radius:10px;padding:12px 14px;margin:10px 0;overflow-wrap:anywhere;word-break:break-word}
.muted{color:var(--muted);font-size:12px}
.text{white-space:pre-wrap;margin:4px 0}
.linkbox{display:flex;gap:10px;align-items:center;background:var(--soft);border-radius:6px;padding:8px;margin:6px 0;color:var(--fg)}
.linkbox .lt{font-weight:500}
.linkbox .ld{font-size:12px;color:var(--muted)}
.linkbox img{width:48px;height:48px;object-fit:cover;border-radius:4px;flex:none}
.ph{display:flex;align-items:center;justify-content:center;background:var(--soft);color:var(--muted);font-size:12px;border-radius:6px;text-align:center;padding:4px}
.tag{display:inline-block;font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:9px;padding:0 7px;margin:0 4px 0 0}
"""


def page(title, subtitle, body, css=""):
    """Complete self-contained HTML document (title/subtitle are escaped here;
    body must already be safe HTML)."""
    return ("<!DOCTYPE html>\n<html lang=\"zh\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
            "<meta name=\"color-scheme\" content=\"light dark\">\n"
            "<meta name=\"referrer\" content=\"no-referrer\">\n"
            f"<title>{esc(title)}</title>\n<style>{BASE_CSS}{css}</style>\n</head>\n<body>\n"
            f"<header><h1>{esc(title)}</h1><div class=\"sub\">{esc(subtitle)}</div></header>\n"
            f"<main>\n{body}</main>\n</body>\n</html>\n")


# ---------------------------------------------------------------- files

def safe_filename(name, fallback="unnamed"):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", name or "")
    s = s.strip().strip(".").strip()
    return s[:150] or fallback


def output_path(export_dir, stem, fmt, output=None):
    if output:
        return os.path.abspath(output)
    return os.path.join(export_dir or DEFAULT_EXPORT_DIR, f"{stem}.{fmt}")


def files_dir_for(path):
    """<dir>/<stem>_files next to an output file."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(os.path.dirname(os.path.abspath(path)), f"{stem}_files")


def existing_decoded(files_dir, stem, exts=("jpg", "png", "gif", "webp", "bmp", "tif",
                                            "heic", "mp4")):
    for ext in exts:
        p = os.path.join(files_dir, f"{stem}.{ext}")
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- CLI

def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD: {s}")


def add_common_args(parser):
    parser.add_argument("-f", "--format", choices=FORMATS, default="html",
                        help="输出格式（默认 html）")
    parser.add_argument("-o", "--output", help="输出文件路径")
    parser.add_argument("--since", type=parse_date, metavar="YYYY-MM-DD",
                        help="只导出该日期（含）之后的内容")
    parser.add_argument("--until", type=parse_date, metavar="YYYY-MM-DD",
                        help="只导出该日期（含）之前的内容")
    parser.add_argument("--images", action="store_true",
                        help="解码本地缓存的图片/视频到 <输出名>_files/ 并在 html/md/json 中引用")
    parser.add_argument("-d", "--decrypted-dir", help="已解密数据库目录")
    parser.add_argument("--export-dir", help="导出目录（默认: 项目下的 export/）")
    parser.add_argument("--no-decrypt", action="store_true",
                        help="跳过自动解密，直接使用现有解密数据")


def time_range(parser, args):
    since = args.since.timestamp() if args.since else None
    until = (args.until + timedelta(days=1)).timestamp() if args.until else None
    if since is not None and until is not None and since >= until:
        parser.error("--since 不能晚于 --until")
    return since, until


def setup(args, cfg=None):
    """Load config (unless cfg is given), decrypt changed databases (unless
    --no-decrypt). Returns (cfg, decrypted_dir, export_dir)."""
    if cfg is None:
        from config import load_config
        cfg = load_config()
    if not args.no_decrypt:
        from decrypt_db import main as decrypt_main
        decrypt_main()
    decrypted_dir = args.decrypted_dir or cfg["decrypted_dir"]
    export_dir = os.path.abspath(args.export_dir) if args.export_dir else DEFAULT_EXPORT_DIR
    return cfg, decrypted_dir, export_dir


def image_keys(cfg):
    """(aes_key, xor_key) for V2 .dat files, (None, None) if unavailable."""
    try:
        import image_decode
        return image_decode.get_image_keys(cfg)
    except Exception:
        return None, None
