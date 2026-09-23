"""
MCP server exposing decrypted WeChat 4.x chats to AI agents (read-only).

Usage
-----
Prerequisites: databases decrypted at least once (`./wechat decrypt`, which
may ask for sudo to extract keys) and `pip install -r requirements.txt`.

Register with Claude Code (user scope):

    claude mcp add --scope user wechat -- \\
        /ABS/PATH/venv/bin/python /ABS/PATH/mcp_server.py

Claude Desktop (~/Library/Application Support/Claude/claude_desktop_config.json):

    {"mcpServers": {"wechat": {
        "command": "/ABS/PATH/venv/bin/python",
        "args": ["/ABS/PATH/mcp_server.py"]}}}

Manual smoke test: `venv/bin/python mcp_server.py --selftest` prints a short
summary to stderr (no message content) and exits.

Tools (all read-only; results are compact JSON, times are local ISO strings):
    list_chats(filter="", type="all"|"single"|"group", limit=50)
    get_messages(chat, since=None, until=None, limit=200, before=None,
                 after=None, from_start=False, newest_first=False)
    search_messages(query, chat=None, since=None, until=None, sender=None, limit=50)
    get_message_context(chat, message_id=None, time=None, before=10, after=10)
    get_contact(name)
    get_image(chat, message_id)
    refresh(all_dbs=False)

`chat` accepts a username, an exact name (remark/nickname/alias), or a
unique substring; an ambiguous name returns {"error": "ambiguous",
"candidates": [...]} instead of guessing. Message ids look like "3:1234"
(message_3.db, local_id 1234) and are only unique within a chat.
since/until accept "YYYY-MM-DD", "YYYY-MM-DD HH:MM[:SS]", unix seconds, or
relative "30m" / "12h" / "7d" / "2w" (ago). A date-only `until` is inclusive.

Freshness: before reads the server re-decrypts changed databases (no sudo,
existing keys only) at most every `mcp_auto_refresh_minutes` (default 5;
0 disables). If keys are missing/stale it never runs the key scanner; the
refresh result tells the user to run `./wechat decrypt` in a terminal.
WeChat keeps recent writes in -wal files until it checkpoints, so the newest
messages can lag behind the app.

Search uses our own SQLite FTS5 index (trigram tokenizer, works for Chinese)
at `mcp_index_path` (default decrypted/mcp_index.db), built on first use and
updated incrementally per message table. WeChat's own message_fts.db uses a
proprietary tokenizer (MMFtsTokenizer) that stock SQLite cannot MATCH with.
Queries shorter than 3 characters fall back to a LIKE scan.

Optional config.json keys:
    "mcp_blocklist": [names or usernames]   hidden from every tool
    "mcp_allowlist": [names or usernames]   if non-empty, only these are visible
    "mcp_auto_refresh_minutes": 5
    "mcp_index_path": "decrypted/mcp_index.db"
    "mcp_access_log": "logs/mcp_access.jsonl"
Names match a username, remark, nickname, alias or display name exactly.
The lists hide chats and contact records; messages a blocked person sent
inside a visible group stay visible in that group.

Every tool call is appended to the access log (JSONL: time, tool, args,
result count, duration) -- never message content.

Nothing may be written to stdout except MCP protocol frames: tool bodies run
with sys.stdout redirected to stderr, and the SDK's stdio transport diverts
fd 1 to stderr while serving.
"""
import contextlib
import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import chats as C

ROOT = os.path.dirname(os.path.abspath(__file__))

MAX_LIMIT = 500          # hard cap for any list result
MAX_CONTEXT = 100        # hard cap for before/after in get_message_context
MAX_TEXT = 4000          # per-message text cap in results
MAX_CANDIDATES = 20
INDEX_VERSION = "1"
PLACEHOLDER_KINDS = ("image", "voice", "video", "emoji")
# Databases the server reads; auto-refresh only re-decrypts these.
_WANTED_DB_RE = re.compile(r"^(message/message_\d+\.db|message/message_resource\.db|contact/contact\.db)$")
_MSG_DB_RE = re.compile(r"message_(\d+)\.db$")


