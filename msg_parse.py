"""
Parse WeChat 4.x message contents into a display summary plus structured data.

    text, extra, kind = parse(content, local_type, resolve=None)

  text   one-line human-readable summary ("[链接] 标题 (来源) https://..."),
         or None for messages that should be dropped. Plain text messages are
         returned unchanged (they may contain newlines).
  extra  dict of structured fields for richer rendering, or None (see
         EXTRA SCHEMA below).
  kind   coarse kind string (text, image, link, quote, transfer, ...).

resolve(username) -> display name or None lets the caller turn usernames that
appear inside the XML (quoted sender, pat participants, system templates) into
the same names used for senders; without it the names in the XML are used.

All XML-derived strings are untrusted: renderers must escape them.

local_type = base | (sub << 32); app messages have base 49 and the app type
in <appmsg><type> (normally equal to sub).

EXTRA SCHEMA (keys only present when known)
  quote         {"reply": str, "quote": {"sender", "text", "kind",
                 "local_type", "server_id", "ts"}}
  link          {"title", "url", "desc", "source"}
  music         {"title", "artist", "url", "source"}
  file          {"title", "size" (bytes), "ext"}
  miniprogram   {"title", "source", "desc"}
  redpacket     {"greeting", "scene"}               (amount is not in the XML)
  transfer      {"amount", "status", "paysubtype", "memo"}
                 status: sent | received | refunded | expired | unknown
  channels      {"author", "desc", "live": bool}
  call          {"media": voice|video|None, "status", "duration" (seconds)}
                 status: completed | cancelled | missed | busy | declined |
                         answered_elsewhere | declined_elsewhere | invite | unknown
  card          {"nickname", "card_type": person|official|enterprise|group,
                 "certinfo"}
  chat_history  {"title", "items": [item]}; item = {"sender", "time", "ts",
                 "kind", "text", "url", "items" (nested chat_history)}
  location      {"poiname", "label", "lat", "lng"}
  pat           {"from", "patted"}
  notice        {"text"}                            (full group announcement)
  note          {"desc"}
"""
import datetime as _dt
import re
import xml.etree.ElementTree as ET

MAX_SNIPPET = 50          # quoted-message snippet length in the summary
MAX_QUOTE_TEXT = 500      # quoted-message text kept in extra
MAX_DEPTH = 5             # nesting limit for quotes / forwarded chat bundles

_PREFIX_RE = re.compile(r"^[A-Za-z0-9_\-@.]+:\n")
_DECL_RE = re.compile(r"<\?xml[^>]*\?>")
_UNSAFE_RE = re.compile(r"<!(DOCTYPE|ENTITY)", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_APPTYPE_RE = re.compile(r"<type>(\d+)</type>")
_TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)

_BASE_LABEL = {3: ("[图片]", "image"), 34: ("[语音]", "voice"),
               43: ("[视频]", "video"), 47: ("[表情]", "emoji")}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def strip_prefix(text):
    """Drop a leading "wxid_xxx:\\n" group-sender prefix."""
    return _PREFIX_RE.sub("", text, count=1) if text else text


def parse_xml(text):
    """Parse message XML robustly -> Element or None.

    Strips a group prefix and any <?xml?> declarations (some messages have
    one after the root tag), refuses DTDs/entities, and retries content with
    several top-level elements wrapped in a <root>."""
    if not text:
        return None
    s = _DECL_RE.sub("", strip_prefix(text)).strip()
    if not s.startswith("<") or _UNSAFE_RE.search(s):
        return None
    try:
        return ET.fromstring(s)
    except ET.ParseError:
        pass
    try:
        return ET.fromstring(f"<root>{s}</root>")
    except ET.ParseError:
        return None


def _t(el, path):
    """Stripped text at path under el ("" if missing)."""
    if el is None:
        return ""
    v = el.findtext(path)
    return v.strip() if v else ""


def one_line(s, limit=None):
    """Collapse whitespace runs to single spaces; optionally truncate with …."""
    s = _WS_RE.sub(" ", s or "").strip()
    if limit and len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def _int(s, default=None):
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return default


def _float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def human_size(n):
    if n is None or n < 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return ""


def _join(*parts, sep=" "):
    return sep.join(p for p in parts if p)


