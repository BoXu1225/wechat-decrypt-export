#!/usr/bin/env python3
"""
从已解密的微信数据库导出聊天记录（单聊和群聊）。

用法:
    python export_chat.py <联系人/群名>                  # 模糊匹配，多个结果时交互选择
    python export_chat.py <联系人> -o out.txt           # 指定输出文件
    python export_chat.py <联系人> -i                   # 增量导出到 export/<名称>.<扩展名>
    python export_chat.py <联系人> --format html        # txt / md / html / json / csv
    python export_chat.py --list [过滤词] [--type group] # 列出聊天（名称、类型、消息数、最后日期）
    python export_chat.py --all [--type single]         # 增量导出全部聊天
    python export_chat.py <联系人> --since 2024-01-01 --until 2024-12-31
    python export_chat.py <联系人> -f html --media      # 图片 + 可播放的语音 / 视频

依赖:
    - 已解密的微信数据库 (decrypt_db.py)
    - pip install -r requirements.txt

结构:
    chats.py        聊天发现、联系人、消息读取（与输出格式无关的记录）
    formatters.py   txt / md / html / json / csv 写入
    本文件           命令行、选择聊天、增量逻辑、文件命名
"""
import argparse
import csv
import functools
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timedelta

import chats as chatlib
import formatters

print = functools.partial(print, flush=True)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXPORT_DIR = os.path.join(PROJECT_ROOT, "export")
FORMATS = list(formatters.FORMATS)
CHAT_TYPES = ("all", "single", "group")

# 与 formatters.write_txt 输出一致的行首时间戳
_LINE_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ")
_TIME_FMT = formatters.TIME_FMT
# Markdown 增量状态（写在文件末尾的 HTML 注释，渲染时不可见；消息中的 "<" 已转义，无法伪造）
_MD_STATE_RE = re.compile(r"^<!-- wechat-export: last=(\d+) n=(\d+) -->\s*$")
# 记录中不写入输出文件的内部字段
_INTERNAL_KEYS = ("packed_info_data", "media_duration")


@functools.lru_cache(maxsize=None)
def get_config():
    from config import load_config
    return load_config()


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

class ChatSource:
    """对 chats.py 的缓存封装：联系人、聊天列表、群昵称只读取一次。"""

    def __init__(self, decrypted_dir, self_wxid):
        self.decrypted_dir = decrypted_dir
        self.self_wxid = self_wxid
        self.contacts = chatlib.load_contacts(decrypted_dir)
        self._chats = {}
        self._room_members = None
        self._group_nicknames = None

    def chats(self, include_system=False):
        """list_chats 结果；没有群名的群按成员命名（与 MCP 服务一致）。"""
        if include_system not in self._chats:
            self._chats[include_system] = chatlib.name_unnamed_groups(
                chatlib.list_chats(self.decrypted_dir, include_system=include_system,
                                   contacts=self.contacts),
                self.room_members, self.contacts, self.self_wxid)
        return self._chats[include_system]

    @property
    def room_members(self):
        if self._room_members is None:
            self._room_members = chatlib.load_room_members(self.decrypted_dir)
        return self._room_members

    @property
    def group_nicknames(self):
        if self._group_nicknames is None:
            self._group_nicknames = chatlib.group_nicknames_from_members(self.room_members)
        return self._group_nicknames

    def messages(self, chat, since=None, until=None, with_packed_info=False):
        """聊天的全部消息记录（按时间排序）；since / until 为 Unix 时间戳（含 since，不含 until）。"""
        recs = chatlib.iter_messages(chat, self.decrypted_dir, self.self_wxid, self.contacts,
                                     self.group_nicknames if chat["is_group"] else None,
                                     with_packed_info=with_packed_info)
        return [r for r in recs
                if (since is None or r["ts"] >= since) and (until is None or r["ts"] < until)]


