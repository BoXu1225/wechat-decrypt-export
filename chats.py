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
import datetime as _dt
import hashlib
import os
import re
import sqlite3
import time

import zstandard

import msg_parse

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


# ---------------------------------------------------------------------------
# Decoding / formatting
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


def format_message(text, local_type, resolve=None):
    """One-line display text for a message (None: drop it). See msg_parse."""
    return msg_parse.parse(text, local_type, resolve)[0]


def parse_message(text, local_type, resolve=None):
    """(display text or None, extra dict or None, kind). See msg_parse."""
    return msg_parse.parse(text, local_type, resolve)


def message_kind(local_type, text):
    """Map a raw local_type (+ decoded content) to a coarse kind string."""
    return msg_parse.kind_of(local_type, text)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def _contact_db(decrypted_dir):
    return os.path.join(decrypted_dir, "contact", "contact.db")


def table_exists(conn, name):
    """True if the SQLite connection has a table called name."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def pick_name(username, remark, nick):
    """Display name: stripped remark > stripped nickname > username."""
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
            if not table_exists(conn, table):
                continue
            for username, remark, nick in conn.execute(
                    f"SELECT username, remark, nick_name FROM [{table}]"):
                if username:
                    out[username] = pick_name(username, remark, nick)
    finally:
        conn.close()
    return out


def load_contact_rows(decrypted_dir):
    """{username: {username, remark, nick_name, alias, local_type, display, source}}.

    Like load_contacts but keeps the individual fields; source is the table
    ("stranger" or "contact"), contact rows override stranger rows."""
    path = _contact_db(decrypted_dir)
    if not os.path.exists(path):
        return {}
    out = {}
    conn = sqlite3.connect(path)
    try:
        for table in ("stranger", "contact"):
            if not table_exists(conn, table):
                continue
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")}
            alias = "alias" if "alias" in cols else "''"
            ltype = "local_type" if "local_type" in cols else "0"
            for u, remark, nick, al, lt in conn.execute(
                    f"SELECT username, remark, nick_name, {alias}, {ltype} FROM [{table}]"):
                if not u:
                    continue
                out[u] = {
                    "username": u,
                    "remark": (remark or "").strip(),
                    "nick_name": (nick or "").strip(),
                    "alias": (al or "").strip(),
                    "local_type": lt or 0,
                    "display": pick_name(u, remark, nick),
                    "source": table,
                }
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
        if not table_exists(conn, "contact"):
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


def load_room_members(decrypted_dir):
    """Return {room_username: {member_username: group_nickname or ''}} for every
    room, including members without a group nickname (in ext_buffer order)."""
    path = _contact_db(decrypted_dir)
    if not os.path.exists(path):
        return {}
    conn = sqlite3.connect(path)
    try:
        if not table_exists(conn, "chat_room"):
            return {}
        return {room: parse_chat_room_members(buf)
                for room, buf in conn.execute("SELECT username, ext_buffer FROM chat_room")
                if room}
    finally:
        conn.close()


def group_nicknames_from_members(room_members):
    """load_room_members output -> {room: {member: group_nickname}}, keeping
    only non-empty nicknames and rooms that have at least one."""
    out = {}
    for room, members in room_members.items():
        nicks = {u: n for u, n in members.items() if n}
        if nicks:
            out[room] = nicks
    return out


def load_group_nicknames(decrypted_dir):
    """Return {room_username: {member_username: group_nickname}} (non-empty only)."""
    return group_nicknames_from_members(load_room_members(decrypted_dir))


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


def unnamed_group_name(members, contacts, self_wxid=None):
    """Name for a group without a name, like WeChat does: the first three
    members other than self ("A、B、C"), plus "等N人" (N = members + self) when
    there are more than three. members: iterable of usernames in room order.
    Returns None when there is nobody to name it after."""
    others = [u for u in members if u != self_wxid]
    if not others:
        return None
    names = [contacts.get(u, u) for u in others[:3]]
    return "、".join(names) + (f"等{len(others) + 1}人" if len(others) > 3 else "")


def name_unnamed_groups(chat_list, room_members, contacts, self_wxid=None):
    """Give list_chats entries of unnamed groups (name == username) a
    member-based name (see unnamed_group_name), in place. Returns chat_list."""
    for c in chat_list:
        if c["is_group"] and c["name"] == c["username"]:
            name = unnamed_group_name(room_members.get(c["username"], {}), contacts, self_wxid)
            if name:
                c["name"] = name
    return chat_list


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


_MSG_DB_RE = re.compile(r"message_(\d+)\.db$")


def db_number(db_path):
    """message_N.db path -> N (None for other paths)."""
    m = _MSG_DB_RE.search(db_path)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def iso(ts):
    """Unix seconds -> local ISO 8601 string (seconds precision); falsy -> None."""
    if not ts:
        return None
    return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


_REL_RE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$")


def parse_time(value, end=False, now=None):
    """Parse a time into unix seconds. None/'' -> None.

    Accepts int/float, unix seconds as a string, "YYYY-MM-DD" (or with "/"),
    "YYYY-MM-DD HH:MM[:SS]" / ISO 8601 (local time unless it has an offset),
    and relative "30m" / "12h" / "7d" / "2w" meaning that long before now.
    end=True makes a date-only value inclusive (end of that day).
    Raises ValueError for anything else."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if re.fullmatch(r"\d{9,11}", s):
        return int(s)
    m = _REL_RE.match(s)
    if m:
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[m.group(2)]
        return int((now or time.time()) - int(m.group(1)) * mult)
    try:
        d = _dt.datetime.fromisoformat(s.replace("/", "-"))
    except ValueError:
        raise ValueError(f"cannot parse time {value!r}; use YYYY-MM-DD, "
                         "YYYY-MM-DD HH:MM, unix seconds, or 7d/12h/30m") from None
    date_only = re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s.replace("/", "-")) is not None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    if end and date_only:
        d = d + _dt.timedelta(days=1) - _dt.timedelta(seconds=1)
    return int(d.timestamp())


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