def _paren(s):
    return f"({s})" if s else ""


def _compact(d):
    """Drop None/"" values (keep False/0)."""
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _name(resolve, user, fallback=""):
    """Display name for a username found in XML."""
    if user and resolve:
        n = resolve(user)
        if n:
            return n
    return fallback or user or ""


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def parse(text, local_type, resolve=None, _depth=0):
    """(summary or None, extra dict or None, kind). See module docstring."""
    local_type = local_type or 0
    base = local_type & 0xFFFFFFFF
    sub = local_type >> 32
    if base > 0xFF and base not in (10000, 10002, 11000):
        base &= 0xFF  # legacy composite types

    if base == 1:
        return (text if text else None), None, "text"
    if base in _BASE_LABEL:
        label, kind = _BASE_LABEL[base]
        return label, None, kind
    if base == 48:
        return _location(text)
    if base == 42:
        return _card(text)
    if base == 50:
        return _call(text)
    if base in (10000, 10002):
        return _system(text, resolve)
    if base == 49:
        return _app(text, sub, resolve, _depth)
    if base == 11000:
        return None, None, "other"

    root = parse_xml(text) if text and text.lstrip().startswith("<") else None
    if root is not None and root.get("nickname"):
        return _card(text, root)
    if text and len(text) < 500 and not text.lstrip().startswith("<"):
        return text, None, "other"
    return f"[消息类型:{local_type}]", None, "other"


def kind_of(local_type, text):
    """Kind only (cheap for non-app messages)."""
    return parse(text, local_type)[2]


# ---------------------------------------------------------------------------
# non-app kinds
# ---------------------------------------------------------------------------

def _location(text):
    root = parse_xml(text)
    loc = None
    if root is not None:
        loc = root if root.tag == "location" else root.find(".//location")
    if loc is None:
        return "[位置]", None, "location"
    poi = one_line(loc.get("poiname"))
    label = one_line(loc.get("label"))
    extra = _compact({"poiname": poi, "label": label,
                      "lat": _float(loc.get("x")), "lng": _float(loc.get("y"))})
    if label == poi:
        label = ""
    return _join("[位置]", poi or label, _paren(label) if poi else ""), extra or None, "location"


def _card(text, root=None):
    if root is None:
        root = parse_xml(text)
    if root is None or not root.get("nickname") and not root.get("username"):
        return "[名片]", None, "card"
    user = root.get("username") or ""
    nick = one_line(root.get("nickname"))
    certflag = _int(root.get("certflag"), 0)
    if user.endswith("@chatroom"):
        ctype, label = "group", "[群名片]"
    elif "@openim" in user:
        ctype, label = "enterprise", "[企业微信名片]"
    elif user.startswith("gh_") or certflag:
        ctype, label = "official", "[公众号名片]"
    else:
        ctype, label = "person", "[名片]"
    extra = _compact({"nickname": nick, "card_type": ctype,
                      "certinfo": one_line(root.get("certinfo"))})
    return _join(label, nick), extra, "card"


_DUR_RE = re.compile(r"(?:duration|call ended|通话时长|聊天时长)\s*[:：]?\s*(\d+(?::\d+){1,2})", re.I)
_CALL_STATUS = [  # (regex on the bubble text, status); first match wins
    (re.compile(r"declined on other|其他设备.*拒绝", re.I), "declined_elsewhere"),
    (re.compile(r"answered elsewhere|其他设备.*接听", re.I), "answered_elsewhere"),
    (re.compile(r"busy|忙线", re.I), "busy"),
    (re.compile(r"cancel|取消", re.I), "cancelled"),
    (re.compile(r"not answered|wasn't answered|no answer|未接|无应答|未应答", re.I), "missed"),
    (re.compile(r"declin|reject|拒绝", re.I), "declined"),
]
_CALL_ZH = {"cancelled": "已取消", "missed": "未接听", "busy": "忙线未接听",
            "declined": "已拒绝", "answered_elsewhere": "已在其他设备接听",
            "declined_elsewhere": "已在其他设备拒绝", "invite": "通话邀请"}


def _hms_seconds(s):
    secs = 0
    for p in s.split(":"):
        secs = secs * 60 + int(p)
    return secs


