#!/usr/bin/env python3
"""
从已解密的微信数据库导出单聊记录为文本文件。

用法:
    python export_chat.py <联系人>                 # 模糊匹配，多个结果时交互选择
    python export_chat.py <联系人> -o out.txt      # 指定输出文件
    python export_chat.py <联系人> -i              # 增量导出到 export/<联系人>.txt
    python export_chat.py --list [过滤词]          # 列出有单聊记录的联系人
    python export_chat.py --all                    # 增量导出全部单聊
    python export_chat.py <联系人> --since 2024-01-01 --until 2024-12-31

依赖:
    - 已解密的微信数据库 (decrypt_db.py)
    - pip install zstandard

结构（便于扩展群聊 / 其他输出格式）:
    load_contacts()     -> {username: 显示名}
    find_chat_tables()  -> 某联系人单聊所在的 (库, 表, rowid)
    list_chats()        -> 所有单聊的概要（消息数、最后消息时间）
    iter_messages()     -> 消息记录 dict 列表（与输出格式无关）
    format_text_line()  -> 文本格式的一行
    export_contact()    -> 编排：过滤、增量、写文件
"""
import argparse
import functools
import glob as globmod
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

import zstandard

from config import load_config

print = functools.partial(print, flush=True)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_cfg = load_config()
DEFAULT_DECRYPTED_DIR = _cfg["decrypted_dir"]
DEFAULT_EXPORT_DIR = os.path.join(PROJECT_ROOT, "export")
MY_WXID = _cfg["self_wxid"]

# 与 format_text_line 输出一致的行首时间戳
_LINE_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ")
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


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


# ---------------------------------------------------------------------------
# 联系人
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def load_contacts(decrypted_dir=None):
    """一次性读取 contact.db，返回 {username: 显示名}。

    优先级与旧版 get_remark 相同：按 sqlite_master 顺序遍历含 username 列
    和 remark/nickname 列的表，用户名首次出现的那张表决定结果，取第一个非空的
    remark，其次 nickname；都为空则为 None。"""
    decrypted_dir = decrypted_dir or DEFAULT_DECRYPTED_DIR
    contact_db = os.path.join(decrypted_dir, "contact", "contact.db")
    result = {}
    if not os.path.exists(contact_db):
        return result
    try:
        conn = sqlite3.connect(contact_db)
        tables = [t[0] for t in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info([{t}])").fetchall()]
            if "userName" not in cols and "username" not in cols:
                continue
            uname_col = "userName" if "userName" in cols else "username"
            remark_col = next((c for c in cols if c.lower() in ("remark", "conremark")), None)
            nick_col = next((c for c in cols if c.lower() in ("nickname", "nick_name", "connickname")), None)
            if not (remark_col or nick_col):
                continue
            select_cols = [c for c in (remark_col, nick_col) if c]
            for row in conn.execute(
                    f"SELECT {uname_col},{','.join(select_cols)} FROM [{t}]"):
                if row[0] in result:
                    continue
                name = None
                for val in row[1:]:
                    if val and val.strip():
                        name = val.strip()
                        break
                result[row[0]] = name
        conn.close()
    except Exception:
        pass
    return result


def get_remark(wxid, contact_db=None):
    """兼容旧接口：返回联系人显示名（使用缓存）。"""
    decrypted_dir = (os.path.dirname(os.path.dirname(contact_db))
                     if contact_db else DEFAULT_DECRYPTED_DIR)
    return load_contacts(decrypted_dir).get(wxid)


# ---------------------------------------------------------------------------
# 消息库
# ---------------------------------------------------------------------------

def message_db_paths(decrypted_dir=None):
    decrypted_dir = decrypted_dir or DEFAULT_DECRYPTED_DIR
    msg_dir = os.path.join(decrypted_dir, "message")
    if not os.path.isdir(msg_dir):
        return []
    return [os.path.join(msg_dir, f) for f in sorted(os.listdir(msg_dir))
            if f.startswith("message_") and f.endswith(".db") and "fts" not in f]


@functools.lru_cache(maxsize=None)
def _db_meta(db_path):
    """读取一次 Name2Id 和 Msg_ 表列表: (usernames 有序列表, {username: rowid}, [表名])。
    没有 Name2Id 的库返回 None。"""
    conn = sqlite3.connect(db_path)
    try:
        name2id = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except Exception:
        conn.close()
        return None
    tables = [t[0] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
    ).fetchall()]
    conn.close()
    rowids = {}
    for rowid, username in name2id:
        rowids[username] = rowid  # 与旧逻辑一致：重复时取最后一个
    return [u for _, u in name2id], rowids, tables


