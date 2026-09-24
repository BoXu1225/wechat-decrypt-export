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

Tools (all read-only; results are compact text lines -- one per message/chat/hit --
with times in local time; errors are JSON):
    list_chats(filter="", type="all"|"single"|"group", limit=50)
    get_messages(chat, since=None, until=None, limit=200, before=None,
                 after=None, from_start=False, newest_first=False)
    search_messages(query, chat=None, since=None, until=None, sender=None, limit=50)
    get_message_context(chat, message_id=None, time=None, before=10, after=10)
    get_contact(name)
    get_image(chat, message_id)
    get_voice(chat, message_id)
    refresh(all_dbs=False)
    get_moments(author=None, since=None, until=None, limit=50, query=None)
    search_favorites(query=None, type=None, since=None, until=None, limit=50)
    get_favorite(id)

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
    "mcp_output": "json"                    return JSON objects instead of text lines
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
from config import chmod_private, private_opener, secure_outputs

ROOT = os.path.dirname(os.path.abspath(__file__))

MAX_LIMIT = 500          # hard cap for any list result
MAX_CONTEXT = 100        # hard cap for before/after in get_message_context
MAX_TEXT = 4000          # per-message text cap in results
MAX_CANDIDATES = 20
INDEX_VERSION = "2"  # 2: rich message summaries (msg_parse)
PLACEHOLDER_KINDS = ("image", "voice", "video", "emoji")
# Databases the server reads; auto-refresh only re-decrypts these.
_WANTED_DB_RE = re.compile(r"^(message/message_\d+\.db|message/message_resource\.db|contact/contact\.db)$")
# Voice blobs; large, so only refreshed on demand by get_voice.
_MEDIA_DB_RE = re.compile(r"^message/media_\d+\.db$")


