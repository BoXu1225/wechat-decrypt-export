"""Output writers for exported chats (pure; no DB access; stdlib only).

Every writer consumes the same record schema::

    {
        "ts": int,              # unix seconds
        "sender": str,          # display name
        "is_self": bool,
        "kind": str,            # text|image|voice|video|emoji|location|link|
                                # file|miniprogram|quote|system|call|card|
                                # redpacket|transfer|chat_history|channels|
                                # pat|music|notice|note|other
        "text": str,            # one-line display text, e.g. message body,
                                # "[图片]" or "[链接] 标题 (来源) https://..."
        "image_path": str|None, # optional path to a decoded image file
        "extra": dict|None,     # optional structured details (msg_parse
                                # schema); html/md/txt use "quote", "url",
                                # "items" (forwarded chat bundle) for richer output
    }

and chat metadata ``{"name": str, "is_group": bool}``.

Writers have the signature ``writer(records, meta, fp, base_dir=None, **kw)``
where ``fp`` is an open text file and ``base_dir`` is the directory the output
file lives in (image paths are written relative to it; defaults to cwd).

Incremental export
------------------
``supports_append(fmt)`` tells whether new records can simply be appended to
an existing file:

* txt  - append lines as-is.
* md   - append; pass ``prev_date`` (last ``## YYYY-MM-DD`` heading already in
         the file) so the day heading is not repeated. ``write(..., append=True)``
         detects it automatically.
* csv  - append rows; the header (and BOM) is only written for a new/empty file.
* html - NOT appendable (single document with closing tags). Re-render the
         whole file from the full record list (old + new).
* json - NOT appendable (one JSON document). Load the existing file, extend
         ``messages`` with the new records and re-render, or re-render from the
         full record list.

``write(..., append=True)`` raises ValueError for html/json.
"""

import csv
import html
import json
import os
import re
from datetime import datetime
from urllib.parse import quote

TIME_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- helpers

def _dt(ts):
    return datetime.fromtimestamp(ts)


