"""
Send WeChat text messages by driving the WeChat macOS app's UI (Accessibility).

No injection, hooking or memory writes: this only does what a user could do by
hand -- focus WeChat, search for the chat, paste the text, press Enter.

    from wechat_send import send_text
    send_text("文件传输助手", "hello")            # -> {"status": "sent", ...}
    send_text("filehelper", "hi", dry_run=True)   # everything except Enter

CLI:
    python wechat_send.py --to <chat> --text "..." [--dry-run] [--yes]
    python wechat_send.py --check          # permission / WeChat state
    python wechat_send.py --probe          # dump WeChat's AX tree (text redacted)

Drivers ("send_driver" in config.json)
  - "helper" (default): WeChatSendHelper.app, a small Swift LaunchAgent that
    performs the UI primitives and is the only thing that needs the macOS
    Accessibility permission (install: helper/install.sh). Works from SSH,
    MCP servers, cron, etc. Socket: ~/Library/Application Support/
    wechat-decrypt-export/sendhelper.sock (0600, same-uid peers only).
  - "direct": pyobjc in this process; needs Accessibility for whatever runs
    python (terminal app; over SSH /usr/libexec/sshd-keygen-wrapper) and
    pyobjc-framework-ApplicationServices / -Quartz / -Cocoa.

Safety model
  - The chat must resolve to exactly one contact/group (exact username or exact
    display name). Ambiguous or fuzzy-only matches raise with candidates.
  - Before Enter, the opened chat's title (read via AX) must equal the
    expected name and the input box must hold exactly our text; otherwise the
    input is cleared and nothing is sent.
  - Rate limit + JSONL send log (no message text, only length and a hash).

Config (config.json, all optional):
    "send_rate_limit": {"min_interval_s": 3, "max_per_minute": 6},
    "send_max_chars": 2000,
    "send_key": "enter"            # or "cmd_enter" if WeChat is set to Cmd+Enter
    "send_log": "logs/send_log.jsonl",
    "send_driver": "helper"        # or "direct"
"""
import argparse
import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

import chats as C

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"

DEFAULT_RATE_LIMIT = {"min_interval_s": 3.0, "max_per_minute": 6}
DEFAULT_MAX_CHARS = 2000
DEFAULT_LOG = os.path.join("logs", "send_log.jsonl")
VERIFY_TIMEOUT_S = 10.0

