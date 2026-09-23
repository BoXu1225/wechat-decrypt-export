"""Output writers for exported chats (pure; no DB access; stdlib only).

Every writer consumes the same record schema::

    {
        "ts": int,              # unix seconds
        "sender": str,          # display name
        "is_self": bool,
        "kind": str,            # text|image|voice|video|emoji|location|link|
                                # file|miniprogram|quote|system|other
        "text": str,            # display text, e.g. message body or "[图片]"
        "image_path": str|None, # optional path to a decoded image file
        "audio_path": str|None, # optional voice file (m4a/wav)
        "video_path": str|None, # optional video file (mp4)
        "poster_path": str|None,# optional video thumbnail
        "duration": int|None,   # optional voice/video length in whole seconds
    }

and chat metadata ``{"name": str, "is_group": bool}``.

Writers have the signature ``writer(records, meta, fp, base_dir=None, **kw)``
where ``fp`` is an open text file and ``base_dir`` is the directory the output
file lives in (media paths are written relative to it; defaults to cwd).

Voice / video records with a ``duration`` are labelled ``[语音 12″]`` /
``[视频 0:35]`` in txt/csv (and wherever there is no playable file); records
without it keep their plain ``text``, so output without media is unchanged.

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


def fmt_duration(kind, sec):
    """12 -> '12″' for voice; 35 -> '0:35' (1:02:03 past an hour) for video."""
    sec = int(sec)
    if kind == "voice":
        return f"{sec}\u2033"
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def label(rec):
    """Display text: ``[语音 12″]`` / ``[视频 0:35]`` when the record has a
    duration, else the record's text."""
    kind, dur = rec.get("kind"), rec.get("duration")
    if dur is not None and kind in ("voice", "video"):
        return f"[{'语音' if kind == 'voice' else '视频'} {fmt_duration(kind, dur)}]"
    return rec.get("text") or ""


_MEDIA_KEYS = ("image_path", "audio_path", "video_path", "poster_path")


# ---------------------------------------------------------------- txt

def write_txt(records, meta, fp, base_dir=None, **_):
    """``[YYYY-MM-DD HH:MM:SS] sender: text`` one record per line (local time).

    Byte-identical to the legacy export_chat.py output.
    """
    for r in records:
        fp.write(f"[{_dt(r['ts']).strftime(TIME_FMT)}] {r['sender']}: {label(r)}\n")


# ---------------------------------------------------------------- markdown

def _md_escape(s):
    """Neutralise raw HTML: many markdown viewers render inline HTML, so a
    message containing ``<script>``/``<img onerror=...>`` must not become a tag."""
    return (s or "").replace("<", "&lt;")


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
        text = _md_escape(label(r))
        img = _img(r)
        audio, video, poster = r.get("audio_path"), r.get("video_path"), r.get("poster_path")
        if img:
            body = f"![{text}]({_rel_image(img, base_dir)})"
        elif audio or video:
            link = _rel_image(audio or video, base_dir)
            desc = f"▶ {text[1:-1]}" if text.startswith("[") and text.endswith("]") else text
            if video and poster:
                body = f"[![{desc}]({_rel_image(poster, base_dir)})]({link})"
            else:
                body = f"[{desc}]({link})"
        elif poster:
            body = f"![{text}]({_rel_image(poster, base_dir)})"
        else:
            # keep continuation lines inside the same paragraph
            body = text.replace("\n", "  \n")
        fp.write(f"**{_md_escape(r['sender'])}** {dt.strftime('%H:%M')}  {body}\n\n")


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
.bubble img,.bubble video{display:block;max-width:100%;max-height:360px;border-radius:6px}
.bubble.voice{display:flex;align-items:center;gap:8px;padding:5px 8px}
.bubble audio{height:36px;max-width:260px}
.dur{font-size:12px;color:var(--muted);white-space:nowrap}
.self .dur{color:inherit;opacity:.7}
.bubble.img .dur{margin:2px 4px 0}
.time{font-size:11px;color:var(--muted);margin:2px 6px 0}
.sys{text-align:center;color:var(--muted);font-size:12px;margin:8px 0;white-space:pre-wrap}
"""


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
        text = label(r)
        hm = dt.strftime("%H:%M")
        if r.get("kind") == "system":
            fp.write(f"<div class=\"sys\" title=\"{dt.strftime(TIME_FMT)}\">{esc(text)}</div>\n")
            continue
        self_ = bool(r.get("is_self"))
        parts = [f"<div class=\"msg{' self' if self_ else ''}\">"]
        if group and not self_:
            parts.append(f"<div class=\"name\">{esc(r.get('sender') or '')}</div>")
        img = _img(r)
        audio, video, poster = r.get("audio_path"), r.get("video_path"), r.get("poster_path")
        dur = r.get("duration")
        dur_html = (f"<span class=\"dur\">{esc(fmt_duration(r.get('kind'), dur))}</span>"
                    if dur is not None and r.get("kind") in ("voice", "video") else "")
        if img:
            parts.append(f"<div class=\"bubble img\"><img loading=\"lazy\" "
                         f"src=\"{esc(_rel_image(img, base_dir))}\" alt=\"{esc(text)}\"></div>")
        elif audio:
            parts.append(f"<div class=\"bubble voice\"><audio controls preload=\"none\" "
                         f"src=\"{esc(_rel_image(audio, base_dir))}\" title=\"{esc(text)}\">"
                         f"</audio>{dur_html}</div>")
        elif video:
            poster_attr = (f" poster=\"{esc(_rel_image(poster, base_dir))}\"" if poster else "")
            parts.append(f"<div class=\"bubble img\"><video controls preload=\"none\"{poster_attr} "
                         f"src=\"{esc(_rel_image(video, base_dir))}\" title=\"{esc(text)}\">"
                         f"</video>{dur_html}</div>")
        elif poster:  # video file not downloaded: show its thumbnail
            parts.append(f"<div class=\"bubble img\"><img loading=\"lazy\" "
                         f"src=\"{esc(_rel_image(poster, base_dir))}\" alt=\"{esc(text)}\">"
                         f"<span class=\"dur\">{esc(text)}</span></div>")
        else:
            parts.append(f"<div class=\"bubble\">{esc(text)}</div>")
        parts.append(f"<div class=\"time\" title=\"{dt.strftime(TIME_FMT)}\">{hm}</div></div>\n")
        fp.write("".join(parts))
    fp.write("</main>\n</body>\n</html>\n")


# ---------------------------------------------------------------- json

def write_json(records, meta, fp, base_dir=None, **_):
    """``{"chat": meta, "messages": [record + "time" ISO string]}``.

    Not appendable: load, extend ``messages`` and re-render instead.
    image/audio/video/poster paths are rewritten relative to base_dir.
    """
    msgs = []
    for r in records:
        m = dict(r)
        m["time"] = _dt(r["ts"]).isoformat()
        for k in _MEDIA_KEYS:
            if r.get(k):
                rel = os.path.relpath(os.path.abspath(r[k]), os.path.abspath(base_dir or "."))
                m[k] = rel.replace(os.sep, "/")
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
                    label(r)])


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
