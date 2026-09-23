"""
Chat discovery and message iteration for decrypted WeChat 4.x databases.

Pure functions (no printing) used by the exporter:

    contacts = load_contacts(decrypted_dir)
    chats = list_chats(decrypted_dir)
    chat = find_chats("ning", chats)[0]
    for rec in iter_messages(chat, decrypted_dir, self_wxid, contacts):
        ...

Storage facts relied on (WeChat 4.x, macOS):
  - message/message_N.db holds one table per chat named Msg_<md5(username)>.
    A chat spans several message_N.db files (each covers roughly one year).
  - Name2Id (per message DB) maps rowid -> username; Msg_*.real_sender_id is
    a rowid into it. The row with an empty username is the "system" sender.
  - Group chats have usernames ending in "@chatroom". Messages from other
    members carry a "<username>:\\n" prefix in message_content; one's own
    messages do not. Some group messages (videos, red packets, transfers) have
    the system sender and no prefix; their XML has a fromusername field.
  - contact/contact.db: contact(username, remark, nick_name, verify_flag, ...);
    chat_room(username, ext_buffer) where ext_buffer is a protobuf with
    repeated field 1 = member {1: username, 2: group nickname, 4: inviter}.
  - message_content is zstd-compressed when WCDB_CT_message_content == 4.
"""
import hashlib
import os
import re
import sqlite3

import zstandard

SELF_DISPLAY = "我"
SYSTEM_DISPLAY = "系统"

# Non-person accounts that show up as chats. gh_* (official accounts) are
# handled by prefix.
SYSTEM_USERNAMES = frozenset({
    "weixin", "filehelper", "fmessage", "medianote", "floatbottle", "qqmail",
    "newsapp", "notifymessage", "tmessage", "qmessage", "qqsync",
    "brandsessionholder", "brandservicesessionholder", "@placeholder_foldgroup",
    "notification_messages", "opencustomerservicemsg", "officialaccounts",
    "mphelper", "exmail_tool", "voip", "voipmsg", "masssendapp", "feedsapp",
    "blogapp", "facebookapp", "lbsapp", "shakeapp", "linkedinplugin",
    "voicevoipapp", "voiceinputapp", "googlecontact", "cardpackage",
    "gamecenter", "appbrand_notify_message", "helper_entry",
    "userexperience_alarm", "chatroom_notify", "downloaderapp",
})

_GROUP_PREFIX_RE = re.compile(r"^([A-Za-z0-9_\-@.]+):\n")
_FROMUSER_RE = re.compile(
    r'fromusername\s*=\s*"([^"]+)"|<fromusername>(?:<!\[CDATA\[)?([^<\]]+)')
_APPTYPE_RE = re.compile(r"<type>(\d+)</type>")


# ---------------------------------------------------------------------------
# Decoding / formatting (copied from export_chat.py; dedupe at integration)
# ---------------------------------------------------------------------------

_zstd = zstandard.ZstdDecompressor()


def decompress_if_needed(content, ct):
    if ct == 4 and isinstance(content, bytes) and len(content) > 0:
        try:
            return _zstd.decompress(content).decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(content, str):
        return content
    return None


def format_message(text, local_type):
    base_type = local_type & 0xFF
    sub_type = local_type >> 32

    if base_type == 1:
        return text if text else None
    if base_type == 3:
        return "[图片]"
    if base_type == 34:
        return "[语音]"
    if base_type == 43:
        return "[视频]"
    if base_type == 47:
        return "[表情]"
    if base_type == 48:
        return "[位置]"

    # System message (10000 & 0xFF = 16)
    if base_type == 16:
        if text:
            clean = re.sub(r"<[^>]+>", "", text).strip()
            return f"[系统消息] {clean}" if clean else "[系统消息]"
        return "[系统消息]"

    # App message (base=49)
    if base_type == 49:
        if not text:
            return _format_app_by_sub(sub_type)

        m = re.search(r"<type>(\d+)</type>", text)
        app_type = int(m.group(1)) if m else sub_type

        if app_type == 8:
            return "[表情]"

        m = re.search(r"<title>(.*?)</title>", text, re.DOTALL)
        if m:
            title = m.group(1).strip()
            if title:
                if app_type == 57:
                    return title
                if app_type == 5:
                    return f"[链接] {title}"
                if app_type == 4:
                    return f"[表情包] {title}"
                if app_type == 6:
                    return f"[文件] {title}"
                if app_type in (33, 36):
                    return f"[小程序] {title}"
                return title

        return _format_app_by_sub(sub_type)

    if base_type == 248:
        return None

    if text and len(text) < 500 and not text.startswith("<?xml"):
        return text
    return f"[消息类型:{local_type}]"


def _format_app_by_sub(sub_type):
    labels = {4: "[表情包]", 5: "[链接]", 6: "[文件]", 8: "[表情]",
              33: "[小程序]", 36: "[小程序]", 57: "[引用消息]"}
    return labels.get(sub_type, f"[应用消息:{sub_type}]")


# ---------------------------------------------------------------------------
# Kind classification
# ---------------------------------------------------------------------------