def filter_chats(chats, pattern=None, chat_type="all"):
    """按类型和名称/ID 子串过滤，保持原有顺序（最后消息时间倒序）。"""
    if chat_type == "single":
        chats = [c for c in chats if not c["is_group"]]
    elif chat_type == "group":
        chats = [c for c in chats if c["is_group"]]
    if pattern:
        keep = {c["username"] for c in chatlib.find_chats(pattern, chats)}
        chats = [c for c in chats if c["username"] in keep]
    return chats


def chat_label(chat):
    return f"{chat['name']} [群]" if chat["is_group"] else chat["name"]


# ---------------------------------------------------------------------------
# 文件命名
# ---------------------------------------------------------------------------

def safe_filename(name, fallback="unnamed"):
    """去掉文件名中的非法字符（/ \\ : * ? \" < > | 及控制字符）。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", name or "")
    s = s.strip().strip(".").strip()
    return s[:150] or fallback


def chat_filename(chat, contacts):
    """聊天对应的文件名主干；显示名与其他联系人重复时附加 wxid 以免互相覆盖。"""
    name, username = chat["name"], chat["username"]
    base = safe_filename(name, fallback=safe_filename(username))
    dup = sum(1 for n in contacts.values() if n == name) > 1
    return f"{base}_{safe_filename(username)}" if dup else base


def default_output_path(export_dir, fname, fmt, incremental):
    """增量 / --all: export/<名称>.<扩展名>；普通导出: export/<名称>_chat.<扩展名>。"""
    suffix = "" if incremental else "_chat"
    return os.path.join(export_dir, f"{fname}{suffix}{formatters.ext_for(fmt)}")


# ---------------------------------------------------------------------------
# 增量状态
# ---------------------------------------------------------------------------

def _state_from_times(times):
    """times: 按顺序的时间字符串 -> (最后时间戳, 该时间戳的条数)。"""
    last_str, n = None, 0
    for t in times:
        if t == last_str:
            n += 1
        else:
            last_str, n = t, 1
    if last_str is None:
        return None
    return datetime.strptime(last_str, _TIME_FMT).timestamp(), n


def _txt_times(path):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = _LINE_TS_RE.match(line)
            if m:  # 否则是多行消息的续行
                yield m.group(1)


def _csv_times(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.reader(f)):
            if i == 0 and row and row[0] == "time":
                continue
            if row:
                yield row[0]


def read_md_state(path):
    """Markdown 文件末尾的状态注释 -> (最后时间戳, 条数)；没有返回 None。"""
    last = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = _MD_STATE_RE.match(line)
            if m:
                last = (int(m.group(1)), int(m.group(2)))
    return last


def read_tail_state(path, fmt="txt"):
    """读取导出文件末尾状态: (最后时间戳, 该时间戳的消息条数)。没有消息返回 None。"""
    if not os.path.exists(path):
        return None
    if fmt == "txt":
        return _state_from_times(_txt_times(path))
    if fmt == "csv":
        return _state_from_times(_csv_times(path))
    if fmt == "md":
        return read_md_state(path)
    raise ValueError(f"{fmt} 不支持追加")


def tail_state_of(records):
    """记录列表的末尾状态（与 read_tail_state 同义，精确到秒）。"""
    if not records:
        return None
    last = int(records[-1]["ts"])
    return last, sum(1 for r in records if int(r["ts"]) == last)


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


def _strip_md_state(path):
    """去掉 Markdown 文件末尾的状态注释行，便于继续追加。"""
    with open(path, "rb+") as f:
        data = f.read()
        idx = data.rfind(b"<!-- wechat-export:")
        if idx >= 0 and (idx == 0 or data[idx - 1:idx] == b"\n") \
                and b"\n" not in data[idx:].rstrip(b"\n"):
            f.truncate(idx)


def _write_md_state(path, state):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"<!-- wechat-export: last={int(state[0])} n={state[1]} -->\n")


def _ensure_trailing_newline(path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb+") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")


def _count_rendered(path, fmt):
    """已有 html/json 文件中的消息条数（用于报告新增条数）。"""
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            if fmt == "json":
                import json
                return len(json.load(f).get("messages", []))
            s = f.read()
            return s.count('<div class="msg') + s.count('<div class="sys"')
    except Exception:
        return 0


def _clean(records):
    for r in records:
        for k in _INTERNAL_KEYS:
            r.pop(k, None)
    return records


def _render_replace(records, meta, path, fmt):
    """重新生成整个文件；内容未变化时不改动文件。返回是否写入。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=formatters.ext_for(fmt), dir=d)
    os.close(fd)
    try:
        formatters.write(records, meta, tmp, fmt)
        if os.path.exists(path):
            with open(path, "rb") as a, open(tmp, "rb") as b:
                if a.read() == b.read():
                    return False
        os.replace(tmp, path)
        return True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# 旧版增量布局迁移: export/<联系人>/output_N.txt -> export/<联系人>.txt