class ToolError(Exception):
    """An anticipated failure reported to the client as {"error": ...}."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.payload = {"error": message, **extra}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

iso = C.iso
db_number = C.db_number


def parse_time(value, end=False, now=None):
    """chats.parse_time, reporting unparseable input as a ToolError."""
    try:
        return C.parse_time(value, end=end, now=now)
    except ValueError as e:
        raise ToolError(str(e)) from None


def clamp(n, lo, hi):
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = hi
    return max(lo, min(hi, n))


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
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
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
        for suffix in ("", "-wal", "-shm"):  # holds message text: owner-only
            chmod_private(self.path + suffix)
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
        return self._cached("contact_rows", lambda: C.load_contact_rows(self.decrypted_dir))

    def room_members(self):
        return self._cached("rooms", lambda: C.load_room_members(self.decrypted_dir))

    def group_nicknames(self):
        return self._cached("group_nicks",
                            lambda: C.group_nicknames_from_members(self.room_members()))

    def all_chats(self):
        """chats.list_chats output with synthesized names for unnamed groups."""
        def build():
            out = C.list_chats(self.decrypted_dir, contacts=self.contacts())
            C.name_unnamed_groups(out, self.room_members(), self.contacts(), self.self_wxid)
            rows = self.contact_rows()
            for c in out:
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

    def _message_row(self, chat, message_id, cols):
        """-> (chat dict, db number, local_id, row of cols) for a "N:local_id" id."""
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
            row = conn.execute(f"SELECT {cols} FROM [{table}] WHERE local_id=?",
                               (lid,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise ToolError(f"message {message_id} not found in this chat")
        return c, n, lid, row

    def get_image(self, chat, message_id):
        """Returns (image_bytes, fmt, meta dict)."""
        import image_decode as I
        c, n, lid, row = self._message_row(chat, message_id,
                                           "local_type, create_time, packed_info_data")
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

    # -- voice --------------------------------------------------------------
    def get_voice(self, chat, message_id):
        """Returns (audio_bytes, fmt ("mp4" = AAC in .m4a, or "wav"), meta dict)."""
        import media_decode as M
        c, n, lid, row = self._message_row(
            chat, message_id,
            "local_type, create_time, server_id, message_content, WCDB_CT_message_content")
        local_type, create_time, server_id, content, ct = row
        if (local_type & 0xFFFFFFFF) != 34:
            raise ToolError(f"message {message_id} is not a voice message (kind "
                            f"{C.message_kind(local_type, None)})")
        with contextlib.suppress(ToolError):
            self.refresh(only=_MEDIA_DB_RE)  # voice blobs live in media_N.db
        with M.VoiceStore(self.decrypted_dir) as store:
            blob = store.get(c["username"], server_id, create_time, lid)
        if blob is None:
            raise ToolError("voice data not found (not downloaded in WeChat, or "
                            "message/media_*.db not decrypted yet)")
        with tempfile.TemporaryDirectory() as td:
            try:
                path, secs = M.voice_to_file(blob, os.path.join(td, "voice"))
            except M.MediaDecodeError as e:
                raise ToolError(f"could not decode voice: {e}")
            with open(path, "rb") as f:
                data = f.read()
        stated = C.media_duration(C.decompress_if_needed(content, ct), "voice")
        fmt = "mp4" if path.endswith(".m4a") else "wav"
        meta = {"chat": c["name"], "id": f"{n}:{lid}", "time": iso(create_time),
                "duration": M.seconds(stated if stated is not None else secs),
                "format": "m4a" if fmt == "mp4" else "wav", "bytes": len(data)}
        return data, fmt, meta

    # -- refresh ------------------------------------------------------------
    def _load_decryptor(self):
        if self._decryptor is None:
            with contextlib.redirect_stdout(sys.stderr):
                import decrypt_db
            self._decryptor = decrypt_db
        return self._decryptor

    def refresh(self, all_dbs=False, only=None):
        """Re-decrypt changed databases with existing keys. Never runs sudo.
        only: regex of relative DB paths to limit the work to (overrides all_dbs)."""
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
                if only is not None:
                    if not only.match(rel):
                        continue
                elif not all_dbs and not _WANTED_DB_RE.match(rel):
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
        if only is None:
            self._last_refresh = time.time()
        if updated and only is None:
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
                os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", opener=private_opener) as f:
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
# Compact text rendering
# ---------------------------------------------------------------------------
# Tool results go into the agent's context, so the default output is plain
# lines rather than JSON: one line per message ("HH:MM sender: text") under a
# date line, the chat named once in a header. Messages with media carry their
# id ("#N:local_id") for get_image / get_voice. config.json
# "mcp_output": "json" restores the JSON objects the data layer returns.

MEDIA_KINDS = ("image", "voice", "video")


def _day(t):
    return (t or "")[:10]


def _hm(t):
    return (t or "")[11:16]


def _when(t):
    return (t or "")[:16].replace("T", " ")


def _oneline(text):
    return " ".join((text or "").split())


def _indent(text, pad="  "):
    return (text or "").replace("\r\n", "\n").replace("\n", "\n" + pad)


def _chat_ref(c):
    kind = "group" if c.get("is_group") else "1:1"
    return f"{c['name']} ({c['username']}, {kind})"


def _message_lines(msgs):
    out, day = [], None
    for m in msgs:
        if _day(m["time"]) != day:
            day = _day(m["time"])
            out.append(f"[{day}]")
        line = f"{_hm(m['time'])} {m['sender']}: {_indent(m.get('text') or '')}"
        if m.get("kind") in MEDIA_KINDS or m.get("anchor"):
            line += f" #{m['id']}"
        out.append(("> " if m.get("anchor") else "") + line)
    return out


def _render_list_chats(r):
    head = f"{r['count']} of {r['total']} chats, most recent first (name | username | type | messages | last)"
    return [head] + [
        f"{c['name']} | {c['username']} | {'group' if c['is_group'] else '1:1'} | "
        f"{c['msg_count']} | {_when(c['last_time'])}" for c in r["chats"]]


def _render_messages(r):
    head = f"{_chat_ref(r['chat'])}: {r['count']} messages"
    more = []
    if r.get("has_more_before"):
        more.append(f"older: before=\"{r['before_cursor']}\"")
    if r.get("has_more_after"):
        more.append(f"newer: after=\"{r['after_cursor']}\"")
    if more:
        head += "; " + ", ".join(more)
    return [head] + _message_lines(r["messages"])


def _render_context(r):
    return [f"{_chat_ref(r['chat'])}: {r['count']} messages around #{r['anchor_id']} (marked >)"] \
        + _message_lines(r["messages"])


def _render_search(r):
    res = r["results"]
    head = f"search {r['query']!r}: {r['count']} hits, newest first"
    if r.get("has_more"):
        head += " (more: narrow with chat/since/until or raise limit)"
    out = [head]
    if not res:
        return out
    out.append("context: get_message_context(chat=<username>, message_id=<#id>)")
    by_chat = {}
    for x in res:
        by_chat.setdefault((x["chat"], x["chat_username"]), []).append(x)
    for (name, user), hits in by_chat.items():
        out.append(f"## {name} ({user})")
        out += [f"{_when(x['time'])} #{x['id']} {x['sender']}: {_oneline(x['snippet'])}"
                for x in hits]
    return out


def _render_contact(r):
    ident = [f"{r['name']} ({r['username']})", "group" if r["is_group"] else
             ("friend" if r.get("is_friend") else "not a friend")]
    ident += [f"{k}: {r[k]}" for k in ("remark", "nickname", "alias") if r.get(k)]
    out = [" | ".join(ident)]
    c = r.get("chat")
    if c:
        out.append(f"chat: {c['msg_count']} messages ({c['sent_by_me']} by me, "
                   f"{c['sent_by_them']} by them), {_day(c['first_time'])} to {_day(c['last_time'])}")
    if r["is_group"]:
        out.append(f"members ({r['member_count']}): " + ", ".join(
            f"{m['name']} ({m['username']})" for m in r["members"]))
    elif r.get("shared_groups") is not None:
        out.append(f"shared groups ({r['shared_group_count']}; name | username | members | "
                   "their nickname | messages | last):")
        out += [f"  {g['name']} | {g['username']} | {g['member_count']} | "
                f"{g['their_group_nickname'] or '-'} | {g['msg_count']} | {_when(g['last_time'])}"
                for g in r["shared_groups"]]
    return out


def _render_moments(r):
    head = f"{r['count']} of {r['total']} Moments posts, newest first"
    if r.get("has_more"):
        head += " (more: narrow with since/until or raise limit)"
    out = [head] + ([r["note"]] if r.get("note") else [])
    for p in r["posts"]:
        meta = [f"#{p['id']}", _when(p["time"]), f"{p['author']} ({p['author_username']})"]
        if p.get("kind"):
            meta.append(p["kind"])
        m = p.get("media")
        if m:
            parts = [f"{m[k]} {k}" for k in ("images", "videos") if m[k]]
            meta.append(", ".join(parts) + f" ({m['cached_locally']} cached)")
        if p.get("location"):
            meta.append(f"at {p['location']}")
        if p.get("private"):
            meta.append("private")
        out.append(" | ".join(meta))
        if p.get("text"):
            out.append("  " + _indent(p["text"], "  "))
        link = " - ".join(p[k] for k in ("title", "description", "source") if p.get(k))
        if link or p.get("url"):
            out.append("  link: " + " ".join(x for x in (_oneline(link), p.get("url")) if x))
        if p.get("with"):
            out.append("  with: " + ", ".join(p["with"]))
        if p.get("likes"):
            out.append("  likes: " + ", ".join(p["likes"]))
        for c in p.get("comments") or []:
            who = c["name"] + (f" -> {c['reply_to']}" if c.get("reply_to") else "")
            out.append(f"  {_when(c['time'])} {who}: {_oneline(c['text'])}")
        if p.get("comments_truncated"):
            out.append(f"  (+{p['comments_truncated']} more comments)")
    return out


def _fav_source_text(s):
    if not s:
        return ""
    parts = [s.get("sender_name"), s.get("chat_name")]
    txt = " in ".join(x for x in parts if x)
    if s.get("chat_username"):
        txt += f" ({s['chat_username']})"
    return txt


def _render_favorites(r):
    head = f"{r['count']} of {r['total']} favorites, newest first"
    if r.get("has_more"):
        head += " (more: narrow the query or raise limit)"
    out = [head]
    if r["favorites"]:
        out.append("full item: get_favorite(id)")
    for f in r["favorites"]:
        parts = [f"#{f['id']}", _when(f["time"]), f["type"]]
        for k in ("title", "snippet", "url"):
            if f.get(k):
                parts.append(_oneline(f[k]))
        if f.get("source"):
            parts.append("from " + _fav_source_text(f["source"]))
        if f.get("tags"):
            parts.append("tags: " + ", ".join(f["tags"]))
        if f.get("item_count"):
            parts.append(f"{f['item_count']} items")
        out.append(" | ".join(parts))
    return out


def _render_favorite(r):
    parts = [f"#{r['id']}", _when(r["time"]), r["type"]]
    if r.get("source"):
        parts.append("from " + _fav_source_text(r["source"]))
    if r.get("tags"):
        parts.append("tags: " + ", ".join(r["tags"]))
    out = [" | ".join(parts)]
    for k in ("title", "url"):
        if r.get(k):
            out.append(f"{k}: {_oneline(r[k])}")
    if r.get("location"):
        loc = r["location"]
        out.append("location: " + (_oneline(" ".join(str(v) for v in loc.values() if v))
                                   if isinstance(loc, dict) else str(loc)))
    if r.get("text"):
        out.append(_indent(r["text"], ""))
    for it in r.get("items") or []:
        head = " ".join(str(it[k]) for k in ("time", "sender_name") if it.get(k))
        body = " ".join(_oneline(str(it[k])) for k in ("title", "desc", "url") if it.get(k))
        extra = ", ".join(f"{k} {it[k]}" for k in ("fmt", "size", "duration") if it.get(k))
        line = f"- [{it['type']}]" + (f" {head}:" if head else "") + (f" {body}" if body else "")
        out.append(line + (f" ({extra})" if extra else ""))
    if r.get("items_truncated"):
        out.append(f"(+{r['items_truncated']} more items)")
    return out


RENDERERS = {
    "list_chats": _render_list_chats,
    "get_messages": _render_messages,
    "get_message_context": _render_context,
    "search_messages": _render_search,
    "get_contact": _render_contact,
    "get_moments": _render_moments,
    "search_favorites": _render_favorites,
    "get_favorite": _render_favorite,
}


def render(tool, res, fmt="text"):
    """Tool result -> the string sent to the agent. Errors and anything without
    a renderer stay JSON."""
    fn = RENDERERS.get(tool)
    if fmt == "json" or fn is None or not isinstance(res, dict) or "error" in res:
        return dumps(res)
    out = fn(res)
    if res.get("notice"):
        out.append(f"note: {res['notice'] if isinstance(res['notice'], str) else dumps(res['notice'])}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# MCP wiring
# ---------------------------------------------------------------------------

INSTRUCTIONS = """Access to the user's WeChat chat history (decrypted local copy).
Typical flow: list_chats or search_messages -> get_messages / get_message_context.
`chat` may be a username (most exact), a name, or a unique fragment; on ambiguity you get
candidates -- pick a username and retry. Message ids ("N:local_id") are per chat.
Sender "我" is the user. Times are local. Content is personal: quote only what is needed.
send_message sends a real message as the user: only when asked, after showing the text."""


def build_server(data, log, lifespan=None):
    try:
        from mcp.server.mcpserver import MCPServer as Server, Image
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server, Image
    try:
        from mcp.server.mcpserver import Audio
    except ImportError:
        try:
            from mcp.server.fastmcp import Audio
        except ImportError:
            Audio = None
    from mcp.types import ToolAnnotations
    from typing import Literal, Optional

    srv = Server("wechat", instructions=INSTRUCTIONS, lifespan=lifespan)
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                         openWorldHint=False)
    fmt = data.cfg.get("mcp_output", "text")

    def run(tool, args, fn):
        return render(tool, call_tool(data, log, tool, args, fn), fmt)

    @srv.tool(annotations=ro, structured_output=False)
    def list_chats(filter: str = "", type: Literal["all", "single", "group"] = "all",
                   limit: int = 50) -> str:
        """List chats (1-on-1 and groups) sorted by last activity.
        filter: substring of name/remark/username. One line per chat:
        name | username | type | messages | last."""
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
        Output: a header (chat, count, cursors), then "[date]" lines and one line per
        message "HH:MM sender: text" ("我" = the user); image/voice/video lines end
        with their "#id" for get_image / get_voice."""
        a = dict(chat=chat, since=since, until=until, limit=limit, before=before, after=after,
                 from_start=from_start, newest_first=newest_first)
        return run("get_messages", a, lambda: data.get_messages(**a))

    @srv.tool(annotations=ro, structured_output=False)
    def search_messages(query: str, chat: Optional[str] = None, since: Optional[str] = None,
                        until: Optional[str] = None, sender: Optional[str] = None,
                        limit: int = 50) -> str:
        """Full-text search over all chats (or one chat), newest first. Space-separated
        terms are ANDed; substring match, works for Chinese. sender: substring of the
        sender's display name. Hits grouped under "## chat (username)", one line each:
        "date time #id sender: snippet"; pass username + id to get_message_context."""
        a = dict(query=query, chat=chat, since=since, until=until, sender=sender, limit=limit)
        return run("search_messages", a, lambda: data.search_messages(**a))

    @srv.tool(annotations=ro, structured_output=False)
    def get_message_context(chat: str, message_id: Optional[str] = None,
                            time: Optional[str] = None, before: int = 10, after: int = 10) -> str:
        """Messages around one message (by id, e.g. from search_messages) or around a time.
        Same line format as get_messages; the anchor line starts with "> ".
        before/after max 100."""
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

    @srv.tool(annotations=ro, structured_output=False)
    def get_voice(chat: str, message_id: str):
        """Decode and return the audio of a voice message (kind "voice") as AAC/m4a,
        plus its duration in seconds. No transcript."""
        a = dict(chat=chat, message_id=message_id)
        res = call_tool(data, log, "get_voice", a, lambda: data.get_voice(**a))
        if isinstance(res, tuple):
            audio, fmt, meta = res
            if Audio is None:  # SDK without audio content support
                return dumps(dict(meta, error="this MCP SDK cannot return audio"))
            return [Audio(data=audio, format=fmt), dumps(meta)]
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

    register_social_tools(srv, data, log, ro)
    if data.cfg.get("mcp_send", True):
        register_send_tool(srv, data, log)
    return srv