class ToolError(Exception):
    """An anticipated failure reported to the client as {"error": ...}."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.payload = {"error": message, **extra}


# ---------------------------------------------------------------------------
# Small helpers (candidates for chats.py)
# ---------------------------------------------------------------------------

def iso(ts):
    if not ts:
        return None
    return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


_REL_RE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$")


def parse_time(value, end=False, now=None):
    """Parse since/until into unix seconds. None/'' -> None.

    end=True makes a date-only value inclusive (end of that day)."""
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
        raise ToolError(f"cannot parse time {value!r}; use YYYY-MM-DD, "
                        "YYYY-MM-DD HH:MM, unix seconds, or 7d/12h/30m")
    date_only = re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s.replace("/", "-")) is not None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    if end and date_only:
        d = d + _dt.timedelta(days=1) - _dt.timedelta(seconds=1)
    return int(d.timestamp())


def clamp(n, lo, hi):
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = hi
    return max(lo, min(hi, n))


def load_contact_rows(decrypted_dir):
    """{username: {username, remark, nick_name, alias, local_type, display, source}}.

    contact rows override stranger rows. (chats.load_contacts only returns the
    display name; we need the individual fields for matching and get_contact.)"""
    path = os.path.join(decrypted_dir, "contact", "contact.db")
    if not os.path.exists(path):
        return {}
    out = {}
    conn = sqlite3.connect(path)
    try:
        for table in ("stranger", "contact"):
            if not C._table_exists(conn, table):
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
                    "display": C._pick_name(u, remark, nick),
                    "source": table,
                }
    finally:
        conn.close()
    return out


def load_room_members(decrypted_dir):
    """{room_username: {member_username: group_nickname or ''}} for all rooms
    (chats.load_group_nicknames drops members without a group nickname)."""
    path = os.path.join(decrypted_dir, "contact", "contact.db")
    if not os.path.exists(path):
        return {}
    conn = sqlite3.connect(path)
    try:
        if not C._table_exists(conn, "chat_room"):
            return {}
        return {room: C.parse_chat_room_members(buf)
                for room, buf in conn.execute("SELECT username, ext_buffer FROM chat_room")
                if room}
    finally:
        conn.close()


def db_number(db_path):
    m = _MSG_DB_RE.search(db_path)
    return int(m.group(1)) if m else None


def _snippet(text, terms, width=60):
    if not text:
        return ""
    low = text.lower()
    pos = -1
    for t in terms:
        pos = low.find(t.lower())
        if pos >= 0:
            break
    if pos < 0 or len(text) <= 2 * width + 20:
        return text if len(text) <= 2 * width + 20 else text[:2 * width] + "…"
    start = max(0, pos - width)
    end = min(len(text), pos + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _cap_text(text):
    if text and len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + f"…[+{len(text) - MAX_TEXT} chars]"
    return text


# ---------------------------------------------------------------------------
# Search / message index
# ---------------------------------------------------------------------------

_INDEX_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS src_tables(db INTEGER, tbl TEXT, chat TEXT, cnt INTEGER,
    max_id INTEGER, type_sum REAL, PRIMARY KEY(db, tbl));
CREATE TABLE IF NOT EXISTS src_files(db INTEGER PRIMARY KEY, mtime REAL, size INTEGER);
CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY, chat TEXT NOT NULL, db INTEGER NOT NULL,
    dbr INTEGER NOT NULL, local_id INTEGER NOT NULL, ts INTEGER NOT NULL, is_self INTEGER,
    sender TEXT, kind TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS msgs_chat_order ON msgs(chat, ts, dbr, local_id);
CREATE UNIQUE INDEX IF NOT EXISTS msgs_ref ON msgs(chat, db, local_id);
CREATE INDEX IF NOT EXISTS msgs_ts ON msgs(ts);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, content='msgs', content_rowid='id',
    tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS msgs_ai AFTER INSERT ON msgs
    WHEN new.kind NOT IN {PLACEHOLDER_KINDS!r} BEGIN
    INSERT INTO fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS msgs_ad AFTER DELETE ON msgs
    WHEN old.kind NOT IN {PLACEHOLDER_KINDS!r} BEGIN
    INSERT INTO fts(fts, rowid, text) VALUES ('delete', old.id, old.text); END;
"""


class MessageIndex:
    """Normalized copy of every (non-system) chat message plus an FTS5 trigram
    index. Kept in sync per source table using a (count, max(local_id),
    sum(local_type)) fingerprint: appended rows are added incrementally, any
    other change (deletion, recall, rebuilt DB) re-indexes that table."""

    def __init__(self, path, self_wxid):
        self.path = path
        self.self_wxid = self_wxid
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = self._open()

    def _open(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_INDEX_SCHEMA)
        meta = dict(conn.execute("SELECT k, v FROM meta"))
        want = {"version": INDEX_VERSION, "self_wxid": self.self_wxid or ""}
        if meta and any(meta.get(k) != v for k, v in want.items()):
            conn.close()
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.path + suffix)
            return self._open()
        conn.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", want.items())
        conn.commit()
        return conn

    def close(self):
        self.conn.close()

    def rebuild(self):
        self.conn.executescript("DELETE FROM msgs; DELETE FROM src_tables; DELETE FROM src_files;"
                                "INSERT INTO fts(fts) VALUES('rebuild');")
        self.conn.commit()

    def sync(self, chat_list, decrypted_dir, contacts, group_nicknames, only=None):
        """Bring the index up to date. only: set of chat usernames to limit
        the work to (tables of other chats are left as they are).
        Returns stats dict."""
        t0 = time.time()
        stats = {"tables_checked": 0, "tables_appended": 0, "tables_reindexed": 0,
                 "messages_added": 0}
        by_db = {}
        for chat in chat_list:
            if only is not None and chat["username"] not in only:
                continue
            for db_path, table in chat["tables"]:
                by_db.setdefault(db_path, []).append((chat, table))
        conn = self.conn
        stored_files = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT db, mtime, size FROM src_files")}
        for db_path in C.message_db_paths(decrypted_dir):
            n = db_number(db_path)
            st = os.stat(db_path)
            sig = (st.st_mtime, st.st_size)
            if only is None and stored_files.get(n) == sig:
                continue
            src = sqlite3.connect(db_path)
            try:
                for chat, table in by_db.get(db_path, []):
                    stats["tables_checked"] += 1
                    self._sync_table(src, n, db_path, chat, table, decrypted_dir, contacts,
                                     group_nicknames, stats)
            finally:
                src.close()
            if only is None:
                # Drop tables that disappeared from this DB (chat deleted / now hidden).
                present = {t for _, t in by_db.get(db_path, [])}
                for (tbl, chat_u) in conn.execute(
                        "SELECT tbl, chat FROM src_tables WHERE db=?", (n,)).fetchall():
                    if tbl not in present:
                        conn.execute("DELETE FROM msgs WHERE db=? AND chat=?", (n, chat_u))
                        conn.execute("DELETE FROM src_tables WHERE db=? AND tbl=?", (n, tbl))
                conn.execute("INSERT OR REPLACE INTO src_files(db, mtime, size) VALUES (?,?,?)",
                             (n, sig[0], sig[1]))
            conn.commit()
        if only is None:
            live = {db_number(p) for p in C.message_db_paths(decrypted_dir)}
            for (n,) in conn.execute("SELECT db FROM src_files").fetchall():
                if n not in live:
                    conn.execute("DELETE FROM msgs WHERE db=?", (n,))
                    conn.execute("DELETE FROM src_tables WHERE db=?", (n,))
                    conn.execute("DELETE FROM src_files WHERE db=?", (n,))
            conn.commit()
        stats["seconds"] = round(time.time() - t0, 3)
        return stats

    def _sync_table(self, src, n, db_path, chat, table, decrypted_dir, contacts,
                    group_nicknames, stats):
        conn = self.conn
        username = chat["username"]
        cnt, max_id, type_sum = src.execute(
            f"SELECT count(*), max(local_id), total(local_type) FROM [{table}]").fetchone()
        max_id = max_id or 0
        row = conn.execute("SELECT cnt, max_id, type_sum FROM src_tables WHERE db=? AND tbl=?",
                           (n, table)).fetchone()
        if row and tuple(row) == (cnt, max_id, type_sum):
            return
        start_after = 0
        if row and max_id >= row[1]:
            old = src.execute(f"SELECT count(*), total(local_type) FROM [{table}] WHERE local_id <= ?",
                              (row[1],)).fetchone()
            if tuple(old) == (row[0], row[2]):
                start_after = row[1]
        if start_after:
            stats["tables_appended"] += 1
        else:
            if row:
                stats["tables_reindexed"] += 1
            conn.execute("DELETE FROM msgs WHERE db=? AND chat=?", (n, username))
        one = dict(chat, tables=[(db_path, table)])
        batch = []
        for rec in C.iter_messages(one, decrypted_dir, self.self_wxid, contacts,
                                   group_nicknames=group_nicknames):
            if rec["local_id"] <= start_after:
                continue
            batch.append((username, n, -n, rec["local_id"], rec["ts"], int(rec["is_self"]),
                          rec["sender"], rec["kind"], rec["text"]))
        conn.executemany("INSERT OR REPLACE INTO msgs(chat, db, dbr, local_id, ts, is_self, sender, "
                         "kind, text) VALUES (?,?,?,?,?,?,?,?,?)", batch)
        stats["messages_added"] += len(batch)
        conn.execute("INSERT OR REPLACE INTO src_tables(db, tbl, chat, cnt, max_id, type_sum) "
                     "VALUES (?,?,?,?,?,?)", (n, table, username, cnt, max_id, type_sum))