def _call(text):
    root = parse_xml(text)
    media = status = None
    duration = None
    msg = ""
    if root is not None:
        bubble = root.find(".//VoIPBubbleMsg") if root.tag != "VoIPBubbleMsg" else root
        invite = root if root.tag == "voipinvitemsg" else root.find(".//voipinvitemsg")
        if bubble is not None:
            msg = one_line(_t(bubble, "msg"))
            media = {"0": "video", "1": "voice"}.get(_t(bubble, "room_type"))
        elif invite is not None:
            media = {"0": "video", "1": "voice"}.get(_t(invite, "invitetype"))
            status = "invite"
    elif text and not text.lstrip().startswith("<"):
        msg = one_line(text)
    if status is None and msg:
        m = _DUR_RE.search(msg)
        if m:
            status, duration = "completed", _hms_seconds(m.group(1))
        else:
            status = next((st for rx, st in _CALL_STATUS if rx.search(msg)), "unknown")
    label = {"voice": "[语音通话]", "video": "[视频通话]"}.get(media, "[通话]")
    if status == "completed":
        detail = "通话时长 " + m.group(1)
    elif status == "unknown":
        detail = msg
    else:
        detail = _CALL_ZH.get(status, "")
    extra = _compact({"media": media, "status": status or "unknown", "duration": duration})
    return _join(label, detail), extra, "call"


def _render_template(tmpl_el, resolve):
    """sysmsgtemplate content_template -> text with $name$ links filled in."""
    template = _t(tmpl_el, "template") or _t(tmpl_el, "plain")
    values = {}
    for link in tmpl_el.findall("link_list/link"):
        key = link.get("name") or ""
        members = link.findall("memberlist/member")
        if members:
            sep = link.findtext("separator") or "、"
            names = [_name(resolve, _t(m, "username"), one_line(_t(m, "nickname")))
                     for m in members]
            values[key] = sep.join(n for n in names if n)
        else:
            values[key] = one_line(_t(link, "title") or _t(link, "plain"))
    return re.sub(r"\$(\w+)\$", lambda m: values.get(m.group(1), ""), template).strip()


def _fill_wxid_template(template, resolve):
    """'${wxid_a} 拍了拍 ${wxid_b}' -> names."""
    return re.sub(r"\$\{([^}]+)\}", lambda m: _name(resolve, m.group(1)), template or "")


def _system(text, resolve):
    if not text:
        return "[系统消息]", None, "system"
    root = parse_xml(text) if "<sysmsg" in text else None
    if root is not None and root.tag == "sysmsg":
        stype = root.get("type") or ""
        body = ""
        if stype == "revokemsg":
            body = one_line(_t(root, "revokemsg/content") or _t(root, "revokemsg/replacemsg"))
        elif stype == "sysmsgtemplate":
            tmpl = root.find("sysmsgtemplate/content_template")
            if tmpl is not None:
                body = _render_template(tmpl, resolve)
        elif stype == "pat":
            pat = root.find("pat")
            body = one_line(_fill_wxid_template(_t(pat, "template"), resolve))
            if body:
                extra = _compact({"from": _name(resolve, _t(pat, "fromusername")),
                                  "patted": _name(resolve, _t(pat, "pattedusername"))})
                return f"[拍一拍] {body}", extra or None, "system"
        if not body:  # paymsg and other types with a <content> (may hold markup)
            body = _TAG_RE.sub("", _t(root, ".//content")).strip()
        if body:
            return f"[系统消息] {body}", None, "system"
    clean = _TAG_RE.sub("", text).strip()
    return (f"[系统消息] {clean}" if clean else "[系统消息]"), None, "system"


# ---------------------------------------------------------------------------
# app messages (base 49)
# ---------------------------------------------------------------------------

_APP_LABEL = {4: "[链接]", 5: "[链接]", 6: "[文件]", 8: "[表情]",
              33: "[小程序]", 36: "[小程序]", 57: "[引用消息]", 2000: "[转账]",
              2001: "[红包]", 19: "[聊天记录]", 51: "[视频号]", 62: "[拍一拍]",
              63: "[视频号直播]", 87: "[群公告]", 17: "[位置共享]", 3: "[音乐]",
              76: "[音乐]", 24: "[笔记]"}