_VOICELEN_RE = re.compile(r'voicelength\s*=\s*"(\d+)"')
_PLAYLEN_RE = re.compile(r'playlength\s*=\s*"(\d+)"')


def media_duration(text, kind):
    """Stated duration in seconds from voice (voicelength, ms) / video
    (playlength, s) message XML, or None."""
    if not text:
        return None
    if kind == "voice":
        m = _VOICELEN_RE.search(text)
        return int(m.group(1)) / 1000.0 if m else None
    m = _PLAYLEN_RE.search(text)
    return float(m.group(1)) if m else None


def iter_messages(chat, decrypted_dir, self_wxid, contacts, group_nicknames=None,
                  with_packed_info=False):
    """Yield message records for a chat, sorted by time.

    Record: {ts, sender, is_self, kind, text, local_type, local_id,
             server_id, create_time[, extra]}
    extra (only when there is something to add) holds structured details of
    rich messages (quote, link url, transfer amount, chat bundle items, ...);
    see msg_parse for the schema.
    with_packed_info=True adds "packed_info_data" (bytes or None) to image
    and video records; it holds the file md5 (see image_decode / media_decode).
    It also adds "media_duration" (seconds, float) to voice records (XML
    voicelength) and video records (XML playlength) when the XML has it.
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

    def resolve(user):
        """Display name for a username seen inside message XML; None if unknown."""
        if not user:
            return None
        if user == self_wxid:
            return SELF_DISPLAY
        if group and room_nicks.get(user):
            return room_nicks[user]
        if user == username:
            return chat_name
        return contacts.get(user)

    records = []
    for db_path, table in chat["tables"]:
        conn = sqlite3.connect(db_path)
        try:
            id2name = dict(conn.execute("SELECT rowid, user_name FROM Name2Id"))
            # Only fetch the blob for image / video rows (cheap when not needed).
            packed_col = ("CASE WHEN (local_type & 4294967295) IN (3, 43) "
                          "THEN packed_info_data END"
                          if with_packed_info else "NULL")
            rows = conn.execute(f"""
                SELECT local_id, server_id, local_type, real_sender_id, create_time,
                       sort_seq, message_content, WCDB_CT_message_content,
                       {packed_col}
                FROM [{table}]
                ORDER BY create_time ASC, sort_seq ASC, local_id ASC
            """).fetchall()
        finally:
            conn.close()

        for (local_id, server_id, local_type, sender_id, create_time,
             sort_seq, content, ct, packed) in rows:
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

            display_text, extra, kind = msg_parse.parse(text, local_type, resolve)
            if display_text is None:
                continue

            if sender is None:
                # Unknown rowid: keep old export behaviour for 1-on-1.
                sender_name = f"未知({sender_id})"
            else:
                sender_name = display(sender)
            rec = {
                "ts": create_time or 0,
                "sender": sender_name,
                "is_self": sender == self_wxid,
                "kind": kind,
                "text": display_text,
                "local_type": local_type,
                "local_id": local_id,
                "server_id": server_id or None,
                "create_time": create_time,
            }
            if extra:
                rec["extra"] = extra
            if with_packed_info:
                kind = rec["kind"]
                if kind in ("image", "video"):
                    rec["packed_info_data"] = bytes(packed) if packed is not None else None
                if kind in ("voice", "video"):
                    dur = media_duration(text, kind)
                    if dur is not None:
                        rec["media_duration"] = dur
            records.append((create_time or 0, sort_seq or 0, rec))

    # Stable sort keeps DB order (oldest DB first) for equal keys.
    records.sort(key=lambda r: (r[0], r[1]))
    for _, _, rec in records:
        yield rec