# How WeChat labels some system chats depending on UI language.
UI_ALIASES = {
    "filehelper": ("文件传输助手", "File Transfer"),
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SendError(Exception):
    """Base class. `.code` is a stable machine-readable string."""
    code = "send_error"

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra

    def to_dict(self):
        return {"status": "failed", "error": self.code, "message": str(self), **self.extra}


class ChatNotFound(SendError):
    code = "chat_not_found"


class AmbiguousChat(SendError):
    code = "ambiguous_chat"


class UnsupportedChat(SendError):
    code = "unsupported_chat"


class InvalidText(SendError):
    code = "invalid_text"


class RateLimited(SendError):
    code = "rate_limited"


class AccessibilityDenied(SendError):
    code = "accessibility_denied"


class WeChatNotRunning(SendError):
    code = "wechat_not_running"


class ScreenLocked(SendError):
    code = "screen_locked"


class HelperUnavailable(SendError):
    code = "helper_unavailable"


class UIError(SendError):
    """UI automation failed (element not found, wrong chat opened, ...)."""
    code = "ui_error"


# ---------------------------------------------------------------------------
# Chat resolution
# ---------------------------------------------------------------------------

def _candidate(username, name):
    return {"username": username, "name": name, "is_group": C.is_group(username)}


def resolve_chat(query, contacts, chat_list):
    """Resolve `query` to exactly one chat: {"username", "name", "is_group"}.

    Matches, in order: exact username; exact display name (remark > nickname,
    as WeChat shows it). Never guesses: no match or several matches raise with
    up to 10 candidates.
    """
    query = (query or "").strip()
    if not query:
        raise ChatNotFound("empty chat name")
    names = dict(contacts)
    for c in chat_list:
        names.setdefault(c["username"], c.get("name") or c["username"])

    if query in names:
        return _candidate(query, names[query])

    exact = sorted(u for u, n in names.items() if n == query)
    if len(exact) == 1:
        return _candidate(exact[0], names[exact[0]])
    if len(exact) > 1:
        raise AmbiguousChat(
            f"{len(exact)} chats are named {query!r}; pass the username instead",
            candidates=[_candidate(u, names[u]) for u in exact[:10]])

    rows = [{"username": u, "name": n} for u, n in names.items()]
    # Prefer chats with recent activity in the suggestion list.
    order = {c["username"]: i for i, c in enumerate(chat_list)}
    rows.sort(key=lambda r: order.get(r["username"], len(order)))
    fuzzy = C.find_chats(query, rows)[:10]
    raise ChatNotFound(
        f"no chat with exact name or username {query!r}",
        candidates=[_candidate(r["username"], r["name"]) for r in fuzzy])


def ui_names(chat):
    """Names WeChat may display for this chat (search query first)."""
    alias = UI_ALIASES.get(chat["username"])
    if alias:
        return list(alias)
    return [chat["name"]]


def title_matches(title, expected_names, is_group=False):
    """True when a chat header title shows one of the expected names.

    Groups may show a member count suffix: "Name (12)" / "Name(12)".
    """
    if not title:
        return False
    title = title.strip()
    for name in expected_names:
        if title == name:
            return True
        if is_group and re.fullmatch(re.escape(name) + r"\s*[(（]\d+[)）]", title):
            return True
    return False


# ---------------------------------------------------------------------------
# Text checks
# ---------------------------------------------------------------------------

def normalize_text(text):
    return "\n".join(line.rstrip() for line in
                     text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


def validate_text(text, max_chars):
    if not isinstance(text, str) or not text.strip():
        raise InvalidText("text is empty")
    if len(text) > max_chars:
        raise InvalidText(f"text is {len(text)} chars; limit is {max_chars}")
    if "\x00" in text:
        raise InvalidText("text contains NUL")


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Send log + rate limit
# ---------------------------------------------------------------------------

class SendLog:
    """Append-only JSONL log. Never stores the message text."""

    def __init__(self, path):
        self.path = path

    def append(self, entry):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent(self, since_ts):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ts", 0) >= since_ts:
                    out.append(e)
        return out

    @contextlib.contextmanager
    def lock(self):
        """Serialize sends across processes."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path + ".lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


# Entries that count against the rate limit: Enter was pressed.
_COUNTED = {"sent"}


def check_rate_limit(log, limits, now):
    min_interval = float(limits.get("min_interval_s", DEFAULT_RATE_LIMIT["min_interval_s"]))
    per_minute = int(limits.get("max_per_minute", DEFAULT_RATE_LIMIT["max_per_minute"]))
    recent = [e for e in log.recent(now - 60) if e.get("status") in _COUNTED]
    if recent:
        last = max(e["ts"] for e in recent)
        wait = min_interval - (now - last)
        if wait > 0:
            raise RateLimited(f"min interval {min_interval:g}s; retry in {wait:.1f}s",
                              retry_after=round(wait, 1))
    if len(recent) >= per_minute:
        oldest = min(e["ts"] for e in recent)
        raise RateLimited(f"max {per_minute} sends per minute reached",
                          retry_after=round(oldest + 60 - now, 1))


# ---------------------------------------------------------------------------
# Post-send verification (decrypted DB)
# ---------------------------------------------------------------------------

def _load_decrypt_db():
    with contextlib.redirect_stdout(sys.stderr):
        import decrypt_db
    return decrypt_db


def refresh_message_dbs(cfg, dd=None):
    """Re-decrypt changed message/message_N.db files with existing keys.

    Uses decrypt_db's incremental decrypt (merges committed -wal frames,
    tracks .db/-wal changes in decrypted/.decrypt_state.json). Never runs
    sudo; all output goes to stderr.
    Returns (ok, note). ok=False means keys are missing/stale or decryption
    failed; the caller should report the send as unverified.
    """
    dd = dd or _load_decrypt_db()
    with contextlib.redirect_stdout(sys.stderr):
        keys = dd.load_keys()
    if not keys:
        return False, "no keys file; run ./wechat decrypt to enable verification"
    db_dir = cfg["db_dir"]
    out_dir = cfg["decrypted_dir"]
    msg_dir = os.path.join(db_dir, "message")
    if not os.path.isdir(msg_dir):
        return False, "message directory not found"
    state = dd.load_state(out_dir)
    for f in sorted(os.listdir(msg_dir)):
        if not re.fullmatch(r"message_\d+\.db", f):
            continue
        rel = f"message/{f}"
        src = os.path.join(db_dir, rel)
        out = os.path.join(out_dir, rel)
        if dd.is_current(state, rel, src, out):
            continue
        if rel not in keys:
            continue
        with contextlib.redirect_stdout(sys.stderr):
            enc_key = bytes.fromhex(keys[rel]["enc_key"])
            with open(src, "rb") as fh:
                page1 = fh.read(dd.PAGE_SZ)
            if len(page1) < dd.PAGE_SZ or not dd.page1_hmac_ok(page1, enc_key):
                return False, f"key for {rel} is stale; run ./wechat decrypt"
            sig = dd.decrypt_database(src, out, enc_key)  # atomic, includes -wal
            if not sig:
                return False, f"decrypt failed for {rel}"
            dd.save_state(out_dir, {rel: sig})
    return True, None


def find_chat_tables(username, decrypted_dir):
    table = C.table_name_for(username)
    out = []
    for db_path in reversed(C.message_db_paths(decrypted_dir)):
        conn = sqlite3.connect(db_path)
        try:
            if C.table_exists(conn, table):
                out.append((db_path, table))
        except sqlite3.DatabaseError:
            pass
        finally:
            conn.close()
    return out


def find_sent_message(chat, text, since_ts, cfg, contacts):
    """Return the matching self-sent record, or None."""
    decrypted_dir = cfg["decrypted_dir"]
    tables = find_chat_tables(chat["username"], decrypted_dir)
    if not tables:
        return None
    target = normalize_text(text)
    one = {"username": chat["username"], "name": chat["name"],
           "is_group": chat["is_group"], "tables": tables}
    found = None
    for rec in C.iter_messages(one, decrypted_dir, cfg.get("self_wxid"), contacts,
                               group_nicknames={}):
        if (rec["is_self"] and rec["kind"] == "text" and rec["ts"] >= since_ts
                and normalize_text(rec["text"] or "") == target):
            found = rec
    return found


def verify_sent(chat, text, since_ts, cfg, contacts, timeout=VERIFY_TIMEOUT_S,
                refresh=refresh_message_dbs, finder=find_sent_message,
                sleep=time.sleep, clock=time.time):
    """Poll the decrypted DB until the message shows up. Returns (status, note)."""
    deadline = clock() + timeout
    while True:
        try:
            ok, note = refresh(cfg)
        except Exception as e:  # never let verification break a completed send
            return "unverified", f"refresh failed: {type(e).__name__}"
        if not ok:
            return "unverified", note
        try:
            if finder(chat, text, since_ts, cfg, contacts):
                return "sent", None
        except sqlite3.DatabaseError as e:
            note = f"db read failed: {type(e).__name__}"
        if clock() >= deadline:
            return "unverified", "message not found in local DB within %gs" % timeout
        sleep(1.0)


# ---------------------------------------------------------------------------
# UI driver interface
# ---------------------------------------------------------------------------

def choose_result(results, expected_names):
    """Pick the search result to click: the top-most row whose text is exactly
    one of the expected names. None if there is no exact match."""
    names = {n.strip() for n in expected_names}
    best = None
    for r in results or []:
        t = (r.get("text") or "").strip()
        if t in names and (best is None or (r.get("y", 0), r.get("x", 0))
                           < (best.get("y", 0), best.get("x", 0))):
            best = r
    return best


class UIDriver:
    """What send_text needs from the GUI.

    Implementations: HelperDriver (default; talks to WeChatSendHelper.app over
    a Unix socket) and AXDriver (direct pyobjc, needs Accessibility for python).
    Drivers only perform UI primitives; all decisions live in Sender.
    """

    def prepare(self):
        """Check permission / WeChat / lock state, bring WeChat to front.
        Returns an opaque token for restore()."""
        raise NotImplementedError

    # Search primitives used by the default open_chat().
    def open_search(self, query):
        """Focus WeChat's search box and put `query` in it."""
        raise NotImplementedError

    def search_results(self):
        """Visible result texts: [{"text", "y", "x", ...}], top to bottom."""
        raise NotImplementedError

    def click_result(self, text):
        """Click the top-most visible result whose text is exactly `text`."""
        raise NotImplementedError

    def search_enter(self):
        """Press Enter in the search box (opens WeChat's top hit)."""
        raise NotImplementedError

    def open_chat(self, query, expected_names, settle=1.2, sleep=time.sleep):
        """Search for `query` and open the matching chat.

        Clicks an exact-text result if one is visible; otherwise opens
        WeChat's top hit. The caller must verify the chat title afterwards.
        """
        self.open_search(query)
        sleep(settle)  # results load asynchronously
        hit = choose_result(self.search_results(), expected_names)
        if hit is not None:
            self.click_result(hit["text"].strip())
        else:
            self.search_enter()

    def chat_title(self):
        """Title of the currently open chat, or None if it can't be read."""
        raise NotImplementedError

    def paste_into_input(self, text):
        raise NotImplementedError

    def input_text(self):
        """Current input box contents, or None if unreadable."""
        raise NotImplementedError

    def clear_input(self):
        raise NotImplementedError

    def input_matches(self, current, text):
        """Does the input box (as read back) hold exactly `text`?"""
        return normalize_text(current) == normalize_text(text)

    def press_send(self, send_key, expected_title, expected_input):
        """Press the send key, but only if the chat title is still exactly
        `expected_title` and the input box still holds exactly
        `expected_input` (the values the caller just read and approved).
        Returns the input box contents afterwards (None if unreadable)."""
        raise NotImplementedError

    def restore(self, token):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def _now_iso(ts):
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _load_data(cfg):
    decrypted_dir = cfg["decrypted_dir"]
    contacts = C.load_contacts(decrypted_dir)
    chat_list = C.list_chats(decrypted_dir, include_system=True, contacts=contacts)
    return contacts, chat_list


class Sender:
    def __init__(self, cfg, driver=None, log=None, data_loader=_load_data,
                 verifier=verify_sent, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self._driver = driver
        log_path = cfg.get("send_log") or DEFAULT_LOG
        if not os.path.isabs(log_path):
            log_path = os.path.join(PROJECT_ROOT, log_path)
        self.log = log or SendLog(log_path)
        self.data_loader = data_loader
        self.verifier = verifier
        self.clock = clock
        self.sleep = sleep

    @property
    def driver(self):
        if self._driver is None:
            self._driver = make_driver(self.cfg)
        return self._driver

    def resolve(self, chat):
        contacts, chat_list = self.data_loader(self.cfg)
        return resolve_chat(chat, contacts, chat_list), contacts

    def send_text(self, chat, text, *, verify=True, dry_run=False):
        max_chars = int(self.cfg.get("send_max_chars", DEFAULT_MAX_CHARS))
        validate_text(text, max_chars)
        target, contacts = self.resolve(chat)
        if target["is_group"] and target["name"] == target["username"]:
            raise UnsupportedChat("group has no name; WeChat search can't find it reliably",
                                  chat=target)
        names = ui_names(target)
        send_key = self.cfg.get("send_key", "enter")

        with self.log.lock():
            if not dry_run:
                check_rate_limit(self.log, self.cfg.get("send_rate_limit") or {},
                                 self.clock())
            entry = {"chat": target["username"], "len": len(text),
                     "sha256_16": text_hash(text), "dry_run": dry_run}
            try:
                sent_at = self._drive(target, names, text, send_key, dry_run)
            except SendError as e:
                ts = self.clock()
                self.log.append({"ts": ts, "time": _now_iso(ts), **entry,
                                 "status": "failed", "error": e.code})
                raise
            except Exception as e:
                ts = self.clock()
                self.log.append({"ts": ts, "time": _now_iso(ts), **entry,
                                 "status": "failed", "error": type(e).__name__})
                raise

            ts = self.clock() if dry_run else sent_at
            self.log.append({"ts": ts, "time": _now_iso(ts), **entry,
                             "status": "dry_run" if dry_run else "sent"})

        if dry_run:
            status, note = "dry_run", "input cleared; nothing sent"
        elif not verify:
            status, note = "unverified", "verification disabled"
        else:
            # Allow for clock skew / second-granularity create_time.
            status, note = self.verifier(target, text, sent_at - 5, self.cfg, contacts)
            ts = self.clock()
            self.log.append({"ts": ts, "time": _now_iso(ts), "chat": target["username"],
                             "sha256_16": entry["sha256_16"], "event": "verify",
                             "verify_status": status})
        result = {"status": status, "chat": target, "time": _now_iso(sent_at)}
        if note:
            result["note"] = note
        return result

    def _drive(self, target, names, text, send_key, dry_run):
        d = self.driver
        token = d.prepare()
        pasted = False
        try:
            d.open_chat(names[0], names)
            title = None
            for _ in range(8):  # the chat pane may take a moment to switch
                title = d.chat_title()
                if title_matches(title, names, target["is_group"]):
                    break
                self.sleep(0.25)
            else:
                raise UIError("opened chat title does not match the expected chat; "
                              "nothing was typed", title_found=title is not None)
            existing = d.input_text()
            if existing is None:
                raise UIError("can't read the chat input box; nothing was typed")
            if existing.strip():
                raise UIError("the chat has an unsent draft; not touching it")
            pasted = True
            d.paste_into_input(text)
            self.sleep(0.2)
            # Re-check right before Enter: the title and our exact text.
            title = d.chat_title()
            if not title_matches(title, names, target["is_group"]):
                raise UIError("chat title changed before sending; input cleared")
            current = d.input_text()
            if current is None or not d.input_matches(current, text):
                raise UIError("input box does not contain exactly the message; "
                              "input cleared", input_readable=current is not None)
            if dry_run:
                d.clear_input()
                pasted = False
                return self.clock()
            sent_at = self.clock()
            leftover = d.press_send(send_key, title, current)
            pasted = False
            if leftover and leftover.strip():
                # Enter inserted a newline instead of sending (Cmd+Enter mode?)
                d.clear_input()
                raise UIError("message was not sent (input still has text; is WeChat "
                              "set to send with Cmd+Enter? set config send_key)")
            return sent_at
        except BaseException:
            if pasted:
                with contextlib.suppress(Exception):
                    d.clear_input()
            raise
        finally:
            with contextlib.suppress(Exception):
                d.restore(token)


def send_text(chat, text, *, verify=True, dry_run=False, cfg=None, driver=None):
    """Send `text` to `chat` (exact username or exact display name).

    Returns {"status": "sent"|"unverified"|"dry_run", "chat": {...}, "time": iso,
    ["note"]}. Raises SendError subclasses on failure (nothing was sent unless
    the error says otherwise).
    """
    if cfg is None:
        with contextlib.redirect_stdout(sys.stderr):
            from config import load_config
            cfg = load_config()
    return Sender(cfg, driver=driver).send_text(chat, text, verify=verify, dry_run=dry_run)


def send_message_tool(chat, text, dry_run=False, cfg=None, driver=None):
    """MCP-friendly wrapper: never raises, always returns a JSON-able dict.

    Success: {"status": "sent"|"unverified"|"dry_run", "chat": {"username",
    "name", "is_group"}, "time": iso8601, ["note"]}
    Failure: {"status": "failed", "error": code, "message": str, [...extra]}
    where code is one of chat_not_found, ambiguous_chat (both carry
    "candidates"), unsupported_chat, invalid_text, rate_limited ("retry_after"),
    accessibility_denied, wechat_not_running, screen_locked, ui_error,
    internal_error.
    """
    try:
        return send_text(chat, text, dry_run=dry_run, cfg=cfg, driver=driver)
    except SendError as e:
        return e.to_dict()
    except Exception as e:
        return {"status": "failed", "error": "internal_error",
                "message": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Default driver: WeChatSendHelper.app over a Unix socket
# ---------------------------------------------------------------------------

HELPER_LABEL = "local.wechat-decrypt-export.sendhelper"
HELPER_APP = os.path.expanduser("~/Applications/WeChatSendHelper.app")
HELPER_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{HELPER_LABEL}.plist")
HELPER_SOCKET = os.path.expanduser(
    "~/Library/Application Support/wechat-decrypt-export/sendhelper.sock")

HELPER_INSTALL_HINT = (
    "WeChatSendHelper is not installed or not running. Install it with "
    "helper/install.sh, then grant it Accessibility in System Settings -> "
    "Privacy & Security -> Accessibility (~/Applications/WeChatSendHelper.app).")
HELPER_TRUST_HINT = (
    "WeChatSendHelper has no Accessibility permission. Open System Settings -> "
    "Privacy & Security -> Accessibility and turn on WeChatSendHelper (if it is "
    "missing, click + and add ~/Applications/WeChatSendHelper.app). After a helper "
    "rebuild, remove the old entry and add it again.")

# Client-side timeouts: the helper's own per-op timeout plus a margin.
_HELPER_TIMEOUTS = {
    "ping": 4, "status": 7, "request_trust": 7, "activate": 10, "restore": 5,
    "open_search": 8, "search_results": 8, "click_result": 8, "search_enter": 6,
    "escape": 5, "chat_title": 8, "input_text": 6, "paste_input": 8,
    "clear_input": 6, "send": 8, "probe": 32,
    "v_ocr": 12, "v_open_search": 8, "v_search_enter": 12, "v_paste": 8, "v_clear": 7,
    "v_send": 27, "v_click_popup": 10,
}

_HELPER_ERRORS = {
    "not_trusted": lambda m: AccessibilityDenied(HELPER_TRUST_HINT),
    "screen_locked": lambda m: ScreenLocked("the screen is locked; unlock the Mac to send"),
    "wechat_not_running": lambda m: WeChatNotRunning("WeChat is not running; start it and log in"),
}


class HelperClient:
    """Newline-delimited JSON over the helper's Unix socket.

    One connection is held for a whole send so requests from other clients
    can't interleave (the helper serves one connection at a time).
    """

    def __init__(self, path=HELPER_SOCKET):
        self.path = path
        self.sock = None
        self._buf = b""
        self._id = 0

    def connect(self):
        import socket
        if self.sock is not None:
            return
        if not os.path.exists(self.path):
            raise HelperUnavailable(HELPER_INSTALL_HINT, socket=self.path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect(self.path)
        except OSError as e:
            s.close()
            raise HelperUnavailable(f"{HELPER_INSTALL_HINT} ({type(e).__name__})",
                                    socket=self.path)
        self.sock = s
        self._buf = b""

    def close(self):
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
            self.sock = None

    def call(self, op, **args):
        """Return the result dict, or raise a SendError."""
        import socket
        self.connect()
        self._id += 1
        req = {"id": self._id, "op": op, **args}
        self.sock.settimeout(_HELPER_TIMEOUTS.get(op, 8))
        try:
            self.sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            while b"\n" not in self._buf:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("helper closed the connection")
                self._buf += chunk
        except socket.timeout:
            self.close()
            raise UIError(f"helper op {op!r} timed out", op=op,
                          maybe_sent=(op == "send"))
        except OSError as e:
            self.close()
            if op == "send":
                raise UIError(f"lost helper connection during send ({type(e).__name__})",
                              op=op, maybe_sent=True)
            raise HelperUnavailable(f"{HELPER_INSTALL_HINT} ({type(e).__name__})")
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            resp = json.loads(line)
        except ValueError:
            self.close()
            raise UIError("helper sent an invalid response", op=op)
        if resp.get("id") != self._id:
            self.close()
            raise UIError("helper response id mismatch", op=op)
        if resp.get("ok"):
            return resp.get("result") or {}
        err = resp.get("error") or {}
        code, msg = err.get("code", "unknown"), err.get("message", "")
        if code in _HELPER_ERRORS:
            raise _HELPER_ERRORS[code](msg)
        raise UIError(f"helper: {msg or code}", helper_error=code, op=op,
                      maybe_sent=(op == "send" and code == "timeout"))


class HelperDriver(UIDriver):
    def __init__(self, client=None):
        self.client = client or HelperClient()

    def status(self):
        try:
            return self.client.call("status")
        finally:
            self.client.close()

    def prepare(self):
        self.client.close()
        st = self.client.call("status")
        if not st.get("trusted"):
            raise AccessibilityDenied(HELPER_TRUST_HINT)
        if st.get("screen_locked"):
            raise ScreenLocked("the screen is locked; unlock the Mac to send")
        if not st.get("wechat_running"):
            raise WeChatNotRunning("WeChat is not running; start it and log in")
        return self.client.call("activate").get("previous_pid")

    def open_search(self, query):
        self.client.call("open_search", query=query)

    def search_results(self):
        return self.client.call("search_results").get("results") or []

    def click_result(self, text):
        self.client.call("click_result", text=text)

    def search_enter(self):
        self.client.call("search_enter")

    def chat_title(self):
        return self.client.call("chat_title").get("title")

    def input_text(self):
        return self.client.call("input_text").get("text")

    def paste_into_input(self, text):
        self.client.call("paste_input", text=text)

    def clear_input(self):
        self.client.call("clear_input")

    def press_send(self, send_key, expected_title, expected_input):
        return self.client.call("send", key=send_key, expected_title=expected_title,
                                expected_input=expected_input).get("leftover")

    def restore(self, token):
        try:
            if token:
                self.client.call("restore", pid=token)
        finally:
            self.client.close()

    def probe(self, show_text=False):
        try:
            res = self.client.call("probe", show_text=show_text)
            return (res.get("lines") or []) + ["# " + d for d in res.get("diag") or []]
        finally:
            self.client.close()


def _squash(text):
    """Text for OCR comparison: no whitespace, full-width punctuation folded."""
    import unicodedata
    return "".join(unicodedata.normalize("NFKC", text or "").split())


def ocr_matches(seen, text, min_ratio=0.92):
    """Loose equality for OCR'd input text: line wrapping and spacing are
    ignored; a few misread characters are tolerated for longer texts, but the
    read-back must cover the whole message (lengths within 8%)."""
    import difflib
    a, b = _squash(seen), _squash(text)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(b) < 12 or abs(len(a) - len(b)) > max(1, len(b) * 0.08):
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= min_ratio


class VisionDriver(HelperDriver):
    """Helper driver for WeChat 4.x, whose UI has no accessibility tree.

    The helper screenshots WeChat's window and OCRs regions (Screen Recording
    permission); this class decides where those regions are. Layout of the
    main window (window-relative points, top-left origin):

        | sidebar | chat list (search box on top) | chat pane: title on top,  |
        |         |                               | messages, toolbar, input  |

    The chat pane's left edge is found from the chat list's right-most text;
    the title is the left-most text in the pane's top band; the input box is
    the band below the toolbar (INPUT_TOP of the window height and below).
    """

    TOP_BAND = 60          # title / search box live above this y
    INPUT_TOP = 0.845      # input box starts at this fraction of the height
    SETTLE = 0.8

    def __init__(self, client=None, sleep=time.sleep):
        super().__init__(client)
        self.sleep = sleep
        self.size = None
        self.title_rect = None
        self.input_rect = None
        self.input_point = None

    def prepare(self):
        self.size = self.title_rect = self.input_rect = self.input_point = None
        token = super().prepare()
        if not self.client.call("status").get("screen_capture"):
            raise AccessibilityDenied(
                "WeChatSendHelper has no Screen Recording permission (it reads WeChat's "
                "window to check the chat title and message). Open System Settings -> "
                "Privacy & Security -> Screen & System Audio Recording and turn on "
                "WeChatSendHelper, then run: launchctl kickstart -k "
                "gui/$(id -u)/local.wechat-decrypt-export.sendhelper")
        return token

    def _ocr(self, rect=None):
        res = self.client.call("v_ocr", **({"rect": [int(v) for v in rect]} if rect else {}))
        self.size = tuple(res.get("window") or (0, 0))
        return res

    def _layout(self):
        """Locate the chat pane, title and input box from one full-window OCR."""
        res = self._ocr()
        w, h = self.size
        items = res.get("items") or []
        pane_left = self._list_right_edge(items, w) + 6
        top = sorted((i for i in items if i["y"] < self.TOP_BAND and i["x"] >= pane_left
                      and i["x"] < w * 0.6), key=lambda i: i["x"])
        self.title_rect = None
        if top:
            t = top[0]
            right = min(w * 0.6, t["x"] + t["w"] + 160)
            self.title_rect = [pane_left, max(0, t["y"] - 8), right - pane_left,
                               min(t["h"] + 16, self.TOP_BAND - max(0, t["y"] - 8))]
        y0 = h * self.INPUT_TOP
        self.input_rect = [pane_left + 4, y0, w - pane_left - 12, h - y0 - 6]
        self.input_point = (pane_left + (w - pane_left) / 2, y0 + (h - y0) / 2)

    def _list_right_edge(self, items, w):
        """The chat list's timestamps are right-aligned: the most common right
        edge (3+ items, 3 pt tolerance) in the left part of the window."""
        edges = sorted(i["x"] + i["w"] for i in items
                       if i["y"] > self.TOP_BAND and i["x"] + i["w"] < w * 0.45)
        best, best_n = None, 0
        for e in edges:
            n = sum(1 for x in edges if e - 3 <= x <= e)
            if n > best_n or (n == best_n and best is not None and e > best):
                best, best_n = e, n
        return best if best_n >= 3 else w * 0.2

    # Section headings of WeChat's search results popup. Only rows under a
    # chat section are ever clicked; other sections (chat history, files,
    # internet search, ...) end the chat sections.
    CHAT_SECTIONS = {"contacts", "group chats", "联系人", "群聊", "聯絡人", "群組"}
    OTHER_SECTIONS = {"chat history", "chat files", "internet search results", "search",
                      "official accounts", "channels", "mini programs", "articles",
                      "聊天记录", "文件", "搜一搜", "公众号", "视频号", "小程序", "文章",
                      "网络搜索结果", "聊天記錄"}

    @classmethod
    def pick_search_result(cls, items, expected_names):
        """The top-most popup row whose text is exactly one of expected_names,
        inside a Contacts / Group Chats section. None if there is none."""
        rows = sorted(items, key=lambda i: (i["y"], i["x"]))
        names = {n.strip() for n in expected_names if n}
        section = None
        for it in rows:
            t = it["text"].strip()
            low = t.lower()
            if low in cls.CHAT_SECTIONS:
                section = "chat"
                continue
            if low in cls.OTHER_SECTIONS or low.startswith(("q ", "search ")):
                section = "other"
                continue
            if section == "chat" and t in names:
                return it
        return None

    def open_chat(self, query, expected_names, settle=1.2, sleep=None):
        sleep = sleep or self.sleep
        self.client.call("v_open_search", query=query)
        hit = None
        for _ in range(4):  # results load asynchronously
            sleep(self.SETTLE)
            try:
                res = self.client.call("v_ocr", popup=True)
            except UIError:
                continue
            hit = self.pick_search_result(res.get("items") or [], expected_names)
            if hit:
                break
        if hit is None:
            with contextlib.suppress(SendError):
                self.client.call("escape")
            raise UIError("WeChat's search shows no contact or group with exactly this name; "
                          "nothing was sent")
        self.client.call("v_click_popup", x=int(hit["x"] + hit["w"] / 2),
                         y=int(hit["y"] + hit["h"] / 2))
        sleep(settle)
        self._layout()

    def chat_title(self):
        if self.title_rect is None:
            self._layout()
        if self.title_rect is None:
            return None
        return self._ocr(self.title_rect).get("text")

    def input_text(self):
        if self.input_rect is None:
            self._layout()
        return self._ocr(self.input_rect).get("text")

    def input_matches(self, current, text):
        return ocr_matches(current, text)

    def paste_into_input(self, text):
        x, y = self.input_point
        self.client.call("v_paste", text=text, x=int(x), y=int(y))

    def clear_input(self):
        x, y = self.input_point
        self.client.call("v_clear", x=int(x), y=int(y))

    def press_send(self, send_key, expected_title, expected_input):
        return self.client.call("v_send", key=send_key, expected_title=expected_title,
                                expected_input=expected_input,
                                title_rect=[int(v) for v in self.title_rect],
                                input_rect=[int(v) for v in self.input_rect]).get("leftover")


def make_driver(cfg):
    kind = (cfg or {}).get("send_driver", "helper")
    socket_path = (cfg or {}).get("send_helper_socket") or HELPER_SOCKET
    if kind == "helper":  # WeChat 4.x: screenshots + OCR through the helper
        return VisionDriver(HelperClient(socket_path))
    if kind == "helper_ax":  # UIs that expose an accessibility tree
        return HelperDriver(HelperClient(socket_path))
    if kind == "direct":
        return AXDriver()
    raise SendError(f"unknown send_driver {kind!r} (use 'helper' or 'direct')")


def helper_check(client=None, launchctl=None):
    """Installation + runtime status of the helper, for --check."""
    import subprocess
    info = {
        "driver": "helper",
        "app_installed": os.path.isdir(HELPER_APP),
        "launch_agent_installed": os.path.isfile(HELPER_PLIST),
        "socket": HELPER_SOCKET,
        "socket_exists": os.path.exists(HELPER_SOCKET),
    }
    if launchctl is None:
        def launchctl():
            r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{HELPER_LABEL}"],
                               capture_output=True, text=True)
            return r.returncode == 0
    with contextlib.suppress(Exception):
        info["launch_agent_loaded"] = bool(launchctl())
    client = client or HelperClient()
    try:
        st = client.call("status")
        info["running"] = True
        info.update({k: st.get(k) for k in (
            "version", "trusted", "screen_capture", "screen_locked", "wechat_running",
            "wechat_frontmost", "wechat_window", "wechat_minimized")})
        if not st.get("trusted"):
            info["hint"] = HELPER_TRUST_HINT
    except SendError as e:
        info["running"] = False
        info["error"] = e.code
        info["hint"] = HELPER_INSTALL_HINT
    finally:
        client.close()
    return info


# ---------------------------------------------------------------------------
# Real driver: macOS Accessibility (pyobjc)
# ---------------------------------------------------------------------------

# Virtual key codes (ANSI layout)
KC_A, KC_F, KC_V = 0, 3, 9
KC_RETURN, KC_DELETE, KC_ESCAPE = 36, 51, 53

_TEXT_ROLES = ("AXStaticText", "AXTextField", "AXTextArea", "AXButton", "AXCell", "AXRow")
_EDIT_ROLES = ("AXTextArea", "AXTextField")


class AXDriver(UIDriver):
    """Drives WeChat through the AX API + synthesized keyboard/mouse events.

    Layout assumptions (WeChat 4.x): left sidebar + session list, chat pane on
    the right with the title at the top and the input box at the bottom.
    """

    def __init__(self, bundle_id=WECHAT_BUNDLE_ID, delay=1.0):
        import AppKit
        import ApplicationServices as AS
        import Quartz
        self.AppKit, self.AS, self.Q = AppKit, AS, Quartz
        self.bundle_id = bundle_id
        self.delay = delay  # multiplier for UI waits
        self.pid = None
        self.app = None
        self.window = None

    # -- AX helpers --------------------------------------------------------
    def _attr(self, el, name):
        err, val = self.AS.AXUIElementCopyAttributeValue(el, name, None)
        if err == self.AS.kAXErrorAPIDisabled:
            raise AccessibilityDenied(_perm_message())
        return val if err == 0 else None

    def _set(self, el, name, value):
        return self.AS.AXUIElementSetAttributeValue(el, name, value) == 0

    def _frame(self, el):
        pos, size = self._attr(el, "AXPosition"), self._attr(el, "AXSize")
        if pos is None or size is None:
            return None
        ok1, p = self.AS.AXValueGetValue(pos, self.AS.kAXValueCGPointType, None)
        ok2, s = self.AS.AXValueGetValue(size, self.AS.kAXValueCGSizeType, None)
        if not (ok1 and ok2):
            return None
        return (p.x, p.y, s.width, s.height)

    def _text(self, el):
        for a in ("AXValue", "AXTitle", "AXDescription"):
            v = self._attr(el, a)
            if isinstance(v, str) and v.strip():
                return v
        return None

    def _walk(self, el, max_nodes=4000, max_depth=40):
        stack = [(el, 0)]
        n = 0
        while stack and n < max_nodes:
            node, depth = stack.pop()
            n += 1
            yield node, depth
            if depth < max_depth:
                kids = self._attr(node, "AXChildren") or []
                stack.extend((k, depth + 1) for k in reversed(list(kids)))

    # -- events ------------------------------------------------------------
    def _key(self, keycode, cmd=False):
        Q = self.Q
        flags = Q.kCGEventFlagMaskCommand if cmd else 0
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(None, keycode, down)
            Q.CGEventSetFlags(ev, flags)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.03)

    def _click(self, frame):
        Q = self.Q
        x, y, w, h = frame
        pt = Q.CGPointMake(x + w / 2, y + h / 2)
        for t in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
            ev = Q.CGEventCreateMouseEvent(None, t, pt, Q.kCGMouseButtonLeft)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.05)

    def _wait(self, s):
        time.sleep(s * self.delay)

    # -- clipboard ---------------------------------------------------------
    def _save_clipboard(self):
        pb = self.AppKit.NSPasteboard.generalPasteboard()
        saved = []
        for item in pb.pasteboardItems() or []:
            saved.append({t: item.dataForType_(t) for t in item.types()
                          if item.dataForType_(t) is not None})
        return saved

    def _restore_clipboard(self, saved):
        AK = self.AppKit
        pb = AK.NSPasteboard.generalPasteboard()
        pb.clearContents()
        items = []
        for d in saved:
            it = AK.NSPasteboardItem.alloc().init()
            for t, data in d.items():
                it.setData_forType_(data, t)
            items.append(it)
        if items:
            pb.writeObjects_(items)

    def _paste(self, text):
        AK = self.AppKit
        saved = self._save_clipboard()
        pb = AK.NSPasteboard.generalPasteboard()
        try:
            pb.clearContents()
            item = AK.NSPasteboardItem.alloc().init()
            item.setString_forType_(text, AK.NSPasteboardTypeString)
            # Ask clipboard managers not to record it.
            item.setString_forType_("", "org.nspasteboard.TransientType")
            item.setString_forType_("", "org.nspasteboard.ConcealedType")
            pb.writeObjects_([item])
            self._key(KC_V, cmd=True)
            self._wait(0.4)
        finally:
            self._restore_clipboard(saved)

    # -- state -------------------------------------------------------------
    def _find_app(self):
        apps = self.AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            self.bundle_id)
        apps = [a for a in apps if not a.isTerminated()]
        if not apps:
            raise WeChatNotRunning("WeChat is not running; start it and log in")
        return apps[0]

    def _main_window(self):
        wins = list(self._attr(self.app, "AXWindows") or [])
        best, area = None, -1
        for w in wins:
            if self._attr(w, "AXSubrole") not in (None, "AXStandardWindow"):
                continue
            f = self._frame(w)
            a = f[2] * f[3] if f else 0
            if a > area:
                best, area = w, a
        return best

    def check(self):
        """Readiness report without touching the UI."""
        info = {"accessibility_trusted": bool(self.AS.AXIsProcessTrusted())}
        d = self.Q.CGSessionCopyCurrentDictionary() or {}
        info["screen_locked"] = bool(d.get("CGSSessionScreenIsLocked"))
        try:
            app = self._find_app()
            info["wechat_running"] = True
            info["wechat_pid"] = app.processIdentifier()
        except WeChatNotRunning:
            info["wechat_running"] = False
        return info

    def prepare(self):
        AK = self.AppKit
        if not self.AS.AXIsProcessTrusted():
            raise AccessibilityDenied(_perm_message())
        d = self.Q.CGSessionCopyCurrentDictionary() or {}
        if d.get("CGSSessionScreenIsLocked"):
            raise ScreenLocked("the screen is locked; unlock the Mac to send")
        if d and not d.get("kCGSSessionOnConsoleKey", True):
            raise ScreenLocked("this login session is not on the console")
        running = self._find_app()
        self.pid = running.processIdentifier()
        self.app = self.AS.AXUIElementCreateApplication(self.pid)

        front = AK.NSWorkspace.sharedWorkspace().frontmostApplication()
        token = front.processIdentifier() if front is not None else None

        if running.isHidden():
            running.unhide()
        self._set(self.app, "AXFrontmost", True)
        running.activateWithOptions_(AK.NSApplicationActivateIgnoringOtherApps)
        self._wait(0.3)
        win = self._main_window()
        if win is None:
            # Main window closed: a reopen event shows it again.
            import subprocess
            subprocess.run(["open", "-b", self.bundle_id], check=False)
            self._wait(1.0)
            win = self._main_window()
        if win is None:
            raise UIError("WeChat main window not found (not logged in?)")
        if self._attr(win, "AXMinimized"):
            self._set(win, "AXMinimized", False)
            self._wait(0.5)
        self.AS.AXUIElementPerformAction(win, "AXRaise")
        self.window = win
        for _ in range(10):
            front = AK.NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is not None and front.processIdentifier() == self.pid:
                break
            self._wait(0.1)
        else:
            raise UIError("could not bring WeChat to the front")
        return token

    def restore(self, token):
        if token is None or token == self.pid:
            return
        app = self.AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(token)
        if app is not None:
            self._set(self.AS.AXUIElementCreateApplication(token), "AXFrontmost", True)
            app.activateWithOptions_(self.AppKit.NSApplicationActivateIgnoringOtherApps)

    def _ensure_front(self):
        front = self.AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None or front.processIdentifier() != self.pid:
            raise UIError("WeChat lost focus during the operation; aborted")

    # -- layout ------------------------------------------------------------
    def _elements(self):
        out = []
        for el, _ in self._walk(self.window):
            role = self._attr(el, "AXRole")
            if role in _TEXT_ROLES:
                f = self._frame(el)
                if f:
                    out.append((role, f, el))
        return out

    def _input_box(self, elements=None):
        """The chat input: lowest, widest editable text area in the right pane."""
        wf = self._frame(self.window)
        if wf is None:
            return None
        best = None
        for role, f, el in elements or self._elements():
            if role not in _EDIT_ROLES:
                continue
            if f[1] < wf[1] + wf[3] * 0.5 or f[2] < wf[2] * 0.3:
                continue  # search box (top-left) and other small fields
            if best is None or f[1] > best[0][1]:
                best = (f, el)
        return best

    def chat_title(self):
        elements = self._elements()
        box = self._input_box(elements)
        if box is None:
            return None
        wf = self._frame(self.window)
        if wf is None:
            return None
        pane_x = box[0][0] - 10
        header = []
        for role, f, el in elements:
            if role != "AXStaticText":
                continue
            if f[0] >= pane_x and wf[1] <= f[1] < wf[1] + 90:
                t = self._text(el)
                if t:
                    header.append((f[1], f[0], t.strip()))
        if not header:
            return None
        header.sort()
        return header[0][2]

    def input_text(self):
        box = self._input_box()
        if box is None:
            return None
        v = self._attr(box[1], "AXValue")
        return v if isinstance(v, str) else None

    def _focus_input(self):
        box = self._input_box()
        if box is None:
            raise UIError("chat input box not found")
        f, el = box
        if not self._set(el, "AXFocused", True) or not self._attr(el, "AXFocused"):
            self._click(f)
            self._wait(0.2)
        return el

    # -- actions -----------------------------------------------------------
    def open_search(self, query):
        self._ensure_front()
        self._key(KC_F, cmd=True)
        self._wait(0.4)
        focused = self._attr(self.app, "AXFocusedUIElement")
        if focused is None or self._attr(focused, "AXRole") not in _EDIT_ROLES:
            self._key(KC_ESCAPE)
            raise UIError("WeChat search box did not take focus")
        self._search = focused
        self._key(KC_A, cmd=True)
        self._paste(query)

    def _results(self):
        self._ensure_front()
        sf = self._frame(self._search) if getattr(self, "_search", None) else None
        if sf is None:
            raise UIError("no active search")
        wf = self._frame(self.window)
        max_x = max(sf[0] + sf[2] + 60, wf[0] + wf[2] * 0.45)
        out = []
        for role, f, el in self._elements():
            if role not in ("AXStaticText", "AXCell", "AXRow", "AXButton"):
                continue
            if f[1] <= sf[1] + sf[3] or not (wf[0] <= f[0] < max_x) or not (0 < f[3] < 200):
                continue
            t = self._text(el)
            if t:
                out.append({"text": t, "x": int(f[0]), "y": int(f[1]), "frame": f})
        out.sort(key=lambda r: (r["y"], r["x"]))
        return out

    def search_results(self):
        return [{k: v for k, v in r.items() if k != "frame"} for r in self._results()]

    def click_result(self, text):
        for r in self._results():
            if r["text"].strip() == text:
                self._click(r["frame"])
                self._wait(0.4)
                self._search = None
                return
        raise UIError("search result not found")

    def search_enter(self):
        self._ensure_front()
        self._key(KC_RETURN)
        self._wait(0.4)
        self._search = None

    def paste_into_input(self, text):
        self._ensure_front()
        self._focus_input()
        self._paste(text)

    def clear_input(self):
        self._focus_input()
        self._key(KC_A, cmd=True)
        self._key(KC_DELETE)
        self._wait(0.1)

    def press_send(self, send_key, expected_title, expected_input):
        self._ensure_front()
        if self.chat_title() != expected_title:
            raise UIError("chat title changed before sending")
        if self.input_text() != expected_input:
            raise UIError("input box changed before sending")
        self._focus_input()
        self._ensure_front()
        self._key(KC_RETURN, cmd=(send_key == "cmd_enter"))
        self._wait(0.5)
        return self.input_text()

    # -- diagnostics -------------------------------------------------------
    def dump_tree(self, show_text=False, out=sys.stdout):
        running = self._find_app()
        self.pid = running.processIdentifier()
        self.app = self.AS.AXUIElementCreateApplication(self.pid)
        self.window = self._main_window()
        if self.window is None:
            print("no main window", file=out)
            return
        for el, depth in self._walk(self.window):
            role = self._attr(el, "AXRole")
            f = self._frame(el)
            t = self._text(el)
            shown = (t if show_text else f"<{len(t)} chars>") if t else ""
            fs = "(%d,%d %dx%d)" % f if f else ""
            print("  " * depth + f"{role} {fs} {shown}", file=out)


