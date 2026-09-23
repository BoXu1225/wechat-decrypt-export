#!/usr/bin/env python3
"""
Favorites (收藏) from a decrypted WeChat 4.x favorite/favorite.db: parse,
search, resolve names, map local files, export (html / md / json / txt).

    ./wechat favorites                   # everything -> export/favorites.html
    ./wechat favorites 发票 --type file   # search + filter by kind
    ./wechat favorites -f md --images

Library use::

    favs = load_favorites(decrypted_dir)          # newest first
    resolve_names(favs, chats.load_contacts(decrypted_dir), self_wxid)
    hits = search(favs, "query", kind="link", since=..., until=...)

Storage facts (WeChat 4.x macOS):
  * fav_db_item(local_id, server_id, type, update_time, content, fromusr,
    realchatname, ...): one row per favorite; content is XML <favitem type=N>:
        title, desc (text favorites), source@sourcetype{fromusr, tousr,
        realchatname, createtime, msgid, link}, datalist/dataitem@datatype{
        datatitle, datadesc, datafmt, fullsize, fullmd5, thumbfullmd5,
        head256md5, cdn_dataurl, stream_weburl, datasrcname, datasrctime,
        dataitemsource{...}}, weburlitem{pagetitle, pagedesc, clean_url},
        locitem{lat, lng, label, poiname}, taglist/tag.
    fromusr is the chat (a person or a @chatroom) the item came from;
    realchatname is the actual sender inside a group.
  * fav_tag_db_item / fav_bind_tag_db_item: user tags (also in taglist).
  * favorite_fts.db uses WeChat's private MMFtsTokenizer; stock SQLite cannot
    query it, so search() here is a plain substring scan (fine for the usual
    few hundred items).
  * Local files: <wechat_base_dir>/business/favorite/{data,mid,thumb}/<xx>/<md5>,
    V2 .dat encrypted (same key as chat images) or plain. The file name is
    not derivable from the item, but md5(decrypted content) equals the
    dataitem's fullmd5 (data) / thumbfullmd5 (thumb), so files are matched by
    content hash (see FileIndex).
"""
import argparse
import glob
import hashlib
import json
import os
import sqlite3
import sys
import time

import social_common as S

FAV_DB = os.path.join("favorite", "favorite.db")

KINDS = {1: "text", 2: "image", 3: "voice", 4: "video", 5: "link", 6: "location",
         7: "music", 8: "file", 10: "product", 12: "tv", 14: "chat_record", 16: "video",
         17: "chat_record", 18: "note", 19: "miniprogram", 20: "channels"}
KIND_LABEL = {"text": "文字", "image": "图片", "voice": "语音", "video": "视频", "link": "链接",
              "location": "位置", "music": "音乐", "file": "文件", "product": "商品", "tv": "视频",
              "chat_record": "聊天记录", "note": "笔记", "miniprogram": "小程序",
              "channels": "视频号", "other": "其他"}
IMAGE_EXTS = ("jpg", "jpeg", "png", "gif", "webp", "bmp", "heic")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _source(e):
    if e is None:
        return {}
    d = {"type": S.to_int(e.get("sourcetype"), 0) or None,
         "from": S.xtext(e, "fromusr") or None,
         "to": S.xtext(e, "tousr") or None,
         "sender": S.xtext(e, "realchatname") or None,
         "msgid": S.xtext(e, "msgid") or None,
         "ts": S.to_int(S.xtext(e, "createtime")) or None,
         "link": S.xtext(e, "link") or None}
    return {k: v for k, v in d.items() if v is not None}


def _dataitem(e):
    dt = S.to_int(e.get("datatype"), 0)
    d = {"datatype": dt, "kind": KINDS.get(dt, "other"),
         "title": S.xtext(e, "datatitle"), "desc": S.xtext(e, "datadesc"),
         "fmt": S.xtext(e, "datafmt").lower().lstrip("."),
         "size": S.to_int(S.xtext(e, "fullsize") or S.xtext(e, "datasize")) or None,
         "fullmd5": S.xtext(e, "fullmd5").lower() or None,
         "thumbmd5": S.xtext(e, "thumbfullmd5").lower() or None,
         "url": S.xtext(e, "stream_weburl") or S.xtext(e, "weburl") or None,
         "sender_name": S.xtext(e, "datasrcname") or None,
         "time": S.xtext(e, "datasrctime") or None,
         "duration": S.to_int(S.xtext(e, "duration")) or None,
         "source": _source(e.find("dataitemsource"))}
    return {k: v for k, v in d.items() if v not in (None, "", {})}