APP_KIND = {1: "text", 3: "music", 4: "link", 5: "link", 6: "file", 8: "emoji",
            17: "location", 19: "chat_history", 24: "note", 33: "miniprogram",
            36: "miniprogram", 51: "channels", 57: "quote", 62: "pat",
            63: "channels", 76: "music", 87: "notice", 2000: "transfer",
            2001: "redpacket"}


def app_label(app_type):
    return _APP_LABEL.get(app_type, f"[应用消息:{app_type}]")


def _app_legacy(text, sub):
    """Regex fallback for app XML that does not parse (truncated/malformed)."""
    m = _APPTYPE_RE.search(text)
    app_type = int(m.group(1)) if m else sub
    kind = APP_KIND.get(app_type, "other")
    if app_type == 8:
        return "[表情]", None, kind
    m = _TITLE_RE.search(text)
    title = one_line(m.group(1)) if m else ""
    if title:
        if app_type == 57:
            return title, None, kind
        if app_type in _APP_LABEL:
            return f"{_APP_LABEL[app_type]} {title}", None, kind
        return title, None, kind
    return app_label(sub), None, kind


def _app(text, sub, resolve, depth):
    if not text:
        return app_label(sub), None, APP_KIND.get(sub, "other")
    root = parse_xml(text)
    appmsg = None
    if root is not None:
        appmsg = root if root.tag == "appmsg" else root.find("appmsg")
        if appmsg is None:
            appmsg = root.find(".//appmsg")
    if appmsg is None:
        return _app_legacy(text, sub)
    app_type = _int(_t(appmsg, "type"), sub)
    kind = APP_KIND.get(app_type, "other")
    handler = _APP_HANDLERS.get(app_type)
    if handler is not None:
        summary, extra = handler(root, appmsg, app_type, resolve, depth)
        return summary, (extra or None), kind
    title = one_line(_t(appmsg, "title"))
    return (title or app_label(app_type)), None, kind


def _source(root, appmsg):
    return one_line(_t(appmsg, "sourcedisplayname") or _t(root, "appinfo/appname")
                    or _t(appmsg, "mmreader/category/name"))


def _h_text(root, appmsg, t, resolve, depth):
    title = _t(appmsg, "title") or _t(appmsg, "des")
    url = _t(appmsg, "url")
    extra = _compact({"url": url})
    return (title or url or app_label(t)), extra


def _h_link(root, appmsg, t, resolve, depth):
    title = one_line(_t(appmsg, "title"))
    url = _t(appmsg, "url") or _t(appmsg, "lowurl")
    source = _source(root, appmsg)
    extra = _compact({"title": title, "url": url, "desc": _t(appmsg, "des"),
                      "source": source})
    return _join("[链接]", title, _paren(source), url), extra


def _h_music(root, appmsg, t, resolve, depth):
    title = one_line(_t(appmsg, "title"))
    artist = one_line(_t(appmsg, "musicShareItem/mvSingerName") or _t(appmsg, "des"), 60)
    source = one_line(_t(root, "appinfo/appname"))
    url = _t(appmsg, "url")
    extra = _compact({"title": title, "artist": artist, "url": url, "source": source})
    head = _join(title, artist, sep=" - ")
    return _join("[音乐]", head, _paren(source)), extra


def _h_file(root, appmsg, t, resolve, depth):
    title = one_line(_t(appmsg, "title"))
    size = _int(_t(appmsg, "appattach/totallen"))
    extra = _compact({"title": title, "size": size, "ext": _t(appmsg, "appattach/fileext")})
    return _join("[文件]", title, _paren(human_size(size) if size else "")), extra


def _h_emoji(root, appmsg, t, resolve, depth):
    return "[表情]", None


def _h_miniprogram(root, appmsg, t, resolve, depth):
    title = one_line(_t(appmsg, "title"))
    source = one_line(_t(appmsg, "sourcedisplayname") or _t(appmsg, "weappinfo/appname")
                      or _t(root, "appinfo/appname"))
    desc = one_line(_t(appmsg, "des"))
    extra = _compact({"title": title, "source": source, "desc": desc})
    if source == title:
        source = ""
    return _join("[小程序]", title, _paren(source)), extra