_BASE_KIND = {1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji",
              48: "location", 10000: "system", 10002: "system"}
_APP_KIND = {3: "link", 4: "link", 5: "link", 6: "file", 8: "emoji",
             33: "miniprogram", 36: "miniprogram", 57: "quote"}


def message_kind(local_type, text):
    """Map a raw local_type (+ decoded content) to a coarse kind string."""
    base = local_type & 0xFFFFFFFF
    if base == 49:
        m = _APPTYPE_RE.search(text) if text else None
        app_type = int(m.group(1)) if m else local_type >> 32
        return _APP_KIND.get(app_type, "other")
    return _BASE_KIND.get(base, "other")


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def _contact_db(decrypted_dir):
    return os.path.join(decrypted_dir, "contact", "contact.db")


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _pick_name(username, remark, nick):
    for v in (remark, nick):
        if v and v.strip():
            return v.strip()
    return username


def load_contacts(decrypted_dir):
    """Return {username: display_name}, display = remark > nickname > username."""
    path = _contact_db(decrypted_dir)
    if not os.path.exists(path):
        return {}
    out = {}
    conn = sqlite3.connect(path)
    try:
        # "stranger" first so real contacts override it.
        for table in ("stranger", "contact"):
            if not _table_exists(conn, table):
                continue
            for username, remark, nick in conn.execute(
                    f"SELECT username, remark, nick_name FROM [{table}]"):
                if username:
                    out[username] = _pick_name(username, remark, nick)
    finally:
        conn.close()
    return out


def _load_contact_flags(decrypted_dir):
    """{username: verify_flag} for filtering official accounts."""
    path = _contact_db(decrypted_dir)
    if not os.path.exists(path):
        return {}
    conn = sqlite3.connect(path)
    try:
        if not _table_exists(conn, "contact"):
            return {}
        return {u: (vf or 0) for u, vf in conn.execute(
            "SELECT username, verify_flag FROM contact")}
    finally:
        conn.close()


def _read_varint(buf, i):
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if b < 0x80:
            return result, i