# ---------------------------------------------------------------------------

def legacy_files(export_dir, name):
    """旧版增量目录中的 output_N.txt，按编号排序。"""
    legacy_dir = os.path.join(export_dir, name)
    if not os.path.isdir(legacy_dir):
        return []
    found = []
    for f in os.listdir(legacy_dir):
        m = re.fullmatch(r"output_(\d+)\.txt", f)
        if m:
            found.append((int(m.group(1)), os.path.join(legacy_dir, f)))
    return [p for _, p in sorted(found)]


def migrate_legacy(files, path):
    """把旧版文件内容按顺序写入新文件（新文件此前不存在）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as out:
        for p in files:
            with open(p, "rb") as f:
                data = f.read()
            if not data:
                continue
            out.write(data)
            if not data.endswith(b"\n"):
                out.write(b"\n")


# ---------------------------------------------------------------------------
# 图片
# ---------------------------------------------------------------------------

class ImageExporter:
    """把图片消息解码到 <名称>_files/<md5>.<扩展名>，并设置 record["image_path"]。

    已解码的文件直接复用（增量友好）；只有缩略图时保存为 <md5>_t.<扩展名>，
    以后原图下载到本地会再解码原图。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.base_dir = cfg.get("wechat_base_dir") or ""
        self._keys = None
        self.decoded = self.thumb = self.existing = self.missing = self.failed = 0

    def keys(self):
        if self._keys is None:
            import image_decode
            try:
                self._keys = image_decode.get_image_keys(self.cfg)
            except Exception:
                self._keys = (None, None)
            if self._keys[0] is None:
                print("[!] 未能获取图片解密密钥（V2 格式图片将无法解码）；"
                      "可在 config.json 中设置 image_aes_key / image_xor_key")
        return self._keys

    @staticmethod
    def _existing(files_dir, stem):
        if not os.path.isdir(files_dir):
            return None
        for ext in ("jpg", "png", "gif", "webp", "bmp", "tif", "heic", "wxgf"):
            p = os.path.join(files_dir, f"{stem}.{ext}")
            if os.path.exists(p):
                return p
        return None

    @classmethod
    def attach_existing(cls, records, files_dir):
        """只引用已解码的图片（未指定 --images 时，保持之前导出的图片引用）。"""
        import image_decode
        for r in records:
            if r.get("kind") == "image":
                md5 = image_decode.image_md5_from_packed_info(r.get("packed_info_data"))
                if md5:
                    p = cls._existing(files_dir, md5) or cls._existing(files_dir, md5 + "_t")
                    if p:
                        r["image_path"] = p

    def attach(self, chat, records, files_dir):
        """为 records 中的图片消息设置 image_path（就地修改）。"""
        import image_decode
        for r in records:
            if r.get("kind") != "image":
                continue
            md5 = image_decode.image_md5_from_packed_info(r.get("packed_info_data"))
            if not md5:
                self.missing += 1
                continue
            full = self._existing(files_dir, md5)
            if full:
                r["image_path"] = full
                self.existing += 1
                continue
            thumb = self._existing(files_dir, md5 + "_t")
            dat = image_decode.find_image_for_message(
                self.base_dir, chat["username"], md5=md5, create_time=r.get("create_time"),
                prefer=("_h", "") if thumb else ("_h", "", "_t"))
            if not dat:
                if thumb:
                    r["image_path"] = thumb
                    self.existing += 1
                else:
                    self.missing += 1
                continue
            is_thumb = dat.endswith("_t.dat")
            aes_key, xor_key = self.keys()
            try:
                data, ext = image_decode.decode_dat(dat, aes_key, xor_key)
            except Exception:
                self.failed += 1
                if thumb:
                    r["image_path"] = thumb
                continue
            os.makedirs(files_dir, exist_ok=True)
            out = os.path.join(files_dir, f"{md5}{'_t' if is_thumb else ''}.{ext}")
            with open(out, "wb") as f:
                f.write(data)
            if thumb and not is_thumb:
                os.remove(thumb)  # 原图替换缩略图
            r["image_path"] = out
            self.decoded += 1
            if is_thumb:
                self.thumb += 1

    def summary(self):
        total = self.decoded + self.existing + self.missing + self.failed
        if not total:
            return
        print(f"[+] 图片: 新解码 {self.decoded}（其中仅缩略图 {self.thumb}），"
              f"已存在 {self.existing}，缺失 {self.missing}，解码失败 {self.failed}")
        if self.missing or self.failed:
            print(f"[!] {self.missing + self.failed} 张图片未能导出（本地没有文件或无法解码），"
                  "输出中显示为 [图片]")