# ---------------------------------------------------------------------------
# Data access (tool implementations)
# ---------------------------------------------------------------------------

def _row_msg(r, anchor_id=None):
    """msgs row (db, local_id, ts, sender, kind, text) -> result dict."""
    db, lid, ts, sender, kind, text = r
    mid = f"{db}:{lid}"
    d = {"id": mid, "time": iso(ts), "sender": sender}
    if kind != "text":
        d["kind"] = kind
    d["text"] = _cap_text(text)
    if anchor_id is not None and mid == anchor_id:
        d["anchor"] = True
    return d


_MSG_COLS = "db, local_id, ts, sender, kind, text"


class WeChatData:
    """All tool logic, independent of MCP. Thread-safe via one RLock."""

    def __init__(self, cfg, decryptor=None):
        self.cfg = cfg
        self.decrypted_dir = cfg["decrypted_dir"]
        self.self_wxid = cfg.get("self_wxid") or ""
        self.lock = threading.RLock()
        self._decryptor = decryptor
        self._cache = {}
        self._cache_sig = None
        self._index = None
        self._last_refresh = 0.0
        self._pending_notice = None
        self._image_keys = None
        block = cfg.get("mcp_blocklist") or []
        allow = cfg.get("mcp_allowlist") or []
        self.blocklist = {str(x).strip() for x in block if str(x).strip()}
        self.allowlist = {str(x).strip() for x in allow if str(x).strip()}
        self.auto_refresh_seconds = float(cfg.get("mcp_auto_refresh_minutes", 5)) * 60
        self.index_path = cfg.get("mcp_index_path") or os.path.join(self.decrypted_dir, "mcp_index.db")
        if not os.path.isabs(self.index_path):
            self.index_path = os.path.join(ROOT, self.index_path)

    # -- caches -------------------------------------------------------------
    def _data_sig(self):
        paths = [os.path.join(self.decrypted_dir, "contact", "contact.db")]
        paths += C.message_db_paths(self.decrypted_dir)
        sig = []
        for p in paths:
            try:
                st = os.stat(p)
                sig.append((p, st.st_mtime, st.st_size))
            except FileNotFoundError:
                pass
        return tuple(sig)

    def invalidate(self):
        self._cache = {}
        self._cache_sig = None

    def _cached(self, key, fn):
        sig = self._data_sig()
        if sig != self._cache_sig:
            self._cache = {}
            self._cache_sig = sig
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def contacts(self):
        return self._cached("contacts", lambda: C.load_contacts(self.decrypted_dir))

    def contact_rows(self):
        return self._cached("contact_rows", lambda: load_contact_rows(self.decrypted_dir))

    def room_members(self):
        return self._cached("rooms", lambda: load_room_members(self.decrypted_dir))

    def group_nicknames(self):
        return self._cached("group_nicks", lambda: {
            r: {u: n for u, n in m.items() if n} for r, m in self.room_members().items()
            if any(m.values())})

    def all_chats(self):
        """chats.list_chats output with synthesized names for unnamed groups."""
        def build():
            out = C.list_chats(self.decrypted_dir, contacts=self.contacts())
            rows, rooms, contacts = self.contact_rows(), self.room_members(), self.contacts()
            for c in out:
                if c["is_group"] and c["name"] == c["username"]:
                    members = [u for u in rooms.get(c["username"], {}) if u != self.self_wxid]
                    if members:
                        names = [contacts.get(u, u) for u in members[:3]]
                        c["name"] = "、".join(names) + (f"等{len(members) + 1}人" if len(members) > 3 else "")
                r = rows.get(c["username"], {})
                c["_names"] = {c["name"], c["username"], r.get("remark"), r.get("nick_name"),
                               r.get("alias")} - {"", None}
            return out
        return self._cached("chats", build)

    def visible_chats(self):
        return self._cached("visible_chats", lambda: [
            c for c in self.all_chats() if self.is_visible(c["username"], c["_names"])])

    def hidden_usernames(self):
        return self._cached("hidden", lambda: {
            c["username"] for c in self.all_chats()
            if not self.is_visible(c["username"], c["_names"])})

    def is_visible(self, username, names=None):
        if not self.blocklist and not self.allowlist:
            return True
        if names is None:
            r = self.contact_rows().get(username, {})
            names = {username, r.get("display"), r.get("remark"), r.get("nick_name"),
                     r.get("alias")} - {"", None}
        if self.blocklist and names & self.blocklist:
            return False
        if self.allowlist and not names & self.allowlist:
            return False
        return True

    def index(self):
        if self._index is None:
            self._index = MessageIndex(self.index_path, self.self_wxid)
        return self._index

    def sync_index(self, only=None):
        return self.index().sync(self.all_chats(), self.decrypted_dir, self.contacts(),
                                 self.group_nicknames(), only=only)

    # -- chat resolution ----------------------------------------------------
    @staticmethod
    def _chat_summary(c):
        return {"name": c["name"], "username": c["username"], "is_group": c["is_group"],
                "msg_count": c["msg_count"], "last_time": iso(c["last_ts"])}

    def resolve_chat(self, query):
        if not query or not str(query).strip():
            raise ToolError("chat is required")
        q = str(query).strip()
        chats = self.visible_chats()
        for c in chats:
            if c["username"] == q:
                return c
        for matcher in (lambda c: q in c["_names"],
                        lambda c: q.lower() in {x.lower() for x in c["_names"]},
                        lambda c: any(q.lower() in x.lower() for x in c["_names"])):
            hits = [c for c in chats if matcher(c)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ToolError("ambiguous", query=q, candidates=[
                    self._chat_summary(c) for c in hits[:MAX_CANDIDATES]],
                    hint="call again with one candidate's username")
        raise ToolError(f"no chat matches {q!r}", hint="use list_chats(filter=...) to browse")

    # -- tools --------------------------------------------------------------
    def list_chats(self, filter="", type="all", limit=50):
        limit = clamp(limit, 1, MAX_LIMIT)
        if type not in ("all", "single", "group"):
            raise ToolError("type must be 'all', 'single' or 'group'")
        chats = self.visible_chats()
        if type != "all":
            chats = [c for c in chats if c["is_group"] == (type == "group")]
        if filter:
            f = filter.lower()
            chats = [c for c in chats if any(f in x.lower() for x in c["_names"])]
        return {"total": len(chats), "count": min(limit, len(chats)),
                "chats": [self._chat_summary(c) for c in chats[:limit]]}

    def _cursor_key(self, chat_u, mid):
        m = re.fullmatch(r"\s*(\d+):(\d+)\s*", str(mid))
        if not m:
            raise ToolError(f"bad message id {mid!r}; expected 'N:local_id'")
        row = self.index().conn.execute(
            "SELECT ts, dbr, local_id FROM msgs WHERE chat=? AND db=? AND local_id=?",
            (chat_u, int(m.group(1)), int(m.group(2)))).fetchone()
        if not row:
            raise ToolError(f"message {mid} not found in this chat")
        return tuple(row)

    def get_messages(self, chat, since=None, until=None, limit=200, before=None, after=None,
                     from_start=False, newest_first=False):
        c = self.resolve_chat(chat)
        limit = clamp(limit, 1, MAX_LIMIT)
        self.sync_index(only={c["username"]})
        conn = self.index().conn
        where, args = ["chat=?"], [c["username"]]
        t_since, t_until = parse_time(since), parse_time(until, end=True)
        if t_since is not None:
            where.append("ts>=?")
            args.append(t_since)
        if t_until is not None:
            where.append("ts<=?")
            args.append(t_until)
        range_where, range_args = list(where), list(args)
        if before:
            where.append("(ts, dbr, local_id) < (?, ?, ?)")
            args += self._cursor_key(c["username"], before)
        if after:
            where.append("(ts, dbr, local_id) > (?, ?, ?)")
            args += self._cursor_key(c["username"], after)
        forward = bool(after) or bool(from_start)
        order = "ASC" if forward else "DESC"
        rows = conn.execute(
            f"SELECT {_MSG_COLS}, dbr FROM msgs WHERE {' AND '.join(where)} "
            f"ORDER BY ts {order}, dbr {order}, local_id {order} LIMIT ?",
            args + [limit]).fetchall()
        if not forward:
            rows.reverse()
        msgs = [_row_msg(r[:6]) for r in rows]

        def exists(cmp, key):
            return conn.execute(
                f"SELECT 1 FROM msgs WHERE {' AND '.join(range_where)} "
                f"AND (ts, dbr, local_id) {cmp} (?, ?, ?) LIMIT 1",
                range_args + list(key)).fetchone() is not None

        if rows:
            first = (rows[0][2], rows[0][6], rows[0][1])
            last = (rows[-1][2], rows[-1][6], rows[-1][1])
            more_before, more_after = exists("<", first), exists(">", last)
        else:
            more_before = more_after = False
        if newest_first:
            msgs.reverse()
        out = {"chat": {"name": c["name"], "username": c["username"], "is_group": c["is_group"]},
               "count": len(msgs), "messages": msgs,
               "has_more_before": more_before, "has_more_after": more_after}
        if rows:
            out["before_cursor"] = f"{rows[0][0]}:{rows[0][1]}"
            out["after_cursor"] = f"{rows[-1][0]}:{rows[-1][1]}"
        return out

    def search_messages(self, query, chat=None, since=None, until=None, sender=None, limit=50):
        q = (query or "").strip()
        if not q:
            raise ToolError("query is required")
        limit = clamp(limit, 1, MAX_LIMIT)
        c = self.resolve_chat(chat) if chat else None
        self.sync_index()
        terms = q.split()
        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]
        where, args = [], []
        if long_terms:
            base = "FROM fts JOIN msgs ON msgs.id = fts.rowid"
            where.append("fts MATCH ?")
            args.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms))
        else:
            base = "FROM msgs"
            where.append(f"msgs.kind NOT IN ({','.join('?' * len(PLACEHOLDER_KINDS))})")
            args += PLACEHOLDER_KINDS
        for t in short_terms:
            where.append("msgs.text LIKE ? ESCAPE '\\'")
            args.append("%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if c:
            where.append("msgs.chat=?")
            args.append(c["username"])
        else:
            if self.allowlist:
                vis = [x["username"] for x in self.visible_chats()]
                where.append(f"msgs.chat IN ({','.join('?' * len(vis))})")
                args += vis
            hidden = self.hidden_usernames()
            if self.blocklist and hidden:
                where.append(f"msgs.chat NOT IN ({','.join('?' * len(hidden))})")
                args += sorted(hidden)
        t_since, t_until = parse_time(since), parse_time(until, end=True)
        if t_since is not None:
            where.append("msgs.ts>=?")
            args.append(t_since)
        if t_until is not None:
            where.append("msgs.ts<=?")
            args.append(t_until)
        if sender:
            where.append("msgs.sender LIKE ? ESCAPE '\\'")
            args.append("%" + sender.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        conn = self.index().conn
        sql_where = " AND ".join(where)
        rows = conn.execute(
            f"SELECT msgs.chat, {', '.join('msgs.' + x for x in _MSG_COLS.split(', '))} "
            f"{base} WHERE {sql_where} ORDER BY msgs.ts DESC LIMIT ?", args + [limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        names = {x["username"]: x["name"] for x in self.all_chats()}
        results = []
        for chat_u, db, lid, ts, snd, kind, text in rows:
            d = {"chat": names.get(chat_u, chat_u), "chat_username": chat_u,
                 "id": f"{db}:{lid}", "time": iso(ts), "sender": snd}
            if kind != "text":
                d["kind"] = kind
            d["snippet"] = _snippet(text, terms)
            results.append(d)
        return {"query": q, "count": len(results), "has_more": more, "results": results,
                "hint": "get_message_context(chat=<chat_username>, message_id=<id>) for surrounding messages"}

    def get_message_context(self, chat, message_id=None, time=None, before=10, after=10):
        c = self.resolve_chat(chat)
        before = clamp(before, 0, MAX_CONTEXT)
        after = clamp(after, 0, MAX_CONTEXT)
        self.sync_index(only={c["username"]})
        conn = self.index().conn
        u = c["username"]
        if message_id:
            key = self._cursor_key(u, message_id)
        elif time not in (None, ""):
            t = parse_time(time)
            row = conn.execute("SELECT ts, dbr, local_id FROM msgs WHERE chat=? AND ts<=? "
                               "ORDER BY ts DESC, dbr DESC, local_id DESC LIMIT 1", (u, t)).fetchone() \
                or conn.execute("SELECT ts, dbr, local_id FROM msgs WHERE chat=? AND ts>? "
                                "ORDER BY ts, dbr, local_id LIMIT 1", (u, t)).fetchone()
            if not row:
                raise ToolError("chat has no messages")
            key = tuple(row)
        else:
            raise ToolError("pass message_id or time")
        anchor = conn.execute(f"SELECT {_MSG_COLS} FROM msgs WHERE chat=? AND ts=? AND dbr=? "
                              "AND local_id=?", (u, *key)).fetchone()
        prev = conn.execute(f"SELECT {_MSG_COLS} FROM msgs WHERE chat=? AND (ts, dbr, local_id) < "
                            "(?, ?, ?) ORDER BY ts DESC, dbr DESC, local_id DESC LIMIT ?",
                            (u, *key, before)).fetchall()
        nxt = conn.execute(f"SELECT {_MSG_COLS} FROM msgs WHERE chat=? AND (ts, dbr, local_id) > "
                           "(?, ?, ?) ORDER BY ts, dbr, local_id LIMIT ?", (u, *key, after)).fetchall()
        rows = list(reversed(prev)) + [anchor] + nxt
        anchor_id = f"{anchor[0]}:{anchor[1]}"
        return {"chat": {"name": c["name"], "username": u, "is_group": c["is_group"]},
                "anchor_id": anchor_id, "count": len(rows),
                "messages": [_row_msg(r, anchor_id) for r in rows]}

    def _resolve_contact(self, name):
        q = (name or "").strip()
        if not q:
            raise ToolError("name is required")
        rows = [r for r in self.contact_rows().values() if self.is_visible(r["username"])]
        chat_names = {c["username"]: c["name"] for c in self.all_chats()}

        def names(r):
            return {r["username"], r["display"], r["remark"], r["nick_name"], r["alias"],
                    chat_names.get(r["username"])} - {"", None}

        for r in rows:
            if r["username"] == q:
                return r
        for matcher in (lambda r: q in names(r),
                        lambda r: q.lower() in {x.lower() for x in names(r)},
                        lambda r: any(q.lower() in x.lower() for x in names(r))):
            hits = [r for r in rows if matcher(r)]
            if len(hits) > 1:
                # Prefer friends / groups over strangers when that disambiguates.
                strong = [r for r in hits if r["source"] == "contact" and r["local_type"] in (1, 2)]
                if len(strong) == 1:
                    return strong[0]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                hits.sort(key=lambda r: (r["source"] != "contact", r["display"]))
                raise ToolError("ambiguous", query=q, candidates=[
                    {"name": r["display"], "username": r["username"],
                     "is_group": C.is_group(r["username"]), "is_friend": r["local_type"] == 1}
                    for r in hits[:MAX_CANDIDATES]],
                    hint="call again with one candidate's username")
        raise ToolError(f"no contact matches {q!r}")

    def get_contact(self, name):
        r = self._resolve_contact(name)
        u = r["username"]
        chats_by_u = {c["username"]: c for c in self.visible_chats()}
        out = {"username": u, "name": chats_by_u.get(u, {}).get("name") or r["display"],
               "remark": r["remark"] or None, "nickname": r["nick_name"] or None,
               "alias": r["alias"] or None, "is_group": C.is_group(u),
               "is_friend": r["source"] == "contact" and r["local_type"] == 1}
        rooms = self.room_members()
        contacts = self.contacts()
        chat = chats_by_u.get(u)
        if chat:
            self.sync_index(only={u})
            n, first, last, mine = self.index().conn.execute(
                "SELECT count(*), min(ts), max(ts), total(is_self) FROM msgs WHERE chat=?",
                (u,)).fetchone()
            out["chat"] = {"msg_count": n, "first_time": iso(first), "last_time": iso(last),
                           "sent_by_me": int(mine), "sent_by_them": n - int(mine)}
        if C.is_group(u):
            members = rooms.get(u, {})
            out["member_count"] = len(members)
            out["members"] = [
                {"name": gn or contacts.get(m, m), "username": m}
                for m, gn in list(members.items())[:MAX_LIMIT]]
            return out
        groups = []
        for room, members in rooms.items():
            if u not in members or not self.is_visible(room):
                continue
            c = chats_by_u.get(room)
            groups.append({"name": c["name"] if c else contacts.get(room, room), "username": room,
                           "their_group_nickname": members[u] or None,
                           "member_count": len(members),
                           "msg_count": c["msg_count"] if c else 0,
                           "last_time": iso(c["last_ts"]) if c else None})
        groups.sort(key=lambda g: g["last_time"] or "", reverse=True)
        out["shared_group_count"] = len(groups)
        out["shared_groups"] = groups[:100]
        return out

    # -- images -------------------------------------------------------------
    def image_keys(self):
        if self._image_keys is None:
            import image_decode
            self._image_keys = image_decode.get_image_keys(self.cfg)
        return self._image_keys

    def get_image(self, chat, message_id):
        """Returns (image_bytes, fmt, meta dict)."""
        import image_decode as I
        c = self.resolve_chat(chat)
        m = re.fullmatch(r"\s*(\d+):(\d+)\s*", str(message_id or ""))
        if not m:
            raise ToolError(f"bad message id {message_id!r}; expected 'N:local_id'")
        n, lid = int(m.group(1)), int(m.group(2))
        db_path = os.path.join(self.decrypted_dir, "message", f"message_{n}.db")
        table = C.table_name_for(c["username"])
        if not os.path.exists(db_path) or (db_path, table) not in [tuple(t) for t in c["tables"]]:
            raise ToolError(f"message {message_id} not found in this chat")
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(f"SELECT local_type, create_time, packed_info_data FROM [{table}] "
                               "WHERE local_id=?", (lid,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise ToolError(f"message {message_id} not found in this chat")
        local_type, create_time, packed = row
        if (local_type & 0xFFFFFFFF) != 3:
            raise ToolError(f"message {message_id} is not an image (kind "
                            f"{C.message_kind(local_type, None)})")
        base = self.cfg.get("wechat_base_dir")
        if not base or not os.path.isdir(base):
            raise ToolError("WeChat data folder not found (wechat_base_dir)")
        path = I.find_image_for_message(base, c["username"], packed, create_time)
        if path is None:
            res_db = os.path.join(self.decrypted_dir, "message", "message_resource.db")
            if os.path.exists(res_db):
                md5 = I.image_md5_from_resource_db(res_db, c["username"], lid, create_time)
                if md5:
                    path = I.find_image_for_message(base, c["username"], md5=md5,
                                                    create_time=create_time)
        if path is None:
            raise ToolError("image file not on disk (probably never downloaded in WeChat)")
        aes_key, xor_key = self.image_keys()
        try:
            data, ext = I.decode_dat(path, aes_key, xor_key, wxgf_to="jpg")
        except I.ImageDecodeError as e:
            raise ToolError(f"could not decode image: {e}")
        variant = {"_h": "hd", "_t": "thumbnail"}.get(
            os.path.basename(path)[32:-4], "full")
        data, ext = _fit_image(data, ext)
        meta = {"chat": c["name"], "id": f"{n}:{lid}", "time": iso(create_time),
                "variant": variant, "format": ext, "bytes": len(data)}
        return data, ext, meta

    # -- refresh ------------------------------------------------------------
    def _load_decryptor(self):
        if self._decryptor is None:
            with contextlib.redirect_stdout(sys.stderr):
                import decrypt_db
            self._decryptor = decrypt_db
        return self._decryptor

    def refresh(self, all_dbs=False):
        """Re-decrypt changed databases with existing keys. Never runs sudo."""
        t0 = time.time()
        db_dir = self.cfg.get("db_dir")
        if not db_dir or not os.path.isdir(db_dir):
            raise ToolError("WeChat db_dir not found; check config.json")
        keys_file = self.cfg.get("keys_file") or os.path.join(ROOT, "all_keys.json")
        keys = {}
        if os.path.exists(keys_file):
            with open(keys_file) as f:
                keys = json.load(f)
            keys.pop("_db_dir", None)
        dd = self._load_decryptor()
        state = dd.load_state(self.decrypted_dir)
        updated, unchanged, missing, stale, failed = [], 0, [], [], []
        for root, _, files in os.walk(db_dir):
            for fn in files:
                if not fn.endswith(".db"):
                    continue
                src = os.path.join(root, fn)
                rel = os.path.relpath(src, db_dir).replace("\\", "/")
                if not all_dbs and not _WANTED_DB_RE.match(rel):
                    continue
                out = os.path.join(self.decrypted_dir, rel)
                if dd.is_current(state, rel, src, out):  # neither .db nor -wal changed
                    unchanged += 1
                    continue
                if rel not in keys:
                    missing.append(rel)
                    continue
                key = bytes.fromhex(keys[rel]["enc_key"])
                with open(src, "rb") as f:
                    page1 = f.read(dd.PAGE_SZ)
                if len(page1) < dd.PAGE_SZ or not dd.page1_hmac_ok(page1, key):
                    stale.append(rel)
                    continue
                tmp = out + ".mcp_tmp"
                try:
                    with contextlib.redirect_stdout(sys.stderr):
                        sig = dd.decrypt_database(src, tmp, key)  # includes -wal
                    if sig:
                        os.replace(tmp, out)
                        dd.save_state(self.decrypted_dir, {rel: sig})
                        updated.append(rel)
                    else:
                        failed.append(rel)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(tmp)
        self._last_refresh = time.time()
        if updated:
            self.invalidate()
        res = {"updated": sorted(updated), "unchanged": unchanged, "failed": sorted(failed),
               "missing_keys": sorted(missing), "stale_keys": sorted(stale),
               "seconds": round(time.time() - t0, 2)}
        important = [r for r in missing + stale if _WANTED_DB_RE.match(r)]
        if important:
            res["action_required"] = ("Some databases have no valid key (WeChat rotated or "
                                      "created them). Ask the user to run `./wechat decrypt` "
                                      "in a terminal (it needs sudo); this server never runs sudo.")
        return res

    def maybe_auto_refresh(self):
        """At most every mcp_auto_refresh_minutes; returns a notice string or None."""
        if self.auto_refresh_seconds <= 0:
            return None
        if time.time() - self._last_refresh < self.auto_refresh_seconds:
            return None
        try:
            res = self.refresh()
        except Exception as e:  # never let freshness break a read
            self._last_refresh = time.time()
            print(f"[mcp] auto refresh failed: {e!r}", file=sys.stderr)
            return None
        return res.get("action_required")


def _fit_image(data, ext, max_bytes=1_500_000, max_dim=1600):
    """Convert odd formats to jpg and shrink large images with macOS sips."""
    if ext in ("jpg", "png", "gif", "webp") and len(data) <= max_bytes:
        return data, ext
    sips = shutil.which("sips")
    if not sips:
        return data, ext
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in." + ext)
        dst = os.path.join(td, "out.jpg")
        with open(src, "wb") as f:
            f.write(data)
        r = subprocess.run([sips, "-s", "format", "jpeg", "-Z", str(max_dim), src, "--out", dst],
                           capture_output=True)
        if r.returncode == 0 and os.path.exists(dst):
            with open(dst, "rb") as f:
                return f.read(), "jpg"
    return data, ext


# ---------------------------------------------------------------------------
# Access log + dispatch
# ---------------------------------------------------------------------------

class AccessLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, tool, args, count, ms, error=None):
        rec = {"time": _dt.datetime.now().isoformat(timespec="seconds"), "tool": tool,
               "args": {k: v for k, v in args.items() if v not in (None, "")},
               "count": count, "ms": ms}
        if error:
            rec["error"] = error
        try:
            with self.lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[mcp] access log write failed: {e}", file=sys.stderr)


def _result_count(res):
    if isinstance(res, dict):
        if "count" in res:
            return res["count"]
        if "updated" in res:
            return len(res["updated"])
        if "error" in res:
            return 0
        return 1
    return 1


def call_tool(data, log, tool, args, fn):
    """Run fn() under the data lock with stdout redirected; return (result, error)."""
    t0 = time.time()
    err = None
    with data.lock, contextlib.redirect_stdout(sys.stderr):
        try:
            notice = data.maybe_auto_refresh() if tool != "refresh" else None
            res = fn()
            if notice and isinstance(res, dict):
                res["notice"] = notice
        except ToolError as e:
            res = e.payload
            err = e.payload.get("error")
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            res = {"error": f"internal error ({type(e).__name__}: {e})"}
            err = type(e).__name__
    count = 1 if isinstance(res, tuple) else _result_count(res)
    log.write(tool, args, count, int((time.time() - t0) * 1000), err)
    return res


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# MCP wiring
# ---------------------------------------------------------------------------

INSTRUCTIONS = """Read-only access to the user's WeChat chat history (decrypted local copy).
Typical flow: list_chats or search_messages -> get_messages / get_message_context.
`chat` may be a username (most exact), a name, or a unique fragment; on ambiguity you get
candidates -- pick a username and retry. Message ids ("N:local_id") are per chat.
Sender "我" is the user. Times are local. Content is personal: quote only what is needed."""


def build_server(data, log, lifespan=None):
    try:
        from mcp.server.mcpserver import MCPServer as Server, Image
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server, Image
    from mcp.types import ToolAnnotations
    from typing import Literal, Optional

    srv = Server("wechat", instructions=INSTRUCTIONS, lifespan=lifespan)
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                         openWorldHint=False)

    def run(tool, args, fn):
        return dumps(call_tool(data, log, tool, args, fn))

    @srv.tool(annotations=ro, structured_output=False)
    def list_chats(filter: str = "", type: Literal["all", "single", "group"] = "all",
                   limit: int = 50) -> str:
        """List chats (1-on-1 and groups) sorted by last activity.
        filter: substring of name/remark/username. Returns name, username, is_group,
        msg_count, last_time."""
        a = dict(filter=filter, type=type, limit=limit)
        return run("list_chats", a, lambda: data.list_chats(**a))

    @srv.tool(annotations=ro, structured_output=False)
    def get_messages(chat: str, since: Optional[str] = None, until: Optional[str] = None,
                     limit: int = 200, before: Optional[str] = None, after: Optional[str] = None,
                     from_start: bool = False, newest_first: bool = False) -> str:
        """Messages of one chat. Default: the most recent `limit` (max 500) in chronological
        order. Page back with before=<before_cursor>, forward with after=<after_cursor>.
        from_start=True returns the earliest messages in the since/until range instead.
        since/until: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', unix seconds, or '7d'/'12h'.
        Each message: id, time, sender ("我" = the user), kind (omitted for text), text."""
        a = dict(chat=chat, since=since, until=until, limit=limit, before=before, after=after,
                 from_start=from_start, newest_first=newest_first)
        return run("get_messages", a, lambda: data.get_messages(**a))

    @srv.tool(annotations=ro, structured_output=False)
    def search_messages(query: str, chat: Optional[str] = None, since: Optional[str] = None,
                        until: Optional[str] = None, sender: Optional[str] = None,
                        limit: int = 50) -> str:
        """Full-text search over all chats (or one chat), newest first. Space-separated
        terms are ANDed; substring match, works for Chinese. sender: substring of the
        sender's display name. Returns chat, chat_username, id, time, sender, snippet;
        pass chat_username + id to get_message_context for the surrounding conversation."""
        a = dict(query=query, chat=chat, since=since, until=until, sender=sender, limit=limit)
        return run("search_messages", a, lambda: data.search_messages(**a))

    @srv.tool(annotations=ro, structured_output=False)
    def get_message_context(chat: str, message_id: Optional[str] = None,
                            time: Optional[str] = None, before: int = 10, after: int = 10) -> str:
        """Messages around one message (by id, e.g. from search_messages) or around a time.
        The anchor message is marked "anchor": true. before/after max 100."""
        a = dict(chat=chat, message_id=message_id, time=time, before=before, after=after)
        return run("get_message_context", a, lambda: data.get_message_context(**a))

    @srv.tool(annotations=ro, structured_output=False)
    def get_contact(name: str) -> str:
        """Contact or group details: username, remark, nickname, alias, 1-on-1 chat stats,
        and shared groups (for a person) or members (for a group)."""
        a = dict(name=name)
        return run("get_contact", a, lambda: data.get_contact(**a))

    @srv.tool(annotations=ro, structured_output=False)
    def get_image(chat: str, message_id: str):
        """Decode and return the picture of an image message (kind "image")."""
        a = dict(chat=chat, message_id=message_id)
        res = call_tool(data, log, "get_image", a, lambda: data.get_image(**a))
        if isinstance(res, tuple):
            img, ext, meta = res
            return [Image(data=img, format={"jpg": "jpeg"}.get(ext, ext)), dumps(meta)]
        return dumps(res)

    @srv.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                          idempotentHint=True, openWorldHint=False),
              structured_output=False)
    def refresh(all_dbs: bool = False) -> str:
        """Re-decrypt WeChat databases changed since the last decrypt (no sudo; uses saved
        keys). Runs automatically every few minutes anyway. all_dbs=True also refreshes
        databases this server does not read (slow)."""
        a = dict(all_dbs=all_dbs)
        return run("refresh", a, lambda: data.refresh(**a))

    return srv


def load_server_config():
    from config import load_config
    with contextlib.redirect_stdout(sys.stderr):
        try:
            return load_config()
        except SystemExit:
            raise SystemExit("config.json is not set up; run ./wechat decrypt once in a terminal")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = load_server_config()
    data = WeChatData(cfg)
    log_path = cfg.get("mcp_access_log") or os.path.join(ROOT, "logs", "mcp_access.jsonl")
    if not os.path.isabs(log_path):
        log_path = os.path.join(ROOT, log_path)
    log = AccessLog(log_path)
    if "--selftest" in argv:
        with contextlib.redirect_stdout(sys.stderr):
            t = time.time()
            print("refresh:", {k: (len(v) if isinstance(v, list) else v)
                               for k, v in data.refresh().items()}, file=sys.stderr)
            print("chats:", len(data.visible_chats()), file=sys.stderr)
            print("index:", data.sync_index(), file=sys.stderr)
            print(f"total {time.time() - t:.1f}s", file=sys.stderr)
        return
    def warm():  # refresh + build/update the index in the background
        try:
            with data.lock, contextlib.redirect_stdout(sys.stderr):
                data.maybe_auto_refresh()
                st = data.sync_index()
            print(f"[mcp] index ready: {st}", file=sys.stderr)
        except Exception:
            traceback.print_exc(file=sys.stderr)

    @contextlib.asynccontextmanager
    async def lifespan(_server):
        # Started only once the stdio transport owns fd 1, so the stdout
        # redirection inside warm() cannot race the transport's setup.
        threading.Thread(target=warm, daemon=True).start()
        yield {}

    build_server(data, log, lifespan=lifespan).run("stdio")


if __name__ == "__main__":
    main()
