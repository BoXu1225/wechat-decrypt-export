#!/usr/bin/env python3
"""
Export a WeChat chat to a text file from decrypted databases.

Usage:
    python export_chat.py <contact_pattern>
    python export_chat.py <contact_pattern> -o <output_file>
    python export_chat.py <contact_pattern> -i

Examples:
    python export_chat.py ning          # fuzzy match, pick from list
    python export_chat.py xx -o out.txt
    python export_chat.py xx -i         # incremental export

Requires:
    - Decrypted WeChat databases (via wechat-decrypt)
    - pip install zstandard
"""
import argparse
import glob as globmod
import sqlite3
import re
import os
from datetime import datetime

import zstandard

# Default paths relative to this script's directory
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DECRYPTED_DIR = os.path.join(PROJECT_ROOT, "decrypted")
MY_WXID_PREFIX = "wxid_dsnzkc2lm38y22"


def decompress_if_needed(content, ct):
    if ct == 4 and isinstance(content, bytes) and len(content) > 0:
        try:
            dctx = zstandard.ZstdDecompressor()
            return dctx.decompress(content).decode("utf-8", errors="replace")
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


def get_remark(wxid, contact_db):
    if not os.path.exists(contact_db):
        return None
    try:
        conn = sqlite3.connect(contact_db)
        tables = [t[0] for t in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

        for t in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info([{t}])").fetchall()]
            if "userName" in cols or "username" in cols:
                uname_col = "userName" if "userName" in cols else "username"
                remark_col = next((c for c in cols if c.lower() in ("remark", "conremark")), None)
                nick_col = next((c for c in cols if c.lower() in ("nickname", "nick_name", "connickname")), None)

                if remark_col or nick_col:
                    select_cols = []
                    if remark_col:
                        select_cols.append(remark_col)
                    if nick_col:
                        select_cols.append(nick_col)
                    row = conn.execute(
                        f"SELECT {','.join(select_cols)} FROM [{t}] WHERE {uname_col}=?",
                        (wxid,)).fetchone()
                    if row:
                        conn.close()
                        for val in row:
                            if val and val.strip():
                                return val.strip()
        conn.close()
    except Exception:
        pass
    return None


def find_contacts(pattern, decrypted_dir):
    """Find contacts whose remark/nickname contains the pattern (case-insensitive).
    Returns list of (remark_name, wxid) tuples."""
    msg_dir = os.path.join(decrypted_dir, "message")
    contact_db = os.path.join(decrypted_dir, "contact", "contact.db")

    db_files = sorted(
        f for f in os.listdir(msg_dir)
        if f.startswith("message_") and f.endswith(".db") and "fts" not in f
    )

    seen_wxids = set()
    matches = []
    pat_lower = pattern.lower()

    for db_file in db_files:
        db_path = os.path.join(msg_dir, db_file)
        conn = sqlite3.connect(db_path)
        try:
            name2id = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
        except Exception:
            conn.close()
            continue
        for rowid, username in name2id:
            if username in seen_wxids:
                continue
            seen_wxids.add(username)
            r = get_remark(username, contact_db)
            if r and pat_lower in r.lower():
                matches.append((r, username))
        conn.close()

    return matches


def resolve_contact(pattern, decrypted_dir):
    """Resolve a contact pattern to (remark_name, wxid).
    Exact match is tried first, then fuzzy. Prompts user if multiple matches."""
    matches = find_contacts(pattern, decrypted_dir)
    if not matches:
        print(f"Could not find contact matching '{pattern}'")
        return None, None

    # Exact match takes priority
    exact = [(r, w) for r, w in matches if r == pattern]
    if len(exact) == 1:
        return exact[0]

    if len(matches) == 1:
        print(f"Matched: {matches[0][0]}")
        return matches[0]

    # Multiple matches - show up to 5 and let user pick
    print(f"Found {len(matches)} contacts matching '{pattern}':")
    shown = matches[:5]
    for i, (remark, wxid) in enumerate(shown, 1):
        print(f"  {i}. {remark}")
    if len(matches) > 5:
        print(f"  ... and {len(matches) - 5} more (use a more specific pattern)")

    while True:
        try:
            choice = input(f"Select [1-{len(shown)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(shown):
                return shown[idx]
        except (ValueError, EOFError):
            pass
        print(f"Please enter a number between 1 and {len(shown)}")


def export_chat(remark_name, output_file=None, decrypted_dir=None, incremental=False):
    if decrypted_dir is None:
        decrypted_dir = DEFAULT_DECRYPTED_DIR

    msg_dir = os.path.join(decrypted_dir, "message")
    contact_db = os.path.join(decrypted_dir, "contact", "contact.db")

    if not os.path.isdir(msg_dir):
        print(f"Message directory not found: {msg_dir}")
        return

    db_files = sorted(
        f for f in os.listdir(msg_dir)
        if f.startswith("message_") and f.endswith(".db") and "fts" not in f
    )

    # Find the target wxid (remark_name is already resolved to exact name)
    target_wxid = None
    for db_file in db_files:
        db_path = os.path.join(msg_dir, db_file)
        conn = sqlite3.connect(db_path)
        try:
            name2id = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
        except Exception:
            conn.close()
            continue
        for rowid, username in name2id:
            r = get_remark(username, contact_db)
            if r == remark_name:
                target_wxid = username
                break
        conn.close()
        if target_wxid:
            break

    if not target_wxid:
        print(f"Could not find contact '{remark_name}'")
        return

    print(f"Target wxid: {target_wxid}")

    # Second pass: find all DBs containing this 1-on-1 chat
    found_dbs = []
    for db_file in db_files:
        db_path = os.path.join(msg_dir, db_file)
        conn = sqlite3.connect(db_path)
        try:
            name2id = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
        except Exception:
            conn.close()
            continue

        # Resolve per-DB rowids
        target_rowid = None
        my_rowid = None
        for rowid, username in name2id:
            if username == target_wxid:
                target_rowid = rowid
            if username.startswith(MY_WXID_PREFIX):
                my_rowid = rowid

        if target_rowid is None:
            conn.close()
            continue

        tables = [t[0] for t in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()]

        for t in tables:
            try:
                has = conn.execute(
                    f"SELECT 1 FROM [{t}] WHERE real_sender_id = ? LIMIT 1",
                    (target_rowid,)).fetchone()
                if has:
                    senders = conn.execute(
                        f"SELECT COUNT(DISTINCT real_sender_id) FROM [{t}]"
                    ).fetchone()[0]
                    if senders <= 2:
                        found_dbs.append((db_path, t, target_rowid, my_rowid))
                        cnt = conn.execute(f"SELECT count(*) FROM [{t}]").fetchone()[0]
                        print(f"Found: {db_file} / {t} ({cnt} msgs)")
                        break
            except Exception:
                continue

        conn.close()

    if not found_dbs:
        print(f"Could not find chat for '{remark_name}'")
        return

    print(f"Chat spans {len(found_dbs)} database(s)")

    # Collect and merge messages from all DBs
    all_rows = []
    for db_path, table_name, target_rowid, my_rowid in found_dbs:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(f"""
            SELECT local_type, real_sender_id, create_time,
                   message_content, WCDB_CT_message_content
            FROM [{table_name}]
            ORDER BY create_time ASC
        """).fetchall()
        for r in rows:
            sender_id = r[1]
            if sender_id == my_rowid:
                sender = "我"
            elif sender_id == target_rowid:
                sender = remark_name
            else:
                sender = f"未知({sender_id})"
            all_rows.append((r[0], sender, r[2], r[3], r[4]))
        conn.close()

    all_rows.sort(key=lambda r: r[2])

    # Determine output path and filter for incremental mode
    after_ts = 0
    if incremental:
        inc_dir = os.path.join(PROJECT_ROOT, "export", remark_name)
        os.makedirs(inc_dir, exist_ok=True)
        existing = sorted(globmod.glob(os.path.join(inc_dir, "output_*.txt")))
        if existing:
            # Read last timestamp from the last existing file
            with open(existing[-1], "r", encoding="utf-8") as f:
                for line in reversed(f.readlines()):
                    m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
                    if m:
                        after_ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                        break
        next_idx = len(existing)
        output_file = os.path.join(inc_dir, f"output_{next_idx}.txt")
    elif output_file is None:
        output_file = os.path.join(PROJECT_ROOT, "export", f"{remark_name}_chat.txt")

    lines = []
    for local_type, sender, ts, content, ct in all_rows:
        if ts <= after_ts:
            continue
        text = decompress_if_needed(content, ct)
        display = format_message(text, local_type)
        if display is None:
            continue
        time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{time_str}] {sender}: {display}")

    if not lines:
        print("No new messages to export.")
        return

    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")

    print(f"Exported {len(lines)} messages to {output_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export a WeChat chat to text file")
    parser.add_argument("contact", help="Contact name or partial match (e.g. 'ning')")
    parser.add_argument("-o", "--output", help="Output file path (default: export/<contact>_chat.txt)")
    parser.add_argument("-d", "--decrypted-dir", help="Path to decrypted databases directory")
    parser.add_argument("-i", "--incremental", action="store_true",
                        help="Incremental export to export/<contact>/output_N.txt")
    args = parser.parse_args()

    decrypted_dir = args.decrypted_dir or DEFAULT_DECRYPTED_DIR
    remark_name, wxid = resolve_contact(args.contact, decrypted_dir)
    if remark_name:
        export_chat(remark_name, args.output, args.decrypted_dir, args.incremental)