# ---------------------------------------------------------------------------
# 语音 / 视频
# ---------------------------------------------------------------------------

class MediaExporter:
    """语音转换为 <名称>_files/<server_id>.m4a，视频复制为 <md5>.mp4（封面 <md5>_thumb.jpg），
    并设置 record 的 audio_path / video_path / poster_path / duration（消息 XML 中的时长，
    没有时取文件时长）。

    已存在的文件直接复用（增量友好）。voice / video=False 时该类型只引用已有文件。"""

    def __init__(self, cfg, voice=True, video=True):
        self.cfg = cfg or {}
        self.voice, self.video = voice, video
        self.base_dir = self.cfg.get("wechat_base_dir") or ""
        self.decrypted_dir = self.cfg.get("decrypted_dir") or ""
        self.v_new = self.v_existing = self.v_missing = self.v_failed = 0
        self.m_new = self.m_existing = self.m_poster = self.m_missing = 0
        self.seconds = 0.0

    @classmethod
    def attach_existing(cls, records, files_dir):
        """只引用已导出的语音 / 视频（未指定 --media / --voice 时保留之前的引用）。"""
        cls(None, voice=False, video=False).attach(None, records, files_dir)

    def attach(self, chat, records, files_dir):
        import time as _time
        import media_decode as M
        t0 = _time.time()
        jobs = []
        for r in records:
            kind = r.get("kind")
            dur = r.get("media_duration")
            if kind in ("voice", "video") and dur is not None:
                r["duration"] = M.seconds(dur)  # stated length from the message XML
            if kind == "voice":
                stem = os.path.join(files_dir, M.voice_stem(
                    r.get("server_id"), r.get("create_time"), r.get("local_id")))
                path = M.existing_audio(stem)
                if path:
                    r["audio_path"] = path
                    if dur is None:
                        r["duration"] = M.seconds(M.audio_file_duration(path))
                    self.v_existing += self.voice
                elif self.voice:
                    jobs.append((r, stem))
            elif kind == "video":
                self._attach_video(M, r, files_dir, dur)
        if jobs:
            self._convert_voices(M, chat, jobs)
        if self.voice or self.video:
            self.seconds += _time.time() - t0

    def _attach_video(self, M, r, files_dir, dur):
        md5 = M.md5_from_packed_info(r.get("packed_info_data"))
        if not md5:
            self.m_missing += self.video
            return
        mp4 = os.path.join(files_dir, f"{md5}.mp4")
        thumb = os.path.join(files_dir, f"{md5}_thumb.jpg")
        have_mp4, have_thumb = os.path.exists(mp4), os.path.exists(thumb)
        if self.video and have_mp4:
            self.m_existing += 1
        elif self.video:
            src, src_thumb = M.find_video_for_message(self.base_dir, md5=md5,
                                                      create_time=r.get("create_time"))
            if src_thumb and not have_thumb:
                have_thumb = _try_copy(M, src_thumb, thumb)
            if src and _try_copy(M, src, mp4):
                have_mp4 = True
                self.m_new += 1
            elif have_thumb:
                self.m_poster += 1
            else:
                self.m_missing += 1
        if have_thumb:
            r["poster_path"] = thumb
        if have_mp4:
            r["video_path"] = mp4
            if dur is None:
                r["duration"] = M.seconds(M.mp4_duration(mp4))

    def _convert_voices(self, M, chat, jobs):
        from concurrent.futures import ThreadPoolExecutor
        todo = []
        with M.VoiceStore(self.decrypted_dir) as store:
            for r, stem in jobs:
                data = store.get(chat["username"], r.get("server_id"), r.get("create_time"),
                                 r.get("local_id"))
                if data is None:
                    self.v_missing += 1
                else:
                    todo.append((r, stem, data))
        if not todo:
            return
        os.makedirs(os.path.dirname(todo[0][1]), exist_ok=True)

        def convert(job):
            r, stem, data = job
            try:
                return r, M.voice_to_file(data, stem)
            except (M.MediaDecodeError, OSError):
                return r, None

        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
            for r, res in pool.map(convert, todo):
                if res is None:
                    self.v_failed += 1
                    continue
                path, dur = res
                r["audio_path"] = path
                r.setdefault("duration", M.seconds(dur))
                self.v_new += 1

    def summary(self):
        if self.voice and (self.v_new + self.v_existing + self.v_missing + self.v_failed):
            print(f"[+] 语音: 新转换 {self.v_new}，已存在 {self.v_existing}，"
                  f"缺失 {self.v_missing}，转换失败 {self.v_failed}")
            if self.v_new and not _has_afconvert():
                print("[!] 未找到 afconvert，语音保存为 WAV")
        if self.video and (self.m_new + self.m_existing + self.m_poster + self.m_missing):
            print(f"[+] 视频: 新复制 {self.m_new}，已存在 {self.m_existing}，"
                  f"仅封面 {self.m_poster}（视频未下载），缺失 {self.m_missing}")
        if self.v_new or self.m_new:
            print(f"[+] 语音/视频处理用时 {self.seconds:.1f} 秒")