def _location(root):
    loc = root.find("locitem")
    if loc is None:
        return None
    d = {"label": S.xtext(loc, "label"), "poiname": S.xtext(loc, "poiname"),
         "latitude": S.to_float(S.xtext(loc, "lat")), "longitude": S.to_float(S.xtext(loc, "lng"))}
    if not (d["label"] or d["poiname"] or d["latitude"]):
        return None
    d["name"] = d["poiname"] or d["label"]
    return d


def parse_favorite(content, local_id=None, ftype=None, update_time=None, server_id=None):
    """fav_db_item row -> favorite dict, or None if the XML is unparseable.

    Keys: id, server_id, type, kind, ts, title, desc, url, source{type, from,
    to, sender, msgid, ts, link}, tags[], location, items[]."""
    root = S.parse_xml(S.blob_text(content))
    if root is None:
        return None
    t = S.to_int(root.get("type"), 0) or S.to_int(ftype, 0)
    items = [_dataitem(d) for d in root.findall("datalist/dataitem")]
    web = root.find("weburlitem")
    first = items[0] if items else {}
    kind = KINDS.get(t, "other")
    title = (S.xtext(root, "title") or S.xtext(web, "pagetitle")
             or (first.get("title") if kind != "chat_record" else "") or "")
    desc = S.xtext(root, "desc") or S.xtext(web, "pagedesc")
    if not desc and len(items) == 1:
        desc = first.get("desc", "")
    url = (S.xtext(web, "clean_url") or S.xtext(root, "source/link")
           or first.get("url") or "")
    tags = [x.text.strip() for x in root.findall("taglist/tag") if x.text and x.text.strip()]
    return {"id": local_id, "server_id": server_id, "type": t, "kind": kind,
            "ts": S.to_int(update_time) or S.to_int(S.xtext(root, "source/createtime")),
            "title": title, "desc": desc, "url": url,
            "source": _source(root.find("source")), "tags": tags,
            "location": _location(root), "items": items}


def fav_db_path(decrypted_dir):
    return os.path.join(decrypted_dir, FAV_DB)


def _db_tags(conn):
    """{fav_local_id: [tag names]} from the tag tables (empty if absent)."""
    names = {S.blob_text(r[0]) for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"fav_tag_db_item", "fav_bind_tag_db_item"} <= names:
        return {}
    out = {}
    for fav, name in conn.execute(
            "SELECT b.fav_local_id, t.name FROM fav_bind_tag_db_item b "
            "JOIN fav_tag_db_item t ON t.local_id = b.tag_local_id"):
        name = S.blob_text(name)
        if name:
            out.setdefault(fav, []).append(name)
    return out


def load_favorites(decrypted_dir, stats=None):
    """All parseable favorites, newest first. stats (dict) gets rows/parsed/failed."""
    path = fav_db_path(decrypted_dir)
    favs, rows, failed = [], 0, 0
    if os.path.exists(path):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.text_factory = bytes
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='fav_db_item'").fetchone():
                tags = _db_tags(conn)
                for lid, sid, ftype, ut, content in conn.execute(
                        "SELECT local_id, server_id, type, update_time, content FROM fav_db_item"):
                    rows += 1
                    f = parse_favorite(content, lid, ftype, ut, sid)
                    if f is None:
                        failed += 1
                        continue
                    for tg in tags.get(lid, []):
                        if tg not in f["tags"]:
                            f["tags"].append(tg)
                    favs.append(f)
        finally:
            conn.close()
    favs.sort(key=lambda f: (f["ts"], f["id"] or 0), reverse=True)
    if stats is not None:
        stats.update(rows=rows, parsed=rows - failed, failed=failed)
    return favs


# ---------------------------------------------------------------------------
# Names / search
# ---------------------------------------------------------------------------

def _name(u, contacts):
    return contacts.get(u) or u


