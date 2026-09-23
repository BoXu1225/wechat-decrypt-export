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

Requirements
  - macOS Accessibility permission for the process that runs Python
    (System Settings -> Privacy & Security -> Accessibility). Over SSH that is
    /usr/libexec/sshd-keygen-wrapper; in a terminal it is the terminal app.
  - pyobjc-framework-ApplicationServices / -Quartz / -Cocoa.

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
    "send_log": "logs/send_log.jsonl"
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

WAL_HDR = 32
WAL_FRAME_HDR = 24


def _apply_wal(wal_path, out_path, enc_key, decrypt_page, page_sz):
    """Overlay committed frames of an encrypted WAL onto a decrypted DB.

    Only frames whose salts match the WAL header, up to the last commit frame,
    are applied (SQLite's own validity rule, minus checksum verification).
    """
    if not os.path.exists(wal_path) or os.path.getsize(wal_path) <= WAL_HDR:
        return 0
    with open(wal_path, "rb") as f:
        data = f.read()
    if int.from_bytes(data[8:12], "big") != page_sz:
        return 0
    salt = data[16:24]
    frames, pending = {}, {}
    db_pages = None
    off = WAL_HDR
    step = WAL_FRAME_HDR + page_sz
    while off + step <= len(data):
        hdr = data[off:off + WAL_FRAME_HDR]
        if hdr[8:16] != salt:
            break
        pgno = int.from_bytes(hdr[0:4], "big")
        commit = int.from_bytes(hdr[4:8], "big")
        pending[pgno] = off + WAL_FRAME_HDR
        if commit:
            frames.update(pending)
            pending = {}
            db_pages = commit
        off += step
    if not frames:
        return 0
    with open(out_path, "r+b") as out:
        for pgno, poff in frames.items():
            page = decrypt_page(enc_key, data[poff:poff + page_sz], pgno)
            out.seek((pgno - 1) * page_sz)
            out.write(page)
        if db_pages:
            out.truncate(db_pages * page_sz)
    return len(frames)


def refresh_message_dbs(cfg):
    """Re-decrypt changed message/message_N.db files (+ their WAL). No sudo.

    Returns (ok, note). ok=False means keys are missing/stale; the caller
    should skip verification.
    """
    with contextlib.redirect_stdout(sys.stderr):
        import decrypt_db as D
        keys = D.load_keys()
    if not keys:
        return False, "no keys file; run ./wechat decrypt to enable verification"
    db_dir = cfg["db_dir"]
    out_dir = cfg["decrypted_dir"]
    msg_dir = os.path.join(db_dir, "message")
    if not os.path.isdir(msg_dir):
        return False, "message directory not found"
    for f in sorted(os.listdir(msg_dir)):
        if not re.fullmatch(r"message_\d+\.db", f):
            continue
        rel = f"message/{f}"
        src = os.path.join(db_dir, rel)
        wal = src + "-wal"
        dst = os.path.join(out_dir, rel)
        src_m = max(os.path.getmtime(src),
                    os.path.getmtime(wal) if os.path.exists(wal) else 0)
        if os.path.exists(dst) and os.path.getmtime(dst) >= src_m:
            continue
        if rel not in keys:
            continue
        with contextlib.redirect_stdout(sys.stderr):
            if not D.key_is_valid(rel, keys[rel]["enc_key"]):
                return False, f"key for {rel} is stale; run ./wechat decrypt"
            enc_key = bytes.fromhex(keys[rel]["enc_key"])
            tmp = dst + ".send_tmp"
            if not D.decrypt_database(src, tmp, enc_key):
                return False, f"decrypt failed for {rel}"
            try:
                _apply_wal(wal, tmp, enc_key, D.decrypt_page, D.PAGE_SZ)
            except OSError:
                pass
            os.replace(tmp, dst)
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

class UIDriver:
    """What send_text needs from the GUI. See AXDriver for the real one."""

    def prepare(self):
        """Check permission / WeChat / lock state, bring WeChat to front.
        Returns an opaque token for restore()."""
        raise NotImplementedError

    def open_chat(self, query, expected_names):
        """Search for `query` and open the matching chat."""
        raise NotImplementedError

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

    def press_send(self, send_key):
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
            self._driver = AXDriver()
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
            title = d.chat_title()
            if not title_matches(title, names, target["is_group"]):
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
            if current is None or normalize_text(current) != normalize_text(text):
                raise UIError("input box does not contain exactly the message; "
                              "input cleared", input_readable=current is not None)
            if dry_run:
                d.clear_input()
                pasted = False
                return self.clock()
            sent_at = self.clock()
            d.press_send(send_key)
            pasted = False
            self.sleep(0.5)
            leftover = d.input_text()
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
    def open_chat(self, query, expected_names):
        self._ensure_front()
        self._key(KC_F, cmd=True)
        self._wait(0.4)
        focused = self._attr(self.app, "AXFocusedUIElement")
        if focused is None or self._attr(focused, "AXRole") not in _EDIT_ROLES:
            self._key(KC_ESCAPE)
            raise UIError("WeChat search box did not take focus")
        self._key(KC_A, cmd=True)
        self._paste(query)
        self._wait(1.2)  # results load asynchronously
        self._ensure_front()

        # Prefer a result row whose text is exactly the expected name, in the
        # left column below the search box. Otherwise take WeChat's top hit;
        # the caller verifies the opened chat's title either way.
        sf = self._frame(focused)
        if sf is None:
            raise UIError("can't locate the search box")
        target = None
        for role, f, el in self._elements():
            if role not in ("AXStaticText", "AXCell", "AXRow"):
                continue
            if f[1] <= sf[1] + sf[3] or f[0] > sf[0] + sf[2] + 40:
                continue
            t = self._text(el)
            if t and t.strip() in expected_names:
                if target is None or f[1] < target[1]:
                    target = f
        if target is not None:
            self._click(target)
        else:
            self._key(KC_RETURN)
        self._wait(0.8)
        self._ensure_front()

    def paste_into_input(self, text):
        self._ensure_front()
        self._focus_input()
        self._paste(text)

    def clear_input(self):
        self._focus_input()
        self._key(KC_A, cmd=True)
        self._key(KC_DELETE)
        self._wait(0.1)

    def press_send(self, send_key):
        self._ensure_front()
        self._key(KC_RETURN, cmd=(send_key == "cmd_enter"))

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

    if args.check:
        print(json.dumps(AXDriver().check(), indent=2))
        return 0
    if args.probe:
        AXDriver().dump_tree(show_text=args.show_text)
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