def _try_copy(M, src, dst):
    try:
        M.copy_file(src, dst)
        return True
    except OSError:
        return False


def _has_afconvert():
    import media_decode
    return media_decode.afconvert_path() is not None


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def export_one(src, chat, fmt="txt", output_file=None, incremental=False, since=None,
               until=None, export_dir=None, quiet=False, images=None, media=None):
    """导出一个聊天。返回写入（新增）的消息条数；html/json 增量模式下为新增条数。

    images: ImageExporter 实例时解码图片到 <名称>_files/ 并在 md/html/json 中引用。
    media: MediaExporter 实例时导出语音 / 视频到 <名称>_files/。"""
    log = (lambda *a: None) if quiet else print
    export_dir = export_dir or DEFAULT_EXPORT_DIR
    fname = chat_filename(chat, src.contacts)
    default_path = output_file is None
    path = output_file or default_output_path(export_dir, fname, fmt, incremental)
    files_dir = os.path.join(os.path.dirname(os.path.abspath(path)), f"{fname}_files")
    meta = {"name": chat["name"], "is_group": chat["is_group"]}

    embeds = fmt in ("md", "html", "json") and os.path.isdir(files_dir)
    reuse_images = images is None and embeds
    reuse_media = media is None and embeds

    def with_images(recs):
        if images is not None:
            images.attach(chat, recs, files_dir)
        elif reuse_images:
            ImageExporter.attach_existing(recs, files_dir)
        if media is not None:
            media.attach(chat, recs, files_dir)
        elif reuse_media:
            MediaExporter.attach_existing(recs, files_dir)
        return _clean(recs)

    records = src.messages(chat, since, until,
                           with_packed_info=bool(images or media or reuse_images or reuse_media))

    if not incremental:
        if not records:
            log("[!] 没有可导出的消息")
            return 0
        formatters.write(with_images(records), meta, path, fmt)
        log(f"[+] 已导出 {len(records)} 条消息到 {path}")
        return len(records)

    if not formatters.supports_append(fmt):
        # html / json：从全部消息重新生成整个文件
        before = _count_rendered(path, fmt)
        if not records and not os.path.exists(path):
            log(f"[+] 没有新消息需要导出: {path}")
            return 0
        changed = _render_replace(with_images(records), meta, path, fmt)
        if not changed:
            log(f"[+] 没有新消息需要导出: {path}")
            return 0
        added = max(len(records) - before, 0)
        log(f"[+] 已重新生成 {path}（共 {len(records)} 条，新增 {added} 条）")
        return added

    if fmt == "txt" and default_path and not os.path.exists(path) \
            and fname == safe_filename(chat["name"], fallback=safe_filename(chat["username"])):
        # 旧版布局 export/<联系人>/output_N.txt：先把旧内容写入新文件，再追加新消息
        # （显示名重名时旧目录归属不明，跳过）
        old_files = legacy_files(export_dir, chat["name"])
        if old_files:
            migrate_legacy(old_files, path)
            print(f"[+] 已将旧版导出 {os.path.join(export_dir, chat['name'])}/output_*.txt"
                  f"（{len(old_files)} 个文件）合并到 {path}")
            print(f"[+] 旧目录已不再使用，确认无误后可以删除: {os.path.join(export_dir, chat['name'])}")

    exists = os.path.exists(path) and os.path.getsize(path) > 0
    state = read_tail_state(path, fmt) if exists else None
    if fmt == "md" and exists and state is None:
        # 没有状态注释（不是本工具增量生成的文件）：重新生成
        log(f"[!] {path} 缺少增量状态，将重新生成")
        new = records
        formatters.write(with_images(new), meta, path, fmt)
    else:
        new = select_new(records, state)
        if not new:
            log(f"[+] 没有新消息需要导出: {path}")
            return 0
        if fmt == "md" and exists:
            _strip_md_state(path)
        if fmt == "txt":
            _ensure_trailing_newline(path)
        formatters.write(with_images(new), meta, path, fmt, append=exists)
    if fmt == "md":
        st = tail_state_of(records) if new is records else _merge_state(state, new)
        if st:
            _write_md_state(path, st)
    log(f"[+] 追加 {len(new)} 条消息到 {path}")
    return len(new)