def resolve_names(favs, contacts, self_wxid=None):
    """Add source names in place: from_name (chat), sender_name (who wrote it:
    realchatname in groups, else from), is_self. Returns favs."""
    for f in favs:
        src = f["source"]
        frm, snd = src.get("from"), src.get("sender")
        if frm:
            src["from_name"] = _name(frm, contacts)
        who = snd or (frm if frm and not frm.endswith("@chatroom") else None)
        if who:
            src["sender_username"] = who
            src["sender_name"] = "我" if self_wxid and who == self_wxid else _name(who, contacts)
        if frm and frm.endswith("@chatroom"):
            src["chat_name"] = src.get("from_name")
        f["is_self"] = bool(self_wxid) and who == self_wxid
    return favs


def related_usernames(f):
    """Usernames a favorite came from (chat and sender), for access policies."""
    s = f.get("source", {})
    out = {s.get("from"), s.get("sender")}
    for it in f.get("items", []):
        src = it.get("source", {})
        out.update((src.get("from"), src.get("sender")))
    return {u for u in out if u}


def search_text(f):
    parts = [f.get("title"), f.get("desc"), f.get("url"), " ".join(f.get("tags", []))]
    s = f.get("source", {})
    parts += [s.get("from_name"), s.get("sender_name"), (f.get("location") or {}).get("name")]
    for it in f.get("items", []):
        parts += [it.get("title"), it.get("desc"), it.get("sender_name")]
    return "\n".join(p for p in parts if p).lower()


def search(favs, query=None, kind=None, since=None, until=None, tag=None):
    """Filter favorites: query terms (space-separated, all must occur,
    case-insensitive substring over title/desc/url/tags/names/items), kind
    (e.g. "link") or type number, time range (since incl., until excl.), tag."""
    terms = [t.lower() for t in (query or "").split()]
    out = []
    for f in favs:
        if kind not in (None, ""):
            if str(kind).isdigit():
                if f["type"] != int(kind):
                    continue
            elif f["kind"] != kind:
                continue
        if not S.in_range(f["ts"], since, until):
            continue
        if tag and tag not in f["tags"]:
            continue
        if terms:
            hay = search_text(f)
            if not all(t in hay for t in terms):
                continue
        out.append(f)
    return out


def summary_text(f, width=200):
    """One-line text for listings."""
    s = f.get("title") or f.get("desc") or ""
    if not s and f.get("items"):
        it = f["items"][0]
        s = it.get("title") or it.get("desc") or ""
    if not s and f.get("location"):
        s = f["location"]["name"]
    s = " ".join(s.split())
    return s if len(s) <= width else s[:width] + "…"


# ---------------------------------------------------------------------------
# Local files
# ---------------------------------------------------------------------------

class FileIndex:
    """Maps content md5 -> decoded file for business/favorite/{data,mid,thumb}.

    Building decrypts every file once (they are few); results are cached."""

    def __init__(self, wechat_base_dir, keys=(None, None)):
        self.base = wechat_base_dir or ""
        self.aes_key, self.xor_key = keys
        self._index = None

    def _read(self, path):
        import image_decode
        with open(path, "rb") as f:
            raw = f.read()
        if image_decode.dat_format(raw[:6]) in ("v1", "v2"):
            try:
                data, _ = image_decode.decrypt_dat(raw, self.aes_key, self.xor_key)
            except Exception:
                return None
            return data
        return raw

    @property
    def index(self):
        if self._index is None:
            idx = {}
            for sub in ("data", "mid", "thumb"):
                for p in glob.glob(os.path.join(glob.escape(self.base), "business", "favorite",
                                                sub, "*", "*")):
                    if not os.path.isfile(p):
                        continue
                    data = self._read(p)
                    if data is not None:
                        idx.setdefault(hashlib.md5(data).hexdigest(), (p, sub))
            self._index = idx
        return self._index

    def get(self, md5):
        """(path, variant) or None."""
        return self.index.get((md5 or "").lower()) if md5 else None

    def data(self, md5):
        hit = self.get(md5)
        return self._read(hit[0]) if hit else None

    def annotate(self, favs):
        """items[*]["local"] = list of available variants ("data", "thumb")."""
        for f in favs:
            for it in f["items"]:
                have = []
                if self.get(it.get("fullmd5")):
                    have.append("data")
                if self.get(it.get("thumbmd5")):
                    have.append("thumb")
                if have:
                    it["local"] = have
        return favs