def _send_target_hidden(data, chat):
    """True if `chat` (exact username or name) names a chat hidden by the
    blocklist / allowlist. Sending never reaches chats agents can't read."""
    q = (chat or "").strip()
    for c in data.all_chats():
        if q == c["username"] or q in c["_names"]:
            if not data.is_visible(c["username"], c["_names"]):
                return True
    return False


def send_chat_message(data, chat, text, dry_run=False, driver=None):
    import wechat_send as WS
    if _send_target_hidden(data, chat):
        raise ToolError(f"chat {chat!r} is not accessible (mcp_blocklist / mcp_allowlist)")
    return WS.send_message_tool(chat, text, dry_run=dry_run, cfg=data.cfg, driver=driver)


def register_send_tool(srv, data, log):
    from mcp.types import ToolAnnotations

    @srv.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                          idempotentHint=False, openWorldHint=True),
              structured_output=False)
    def send_message(chat: str, text: str, dry_run: bool = False) -> str:
        """SEND a WeChat text message as the user -- a real message another person
        will read. Only call this when the user has asked for this exact message to
        this exact chat; show them the text and recipient first.
        chat: exact username (from list_chats) or exact display name; fuzzy names are
        refused with candidates. text: plain text (newlines ok, no files/images).
        dry_run=True opens the chat, types and clears the text without sending.
        Drives the WeChat app on this Mac (it must be running and unlocked; takes
        ~10 s) and verifies the message landed in the chat. It first waits (up to
        30 s) until the user stops using the keyboard/mouse, and shows an on-screen
        "hands off" banner while it works. Returns status "sent" | "unverified" |
        "dry_run", or "failed" with error and message. error "user_busy" (never
        started) or "user_activity" (the user used the Mac mid-send; nothing was
        sent): tell the user and offer to retry when they are away from the
        keyboard; if "draft_left" is set, the text is still typed in that chat."""
        import hashlib
        a = dict(chat=chat, text_sha256_16=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                 text_len=len(text), dry_run=dry_run)  # the log never holds the text
        return dumps(call_tool(data, log, "send_message", a,
                               lambda: send_chat_message(data, chat, text, dry_run)))


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
    secure_outputs(cfg)  # umask 077 + tighten existing outputs (messages go to stderr)
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