def _is_one_on_one(sender_counts, total, target_rowid, my_rowid):
    """单聊判定：对方有发言，且对方 + 自己的消息占 95% 以上。"""
    if not sender_counts.get(target_rowid):
        return False
    known = {target_rowid} | ({my_rowid} if my_rowid else set())
    main_cnt = sum(sender_counts.get(k, 0) for k in known)
    return main_cnt >= total * 0.95


def find_chat_tables(wxid, decrypted_dir=None):
    """查找某联系人的单聊表（可能跨多个库）。
    返回 [(db_path, table, target_rowid, my_rowid, total)]。"""
    found = []
    for db_path in message_db_paths(decrypted_dir):
        meta = _db_meta(db_path)
        if meta is None:
            continue
        _, rowids, tables = meta
        target_rowid = rowids.get(wxid)
        if target_rowid is None:
            continue
        my_rowid = rowids.get(MY_WXID)
        conn = sqlite3.connect(db_path)
        for t in tables:
            try:
                has = conn.execute(
                    f"SELECT 1 FROM [{t}] WHERE real_sender_id = ? LIMIT 1",
                    (target_rowid,)).fetchone()
                if not has:
                    continue
                counts = dict(conn.execute(
                    f"SELECT real_sender_id, count(*) FROM [{t}] GROUP BY real_sender_id"
                ).fetchall())
                total = sum(counts.values())
                if _is_one_on_one(counts, total, target_rowid, my_rowid):
                    found.append((db_path, t, target_rowid, my_rowid, total))
                    break
            except Exception:
                continue
        conn.close()
    return found


def list_chats(decrypted_dir=None, pattern=None):
    """列出所有有单聊记录的联系人（每个库每张表只扫描一次）。

    返回 [{"username", "name", "count", "last_ts", "tables"}]，按最后消息时间倒序。
    pattern 为不区分大小写的显示名子串过滤。"""
    contacts = load_contacts(decrypted_dir)
    pat = pattern.lower() if pattern else None
    chats = {}
    for db_path in message_db_paths(decrypted_dir):
        meta = _db_meta(db_path)
        if meta is None:
            continue
        usernames, rowids, tables = meta
        my_rowid = rowids.get(MY_WXID)
        candidates = {}
        for u in usernames:
            if u == MY_WXID or u in candidates:
                continue
            name = contacts.get(u)
            if not name or (pat and pat not in name.lower()):
                continue
            candidates[u] = rowids[u]
        if not candidates:
            continue
        conn = sqlite3.connect(db_path)
        stats = []
        for t in tables:
            try:
                rows = conn.execute(
                    f"SELECT real_sender_id, count(*), max(create_time) FROM [{t}] "
                    "GROUP BY real_sender_id").fetchall()
            except Exception:
                continue
            counts = {r[0]: r[1] for r in rows}
            last = max((r[2] or 0 for r in rows), default=0)
            stats.append((t, counts, sum(counts.values()), last))
        conn.close()
        for u, rowid in candidates.items():
            # 与 find_chat_tables 一致：每个库取第一张符合条件的表
            for t, counts, total, last in stats:
                if _is_one_on_one(counts, total, rowid, my_rowid):
                    c = chats.setdefault(u, {"username": u, "name": contacts[u],
                                             "count": 0, "last_ts": 0, "tables": []})
                    c["count"] += total
                    c["last_ts"] = max(c["last_ts"], last)
                    c["tables"].append((db_path, t, rowid, my_rowid, total))
                    break
    return sorted(chats.values(), key=lambda c: c["last_ts"], reverse=True)


