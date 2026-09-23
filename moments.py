#!/usr/bin/env python3
"""
Moments (朋友圈) from a decrypted WeChat 4.x sns/sns.db: parse, resolve names,
map media to the local cache, export (html / md / json / txt).

    ./wechat moments                 # every cached post -> export/moments.html
    ./wechat moments 张三 --images    # one person's posts with cached pictures/videos
    ./wechat moments --since 2025-01-01 -f md

Library use (pure functions returning dicts)::

    posts = load_posts(decrypted_dir)                       # newest first
    resolve_names(posts, chats.load_contacts(decrypted_dir), self_wxid)
    cache = MediaCache(cfg["wechat_base_dir"])
    cache.lookup(post["id"], media["id"])  -> {"image": path, "thumb": ..., "video": ...}

Storage facts (WeChat 4.x macOS, verified on real data):
  * SnsTimeLine(tid, user_name, content, pack_info_buf): one row per post the
    client has cached (own posts and friends' posts that were scrolled past).
    tid is the post id as a signed 64-bit int; TimelineObject/id is the same
    number unsigned. content is plain XML (zstd is handled just in case):
        <SnsDataItem>
          <TimelineObject> id, username, createTime, contentDesc (text), private,
            location@{poiName,poiAddress,city,country,latitude,longitude},
            ContentObject{type, title, description, contentUrl,
              mediaList/media{id,type,url,thumb,size@{width,height},videoDuration,LivePhoto},
              finderFeed{nickname,desc,...}, musicShareItem{mvSingerName,...}},
            sourceNickName, appInfo/appName
          <LocalExtraInfo> nickname (author's name at the time), like_user_list,
            comment_user_list, with_user_list: user_comment{username, nickname,
            create_time, content, ref_username, b_deleted, emojilist, imagelist}
  * Likes and comments are embedded in LocalExtraInfo; SnsMessage_tmp3 (the
    notification list) duplicates them and is not needed.
  * ContentObject/type: 1 images, 54 images incl. live photos, 7 images (other
    app), 2 text only, 3 link/article, 15 video, 5 video link, 28 Channels
    (视频号) video, 34 Channels live, 42 music, others rare.
  * media/type: 2 image, 6 video, others are link/music covers. Media URLs
    point at Tencent CDNs and are encrypted/tokenised; we never fetch them.
  * Local cache: <wechat_base_dir>/cache/<YYYY-MM>/Sns/{Img,Video}/<h[:2]>/<h[2:]>
    with h = md5(f"{post_id_unsigned}_{media_id}_{variant}"):
        variant 1 = thumbnail (~200px), 2 = full image, 6 = square grid thumb
        (240px), 3 = video (Video/..mp4, plain MP4; .tmp = partial download).
    Img files are V2 .dat (AES+XOR, same account key as chat images; see
    image_decode.py). <YYYY-MM> is when it was viewed, not posted.
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

import social_common as S

SNS_DB = os.path.join("sns", "sns.db")

POST_KINDS = {1: "image", 7: "image", 54: "image", 2: "text", 3: "link", 5: "video_link",
              15: "video", 28: "channels", 34: "channels_live", 42: "music"}
KIND_LABEL = {"image": "图片", "text": "文字", "link": "链接", "video_link": "视频链接",
              "video": "视频", "channels": "视频号", "channels_live": "视频号直播",
              "music": "音乐", "other": "其他"}
MEDIA_KINDS = {2: "image", 6: "video"}

VARIANT_THUMB, VARIANT_FULL, VARIANT_VIDEO, VARIANT_SQUARE = 1, 2, 3, 6

SELF_ALIASES = ("我", "me", "self")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def unsigned_id(tid):
    return str(int(tid) & 0xFFFFFFFFFFFFFFFF)


def _user_comment(e):
    """<user_comment> -> dict (None if deleted)."""
    if S.to_int(S.xtext(e, "b_deleted", "0")):
        return None
    d = {"username": S.xtext(e, "username"), "nickname": S.xtext(e, "nickname"),
         "ts": S.to_int(S.xtext(e, "create_time"))}
    return d


def _comment(e):
    d = _user_comment(e)
    if d is None:
        return None
    text = S.xtext(e, "content")
    extras = []
    if e.findall("imagelist/imageinfo"):
        extras.append("[图片]")
    if e.findall("emojilist/emojiinfo"):
        extras.append("[表情]")
    d["text"] = " ".join([text] + extras if text else extras)
    d["reply_to"] = S.xtext(e, "ref_username") or None
    d["id"] = S.xtext(e, "comment_id") or None
    return d


def _location(t):
    loc = t.find("location")
    if loc is None:
        return None
    a = loc.attrib
    name = (a.get("poiName") or "").strip()
    city = (a.get("city") or "").strip()
    address = (a.get("poiAddress") or "").strip()
    lat, lon = S.to_float(a.get("latitude")), S.to_float(a.get("longitude"))
    if lat == 0 and lon == 0:
        lat = lon = None
    if not (name or city or address or lat is not None):
        return None
    return {"name": name or city, "address": address, "city": city,
            "country": (a.get("country") or "").strip(), "latitude": lat, "longitude": lon}


def _media(m):
    size = m.find("size")
    w = S.to_int(size.get("width")) if size is not None else 0
    h = S.to_int(size.get("height")) if size is not None else 0
    mtype = S.to_int(S.xtext(m, "type"), -1)
    d = {"id": S.xtext(m, "id"), "type": MEDIA_KINDS.get(mtype, "cover"),
         "url": S.xtext(m, "url"), "thumb": S.xtext(m, "thumb"),
         "width": w, "height": h}
    dur = S.to_float(S.xtext(m, "videoDuration"))
    if dur:
        d["duration"] = dur
    if m.find("LivePhoto") is not None:
        d["live_photo"] = True
    title = S.xtext(m, "title")
    if title:
        d["title"] = title
    return d


def parse_post(content, tid=None, user_name=None):
    """SnsTimeLine.content (XML str/bytes) -> post dict, or None if unparseable.

    Keys: id (unsigned str), username, nickname, ts, type, kind, text, title,
    description, url, source, private, location, media[], likes[], comments[],
    with[]. Names are not resolved (see resolve_names)."""
    root = S.parse_xml(S.blob_text(content))
    if root is None:
        return None
    t = root.find("TimelineObject") if root.tag != "TimelineObject" else root
    if t is None:
        return None
    extra = root.find("LocalExtraInfo")
    co = t.find("ContentObject")
    ctype = S.to_int(S.xtext(co, "type"), 0)
    pid = S.xtext(t, "id") or (unsigned_id(tid) if tid is not None else "")
    post = {
        "id": pid,
        "username": S.xtext(t, "username") or (user_name or ""),
        "nickname": S.xtext(extra, "nickname"),
        "ts": S.to_int(S.xtext(t, "createTime")),
        "type": ctype,
        "kind": POST_KINDS.get(ctype, "other"),
        "text": (t.findtext("contentDesc") or "").strip(),
        "title": S.xtext(co, "title"),
        "description": S.xtext(co, "description"),
        "url": S.xtext(co, "contentUrl"),
        "source": (S.xtext(t, "sourceNickName") or S.xtext(co, "finderFeed/nickname")
                   or S.xtext(co, "finderLive/nickname")
                   or S.xtext(co, "musicShareItem/mvSingerName") or S.xtext(t, "appInfo/appName")),
        "private": S.xtext(t, "private") == "1",
        "location": _location(t),
        "media": [_media(m) for m in (co.findall("mediaList/media") if co is not None else [])],
        "likes": [], "comments": [], "with": [],
    }
    if not post["description"]:
        post["description"] = (S.xtext(co, "finderFeed/desc") or S.xtext(co, "finderLive/desc"))
    if extra is not None:
        post["likes"] = [x for x in map(_user_comment, extra.findall("like_user_list/user_comment"))
                         if x]
        post["comments"] = sorted(
            (x for x in map(_comment, extra.findall("comment_user_list/user_comment")) if x),
            key=lambda c: c["ts"])
        post["with"] = [u for u in (S.xtext(x, "username") for x in
                                    extra.findall("with_user_list/user_comment")) if u]
    return post


def sns_db_path(decrypted_dir):
    return os.path.join(decrypted_dir, SNS_DB)


def iter_raw_posts(decrypted_dir):
    """Yield (tid, user_name, content) from SnsTimeLine (empty if no DB)."""
    path = sns_db_path(decrypted_dir)
    if not os.path.exists(path):
        return
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.text_factory = bytes
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                            "name='SnsTimeLine'").fetchone():
            return
        for tid, user, content in conn.execute(
                "SELECT tid, user_name, content FROM SnsTimeLine"):
            yield tid, S.blob_text(user) or "", content
    finally:
        conn.close()


def load_posts(decrypted_dir, since=None, until=None, stats=None):
    """All parseable posts, newest first. since/until: unix seconds
    (since inclusive, until exclusive). stats (dict) gets rows/parsed/failed."""
    posts, rows, failed = [], 0, 0
    for tid, user, content in iter_raw_posts(decrypted_dir):
        rows += 1
        p = parse_post(content, tid, user)
        if p is None:
            failed += 1
            continue
        if S.in_range(p["ts"], since, until):
            posts.append(p)
    posts.sort(key=lambda p: (p["ts"], p["id"]), reverse=True)
    if stats is not None:
        stats.update(rows=rows, parsed=rows - failed, failed=failed)
    return posts


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def display_name(username, nickname, contacts):
    """contacts (remark > nickname) > nickname stored with the post > username."""
    n = contacts.get(username)
    if n and n != username:
        return n
    return nickname or n or username


def resolve_names(posts, contacts, self_wxid=None):
    """Add author / is_self to posts and name to likes, comments (and
    reply_to_name) in place. Returns posts."""
    for p in posts:
        p["author"] = display_name(p["username"], p["nickname"], contacts)
        p["is_self"] = bool(self_wxid) and p["username"] == self_wxid
        known = {p["username"]: p["author"]}
        for x in p["likes"] + p["comments"]:
            x["name"] = display_name(x["username"], x["nickname"], contacts)
            known.setdefault(x["username"], x["name"])
        for c in p["comments"]:
            if c.get("reply_to"):
                c["reply_to_name"] = known.get(c["reply_to"]) or display_name(
                    c["reply_to"], "", contacts)
        p["with_names"] = [display_name(u, "", contacts) for u in p["with"]]
    return posts


def author_names(posts, contact_rows=None):
    """{username: set(all known names)} for authors: post nicknames plus
    remark/nick/alias/display from chats.load_contact_rows()."""
    out = {}
    for p in posts:
        s = out.setdefault(p["username"], {p["username"]})
        for k in ("nickname", "author"):
            if p.get(k):
                s.add(p[k])
    for u, s in out.items():
        r = (contact_rows or {}).get(u)
        if r:
            s.update(v for v in (r.get("display"), r.get("remark"), r.get("nick_name"),
                                 r.get("alias")) if v)
    return out


def match_authors(query, posts, contact_rows=None, self_wxid=None):
    """Usernames of authors matching query: exact username/name first, else
    case-insensitive substring of any name. "我"/"me" means self_wxid."""
    q = (query or "").strip()
    if not q:
        return []
    if self_wxid and q.lower() in SELF_ALIASES:
        return [self_wxid]
    names = author_names(posts, contact_rows)
    exact = [u for u, s in names.items() if q in s]
    if exact:
        return sorted(exact)
    ql = q.lower()
    return sorted(u for u, s in names.items() if any(ql in n.lower() for n in s))


def post_search_text(p):
    parts = [p.get("text"), p.get("title"), p.get("description"), p.get("source"),
             (p.get("location") or {}).get("name"), (p.get("location") or {}).get("address")]
    parts += [c.get("text") for c in p.get("comments", [])]
    return "\n".join(x for x in parts if x).lower()


def filter_posts(posts, authors=None, query=None, since=None, until=None):
    """authors: iterable of usernames (None = all); query: space-separated
    terms, all must occur (case-insensitive) in text/title/desc/location/comments."""
    authors = set(authors) if authors is not None else None
    terms = [t.lower() for t in (query or "").split()]
    out = []
    for p in posts:
        if authors is not None and p["username"] not in authors:
            continue
        if not S.in_range(p["ts"], since, until):
            continue
        if terms:
            hay = post_search_text(p)
            if not all(t in hay for t in terms):
                continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Local media cache
# ---------------------------------------------------------------------------

def cache_key(post_id, media_id, variant):
    """md5 hex naming a cached Moments media file."""
    return hashlib.md5(f"{post_id}_{media_id}_{variant}".encode()).hexdigest()


class MediaCache:
    """Index of <wechat_base_dir>/cache/*/Sns/{Img,Video} (built lazily)."""

    def __init__(self, wechat_base_dir):
        self.base = wechat_base_dir or ""
        self._index = None

    @property
    def index(self):
        if self._index is None:
            idx = {}
            for kind in ("Img", "Video"):
                for p in glob.glob(os.path.join(glob.escape(self.base), "cache", "*", "Sns",
                                                kind, "*", "*")):
                    sub, name = p.split(os.sep)[-2:]
                    stem, ext = os.path.splitext(name)
                    if ext == ".tmp" or not os.path.isfile(p):
                        continue  # partial download
                    key = (sub + stem).lower()
                    if len(key) != 32:
                        continue
                    idx.setdefault(key, []).append(p)
            for v in idx.values():  # newest copy first
                v.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            self._index = idx
        return self._index

    def _get(self, post_id, media_id, variant, want_ext=None):
        for p in self.index.get(cache_key(post_id, media_id, variant), []):
            ext = os.path.splitext(p)[1].lower()
            if want_ext is None or ext == want_ext:
                return p
        return None

    def lookup(self, post_id, media_id):
        """{"image": full|None, "thumb": thumb|square|None, "video": mp4|None}."""
        return {"image": self._get(post_id, media_id, VARIANT_FULL),
                "thumb": (self._get(post_id, media_id, VARIANT_THUMB)
                          or self._get(post_id, media_id, VARIANT_SQUARE)),
                "video": self._get(post_id, media_id, VARIANT_VIDEO, ".mp4")}

    def annotate(self, posts):
        """Set media["cached"] = list of available variants for every media."""
        for p in posts:
            for m in p["media"]:
                found = self.lookup(p["id"], m["id"])
                m["cached"] = [k for k, v in found.items() if v]
        return posts


class MediaExporter:
    """Decodes cached media of posts into files_dir (reusing earlier output)."""

    def __init__(self, cache, keys, files_dir):
        self.cache = cache
        self.aes_key, self.xor_key = keys
        self.files_dir = files_dir
        self.images = self.thumbs = self.videos = self.live = self.reused = self.missing = self.failed = 0

    def _decode(self, src, stem):
        import image_decode
        out = S.existing_decoded(self.files_dir, stem)
        if out:
            self.reused += 1
            return out
        try:
            data, ext = image_decode.decode_dat(src, self.aes_key, self.xor_key)
        except Exception:
            self.failed += 1
            return None
        os.makedirs(self.files_dir, exist_ok=True)
        out = os.path.join(self.files_dir, f"{stem}.{ext}")
        with open(out, "wb") as f:
            f.write(data)
        return out

    def attach(self, posts):
        """Sets media image_path / video_path (absolute) where possible; for
        live photos the motion clip goes to live_path instead of video_path."""
        for p in posts:
            for m in p["media"]:
                found = self.cache.lookup(p["id"], m["id"])
                src, variant = (found["image"], VARIANT_FULL) if found["image"] else \
                    (found["thumb"], VARIANT_THUMB)
                if src:
                    out = self._decode(src, cache_key(p["id"], m["id"], variant))
                    if out:
                        m["image_path"] = out
                        if variant == VARIANT_FULL:
                            self.images += 1
                        else:
                            self.thumbs += 1
                if found["video"]:
                    stem = cache_key(p["id"], m["id"], VARIANT_VIDEO)
                    out = os.path.join(self.files_dir, stem + ".mp4")
                    if not os.path.exists(out):
                        os.makedirs(self.files_dir, exist_ok=True)
                        shutil.copyfile(found["video"], out)
                    if m["type"] == "video":
                        m["video_path"] = out
                        self.videos += 1
                    else:
                        m["live_path"] = out
                        self.live += 1
                if not src and not found["video"] and m["type"] in ("image", "video"):
                    self.missing += 1
        return posts

    def summary(self):
        return (f"图片 {self.images}（仅缩略图 {self.thumbs}），视频 {self.videos}，实况 {self.live}，"
                f"复用 {self.reused}，本地无缓存 {self.missing}，解码失败 {self.failed}")


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _counts_line(p):
    n = {}
    for m in p["media"]:
        n[m["type"]] = n.get(m["type"], 0) + 1
    bits = []
    if n.get("image"):
        bits.append(f"图片×{n['image']}")
    if n.get("video"):
        bits.append(f"视频×{n['video']}")
    return bits


def write_txt(posts, meta, fp, base_dir=None):
    for p in posts:
        head = f"[{S.fmt_time(p['ts'])}] {p.get('author') or p['username']}"
        tags = [KIND_LABEL.get(p["kind"], p["kind"])] + _counts_line(p)
        if p.get("private"):
            tags.append("私密")
        fp.write(f"{head} ({', '.join(tags)})\n")
        if p["text"]:
            fp.write(p["text"] + "\n")
        if p["title"] or p["url"]:
            fp.write(f"  链接: {p['title']} {p['url']}".rstrip() + "\n")
        if p["description"] and p["kind"] != "image":
            fp.write(f"  摘要: {p['description']}\n")
        if p["source"]:
            fp.write(f"  来源: {p['source']}\n")
        if p["location"]:
            fp.write(f"  位置: {p['location']['name']}\n")
        if p["likes"]:
            fp.write("  赞: " + ", ".join(x.get("name") or x["username"] for x in p["likes"]) + "\n")
        for c in p["comments"]:
            who = c.get("name") or c["username"]
            if c.get("reply_to"):
                who += f" 回复 {c.get('reply_to_name') or c['reply_to']}"
            fp.write(f"  {who}: {c['text']}\n")
        fp.write("\n")


def write_md(posts, meta, fp, base_dir=None):
    e = S.md_escape
    fp.write(f"# {e(meta.get('title', '朋友圈'))}\n\n")
    for p in posts:
        fp.write(f"## {e(p.get('author') or p['username'])} · {S.fmt_time(p['ts'], '%Y-%m-%d %H:%M')}\n\n")
        if p["text"]:
            fp.write(e(p["text"]).replace("\n", "  \n") + "\n\n")
        url = S.safe_url(p["url"])
        if p["title"] or url:
            title = e(p["title"] or url).replace("[", "\\[").replace("]", "\\]")
            fp.write((f"[{title}](<{url}>)" if url else title) + "\n\n")
        imgs = []
        for m in p["media"]:
            if m.get("video_path"):
                imgs.append(f"[视频]({S.rel_path(m['video_path'], base_dir)})")
            elif m.get("image_path"):
                imgs.append(f"![]({S.rel_path(m['image_path'], base_dir)})")
                if m.get("live_path"):
                    imgs.append(f"[实况]({S.rel_path(m['live_path'], base_dir)})")
        if imgs:
            fp.write(" ".join(imgs) + "\n\n")
        elif _counts_line(p):
            fp.write(f"*[{'，'.join(_counts_line(p))}]*\n\n")
        info = []
        if p["location"]:
            info.append(f"位置: {e(p['location']['name'])}")
        if p["source"]:
            info.append(f"来源: {e(p['source'])}")
        if p["likes"]:
            info.append("赞: " + e(", ".join(x.get("name") or x["username"] for x in p["likes"])))
        for line in info:
            fp.write(f"> {line}  \n")
        for c in p["comments"]:
            who = c.get("name") or c["username"]
            if c.get("reply_to"):
                who += f" 回复 {c.get('reply_to_name') or c['reply_to']}"
            fp.write(f"> **{e(who)}**: {e(c['text'])}  \n")
        fp.write("\n")


_CSS = """
.post{display:flex;gap:10px}
.av{flex:none;width:40px;height:40px;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;font-size:17px}
.body{flex:1;min-width:0}
.name{color:var(--link);font-weight:600}
.grid{display:grid;gap:4px;margin:6px 0;max-width:330px}
.g1{grid-template-columns:1fr;max-width:260px}
.g2,.g4{grid-template-columns:repeat(2,1fr);max-width:220px}
.g3{grid-template-columns:repeat(3,1fr)}
.grid .cell,.grid .ph,.grid video{display:block;width:100%;aspect-ratio:1/1;border-radius:4px;overflow:hidden;position:relative}
.grid .cell>a:first-child{display:block;width:100%;height:100%}
.g1 .cell,.g1 video{aspect-ratio:auto}
.live{position:absolute;left:4px;top:4px;font-size:10px;font-weight:600;color:#fff;background:rgba(0,0,0,.45);border-radius:3px;padding:0 4px}
.grid img{width:100%;height:100%;object-fit:cover;display:block}
.g1 img{height:auto;max-height:360px;object-fit:contain}
.grid video{background:#000;aspect-ratio:auto;max-height:360px}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}
.loc{color:var(--link);font-size:12px}
.fb{background:var(--soft);border-radius:4px;margin-top:6px;padding:5px 8px;font-size:13px}
.fb .likes{color:var(--link)}
.fb .likes+.cm{border-top:1px solid var(--line);margin-top:4px;padding-top:4px}
.cm div{margin:1px 0}
.cm b{color:var(--link);font-weight:500}
"""

_AV_COLORS = ("#5b8def", "#e36f5a", "#43a86b", "#b36bd6", "#e0a33b", "#3aa3b8", "#d65b8a", "#7a8a99")


def _avatar(p):
    name = p.get("author") or p["username"] or "?"
    color = _AV_COLORS[int(hashlib.md5(p["username"].encode()).hexdigest(), 16) % len(_AV_COLORS)]
    return f'<div class="av" style="background:{color}">{S.esc(name[:1])}</div>'


def _media_html(p, base_dir):
    ms = [m for m in p["media"] if m["type"] in ("image", "video")]
    if not ms:
        return ""
    n = len(ms)
    cls = "g1" if n == 1 else "g2" if n == 2 else "g4" if n == 4 else "g3"
    cells = []
    for m in ms:
        img = m.get("image_path")
        if m.get("video_path"):
            poster = f' poster="{S.esc(S.rel_path(img, base_dir))}"' if img else ""
            cells.append(f'<video controls preload="none"{poster} '
                         f'src="{S.esc(S.rel_path(m["video_path"], base_dir))}"></video>')
        elif img:
            src = S.esc(S.rel_path(img, base_dir))
            label = "视频封面" if m["type"] == "video" else ""
            live = ""
            if m.get("live_path"):
                live = (f'<a class="live" href="{S.esc(S.rel_path(m["live_path"], base_dir))}" '
                        f'target="_blank" rel="noopener">LIVE</a>')
            cells.append(f'<div class="cell"><a href="{src}" target="_blank" rel="noopener">'
                         f'<img loading="lazy" src="{src}" alt="{label}"></a>{live}</div>')
        else:
            cells.append(f'<div class="ph">{"视频" if m["type"] == "video" else "图片"}<br>未缓存</div>')
    return f'<div class="grid {cls}">{"".join(cells)}</div>'


def _link_html(p, base_dir):
    if p["kind"] in ("image", "text", "video") or not (p["title"] or p["url"]):
        return ""
    url = S.safe_url(p["url"])
    cover = next((m.get("image_path") for m in p["media"] if m.get("image_path")), None)
    img = f'<img loading="lazy" src="{S.esc(S.rel_path(cover, base_dir))}" alt="">' if cover else ""
    desc = p["description"] or p["source"] or S.url_host(url)
    inner = (f'{img}<div><div class="lt">{S.esc(p["title"] or url)}</div>'
             f'<div class="ld">{S.esc(desc)}</div></div>')
    if url:
        return f'<a class="linkbox" href="{S.esc(url)}" target="_blank" rel="noopener noreferrer">{inner}</a>'
    return f'<div class="linkbox">{inner}</div>'


def _post_html(p, base_dir):
    e = S.esc
    parts = [f'<article class="card post" id="p{e(p["id"])}">{_avatar(p)}<div class="body">',
             f'<div class="name">{e(p.get("author") or p["username"])}</div>']
    if p["text"]:
        parts.append(f'<div class="text">{e(p["text"])}</div>')
    parts.append(_link_html(p, base_dir))
    if p["kind"] != "link":
        parts.append(_media_html(p, base_dir))
    if p["location"]:
        parts.append(f'<div class="loc">{e(p["location"]["name"])}</div>')
    meta = [f'<span title="{e(S.fmt_time(p["ts"]))}">{e(S.fmt_time(p["ts"], "%Y-%m-%d %H:%M"))}</span>']
    if p["kind"] not in ("image", "text"):
        meta.append(f'<span>{e(KIND_LABEL.get(p["kind"], p["kind"]))}</span>')
    if p["source"] and p["kind"] in ("image", "text", "video"):
        meta.append(f'<span>{e(p["source"])}</span>')
    if p.get("private"):
        meta.append('<span>私密</span>')
    if p.get("with_names"):
        meta.append(f'<span>和 {e(", ".join(p["with_names"]))}</span>')
    parts.append(f'<div class="meta muted">{"".join(meta)}</div>')
    if p["likes"] or p["comments"]:
        parts.append('<div class="fb">')
        if p["likes"]:
            names = ", ".join(x.get("name") or x["username"] for x in p["likes"])
            parts.append(f'<div class="likes">♡ {e(names)}</div>')
        if p["comments"]:
            parts.append('<div class="cm">')
            for c in p["comments"]:
                who = f'<b>{e(c.get("name") or c["username"])}</b>'
                if c.get("reply_to"):
                    who += f' 回复 <b>{e(c.get("reply_to_name") or c["reply_to"])}</b>'
                parts.append(f'<div title="{e(S.fmt_time(c["ts"]))}">{who}: {e(c["text"])}</div>')
            parts.append('</div>')
        parts.append('</div>')
    parts.append('</div></article>\n')
    return "".join(parts)


def write_html(posts, meta, fp, base_dir=None):
    body = "".join(_post_html(p, base_dir) for p in posts)
    if not posts:
        body = '<p class="muted" style="text-align:center">没有朋友圈内容</p>'
    fp.write(S.page(meta.get("title", "朋友圈"), meta.get("subtitle", ""), body, _CSS))


def to_json_post(p, base_dir=None):
    d = {k: v for k, v in p.items()}
    d["time"] = S.iso(p["ts"])
    d["media"] = []
    for m in p["media"]:
        m = dict(m)
        for k in ("image_path", "video_path", "live_path"):
            if m.get(k):
                m[k] = S.rel_plain(m[k], base_dir)
        d["media"].append(m)
    return d


def write_json(posts, meta, fp, base_dir=None):
    json.dump({"meta": meta, "posts": [to_json_post(p, base_dir) for p in posts]}, fp,
              ensure_ascii=False, indent=2)
    fp.write("\n")


WRITERS = {"txt": write_txt, "md": write_md, "html": write_html, "json": write_json}


def write(posts, meta, path, fmt):
    if fmt not in WRITERS:
        raise ValueError(f"unknown format: {fmt!r}")
    base_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(base_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        WRITERS[fmt](posts, meta, fp, base_dir=base_dir)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="wechat moments",
        description="导出本地缓存的朋友圈（自己和好友的动态，含点赞/评论）",
        epilog="示例: ./wechat moments 张三 --since 2025-01-01 --images")
    p.add_argument("name", nargs="?", help="只导出该作者（备注/昵称/微信号，支持部分匹配；'我' = 自己）")
    p.add_argument("--query", "-q", help="只导出包含这些词的动态（空格分隔，全部匹配）")
    S.add_common_args(p)
    return p


def main(argv=None, cfg=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    since, until = S.time_range(parser, args)
    cfg, decrypted_dir, export_dir = S.setup(args, cfg)
    if not os.path.exists(sns_db_path(decrypted_dir)):
        print(f"[!] 未找到朋友圈数据库: {sns_db_path(decrypted_dir)}")
        return 1
    import chats as chatlib
    self_wxid = cfg.get("self_wxid")
    stats = {}
    posts = load_posts(decrypted_dir, stats=stats)
    resolve_names(posts, chatlib.load_contacts(decrypted_dir), self_wxid)
    authors = None
    stem = "moments"
    if args.name:
        authors = match_authors(args.name, posts, chatlib.load_contact_rows(decrypted_dir),
                                self_wxid)
        if not authors:
            print(f"[!] 没有找到作者: {args.name}")
            return 1
        names = {p["username"]: p["author"] for p in posts}
        print("[+] 作者: " + "、".join(names.get(u, u) for u in authors))
        stem = f"moments_{S.safe_filename(args.name)}"
    posts = filter_posts(posts, authors, args.query, since, until)
    path = S.output_path(export_dir, stem, args.format, args.output)
    if args.images:
        exporter = MediaExporter(MediaCache(cfg.get("wechat_base_dir")), S.image_keys(cfg),
                                 S.files_dir_for(path))
        exporter.attach(posts)
    else:
        exporter = None
    title = "朋友圈" + (f" · {args.name}" if args.name else "")
    rng = ""
    if posts:
        rng = f"{S.fmt_time(posts[-1]['ts'], '%Y-%m-%d')} – {S.fmt_time(posts[0]['ts'], '%Y-%m-%d')}"
    meta = {"title": title, "subtitle": f"{len(posts)} 条 · {rng}".strip(" ·"),
            "count": len(posts), "exported_at": S.iso(int(time.time()))}
    write(posts, meta, path, args.format)
    print(f"[+] 数据库中 {stats.get('rows', 0)} 条动态（解析失败 {stats.get('failed', 0)}），"
          f"导出 {len(posts)} 条 -> {path}")
    if exporter:
        print(f"[+] 媒体: {exporter.summary()}")
        if args.format == "txt":
            print("[!] txt 格式不引用图片，文件只保存到 _files/ 目录")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] 已取消")
        sys.exit(130)