# ---------------------------------------------------------------------------
# Moments (朋友圈) and Favorites (收藏) tools
# ---------------------------------------------------------------------------
# Kept in one appended block (logic lives in moments.py / favorites.py).
# Access policy: a Moments post is visible iff its author is visible (the
# user's own posts unless they are blocklisted by name/username); likes and
# comments on a visible post stay visible, like group messages. A favorite is
# hidden if any chat/sender it came from (other than the user) is hidden.

MAX_COMMENTS = 100       # per post in get_moments
MAX_ITEMS = 200          # dataitems per favorite in get_favorite

# Auto-refresh also re-decrypts the Moments and Favorites databases.
_WANTED_DB_RE = re.compile(f"(?:{_WANTED_DB_RE.pattern})"
                           r"|^(?:sns/sns\.db|favorite/favorite\.db)$")


def _file_sig(path):
    try:
        st = os.stat(path)
        return st.st_mtime, st.st_size
    except FileNotFoundError:
        return None


def _social_cached(data, key, db_rel, build):
    """Per-WeChatData cache invalidated by the DB file or contacts changing."""
    cache = data.__dict__.setdefault("_social_cache", {})
    sig = (_file_sig(os.path.join(data.decrypted_dir, db_rel)), data._data_sig()[:1])
    hit = cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    val = build()
    cache[key] = (sig, val)
    return val