def _perm_message():
    return ("Accessibility permission is not granted to the process running Python. "
            "Grant it in System Settings -> Privacy & Security -> Accessibility to "
            "the app that launched this (your terminal app; for SSH sessions, "
            "/usr/libexec/sshd-keygen-wrapper), then retry.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Send a WeChat text message via UI automation")
    ap.add_argument("--to", help="exact chat username or display name")
    ap.add_argument("--text", help="message text (use '-' to read stdin)")
    ap.add_argument("--dry-run", action="store_true",
                    help="open the chat and paste, then clear instead of sending")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    ap.add_argument("--check", action="store_true", help="report permission/WeChat state")
    ap.add_argument("--probe", action="store_true", help="dump WeChat's AX tree")
    ap.add_argument("--show-text", action="store_true", help="with --probe: show texts")
    args = ap.parse_args(argv)

    if args.check or args.probe:
        with contextlib.redirect_stdout(sys.stderr):
            from config import load_config
            kind = load_config().get("send_driver", "helper")
        if args.check:
            info = helper_check() if kind == "helper" else {"driver": "direct",
                                                            **AXDriver().check()}
            print(json.dumps(info, indent=2, ensure_ascii=False))
            return 0
        try:
            if kind == "helper":
                print("\n".join(HelperDriver().probe(show_text=args.show_text)))
            else:
                AXDriver().dump_tree(show_text=args.show_text)
        except SendError as e:
            print(json.dumps(e.to_dict(), ensure_ascii=False, indent=2))
            return 2
        return 0
    if not args.to or args.text is None:
        ap.error("--to and --text are required")
    text = sys.stdin.read() if args.text == "-" else args.text

    with contextlib.redirect_stdout(sys.stderr):
        from config import load_config
        cfg = load_config()
    sender = Sender(cfg)
    try:
        target, _ = sender.resolve(args.to)
    except SendError as e:
        print(json.dumps(e.to_dict(), ensure_ascii=False, indent=2))
        return 2
    if not args.yes:
        kind = "group" if target["is_group"] else "chat"
        verb = "DRY RUN (will not send) to" if args.dry_run else "Send to"
        print(f"{verb} {kind} {target['name']!r} ({target['username']}), "
              f"{len(text)} chars. Continue? [y/N] ", end="", file=sys.stderr, flush=True)
        if input().strip().lower() not in ("y", "yes"):
            print("aborted", file=sys.stderr)
            return 1
    try:
        res = sender.send_text(args.to, text, verify=not args.no_verify,
                               dry_run=args.dry_run)
    except SendError as e:
        print(json.dumps(e.to_dict(), ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["status"] in ("sent", "dry_run", "unverified") else 2


if __name__ == "__main__":
    sys.exit(main())