class FileExporter:
    """Writes local favorite files into files_dir (image_path / file_path)."""

    def __init__(self, index, files_dir):
        self.index = index
        self.files_dir = files_dir
        self.written = self.reused = self.missing = 0

    def _write(self, md5, fmt):
        import image_decode
        data = self.index.data(md5)
        if data is None:
            return None
        ext = image_decode.detect_ext(data) or (fmt if fmt and fmt.isalnum() else "bin")
        out = os.path.join(self.files_dir, f"{md5}.{ext}")
        if os.path.exists(out):
            self.reused += 1
            return out
        os.makedirs(self.files_dir, exist_ok=True)
        if ext == "wxgf":
            try:
                data, ext = image_decode.heic_convert(image_decode.wxgf_to_heic(data)), "jpg"
                out = os.path.join(self.files_dir, f"{md5}.{ext}")
            except Exception:
                pass
        with open(out, "wb") as fh:
            fh.write(data)
        self.written += 1
        return out

    def attach(self, favs):
        for f in favs:
            for it in f["items"]:
                full = it.get("fullmd5") and self.index.get(it["fullmd5"])
                if full:
                    p = self._write(it["fullmd5"], it.get("fmt"))
                    if p:
                        ext = os.path.splitext(p)[1].lstrip(".").lower()
                        it["image_path" if ext in IMAGE_EXTS else "file_path"] = p
                if "image_path" not in it and it.get("thumbmd5") and self.index.get(it["thumbmd5"]):
                    p = self._write(it["thumbmd5"], "jpg")
                    if p:
                        it["image_path"] = p
                if it["kind"] in ("image", "file", "video") and not (
                        it.get("image_path") or it.get("file_path")):
                    self.missing += 1
        return favs

    def summary(self):
        return f"新写入 {self.written}，复用 {self.reused}，本地无文件 {self.missing}"


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _source_line(f):
    s = f["source"]
    bits = []
    if s.get("sender_name"):
        bits.append(s["sender_name"])
    if s.get("chat_name") and s.get("chat_name") != s.get("sender_name"):
        bits.append(f"群: {s['chat_name']}")
    return " · ".join(bits)


def _record_lines(f):
    """Chat-record items as (sender, time, text)."""
    out = []
    for it in f["items"]:
        text = it.get("desc") or it.get("title") or f"[{KIND_LABEL.get(it['kind'], it['kind'])}]"
        out.append((it.get("sender_name") or "", it.get("time") or "", text))
    return out


def write_txt(favs, meta, fp, base_dir=None):
    for f in favs:
        head = f"[{S.fmt_time(f['ts'])}] [{KIND_LABEL.get(f['kind'], f['kind'])}]"
        src = _source_line(f)
        fp.write(f"{head} {src}".rstrip() + "\n")
        if f["title"]:
            fp.write(f"  {f['title']}\n")
        if f["desc"] and f["desc"] != f["title"]:
            fp.write("  " + f["desc"].replace("\n", "\n  ") + "\n")
        if f["url"]:
            fp.write(f"  {f['url']}\n")
        if f["location"]:
            fp.write(f"  位置: {f['location']['name']}\n")
        if f["kind"] in ("chat_record", "note"):
            for who, t, text in _record_lines(f):
                fp.write(f"    {who} {t}: {text}\n".replace("  :", ":"))
        elif len(f["items"]) > 1 or (f["items"] and not (f["title"] or f["desc"])):
            for it in f["items"]:
                label = it.get("title") or it.get("desc") or ""
                fp.write(f"    [{KIND_LABEL.get(it['kind'], it['kind'])}] {label}".rstrip() + "\n")
        if f["tags"]:
            fp.write("  标签: " + ", ".join(f["tags"]) + "\n")
        fp.write("\n")