def _self_visible(data):
    if not data.blocklist:
        return True
    r = data.contact_rows().get(data.self_wxid, {})
    names = {data.self_wxid, r.get("display"), r.get("remark"), r.get("nick_name"),
             r.get("alias")} - {"", None}
    return not (names & data.blocklist)


def _user_visible(data, username):
    if username and username == data.self_wxid:
        return _self_visible(data)
    return data.is_visible(username)


def _moment_posts(data):
    import moments as MO

    def build():
        posts = MO.load_posts(data.decrypted_dir)
        MO.resolve_names(posts, data.contacts(), data.self_wxid)
        return posts
    return _social_cached(data, "moments", MO.SNS_DB, build)


def _moment_cache(data):
    import moments as MO
    mc = data.__dict__.get("_moment_media")
    if mc is None:
        mc = data.__dict__["_moment_media"] = MO.MediaCache(data.cfg.get("wechat_base_dir"))
    return mc


def _resolve_moment_author(data, query, posts):
    import moments as MO
    q = str(query).strip()
    if data.self_wxid and q.lower() in MO.SELF_ALIASES:
        if not _self_visible(data):
            raise ToolError(f"no Moments author matches {q!r}")
        return [data.self_wxid]
    visible = [p for p in posts if _user_visible(data, p["username"])]
    names = MO.author_names(visible, data.contact_rows())
    hits = [u for u, s in names.items() if q in s] or \
        [u for u, s in names.items() if q.lower() in {x.lower() for x in s}] or \
        [u for u, s in names.items() if any(q.lower() in x.lower() for x in s)]
    if not hits:
        raise ToolError(f"no Moments author matches {q!r}",
                        hint="only people whose posts WeChat has cached locally have Moments")
    if len(hits) > 1:
        counts = {}
        for p in visible:
            counts[p["username"]] = counts.get(p["username"], 0) + 1
        first = {p["username"]: p["author"] for p in reversed(visible)}
        raise ToolError("ambiguous", query=q, candidates=[
            {"name": first.get(u, u), "username": u, "posts": counts.get(u, 0)}
            for u in sorted(hits, key=lambda u: -counts.get(u, 0))[:MAX_CANDIDATES]],
            hint="call again with one candidate's username")
    return hits