def iter_messages(wxid, name, decrypted_dir=None, since=None, until=None, tables=None):
    """读取某联系人单聊的全部消息，返回按时间排序的记录列表。

    每条记录: {"ts", "sender", "is_self", "local_type", "text", "local_id"}
    text 为 format_message 处理后的显示文本；显示为空的消息已被跳过。
    since / until 为 Unix 时间戳（含 since，不含 until）。"""
    if tables is None:
        tables = find_chat_tables(wxid, decrypted_dir)
    where, params = [], []
    if since is not None:
        where.append("create_time >= ?")
        params.append(int(since))
    if until is not None:
        where.append("create_time < ?")
        params.append(int(until))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    records = []
    for db_path, table_name, target_rowid, my_rowid, _ in tables:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(f"""
            SELECT local_type, real_sender_id, create_time,
                   message_content, WCDB_CT_message_content, local_id
            FROM [{table_name}] {where_sql}
            ORDER BY create_time ASC, local_id ASC
        """, params).fetchall()
        conn.close()
        for local_type, sender_id, ts, content, ct, local_id in rows:
            display = format_message(decompress_if_needed(content, ct), local_type)
            if display is None:
                continue
            is_self = sender_id == my_rowid
            if is_self:
                sender = "我"
            elif sender_id == target_rowid:
                sender = name
            else:
                sender = f"未知({sender_id})"
            records.append({"ts": ts, "sender": sender, "is_self": is_self,
                            "local_type": local_type, "text": display,
                            "local_id": local_id})
    records.sort(key=lambda r: r["ts"])  # 稳定排序：同一秒内保持库顺序 + local_id 顺序
    return records


# ---------------------------------------------------------------------------
# 文本输出 / 增量
# ---------------------------------------------------------------------------

def format_text_line(rec):
    time_str = datetime.fromtimestamp(rec["ts"]).strftime(_TIME_FMT)
    return f"[{time_str}] {rec['sender']}: {rec['text']}"