def _merge_state(state, new):
    """追加 new 之后的末尾状态。"""
    st = tail_state_of(new)
    if state and st and int(state[0]) == st[0]:
        return st[0], state[1] + st[1]
    return st


# ---------------------------------------------------------------------------
# 选择聊天
# ---------------------------------------------------------------------------

def resolve_chat(src, pattern, chat_type="all"):
    """把输入解析为聊天。精确匹配优先，其次模糊匹配；多个结果时交互选择。
    先在普通聊天中查找，找不到时再包括公众号等系统账号。"""
    matches = []
    for include_system in (False, True):
        matches = chatlib.find_chats(pattern, filter_chats(src.chats(include_system),
                                                           chat_type=chat_type))
        if matches:
            break
    if not matches:
        print(f"[!] 未找到匹配「{pattern}」的聊天")
        return None

    exact = [c for c in matches if c["name"] == pattern or c["username"] == pattern]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        print(f"[+] 匹配到: {chat_label(matches[0])}")
        return matches[0]

    print(f"[+] 找到 {len(matches)} 个匹配「{pattern}」的聊天:")
    shown = matches[:5]
    for i, c in enumerate(shown, 1):
        print(f"  {i}. {chat_label(c)}")
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
            return None
        print(f"请输入 1 到 {len(shown)} 之间的数字")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s, width):
    """按显示宽度（中文占 2 列）截断并补齐。"""
    out, w = "", 0
    for ch in s:
        cw = _width(ch)
        if w + cw > width:
            if out:
                while _width(out) > width - 1:
                    out = out[:-1]
                out += "…"
            break
        out += ch
        w += cw
    return out + " " * (width - _width(out))


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD: {s}")