def _rel_image(path, base_dir):
    """Image path relative to base_dir, POSIX separators, URL-quoted."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base_dir or "."))
    return quote(rel.replace(os.sep, "/"))


def _img(rec):
    return rec.get("image_path") or None


def _extra(rec):
    e = rec.get("extra")
    return e if isinstance(e, dict) else {}


_SAFE_URL_RE = re.compile(r"(?i)^https?://[^\s\x00-\x1f\x7f]+$")


def _safe_url(url):
    """http(s) URL or None (no javascript:, data:, whitespace, ...)."""
    return url if isinstance(url, str) and _SAFE_URL_RE.match(url) else None


def _items(rec_or_item):
    """Forwarded chat bundle items of a record (or nested item)."""
    src = _extra(rec_or_item) if "extra" in rec_or_item else rec_or_item
    items = src.get("items")
    return items if isinstance(items, list) else []


def _item_text(it):
    return str(it.get("text") or "")


# ---------------------------------------------------------------- txt

def _txt_items(items, fp, depth):
    pad = "    " * depth
    for it in items:
        text = _item_text(it).replace("\n", "\n" + pad + "  ")
        fp.write(f"{pad}[{it.get('time') or ''}] {it.get('sender') or ''}: {text}\n")
        _txt_items(_items(it), fp, depth + 1)


def write_txt(records, meta, fp, base_dir=None, **_):
    """``[YYYY-MM-DD HH:MM:SS] sender: text`` one record per line (local time).

    Identical to the legacy export_chat.py output, except that forwarded chat
    bundles (extra.items) are followed by their messages as indented lines
    (``    [time] sender: text``; nested bundles indent further).
    """
    for r in records:
        fp.write(f"[{_dt(r['ts']).strftime(TIME_FMT)}] {r['sender']}: {r['text']}\n")
        _txt_items(_items(r), fp, 1)


# ---------------------------------------------------------------- markdown

def _md_escape(s):
    """Neutralise raw HTML: many markdown viewers render inline HTML, so a
    message containing ``<script>``/``<img onerror=...>`` must not become a tag."""
    return (s or "").replace("<", "&lt;")


def _md_link_text(s):
    return _md_escape(s).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _md_url(url):
    return quote(url, safe=":/?#[]@!$&'*+,;=%~.-_")


def _md_items(items, depth):
    out = []
    pre = "> " * depth
    for it in items:
        text = _md_escape(_item_text(it))
        url = _safe_url(it.get("url"))
        if url:
            text = f"[{_md_link_text(_item_text(it))}]({_md_url(url)})"
        text = text.replace("\n", "  \n" + pre)
        out.append(f"{pre}**{_md_escape(it.get('sender'))}** {_md_escape(it.get('time'))}: {text}  ")
        nested = _items(it)
        if nested:
            out.extend(_md_items(nested, depth + 1))
    return out


def _md_body(r):
    """Markdown body for a non-image record, plus trailing block lines."""
    e = _extra(r)
    kind = r.get("kind")
    text = r.get("text") or ""
    after = []
    q = e.get("quote")
    if kind == "quote" and isinstance(q, dict):
        text = e.get("reply") or ""
        after.append(f"> {_md_escape(q.get('sender'))}: {_md_escape(q.get('text'))}"
                      .replace("\n", " "))
        body = _md_escape(text)
    elif kind in ("link", "music") and _safe_url(e.get("url")):
        label = "[音乐]" if kind == "music" else "[链接]"
        title = e.get("title") or e.get("url")
        body = f"{label} [{_md_link_text(title)}]({_md_url(e['url'])})"
        if e.get("source"):
            body += f" ({_md_escape(e['source'])})"
    else:
        body = _md_escape(text)
    items = _items(r)
    if items:
        after.extend(_md_items(items, 1))
    return body.replace("\n", "  \n"), after


def write_markdown(records, meta, fp, base_dir=None, prev_date=None, header=True, **_):
    """Markdown with a ``## YYYY-MM-DD`` heading per day.

    ``prev_date`` (str) suppresses a heading for that day (used when
    appending). ``header`` writes a ``# name`` title; ignored if prev_date set.
    ``<`` in names/senders/text is written as ``&lt;`` (no raw HTML).
    """
    if header and prev_date is None and meta.get("name"):
        fp.write(f"# {_md_escape(meta['name'])}\n\n")
    cur = prev_date
    for r in records:
        dt = _dt(r["ts"])
        day = dt.strftime("%Y-%m-%d")
        if day != cur:
            fp.write(f"## {day}\n\n")
            cur = day
        img = _img(r)
        after = []
        if img:
            body = f"![{_md_escape(r.get('text'))}]({_rel_image(img, base_dir)})"
        else:
            # keep continuation lines inside the same paragraph
            body, after = _md_body(r)
        fp.write(f"**{_md_escape(r['sender'])}** {dt.strftime('%H:%M')}  {body}\n\n")
        if after:
            fp.write("\n".join(after) + "\n\n")


def _last_md_date(path):
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"## (\d{4}-\d{2}-\d{2})\s*$", line)
            if m:
                last = m.group(1)
    return last


# ---------------------------------------------------------------- html

_CSS = """
:root{--bg:#ededed;--fg:#111;--muted:#888;--other:#fff;--self:#95ec69;--selffg:#111;--sys:#dadada}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#e6e6e6;--muted:#8a8a8a;--other:#2c2c2c;--self:#3eb575;--selffg:#0b0b0b;--sys:#262626}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px;border-bottom:1px solid var(--sys);font-weight:600;text-align:center;z-index:1}
main{max-width:760px;margin:0 auto;padding:8px 12px 32px}
.day{text-align:center;margin:18px 0 8px}
.day span{background:var(--sys);color:var(--muted);font-size:12px;padding:2px 10px;border-radius:10px}
.msg{display:flex;flex-direction:column;align-items:flex-start;margin:6px 0}
.msg.self{align-items:flex-end}
.name{font-size:12px;color:var(--muted);margin:0 6px 2px}
.bubble{max-width:min(78%,560px);background:var(--other);padding:8px 11px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
.self .bubble{background:var(--self);color:var(--selffg)}
.bubble.img{padding:3px;background:transparent}
.bubble img{display:block;max-width:100%;max-height:360px;border-radius:6px}
.time{font-size:11px;color:var(--muted);margin:2px 6px 0}
.sys{text-align:center;color:var(--muted);font-size:12px;margin:8px 0;white-space:pre-wrap}
.bubble a{color:inherit}
.quote{margin-top:6px;padding:4px 8px;border-left:3px solid var(--muted);background:var(--sys);color:var(--muted);font-size:13px;border-radius:4px}
.meta{color:var(--muted);font-size:12px;margin-top:4px}
.bubble.k-redpacket,.bubble.k-transfer{background:#f79c42;color:#fff}
details summary{cursor:pointer}
.rec{margin-top:6px;padding-left:8px;border-left:2px solid var(--muted);font-size:13px}
.ri{margin:4px 0}
.ri .rs{font-weight:600}
.ri .rt{color:var(--muted);font-size:11px}
"""


def _html_items(items):
    esc = html.escape
    out = ['<div class="rec">']
    for it in items:
        url = _safe_url(it.get("url"))
        text = esc(_item_text(it))
        if url:
            text = (f'<a href="{esc(url)}" target="_blank" '
                    f'rel="noopener noreferrer nofollow">{text}</a>')
        out.append(f'<div class="ri"><span class="rs">{esc(str(it.get("sender") or ""))}</span> '
                   f'<span class="rt">{esc(str(it.get("time") or ""))}</span><div>{text}</div>')
        nested = _items(it)
        if nested:
            out.append(f"<details><summary>{text}</summary>{_html_items(nested)}</details>")
        out.append("</div>")
    out.append("</div>")
    return "".join(out)


def _html_body(r):
    """Inner HTML of a non-image bubble (every string escaped)."""
    esc = html.escape
    e = _extra(r)
    kind = r.get("kind")
    text = r.get("text") or ""
    q = e.get("quote")
    if kind == "quote" and isinstance(q, dict):
        return (f"{esc(str(e.get('reply') or ''))}<div class=\"quote\">"
                f"{esc(str(q.get('sender') or ''))}: {esc(str(q.get('text') or ''))}</div>")
    url = _safe_url(e.get("url"))
    if kind in ("link", "music") and url:
        label = "[音乐] " if kind == "music" else "[链接] "
        title = str(e.get("title") or url)
        parts = [f'{label}<a href="{esc(url)}" target="_blank" '
                 f'rel="noopener noreferrer nofollow">{esc(title)}</a>']
        desc = str(e.get("desc") or e.get("artist") or "").strip()
        if desc:
            parts.append(f'<div class="meta">{esc(desc[:300])}</div>')
        if e.get("source"):
            parts.append(f'<div class="meta">{esc(str(e["source"]))}</div>')
        return "".join(parts)
    items = _items(r)
    if items:
        return f"<details><summary>{esc(text)}</summary>{_html_items(items)}</details>"
    return esc(text)


def write_html(records, meta, fp, base_dir=None, **_):
    """Single self-contained HTML page, chat-bubble layout.

    Not appendable: for incremental export re-render the whole file from the
    full record list.
    """
    esc = html.escape
    name = meta.get("name") or ""
    group = bool(meta.get("is_group"))
    fp.write("<!DOCTYPE html>\n<html lang=\"zh\">\n<head>\n<meta charset=\"utf-8\">\n"
             "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
             "<meta name=\"color-scheme\" content=\"light dark\">\n"
             f"<title>{esc(name)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
             f"<header>{esc(name)}</header>\n<main>\n")
    cur = None
    for r in records:
        dt = _dt(r["ts"])
        day = dt.strftime("%Y-%m-%d")
        if day != cur:
            fp.write(f"<div class=\"day\"><span>{day}</span></div>\n")
            cur = day
        text = r.get("text") or ""
        hm = dt.strftime("%H:%M")
        if r.get("kind") in ("system", "pat"):
            fp.write(f"<div class=\"sys\" title=\"{dt.strftime(TIME_FMT)}\">{esc(text)}</div>\n")
            continue
        self_ = bool(r.get("is_self"))
        parts = [f"<div class=\"msg{' self' if self_ else ''}\">"]
        if group and not self_:
            parts.append(f"<div class=\"name\">{esc(r.get('sender') or '')}</div>")
        img = _img(r)
        if img:
            parts.append(f"<div class=\"bubble img\"><img loading=\"lazy\" "
                         f"src=\"{esc(_rel_image(img, base_dir))}\" alt=\"{esc(text)}\"></div>")
        else:
            kind = re.sub(r"[^a-z_]", "", str(r.get("kind") or ""))
            parts.append(f"<div class=\"bubble k-{kind}\">{_html_body(r)}</div>")
        parts.append(f"<div class=\"time\" title=\"{dt.strftime(TIME_FMT)}\">{hm}</div></div>\n")
        fp.write("".join(parts))
    fp.write("</main>\n</body>\n</html>\n")


# ---------------------------------------------------------------- json

def write_json(records, meta, fp, base_dir=None, **_):
    """``{"chat": meta, "messages": [record + "time" ISO string]}``.

    Not appendable: load, extend ``messages`` and re-render instead.
    image_path is rewritten relative to base_dir.
    """
    msgs = []
    for r in records:
        m = dict(r)
        m["time"] = _dt(r["ts"]).isoformat()
        if _img(r):
            rel = os.path.relpath(os.path.abspath(r["image_path"]),
                                  os.path.abspath(base_dir or "."))
            m["image_path"] = rel.replace(os.sep, "/")
        msgs.append(m)
    json.dump({"chat": dict(meta), "messages": msgs}, fp, ensure_ascii=False, indent=2)
    fp.write("\n")


# ---------------------------------------------------------------- csv

CSV_COLUMNS = ["time", "sender", "is_self", "kind", "text"]


def write_csv(records, meta, fp, base_dir=None, header=True, **_):
    """CSV with columns time,sender,is_self,kind,text.

    Open the file with ``encoding="utf-8-sig", newline=""`` (``write`` does)
    so Excel detects UTF-8. ``header=False`` when appending.
    """
    w = csv.writer(fp)
    if header:
        w.writerow(CSV_COLUMNS)
    for r in records:
        w.writerow([_dt(r["ts"]).strftime(TIME_FMT), r.get("sender", ""),
                    "1" if r.get("is_self") else "0", r.get("kind", ""),
                    r.get("text") or ""])


# ---------------------------------------------------------------- dispatch

FORMATS = {
    "txt": write_txt,
    "md": write_markdown,
    "html": write_html,
    "json": write_json,
    "csv": write_csv,
}

_APPENDABLE = {"txt", "md", "csv"}


def ext_for(fmt):
    """File extension (with dot) for a format name."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format: {fmt!r} (choose from {', '.join(FORMATS)})")
    return "." + fmt


def supports_append(fmt):
    """True if new records can be appended to an existing file of this format."""
    return fmt in _APPENDABLE


def write(records, meta, path, fmt, append=False):
    """Write records to ``path`` in ``fmt``; image paths relative to its dir.

    ``append=True`` (txt/md/csv only) appends to an existing file; for md the
    last day heading is detected so it is not repeated, for csv the header is
    skipped if the file is non-empty. html/json raise ValueError: re-render.
    """
    if fmt not in FORMATS:
        raise ValueError(f"unknown format: {fmt!r} (choose from {', '.join(FORMATS)})")
    if append and not supports_append(fmt):
        raise ValueError(f"{fmt} does not support append; re-render the whole file")
    base_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(base_dir, exist_ok=True)
    exists = append and os.path.exists(path) and os.path.getsize(path) > 0
    kw = {}
    if fmt == "md" and exists:
        kw["prev_date"] = _last_md_date(path) or ""
        kw["header"] = False
    if fmt == "csv":
        kw["header"] = not exists
    encoding = "utf-8-sig" if fmt == "csv" and not exists else "utf-8"
    newline = "" if fmt == "csv" else None
    with open(path, "a" if exists else "w", encoding=encoding, newline=newline) as fp:
        FORMATS[fmt](records, meta, fp, base_dir=base_dir, **kw)