def _moment_out(p, cache):
    d = {"id": p["id"], "time": iso(p["ts"]), "author": p["author"],
         "author_username": p["username"]}
    if p["kind"] != "image":
        d["kind"] = p["kind"]
    if p["text"]:
        d["text"] = _cap_text(p["text"])
    for k in ("title", "description", "url", "source"):
        if p.get(k):
            d[k] = _cap_text(p[k])
    if p.get("private"):
        d["private"] = True
    if p["location"]:
        d["location"] = p["location"]["name"] or p["location"]["address"]
    media = [m for m in p["media"] if m["type"] in ("image", "video")]
    if media:
        cached = sum(1 for m in media if any(cache.lookup(p["id"], m["id"]).values()))
        d["media"] = {"images": sum(m["type"] == "image" for m in media),
                      "videos": sum(m["type"] == "video" for m in media),
                      "cached_locally": cached}
    if p.get("with_names"):
        d["with"] = p["with_names"]
    if p["likes"]:
        d["likes"] = [x["name"] for x in p["likes"]]
    if p["comments"]:
        cs = []
        for c in p["comments"][:MAX_COMMENTS]:
            x = {"name": c["name"], "time": iso(c["ts"]), "text": _cap_text(c["text"])}
            if c.get("reply_to"):
                x["reply_to"] = c.get("reply_to_name") or c["reply_to"]
            cs.append(x)
        d["comments"] = cs
        if len(p["comments"]) > MAX_COMMENTS:
            d["comments_truncated"] = len(p["comments"]) - MAX_COMMENTS
    return d