def _h_redpacket(root, appmsg, t, resolve, depth):
    pay = appmsg.find("wcpayinfo")
    scene = one_line(_t(pay, "scenetext"))
    greeting = one_line(_t(pay, "receivertitle") or _t(pay, "sendertitle"))
    title = one_line(_t(appmsg, "title"))
    if scene == "群收款" or title == "群收款":
        label = "[群收款]"
        detail = greeting or (title if title != "群收款" else "")
    else:
        label = "[红包]"
        detail = greeting
        if not detail:
            # "我给你发了一个红包，赶紧去拆! 祝：恭喜发财" -> greeting after 祝：
            m = re.search(r"祝[:：]\s*(.+)$", _t(appmsg, "des"))
            detail = one_line(m.group(1)) if m else ""
    extra = _compact({"greeting": detail, "scene": scene or title})
    return _join(label, detail), extra


# paysubtype -> status. 1/3/4 are well established; 8 is the newer "sent"
# bubble, 9/10 are inferred from real data (refund / expiry notices).
_TRANSFER_STATUS = {1: "sent", 8: "sent", 3: "received", 4: "refunded",
                    9: "refunded", 5: "expired", 10: "expired"}
_TRANSFER_ZH = {"received": "已收款", "refunded": "已退还", "expired": "已过期"}
_AMOUNT_RE = re.compile(r"[￥¥]?\s*\d+(?:\.\d+)?")


def _h_transfer(root, appmsg, t, resolve, depth):
    pay = appmsg.find("wcpayinfo")
    amount = one_line(_t(pay, "feedesc"))
    if not amount:
        m = re.search(r"([￥¥]?\d+(?:\.\d+)?)\s*元", _t(appmsg, "des"))
        amount = m.group(1) if m else ""
    if amount and not amount[0] in "¥￥" and _AMOUNT_RE.fullmatch(amount):
        amount = "¥" + amount
    amount = amount.replace("￥", "¥")
    pst = _int(_t(pay, "paysubtype"))
    status = _TRANSFER_STATUS.get(pst, "unknown")
    memo = one_line(_t(pay, "pay_memo"))
    extra = _compact({"amount": amount, "status": status, "paysubtype": pst, "memo": memo})
    head = _join("[转账]", amount, _TRANSFER_ZH.get(status, ""))
    return _join(head, memo, sep=" · "), extra


def _h_channels(root, appmsg, t, resolve, depth):
    live = t == 63
    node = appmsg.find("finderLive" if live else "finderFeed")
    author = one_line(_t(node, "nickname"))
    desc = one_line(_t(node, "desc"))
    extra = _compact({"author": author, "desc": desc, "live": live})
    label = "[视频号直播]" if live else "[视频号]"
    if author and desc:
        return f"{label} {author}: {one_line(desc, 100)}", extra
    return _join(label, author or one_line(desc, 100)), extra


def _h_pat(root, appmsg, t, resolve, depth):
    pat = appmsg.find("patinfo")
    title = one_line(_t(appmsg, "title"))
    if not title or "${" in title:
        title = one_line(_fill_wxid_template(_t(pat, "template") or title, resolve))
    extra = _compact({"from": _name(resolve, _t(pat, "fromusername")),
                      "patted": _name(resolve, _t(pat, "pattedusername"))})
    return _join("[拍一拍]", title), extra


def _h_notice(root, appmsg, t, resolve, depth):
    body = _t(appmsg, "textannouncement") or _t(appmsg, "title")
    return _join("[群公告]", one_line(body, 100)), _compact({"text": body})


def _h_note(root, appmsg, t, resolve, depth):
    desc = _t(appmsg, "des") or _t(appmsg, "title")
    return _join("[笔记]", one_line(desc, 100)), _compact({"desc": desc})


def _h_live_location(root, appmsg, t, resolve, depth):
    return "[位置共享]", None