def _iter_proto(buf):
    """Yield (field, wire_type, value) for a protobuf message. Raises on junk."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(buf, i)
        elif wire == 2:
            length, i = _read_varint(buf, i)
            if i + length > n:
                raise ValueError("truncated")
            val = buf[i:i + length]
            i += length
        elif wire == 1:
            val = buf[i:i + 8]
            i += 8
        elif wire == 5:
            val = buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield field, wire, val


def parse_chat_room_members(ext_buffer):
    """Parse chat_room.ext_buffer -> {member_username: group_nickname or ''}."""
    members = {}
    if not ext_buffer:
        return members
    try:
        for field, wire, val in _iter_proto(bytes(ext_buffer)):
            if field != 1 or wire != 2:
                continue
            username, nick = None, ""
            for f2, w2, v2 in _iter_proto(val):
                if w2 != 2:
                    continue
                if f2 == 1:
                    username = v2.decode("utf-8", errors="replace")
                elif f2 == 2:
                    nick = v2.decode("utf-8", errors="replace").strip()
            if username:
                members[username] = nick
    except (ValueError, IndexError):
        pass  # keep whatever parsed cleanly
    return members


def load_group_nicknames(decrypted_dir):
    """Return {room_username: {member_username: group_nickname}} (non-empty only)."""
    path = _contact_db(decrypted_dir)
    if not os.path.exists(path):
        return {}
    conn = sqlite3.connect(path)
    try:
        if not _table_exists(conn, "chat_room"):
            return {}
        out = {}
        for room, buf in conn.execute("SELECT username, ext_buffer FROM chat_room"):
            nicks = {u: n for u, n in parse_chat_room_members(buf).items() if n}
            if room and nicks:
                out[room] = nicks
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------

def table_name_for(username):
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def message_db_paths(decrypted_dir):
    msg_dir = os.path.join(decrypted_dir, "message")
    if not os.path.isdir(msg_dir):
        return []

    def idx(f):
        m = re.fullmatch(r"message_(\d+)\.db", f)
        return int(m.group(1)) if m else None

    files = [f for f in os.listdir(msg_dir) if idx(f) is not None]
    return [os.path.join(msg_dir, f) for f in sorted(files, key=idx)]


def is_group(username):
    return username.endswith("@chatroom")


def is_system_username(username, verify_flag=0):
    """True for official/service accounts and other non-person accounts."""
    if username.startswith("gh_") or username in SYSTEM_USERNAMES:
        return True
    if username.startswith("@"):
        return True
    # Verified (official/enterprise) accounts without a gh_ id.
    if verify_flag and not username.startswith("wxid_") and not is_group(username) \
            and "@" not in username:
        return True
    return False


def list_chats(decrypted_dir, include_system=False, contacts=None):
    """List every chat (1-on-1 and group) with messages.

    Returns a list of dicts sorted by last_ts descending:
      {username, name, is_group, msg_count, last_ts, tables: [(db_path, table)]}
    tables is ordered oldest DB first.
    """
    if contacts is None:
        contacts = load_contacts(decrypted_dir)
    flags = _load_contact_flags(decrypted_dir)

    chats = {}
    # Oldest DB (highest N) first so tables are chronological.
    for db_path in reversed(message_db_paths(decrypted_dir)):
        conn = sqlite3.connect(db_path)
        try:
            try:
                names = [r[0] for r in conn.execute("SELECT user_name FROM Name2Id")]
            except sqlite3.DatabaseError:
                continue
            by_hash = {table_name_for(u): u for u in names if u}
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg\\_%' ESCAPE '\\'")]
            for table in tables:
                username = by_hash.get(table)
                if username is None:
                    continue
                if not include_system and is_system_username(username, flags.get(username, 0)):
                    continue
                cnt, last = conn.execute(
                    f"SELECT count(*), max(create_time) FROM [{table}]").fetchone()
                if not cnt:
                    continue
                chat = chats.get(username)
                if chat is None:
                    chat = chats[username] = {
                        "username": username,
                        "name": contacts.get(username, username),
                        "is_group": is_group(username),
                        "msg_count": 0,
                        "last_ts": 0,
                        "tables": [],
                    }
                chat["msg_count"] += cnt
                chat["last_ts"] = max(chat["last_ts"], last or 0)
                chat["tables"].append((db_path, table))
        finally:
            conn.close()

    return sorted(chats.values(), key=lambda c: (-c["last_ts"], c["username"]))


def find_chats(pattern, chats):
    """Case-insensitive substring match on name or username.

    Exact name matches come first (case-sensitive, then case-insensitive),
    then the rest in the order given.
    """
    pat = pattern.lower()
    exact, exact_ci, rest = [], [], []
    for c in chats:
        name = c["name"] or ""
        if name == pattern or c["username"] == pattern:
            exact.append(c)
        elif name.lower() == pat or c["username"].lower() == pat:
            exact_ci.append(c)
        elif pat in name.lower() or pat in c["username"].lower():
            rest.append(c)
    return exact + exact_ci + rest


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def _sender_from_xml(text):
    if not text:
        return None
    m = _FROMUSER_RE.search(text)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def iter_messages(chat, decrypted_dir, self_wxid, contacts, group_nicknames=None):
    """Yield message records for a chat, sorted by time.

    Record: {ts, sender, is_self, kind, text, local_type, local_id,
             server_id, create_time}
    Messages whose formatted text is None are dropped.
    decrypted_dir is accepted for API symmetry (and group nicknames);
    table locations come from chat["tables"].
    """
    username = chat["username"]
    group = chat.get("is_group", is_group(username))
    room_nicks = {}
    if group:
        if group_nicknames is None:
            group_nicknames = load_group_nicknames(decrypted_dir)
        room_nicks = group_nicknames.get(username, {})
    chat_name = chat.get("name") or contacts.get(username, username)

    def display(user):
        if user == self_wxid:
            return SELF_DISPLAY
        if not user:
            return SYSTEM_DISPLAY
        if group:
            return room_nicks.get(user) or contacts.get(user, user)
        if user == username:
            return chat_name
        return contacts.get(user, user)

    records = []
    for db_path, table in chat["tables"]:
        conn = sqlite3.connect(db_path)
        try:
            id2name = dict(conn.execute("SELECT rowid, user_name FROM Name2Id"))
            rows = conn.execute(f"""
                SELECT local_id, server_id, local_type, real_sender_id, create_time,
                       sort_seq, message_content, WCDB_CT_message_content
                FROM [{table}]
                ORDER BY create_time ASC, sort_seq ASC, local_id ASC
            """).fetchall()
        finally:
            conn.close()

        for (local_id, server_id, local_type, sender_id, create_time,
             sort_seq, content, ct) in rows:
            text = decompress_if_needed(content, ct)
            sender = id2name.get(sender_id)  # None: unknown id, "": system

            if group:
                m = _GROUP_PREFIX_RE.match(text) if text else None
                if m and sender != self_wxid:
                    prefix_user = m.group(1)
                    text = text[m.end():]
                    if not sender and prefix_user != username:
                        sender = prefix_user
                if not sender and (local_type & 0xFFFFFFFF) != 10000:
                    sender = _sender_from_xml(text) or sender

            display_text = format_message(text, local_type)
            if display_text is None:
                continue

            if sender is None:
                # Unknown rowid: keep old export behaviour for 1-on-1.
                sender_name = f"未知({sender_id})"
            else:
                sender_name = display(sender)
            records.append((create_time or 0, sort_seq or 0, {
                "ts": create_time or 0,
                "sender": sender_name,
                "is_self": sender == self_wxid,
                "kind": message_kind(local_type, text),
                "text": display_text,
                "local_type": local_type,
                "local_id": local_id,
                "server_id": server_id or None,
                "create_time": create_time,
            }))

    # Stable sort keeps DB order (oldest DB first) for equal keys.
    records.sort(key=lambda r: (r[0], r[1]))
    for _, _, rec in records:
        yield rec