def _type_desc(chat_type):
    return {"all": "聊天", "single": "单聊", "group": "群聊"}[chat_type]


def cmd_list(src, pattern, chat_type="all"):
    chats = filter_chats(src.chats(), pattern, chat_type)
    if not chats:
        print(f"[!] 没有找到{_type_desc(chat_type)}记录" + (f"（过滤: {pattern}）" if pattern else ""))
        return
    name_w = min(max(_width(c["name"]) for c in chats), 40)
    name_w = max(name_w, 4)
    print(f"{_pad('名称', name_w)}  类型  {'消息数':>7}  最后消息")
    for c in chats:
        day = datetime.fromtimestamp(c["last_ts"]).strftime("%Y-%m-%d") if c["last_ts"] else "-"
        kind = "[群]" if c["is_group"] else "[单]"
        print(f"{_pad(c['name'], name_w)}  {kind}  {c['msg_count']:>10}  {day}")
    n_group = sum(1 for c in chats if c["is_group"])
    print(f"[+] 共 {len(chats)} 个聊天（单聊 {len(chats) - n_group}，群聊 {n_group}）")


def cmd_all(src, pattern, export_dir, since, until, fmt="txt", chat_type="all", images=None,
            media=None):
    chats = filter_chats(src.chats(), pattern, chat_type)
    if not chats:
        print(f"[!] 没有找到{_type_desc(chat_type)}记录")
        return
    n_group = sum(1 for c in chats if c["is_group"])
    print(f"[+] 共 {len(chats)} 个聊天（单聊 {len(chats) - n_group}，群聊 {n_group}），"
          f"增量导出到 {export_dir}")
    total_new = changed = 0
    for i, c in enumerate(chats, 1):
        n = export_one(src, c, fmt, None, incremental=True, since=since, until=until,
                       export_dir=export_dir, quiet=True, images=images, media=media)
        if n:
            changed += 1
            total_new += n
            print(f"  [{i}/{len(chats)}] {chat_label(c)}: +{n}")
    print(f"[+] 完成: {changed} 个聊天有新消息，共新增 {total_new} 条")