def safe_filename(name, fallback="unnamed"):
    """去掉文件名中的非法字符（/ \\ : * ? \" < > | 及控制字符）。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", name or "")
    s = s.strip().strip(".").strip()
    return s[:150] or fallback


def contact_filename(wxid, name, decrypted_dir=None):
    """联系人对应的文件名主干；显示名与其他联系人重复时附加 wxid 以免互相覆盖。"""
    contacts = load_contacts(decrypted_dir)
    base = safe_filename(name, fallback=safe_filename(wxid))
    dup = sum(1 for n in contacts.values() if n == name) > 1
    return f"{base}_{safe_filename(wxid)}" if dup else base


def read_tail_state(path):
    """读取文本导出文件末尾状态: (最后时间戳, 该时间戳的消息条数)。没有消息返回 None。"""
    if not os.path.exists(path):
        return None
    last_str, n = None, 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = _LINE_TS_RE.match(line)
            if not m:
                continue  # 多行消息的续行
            if m.group(1) == last_str:
                n += 1
            else:
                last_str, n = m.group(1), 1
    if last_str is None:
        return None
    return datetime.strptime(last_str, _TIME_FMT).timestamp(), n


def _legacy_tail_state(export_dir, name):
    """旧版增量布局 export/<联系人>/output_N.txt 的末尾状态（取编号最大的有效文件）。"""
    legacy_dir = os.path.join(export_dir, name)
    files = globmod.glob(os.path.join(legacy_dir, "output_*.txt"))

    def idx(p):
        m = re.search(r"output_(\d+)\.txt$", p)
        return int(m.group(1)) if m else -1
    for p in sorted(files, key=idx, reverse=True):
        state = read_tail_state(p)
        if state:
            return state
    return None


def select_new(records, state):
    """根据文件末尾状态挑出需要追加的记录。
    时间戳相同（同一秒）的消息：文件中已有 n 条，则跳过该秒的前 n 条。"""
    if not state:
        return records
    last_ts, n_at_last = state
    out, seen_at_last = [], 0
    for r in records:
        if r["ts"] < last_ts:
            continue
        if r["ts"] == last_ts:
            seen_at_last += 1
            if seen_at_last <= n_at_last:
                continue
        out.append(r)
    return out


def _append_lines(path, lines):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    needs_nl = False
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_nl = f.read(1) != b"\n"
    with open(path, "a", encoding="utf-8") as f:
        if needs_nl:
            f.write("\n")
        f.write("\n".join(lines))
        f.write("\n")


def export_contact(wxid, name, output_file=None, decrypted_dir=None, incremental=False,
                   since=None, until=None, export_dir=None, tables=None, quiet=False):
    """导出一个联系人的单聊。返回写入的消息条数（找不到聊天返回 None）。"""
    log = (lambda *a: None) if quiet else print
    export_dir = export_dir or DEFAULT_EXPORT_DIR

    if tables is None:
        tables = find_chat_tables(wxid, decrypted_dir)
        for db_path, t, _, _, total in tables:
            log(f"[+] 找到: {os.path.basename(db_path)} / {t} ({total} 条)")
    if not tables:
        print(f"[!] 未找到与「{name}」的单聊记录")
        return None
    log(f"[+] 聊天记录分布在 {len(tables)} 个数据库中")

    records = iter_messages(wxid, name, decrypted_dir, since, until, tables)

    if incremental:
        default_path = output_file is None
        if default_path:
            fname = contact_filename(wxid, name, decrypted_dir)
            output_file = os.path.join(export_dir, f"{fname}.txt")
        state = read_tail_state(output_file)
        # 新文件还不存在时，沿用旧版布局的进度，避免重复导出（显示名重名时旧目录归属不明，跳过）
        if state is None and default_path and fname == safe_filename(name, fallback=safe_filename(wxid)):
            state = _legacy_tail_state(export_dir, name)
            if state:
                log(f"[+] 检测到旧版导出目录，从 {datetime.fromtimestamp(state[0]).strftime(_TIME_FMT)} 之后继续")
        records = select_new(records, state)
    elif output_file is None:
        fname = contact_filename(wxid, name, decrypted_dir)
        output_file = os.path.join(export_dir, f"{fname}_chat.txt")

    lines = [format_text_line(r) for r in records]
    if not lines:
        log(f"[+] 没有新消息需要导出: {output_file}" if incremental else "[!] 没有可导出的消息")
        return 0

    if incremental:
        _append_lines(output_file, lines)
        log(f"[+] 追加 {len(lines)} 条消息到 {output_file}")
    else:
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.write("\n")
        log(f"[+] 已导出 {len(lines)} 条消息到 {output_file}")
    return len(lines)


def export_chat(remark_name, output_file=None, decrypted_dir=None, incremental=False,
                wxid=None, **kwargs):
    """兼容旧接口：按显示名导出（优先使用传入的 wxid）。"""
    if wxid is None:
        wxid = next((u for u, _ in find_contacts(remark_name, decrypted_dir)
                     if load_contacts(decrypted_dir).get(u) == remark_name), None)
        if wxid is None:
            print(f"[!] 未找到联系人「{remark_name}」")
            return None
    return export_contact(wxid, remark_name, output_file, decrypted_dir, incremental, **kwargs)


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

def find_contacts(pattern, decrypted_dir=None):
    """在消息库 Name2Id 中出现过的联系人里，按显示名做不区分大小写的子串匹配。
    返回 [(显示名, wxid)]，顺序与旧版相同。"""
    contacts = load_contacts(decrypted_dir)
    pat_lower = pattern.lower()
    seen, matches = set(), []
    for db_path in message_db_paths(decrypted_dir):
        meta = _db_meta(db_path)
        if meta is None:
            continue
        for username in meta[0]:
            if username in seen:
                continue
            seen.add(username)
            r = contacts.get(username)
            if r and pat_lower in r.lower():
                matches.append((r, username))
    return matches


def resolve_contact(pattern, decrypted_dir=None):
    """把输入解析为 (显示名, wxid)。精确匹配优先，其次模糊匹配；多个结果时交互选择。"""
    matches = find_contacts(pattern, decrypted_dir)
    if not matches:
        print(f"[!] 未找到匹配「{pattern}」的联系人")
        return None, None

    exact = [(r, w) for r, w in matches if r == pattern]
    if len(exact) == 1:
        return exact[0]

    if len(matches) == 1:
        print(f"[+] 匹配到: {matches[0][0]}")
        return matches[0]

    print(f"[+] 找到 {len(matches)} 个匹配「{pattern}」的联系人:")
    shown = matches[:5]
    for i, (remark, wxid) in enumerate(shown, 1):
        print(f"  {i}. {remark}")
    if len(matches) > 5:
        print(f"  ... 另有 {len(matches) - 5} 个（请使用更精确的关键词）")

    while True:
        try:
            choice = input(f"请选择 [1-{len(shown)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(shown):
                return shown[idx]
        except ValueError:
            pass
        except (EOFError, KeyboardInterrupt):
            print()
            return None, None
        print(f"请输入 1 到 {len(shown)} 之间的数字")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD: {s}")


def cmd_list(pattern, decrypted_dir):
    chats = list_chats(decrypted_dir, pattern)
    if not chats:
        print("[!] 没有找到单聊记录" + (f"（过滤: {pattern}）" if pattern else ""))
        return
    print(f"{'最后消息':<12}{'消息数':>8}  联系人")
    for c in chats:
        day = datetime.fromtimestamp(c["last_ts"]).strftime("%Y-%m-%d") if c["last_ts"] else "-"
        print(f"{day:<12}{c['count']:>10}  {c['name']}")
    print(f"[+] 共 {len(chats)} 个单聊")


def cmd_all(pattern, decrypted_dir, export_dir, since, until):
    chats = list_chats(decrypted_dir, pattern)
    if not chats:
        print("[!] 没有找到单聊记录")
        return
    print(f"[+] 共 {len(chats)} 个单聊，增量导出到 {export_dir}")
    total_new = changed = 0
    for i, c in enumerate(chats, 1):
        n = export_contact(c["username"], c["name"], None, decrypted_dir, incremental=True,
                           since=since, until=until, export_dir=export_dir,
                           tables=c["tables"], quiet=True)
        if n:
            changed += 1
            total_new += n
            print(f"  [{i}/{len(chats)}] {c['name']}: +{n}")
    print(f"[+] 完成: {changed} 个联系人有新消息，共追加 {total_new} 条")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="导出微信单聊记录为文本文件",
        epilog="示例: python export_chat.py 张三 -i --since 2024-01-01")
    parser.add_argument("contact", nargs="?",
                        help="联系人备注/昵称（支持部分匹配）；配合 --all 时作为过滤词")
    parser.add_argument("-o", "--output", help="输出文件路径（默认 export/<联系人>_chat.txt，增量模式默认 export/<联系人>.txt）")
    parser.add_argument("-d", "--decrypted-dir", help="已解密数据库目录")
    parser.add_argument("-i", "--incremental", action="store_true",
                        help="增量导出：追加到 export/<联系人>.txt，只写入比文件中最后一条更新的消息")
    parser.add_argument("--list", nargs="?", const="", metavar="过滤词", dest="list_filter",
                        help="列出有单聊记录的联系人（消息数、最后消息日期），可选按名称过滤")
    parser.add_argument("--all", action="store_true",
                        help="增量导出所有单聊到 export/<联系人>.txt")
    parser.add_argument("--since", type=_parse_date, metavar="YYYY-MM-DD",
                        help="只导出该日期（含）之后的消息")
    parser.add_argument("--until", type=_parse_date, metavar="YYYY-MM-DD",
                        help="只导出该日期（含）之前的消息")
    parser.add_argument("--export-dir", help="导出目录（默认: 项目下的 export/）")
    parser.add_argument("--no-decrypt", action="store_true", help="跳过自动解密，直接使用现有解密数据")
    args = parser.parse_args(argv)

    if args.list_filter is None and not args.all and not args.contact:
        parser.error("请指定联系人，或使用 --list / --all")
    if args.all and args.output:
        parser.error("--all 不能与 -o 同时使用（请用 --export-dir）")

    since = args.since.timestamp() if args.since else None
    until = (args.until + timedelta(days=1)).timestamp() if args.until else None
    if since is not None and until is not None and since >= until:
        parser.error("--since 不能晚于 --until")

    if not args.no_decrypt:
        # 自动解密（跳过未变化的数据库）
        from decrypt_db import main as decrypt_main
        decrypt_main()

    decrypted_dir = args.decrypted_dir or DEFAULT_DECRYPTED_DIR
    export_dir = os.path.abspath(args.export_dir) if args.export_dir else DEFAULT_EXPORT_DIR
    if not message_db_paths(decrypted_dir):
        print(f"[!] 未找到消息数据库目录: {os.path.join(decrypted_dir, 'message')}")
        return 1

    if args.list_filter is not None:
        cmd_list(args.list_filter or args.contact, decrypted_dir)
        return 0
    if args.all:
        cmd_all(args.contact, decrypted_dir, export_dir, since, until)
        return 0

    remark_name, wxid = resolve_contact(args.contact, decrypted_dir)
    if not remark_name:
        return 1
    print(f"[+] 目标 wxid: {wxid}")
    export_contact(wxid, remark_name, args.output, decrypted_dir, args.incremental,
                   since, until, export_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