def moments_query(data, author=None, since=None, until=None, limit=50, query=None):
    import moments as MO
    limit = clamp(limit, 1, MAX_LIMIT)
    posts = _moment_posts(data)
    t_since, t_until = parse_time(since), parse_time(until, end=True)
    authors = _resolve_moment_author(data, author, posts) if author else None
    hits = [p for p in MO.filter_posts(posts, authors, query, t_since,
                                       t_until + 1 if t_until is not None else None)
            if _user_visible(data, p["username"])]
    cache = _moment_cache(data)
    out = {"total": len(hits), "count": min(limit, len(hits)), "has_more": len(hits) > limit,
           "posts": [_moment_out(p, cache) for p in hits[:limit]]}
    if not posts:
        out["note"] = "no Moments in the local database (sns/sns.db missing or empty)"
    return out


def _favorites(data):
    import favorites as FV

    def build():
        favs = FV.load_favorites(data.decrypted_dir)
        FV.resolve_names(favs, data.contacts(), data.self_wxid)
        return favs
    return _social_cached(data, "favorites", FV.FAV_DB, build)


def _fav_visible(data, f):
    import favorites as FV
    return all(_user_visible(data, u) for u in FV.related_usernames(f) if u != data.self_wxid)


def _fav_source(f):
    s = f["source"]
    d = {k: s[k] for k in ("sender_name", "chat_name") if s.get(k)}
    if s.get("from"):
        d["chat_username"] = s["from"]
    return d