def _h_quote(root, appmsg, t, resolve, depth):
    reply = _t(appmsg, "title")
    ref = appmsg.find("refermsg")
    if ref is None or depth >= MAX_DEPTH:
        return (reply or "[引用消息]"), None
    if depth > 0:  # a quote inside a quote: its reply text is enough
        return (reply or "[引用消息]"), None
    rtype = _int(_t(ref, "type"), 0)
    content = ref.findtext("content") or ""
    user = _t(ref, "chatusr") or _t(ref, "fromusr")
    if user.endswith("@chatroom"):
        user = ""
    sender = _name(resolve, user, one_line(_t(ref, "displayname")))
    if rtype == 1 or (rtype == 0 and not content.lstrip().startswith("<")):
        qtext, qkind = content.strip(), "text"
    else:
        qtext, _, qkind = parse(content, rtype, resolve, depth + 1)
    qtext = one_line(qtext)
    quote = _compact({"sender": sender, "text": one_line(qtext, MAX_QUOTE_TEXT),
                      "kind": qkind, "local_type": rtype,
                      "server_id": _int(_t(ref, "svrid")) or None,
                      "ts": _int(_t(ref, "createtime")) or None})
    snippet = one_line(qtext, MAX_SNIPPET)
    q = f"[引用 {sender}: {snippet}]" if sender else f"[引用: {snippet}]"
    return _join(reply, q), {"reply": reply, "quote": quote}


# recorditem dataitem datatype -> (label, kind)
_REC_TYPES = {1: ("", "text"), 2: ("[图片]", "image"), 3: ("[语音]", "voice"),
              4: ("[视频]", "video"), 5: ("[链接]", "link"), 6: ("[位置]", "location"),
              7: ("[音乐]", "music"), 8: ("[文件]", "file"), 17: ("[聊天记录]", "chat_history"),
              19: ("[小程序]", "miniprogram"), 22: ("[视频号]", "channels"),
              23: ("[视频号直播]", "channels"), 29: ("[音乐]", "music"),
              37: ("[表情]", "emoji")}


def _record_items(info, depth):
    """recordinfo Element -> list of item dicts."""
    items = []
    if info is None:
        return items
    for d in info.findall("datalist/dataitem"):
        dtype = _int(d.get("datatype"), 0)
        label, kind = _REC_TYPES.get(dtype, ("", "other"))
        title = one_line(_t(d, "datatitle"))
        desc = _t(d, "datadesc")
        item = {"sender": one_line(_t(d, "sourcename"))}
        ts = _int(_t(d, "srcMsgCreateTime"))
        if ts:
            item["ts"] = ts
            item["time"] = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        else:
            item["time"] = one_line(_t(d, "sourcetime"))
        item["kind"] = kind
        if dtype == 1:
            text = desc
        elif dtype == 6:
            loc = d.find("locitem")
            place = one_line(_t(loc, "poiname") or _t(loc, "label")) if loc is not None else ""
            text = _join(label, place or title)
        elif dtype == 17:
            text = _join(label, title)
            if depth < MAX_DEPTH:
                nested = _record_items(d.find("recordxml/recordinfo"), depth + 1)
                if nested:
                    item["items"] = nested
        elif dtype == 8:
            size = _int(_t(d, "datasize"))
            text = _join(label, title, _paren(human_size(size) if size else ""))
        else:
            text = _join(label, title) if label else (one_line(desc) or title or "[消息]")
            if dtype in (2, 3, 4, 37):
                text = label
        url = _t(d, "link") or _t(d, "weburlitem/link")
        if url and dtype == 5:
            item["url"] = url
        item["text"] = text or label or "[消息]"
        items.append(_compact(item))
    return items


def _h_chat_history(root, appmsg, t, resolve, depth):
    title = one_line(_t(appmsg, "title"))
    info = parse_xml(appmsg.findtext("recorditem") or "")
    if info is not None and info.tag != "recordinfo":
        info = info.find(".//recordinfo")
    items = _record_items(info, depth)
    extra = _compact({"title": title, "items": items or None})
    count = f"({len(items)}条)" if items else ""
    return _join("[聊天记录]", title, count), extra


_APP_HANDLERS = {
    1: _h_text, 3: _h_music, 76: _h_music, 4: _h_link, 5: _h_link, 6: _h_file,
    8: _h_emoji, 17: _h_live_location, 19: _h_chat_history, 24: _h_note,
    33: _h_miniprogram, 36: _h_miniprogram, 51: _h_channels, 57: _h_quote,
    62: _h_pat, 63: _h_channels, 87: _h_notice, 2000: _h_transfer,
    2001: _h_redpacket,
}