def build_parser():
    parser = argparse.ArgumentParser(
        description="导出微信聊天记录（单聊和群聊）为 txt / md / html / json / csv",
        epilog="示例: python export_chat.py 张三 -i --since 2024-01-01")
    parser.add_argument("contact", nargs="?",
                        help="联系人备注/昵称或群名（支持部分匹配）；配合 --list / --all 时作为过滤词")
    parser.add_argument("-o", "--output",
                        help="输出文件路径（默认 export/<名称>_chat.<扩展名>，增量模式默认 export/<名称>.<扩展名>）")
    parser.add_argument("-d", "--decrypted-dir", help="已解密数据库目录")
    parser.add_argument("-i", "--incremental", action="store_true",
                        help="增量导出到 export/<名称>.<扩展名>：txt/md/csv 只追加新消息，html/json 重新生成")
    parser.add_argument("-f", "--format", choices=FORMATS, default="txt",
                        help="输出格式（默认 txt）")
    parser.add_argument("--list", nargs="?", const="", metavar="过滤词", dest="list_filter",
                        help="列出聊天（名称、类型、消息数、最后消息日期），可选按名称过滤")
    parser.add_argument("--all", action="store_true",
                        help="增量导出所有聊天到 export/<名称>.<扩展名>")
    parser.add_argument("--type", choices=CHAT_TYPES, default="all", dest="chat_type",
                        help="聊天类型过滤: all / single（单聊）/ group（群聊），默认 all")
    parser.add_argument("--images", action="store_true",
                        help="解码图片到 export/<名称>_files/，md/html/json 中直接引用（txt/csv 仍显示 [图片]）")
    parser.add_argument("--voice", action="store_true",
                        help="把语音转换为 m4a 保存到 export/<名称>_files/，md/html/json 中可直接播放"
                             "（txt/csv 显示 [语音 12″]）")
    parser.add_argument("--media", action="store_true",
                        help="导出图片、语音和视频（= --images --voice + 复制已下载的视频及封面）")
    parser.add_argument("--since", type=_parse_date, metavar="YYYY-MM-DD",
                        help="只导出该日期（含）之后的消息")
    parser.add_argument("--until", type=_parse_date, metavar="YYYY-MM-DD",
                        help="只导出该日期（含）之前的消息")
    parser.add_argument("--export-dir", help="导出目录（默认: 项目下的 export/）")
    parser.add_argument("--no-decrypt", action="store_true", help="跳过自动解密，直接使用现有解密数据")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_filter is None and not args.all and not args.contact:
        parser.error("请指定联系人，或使用 --list / --all")
    if args.all and args.output:
        parser.error("--all 不能与 -o 同时使用（请用 --export-dir）")

    since = args.since.timestamp() if args.since else None
    until = (args.until + timedelta(days=1)).timestamp() if args.until else None
    if since is not None and until is not None and since >= until:
        parser.error("--since 不能晚于 --until")

    cfg = get_config()
    from config import secure_outputs
    secure_outputs(cfg)  # 新文件仅本人可读写，并收紧已有输出的权限
    if not args.no_decrypt:
        # 自动解密（跳过未变化的数据库）
        from decrypt_db import main as decrypt_main
        decrypt_main()

    decrypted_dir = args.decrypted_dir or cfg["decrypted_dir"]
    export_dir = os.path.abspath(args.export_dir) if args.export_dir else DEFAULT_EXPORT_DIR
    if not chatlib.message_db_paths(decrypted_dir):
        print(f"[!] 未找到消息数据库目录: {os.path.join(decrypted_dir, 'message')}")
        return 1
    src = ChatSource(decrypted_dir, cfg["self_wxid"])
    images = ImageExporter(cfg) if args.images or args.media else None
    media = (MediaExporter(dict(cfg, decrypted_dir=decrypted_dir), voice=True, video=args.media)
             if args.voice or args.media else None)
    if (images or media) is not None and args.format in ("txt", "csv") \
            and args.list_filter is None:
        what = "图片/语音/视频" if media is not None and images is not None else \
            "图片" if images is not None else "语音"
        print(f"[!] {args.format} 格式不嵌入{what}，文件只保存到 <名称>_files/ 目录")

    if args.list_filter is not None:
        cmd_list(src, args.list_filter or args.contact, args.chat_type)
        return 0
    if args.all:
        cmd_all(src, args.contact, export_dir, since, until, args.format, args.chat_type, images,
                media)
        if images is not None:
            images.summary()
        if media is not None:
            media.summary()
        return 0

    chat = resolve_chat(src, args.contact, args.chat_type)
    if not chat:
        return 1
    print(f"[+] 目标: {chat_label(chat)}（{chat['username']}）")
    print(f"[+] 聊天记录分布在 {len(chat['tables'])} 个数据库中（共 {chat['msg_count']} 条原始记录）")
    export_one(src, chat, args.format, args.output, args.incremental, since, until, export_dir,
               images=images, media=media)
    if images is not None:
        images.summary()
    if media is not None:
        media.summary()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] 已取消")
        sys.exit(130)
    except BrokenPipeError:  # 例如 | head
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