def favorites_search(data, query=None, type=None, since=None, until=None, limit=50):
    import favorites as FV
    limit = clamp(limit, 1, MAX_LIMIT)
    if type not in (None, "") and not str(type).isdigit() and type not in FV.KIND_LABEL:
        raise ToolError(f"unknown type {type!r}", valid=sorted(FV.KIND_LABEL))
    t_since, t_until = parse_time(since), parse_time(until, end=True)
    favs = [f for f in FV.search(_favorites(data), query, type, t_since,
                                 t_until + 1 if t_until is not None else None)
            if _fav_visible(data, f)]
    terms = (query or "").split()
    res = []
    for f in favs[:limit]:
        d = {"id": f["id"], "time": iso(f["ts"]), "type": f["kind"]}
        if f["title"]:
            d["title"] = _cap_text(f["title"])
        text = f["desc"] or FV.summary_text(f, 10_000)
        if text and text != f["title"]:
            d["snippet"] = _snippet(text, terms) if terms else _snippet(text, [], 100)
        if f["url"]:
            d["url"] = f["url"]
        src = _fav_source(f)
        if src:
            d["source"] = src
        if f["tags"]:
            d["tags"] = f["tags"]
        if len(f["items"]) > 1:
            d["item_count"] = len(f["items"])
        res.append(d)
    return {"total": len(favs), "count": len(res), "has_more": len(favs) > limit,
            "favorites": res, "hint": "get_favorite(id) for the full item (e.g. chat records)"}


def favorite_get(data, id):
    try:
        fid = int(str(id).strip())
    except (TypeError, ValueError):
        raise ToolError(f"bad favorite id {id!r}; expected an integer from search_favorites")
    f = next((x for x in _favorites(data) if x["id"] == fid), None)
    if f is None or not _fav_visible(data, f):
        raise ToolError(f"favorite {fid} not found")
    d = {"id": f["id"], "time": iso(f["ts"]), "type": f["kind"], "title": f["title"] or None,
         "text": _cap_text(f["desc"]) or None, "url": f["url"] or None,
         "source": _fav_source(f) or None, "tags": f["tags"] or None,
         "location": f["location"]}
    items = []
    for it in f["items"][:MAX_ITEMS]:
        x = {"type": it["kind"]}
        for k in ("title", "desc", "sender_name", "time", "url", "fmt", "size", "duration"):
            if it.get(k):
                x[k] = _cap_text(it[k]) if isinstance(it[k], str) else it[k]
        items.append(x)
    d["items"] = items
    if len(f["items"]) > MAX_ITEMS:
        d["items_truncated"] = len(f["items"]) - MAX_ITEMS
    return {k: v for k, v in d.items() if v is not None}


def register_social_tools(srv, data, log, ro):
    from typing import Optional
    fmt = data.cfg.get("mcp_output", "text")

    def run(tool, args, fn):
        return render(tool, call_tool(data, log, tool, args, fn), fmt)

    @srv.tool(annotations=ro, structured_output=False)
    def get_moments(author: Optional[str] = None, since: Optional[str] = None,
                    until: Optional[str] = None, limit: int = 50,
                    query: Optional[str] = None) -> str:
        """Moments (朋友圈) posts cached locally by WeChat, newest first: the user's own
        and friends' posts the app has loaded. author: name/remark/username of the poster
        ("我" = the user); query: space-separated terms (all must match) over post text,
        link title, location and comments. Each post: id, time, author, kind (omitted for
        photo posts), text, title/url for shared links, location, media counts, likes
        (names) and comments (time, name -> reply_to: text). limit max 500."""
        a = dict(author=author, since=since, until=until, limit=limit, query=query)
        return run("get_moments", a, lambda: moments_query(data, **a))

    @srv.tool(annotations=ro, structured_output=False)
    def search_favorites(query: Optional[str] = None, type: Optional[str] = None,
                         since: Optional[str] = None, until: Optional[str] = None,
                         limit: int = 50) -> str:
        """Search the user's WeChat Favorites (收藏), newest first. query: space-separated
        terms (substring, all must match) over title, text, URL, tags, source names and
        chat-record contents; omit to list. type: text, image, voice, video, link, location,
        music, file, chat_record, note, miniprogram, channels, product, other.
        One line each: #id | time | type | title | snippet | url | from | tags."""
        a = dict(query=query, type=type, since=since, until=until, limit=limit)
        return run("search_favorites", a, lambda: favorites_search(data, **a))

    @srv.tool(annotations=ro, structured_output=False)
    def get_favorite(id: int) -> str:
        """One favorite in full by id (from search_favorites), including every entry of a
        saved chat record or note (sender, time, text)."""
        a = dict(id=id)
        return run("get_favorite", a, lambda: favorite_get(data, **a))


if __name__ == "__main__":
    main()