def write_md(favs, meta, fp, base_dir=None):
    e = S.md_escape
    fp.write(f"# {e(meta.get('title', '收藏'))}\n\n")
    for f in favs:
        label = KIND_LABEL.get(f["kind"], f["kind"])
        title = f["title"] or summary_text(f, 60) or label
        fp.write(f"## {e(title)}\n\n")
        info = [S.fmt_time(f["ts"], "%Y-%m-%d %H:%M"), label]
        if _source_line(f):
            info.append(e(_source_line(f)))
        if f["tags"]:
            info.append("标签: " + e(", ".join(f["tags"])))
        fp.write("*" + " · ".join(info) + "*\n\n")
        if f["desc"] and f["desc"] != f["title"]:
            fp.write(e(f["desc"]).replace("\n", "  \n") + "\n\n")
        url = S.safe_url(f["url"])
        if url:
            fp.write(f"<{url}>\n\n")
        if f["location"]:
            fp.write(f"位置: {e(f['location']['name'])}\n\n")
        if f["kind"] in ("chat_record", "note"):
            for who, t, text in _record_lines(f):
                fp.write(f"> **{e(who)}** {e(t)}  \n> {e(text).replace(chr(10), '  ' + chr(10) + '> ')}\n>\n")
            fp.write("\n")
        for it in f["items"]:
            if it.get("image_path"):
                fp.write(f"![]({S.rel_path(it['image_path'], base_dir)})\n\n")
            elif it.get("file_path"):
                name = e(it.get("title") or os.path.basename(it["file_path"]))
                fp.write(f"[{name}]({S.rel_path(it['file_path'], base_dir)})\n\n")


_CSS = """
.fav .top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.fav .kind{font-size:11px;color:#fff;background:var(--accent);border-radius:9px;padding:0 7px;flex:none}
.fav .title{font-weight:600;margin:4px 0}
.fav .imgs{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.fav .imgs img{max-width:100%;max-height:320px;border-radius:6px;display:block}
.rec{background:var(--soft);border-radius:6px;padding:6px 10px;margin:6px 0;font-size:14px}
.rec div{margin:3px 0;white-space:pre-wrap}
.rec b{color:var(--link);font-weight:500}
.file{display:inline-block;background:var(--soft);border-radius:6px;padding:6px 10px;margin:4px 0}
"""


def _fav_html(f, base_dir):
    e = S.esc
    label = KIND_LABEL.get(f["kind"], f["kind"])
    parts = [f'<article class="card fav" id="f{e(str(f["id"]))}"><div class="top">'
             f'<span class="muted">{e(S.fmt_time(f["ts"], "%Y-%m-%d %H:%M"))}'
             f'{" · " + e(_source_line(f)) if _source_line(f) else ""}</span>'
             f'<span class="kind">{e(label)}</span></div>']
    url = S.safe_url(f["url"])
    if f["kind"] == "link" or (url and f["kind"] not in ("text", "chat_record", "note")):
        inner = (f'<div><div class="lt">{e(f["title"] or url)}</div>'
                 f'<div class="ld">{e(f["desc"] or S.url_host(url))}</div></div>')
        parts.append(f'<a class="linkbox" href="{e(url)}" target="_blank" rel="noopener noreferrer">{inner}</a>'
                     if url else f'<div class="linkbox">{inner}</div>')
    else:
        if f["title"]:
            parts.append(f'<div class="title">{e(f["title"])}</div>')
        if f["desc"] and f["desc"] != f["title"]:
            parts.append(f'<div class="text">{e(f["desc"])}</div>')
    if f["location"]:
        parts.append(f'<div class="muted">位置: {e(f["location"]["name"])}</div>')
    if f["kind"] in ("chat_record", "note") and f["items"]:
        rows = []
        for who, t, text in _record_lines(f):
            head = f'<b>{e(who)}</b> <span class="muted">{e(t)}</span><br>' if who or t else ""
            rows.append(f"<div>{head}{e(text)}</div>")
        parts.append(f'<div class="rec">{"".join(rows)}</div>')
    imgs = [it for it in f["items"] if it.get("image_path")]
    if imgs:
        parts.append('<div class="imgs">' + "".join(
            f'<a href="{e(S.rel_path(it["image_path"], base_dir))}" target="_blank" rel="noopener">'
            f'<img loading="lazy" src="{e(S.rel_path(it["image_path"], base_dir))}" alt=""></a>'
            for it in imgs) + '</div>')
    for it in f["items"]:
        if it.get("file_path"):
            parts.append(f'<a class="file" href="{e(S.rel_path(it["file_path"], base_dir))}">'
                         f'{e(it.get("title") or os.path.basename(it["file_path"]))}</a>')
        elif it["kind"] in ("image", "file", "video") and not it.get("image_path") \
                and f["kind"] not in ("chat_record", "note"):
            name = it.get("title") or KIND_LABEL.get(it["kind"], it["kind"])
            parts.append(f'<div class="ph" style="height:48px;margin:4px 0">{e(name)}（本地无文件）</div>')
    if f["tags"]:
        parts.append("<div>" + "".join(f'<span class="tag">{e(t)}</span>' for t in f["tags"]) + "</div>")
    parts.append("</article>\n")
    return "".join(parts)


def write_html(favs, meta, fp, base_dir=None):
    body = "".join(_fav_html(f, base_dir) for f in favs) or \
        '<p class="muted" style="text-align:center">没有收藏</p>'
    fp.write(S.page(meta.get("title", "收藏"), meta.get("subtitle", ""), body, _CSS))


def to_json_fav(f, base_dir=None):
    d = dict(f)
    d["time"] = S.iso(f["ts"])
    d["items"] = []
    for it in f["items"]:
        it = dict(it)
        for k in ("image_path", "file_path"):
            if it.get(k):
                it[k] = S.rel_plain(it[k], base_dir)
        d["items"].append(it)
    return d


def write_json(favs, meta, fp, base_dir=None):
    json.dump({"meta": meta, "favorites": [to_json_fav(f, base_dir) for f in favs]}, fp,
              ensure_ascii=False, indent=2)
    fp.write("\n")


WRITERS = {"txt": write_txt, "md": write_md, "html": write_html, "json": write_json}


def write(favs, meta, path, fmt):
    if fmt not in WRITERS:
        raise ValueError(f"unknown format: {fmt!r}")
    base_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(base_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        WRITERS[fmt](favs, meta, fp, base_dir=base_dir)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="wechat favorites", description="导出微信收藏（文字、链接、图片、文件、聊天记录、笔记等）",
        epilog="示例: ./wechat favorites 发票 --type file -f md")
    p.add_argument("query", nargs="?", help="只导出包含这些词的收藏（空格分隔，全部匹配）")
    p.add_argument("--type", dest="kind", choices=sorted(set(KINDS.values()) | {"other"}),
                   help="只导出该类型")
    p.add_argument("--tag", help="只导出带该标签的收藏")
    S.add_common_args(p)
    return p


def main(argv=None, cfg=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    since, until = S.time_range(parser, args)
    cfg, decrypted_dir, export_dir = S.setup(args, cfg)
    if not os.path.exists(fav_db_path(decrypted_dir)):
        print(f"[!] 未找到收藏数据库: {fav_db_path(decrypted_dir)}")
        return 1
    import chats as chatlib
    stats = {}
    favs = load_favorites(decrypted_dir, stats=stats)
    resolve_names(favs, chatlib.load_contacts(decrypted_dir), cfg.get("self_wxid"))
    favs = search(favs, args.query, args.kind, since, until, args.tag)
    path = S.output_path(export_dir, "favorites", args.format, args.output)
    exporter = None
    if args.images:
        index = FileIndex(cfg.get("wechat_base_dir"), S.image_keys(cfg))
        exporter = FileExporter(index, S.files_dir_for(path))
        exporter.attach(favs)
    filt = " ".join(x for x in (args.query, args.kind, args.tag) if x)
    meta = {"title": "收藏" + (f" · {filt}" if filt else ""),
            "subtitle": f"{len(favs)} 条", "count": len(favs),
            "exported_at": S.iso(int(time.time()))}
    write(favs, meta, path, args.format)
    print(f"[+] 数据库中 {stats.get('rows', 0)} 条收藏（解析失败 {stats.get('failed', 0)}），"
          f"导出 {len(favs)} 条 -> {path}")
    if exporter:
        print(f"[+] 本地文件: {exporter.summary()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] 已取消")
        sys.exit(130)
