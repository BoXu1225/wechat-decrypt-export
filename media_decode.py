"""
media_decode.py - 微信 4.x (macOS) 语音 / 视频消息的查找与转换

Voice (local_type 34)
    The audio is not a file on disk: it is a blob in the decrypted
    message/media_0.db (media_N.db):
        VoiceInfo(chat_name_id, create_time, local_id, svr_id, voice_data, data_index)
        Name2Id(user_name)                -- chat_name_id is a rowid into it
    A message maps to its row by (chat username, svr_id == Msg_*.server_id), or
    by (chat username, create_time, local_id) -- local_id alone is only unique
    inside one message_N.db, create_time disambiguates.
    voice_data is Tencent SILK v3: an optional 0x02 byte, then "#!SILK_V3",
    then frames of [int16 LE length][payload] (20 ms each). Decoded with the
    silk-python package (import name pysilk) to 16-bit mono PCM at 24 kHz, then
    encoded to AAC (.m4a) with macOS' built-in `afconvert` (falls back to .wav
    when afconvert is unavailable). The stated length is the message XML's
    voicelength="<ms>" attribute (chats.media_duration).

Video (local_type 43)
    Plain (unencrypted) MP4 files under
        <wechat_base_dir>/msg/video/<YYYY-MM>/<md5>.mp4        (only if downloaded)
        <wechat_base_dir>/msg/video/<YYYY-MM>/<md5>_thumb.jpg  (poster, usually present)
    <md5> is the 32-hex string in the message's packed_info_data (same as for
    images). The message XML's playlength="<s>" attribute is the duration.

No printing except in the CLI.

CLI:
    python media_decode.py --test 200 [-o out_dir]       # random voice sample + video stats
    python media_decode.py --voice <chat> <server_id> -o out_dir
"""
import argparse
import ctypes
import glob
import io
import os
import random
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import wave
from collections import Counter

SILK_MAGIC = b"#!SILK_V3"
SAMPLE_RATE = 24000  # WeChat records voice at 24 kHz mono
AAC_BITRATE = 24000  # speech; the SILK source is ~15 kbit/s

_MEDIA_DB_RE = re.compile(r"media_(\d+)\.db$")
_MD5_RE = re.compile(rb"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])")


class MediaDecodeError(Exception):
    pass


# --------------------------------------------------------------------------
# durations
# --------------------------------------------------------------------------

def seconds(value):
    """Duration in seconds (float) -> whole seconds as WeChat shows them (>= 1)."""
    if value is None:
        return None
    return max(1, int(value + 0.5))


def mp4_duration(path):
    """Duration in seconds from an MP4/M4A file's moov/mvhd box, or None."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            return _find_mvhd(f, 0, size)
    except (OSError, struct.error):
        return None


def _find_mvhd(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return None
        box_size, typ = struct.unpack(">I4s", hdr)
        hlen = 8
        if box_size == 1:
            box_size = struct.unpack(">Q", f.read(8))[0]
            hlen = 16
        elif box_size == 0:
            box_size = end - pos
        if box_size < hlen:
            return None
        if typ == b"moov":
            return _find_mvhd(f, pos + hlen, min(pos + box_size, end))
        if typ == b"mvhd":
            body = f.read(32)
            if body[0] == 1:
                timescale, duration = struct.unpack(">IQ", body[20:32])
            else:
                timescale, duration = struct.unpack(">II", body[12:20])
            return duration / timescale if timescale else None
        pos += box_size
    return None


def wav_duration(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (OSError, wave.Error, EOFError):
        return None


def audio_file_duration(path):
    return wav_duration(path) if path.endswith(".wav") else mp4_duration(path)


# --------------------------------------------------------------------------
# SILK -> PCM -> m4a / wav
# --------------------------------------------------------------------------

def _silk_payload(data):
    data = bytes(data)
    if data[:1] == b"\x02" and data[1:10] == SILK_MAGIC:
        return data[1:]
    if data.startswith(SILK_MAGIC):
        return data
    if data.startswith(b"#!AMR"):
        raise MediaDecodeError("AMR voice is not supported")
    raise MediaDecodeError("not a SILK v3 stream")


def silk_frame_count(data):
    """Number of 20 ms frames in a SILK stream (cheap duration estimate)."""
    p = _silk_payload(data)
    i, n = len(SILK_MAGIC), 0
    while i + 2 <= len(p):
        (ln,) = struct.unpack("<h", p[i:i + 2])
        if ln <= 0 or i + 2 + ln > len(p):
            break
        i += 2 + ln
        n += 1
    return n


def _pysilk():
    try:
        import pysilk
    except ImportError:
        raise MediaDecodeError("silk-python is not installed (pip install -r requirements.txt)")
    return pysilk


def silk_to_pcm(data, sample_rate=SAMPLE_RATE):
    """Decode a WeChat SILK v3 blob -> 16-bit little-endian mono PCM bytes."""
    payload = _silk_payload(data)
    pysilk = _pysilk()
    out = io.BytesIO()
    try:
        pysilk.decode(io.BytesIO(payload), out, sample_rate)
    except Exception as e:  # pysilk.SilkError and friends
        raise MediaDecodeError(f"SILK decode failed: {e}")
    pcm = out.getvalue()
    if not pcm:
        raise MediaDecodeError("SILK decode produced no audio")
    return pcm


def pcm_to_wav(pcm, sample_rate=SAMPLE_RATE):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def afconvert_path():
    return shutil.which("afconvert")


def encode_audio(pcm, out_stem, sample_rate=SAMPLE_RATE, fmt="m4a"):
    """Write PCM as <out_stem>.m4a (AAC via afconvert) or <out_stem>.wav.

    fmt="m4a" falls back to wav when afconvert is unavailable or fails.
    Files are written atomically (temp file + rename). Returns the path."""
    d = os.path.dirname(os.path.abspath(out_stem)) or "."
    os.makedirs(d, exist_ok=True)
    wav = pcm_to_wav(pcm, sample_rate)
    afc = afconvert_path() if fmt == "m4a" else None
    if afc:
        fd, tmp_wav = tempfile.mkstemp(prefix=".tmp_", suffix=".wav", dir=d)
        tmp_m4a = tmp_wav[:-4] + ".m4a"
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(wav)
            r = subprocess.run([afc, "-f", "m4af", "-d", "aac", "-b", str(AAC_BITRATE),
                                tmp_wav, tmp_m4a], capture_output=True)
            if r.returncode == 0 and os.path.getsize(tmp_m4a) > 0:
                os.replace(tmp_m4a, out_stem + ".m4a")
                return out_stem + ".m4a"
        except OSError:
            pass
        finally:
            for p in (tmp_wav, tmp_m4a):
                if os.path.exists(p):
                    os.remove(p)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".wav", dir=d)
    with os.fdopen(fd, "wb") as f:
        f.write(wav)
    os.replace(tmp, out_stem + ".wav")
    return out_stem + ".wav"


def voice_to_file(data, out_stem, sample_rate=SAMPLE_RATE, fmt="m4a"):
    """SILK blob -> audio file. Returns (path, duration_seconds float)."""
    pcm = silk_to_pcm(data, sample_rate)
    path = encode_audio(pcm, out_stem, sample_rate, fmt)
    return path, len(pcm) / 2.0 / sample_rate


def existing_audio(out_stem):
    for ext in (".m4a", ".wav"):
        if os.path.exists(out_stem + ext):
            return out_stem + ext
    return None


# --------------------------------------------------------------------------
# voice lookup (media_N.db)
# --------------------------------------------------------------------------

def media_db_paths(decrypted_dir):
    d = os.path.join(decrypted_dir, "message")
    if not os.path.isdir(d):
        return []
    found = []
    for f in os.listdir(d):
        m = _MEDIA_DB_RE.fullmatch(f)
        if m:
            found.append((int(m.group(1)), os.path.join(d, f)))
    return [p for _, p in sorted(found)]


def voice_stem(server_id, create_time, local_id):
    """File name stem for a voice message: its server id, else create_time_local_id."""
    return str(server_id) if server_id else f"{create_time}_{local_id}"


class VoiceStore:
    """Reads voice blobs from decrypted message/media_N.db files.

    Not thread-safe (one sqlite connection per DB); use from one thread."""

    def __init__(self, decrypted_dir):
        self.paths = media_db_paths(decrypted_dir)
        self._conns = []
        self._ids = {}  # (conn index, username) -> chat_name_id or None

    def _connections(self):
        if not self._conns and self.paths:
            for p in self.paths:
                c = None
                try:
                    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                    c.execute("SELECT 1 FROM VoiceInfo LIMIT 1")
                    self._conns.append(c)
                except sqlite3.Error:
                    if c is not None:
                        c.close()
        return self._conns

    def close(self):
        for c in self._conns:
            c.close()
        self._conns = []
        self._ids = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _chat_id(self, i, conn, username):
        key = (i, username)
        if key not in self._ids:
            row = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?",
                               (username,)).fetchone()
            self._ids[key] = row[0] if row else None
        return self._ids[key]

    def get(self, username, server_id=None, create_time=None, local_id=None):
        """Voice blob (SILK bytes) for a message, or None if not stored."""
        for i, conn in enumerate(self._connections()):
            cid = self._chat_id(i, conn, username)
            if cid is None:
                continue
            rows = []
            if server_id:
                rows = conn.execute(
                    "SELECT data_index, voice_data FROM VoiceInfo WHERE chat_name_id=? "
                    "AND svr_id=?", (cid, server_id)).fetchall()
            if not rows and create_time is not None and local_id is not None:
                rows = conn.execute(
                    "SELECT data_index, voice_data FROM VoiceInfo WHERE chat_name_id=? "
                    "AND create_time=? AND local_id=?", (cid, create_time, local_id)).fetchall()
            rows = [(d, b) for d, b in rows if b]
            if rows:
                rows.sort(key=lambda r: int(r[0]) if str(r[0]).isdigit() else 0)
                return b"".join(bytes(b) for _, b in rows)
        return None


# --------------------------------------------------------------------------
# video lookup (msg/video)
# --------------------------------------------------------------------------

def md5_from_packed_info(packed_info_data):
    """The 32-hex file md5 inside Msg_*.packed_info_data (image / video rows)."""
    if not packed_info_data:
        return None
    m = _MD5_RE.search(bytes(packed_info_data))
    return m.group(1).decode() if m else None


def is_mp4(path):
    try:
        with open(path, "rb") as f:
            return f.read(8)[4:8] == b"ftyp"
    except OSError:
        return False


def find_video_for_message(wechat_base_dir, packed_info_data=None, create_time=None, md5=None):
    """Locate a video message's files. Returns (mp4 path or None, thumb path or None).

    Incomplete downloads (no MP4 'ftyp' header) count as missing."""
    md5 = md5 or md5_from_packed_info(packed_info_data)
    if not md5 or not wechat_base_dir:
        return None, None
    root = os.path.join(wechat_base_dir, "msg", "video")
    dirs = []
    if create_time:
        for ts in (create_time, create_time - 86400, create_time + 86400):
            d = os.path.join(root, time.strftime("%Y-%m", time.localtime(ts)))
            if d not in dirs:
                dirs.append(d)

    def find(name):
        for d in dirs:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
        hits = glob.glob(os.path.join(root, "*", name))  # slow path: any month
        return hits[0] if hits else None

    mp4 = find(md5 + ".mp4")
    if mp4 and not is_mp4(mp4):
        mp4 = None
    return mp4, find(md5 + "_thumb.jpg")


def _clonefile(src, dst):
    """APFS copy-on-write clone (macOS); False if unsupported."""
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        return libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0
    except (OSError, AttributeError):
        return False


def copy_file(src, dst):
    """Copy src to dst atomically (APFS clone when possible, else a byte copy).
    Never hardlinks: exported files must not share an inode with WeChat's."""
    d = os.path.dirname(os.path.abspath(dst))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".tmp_%d_%s" % (os.getpid(), os.path.basename(dst)))
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        if not _clonefile(src, tmp):
            shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dst


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _load_cfg():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import load_config
    return load_config()


def _run_test(n, out_dir, cfg):
    decrypted = cfg["decrypted_dir"]
    paths = media_db_paths(decrypted)
    if not paths:
        print("[!] 未找到 message/media_*.db（请先运行 decrypt_db.py）")
        return
    rows = []
    for p in paths:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        rows += [(p, r) for (r,) in c.execute("SELECT rowid FROM VoiceInfo")]
        c.close()
    random.shuffle(rows)
    rows = rows[:n]
    ok, errors, head = 0, Counter(), Counter()
    t_dec = t_enc = 0.0
    frame_match = 0
    tmpdir = out_dir or tempfile.mkdtemp(prefix="voice_test_")
    for i, (p, rowid) in enumerate(rows):
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        (blob,) = c.execute("SELECT voice_data FROM VoiceInfo WHERE rowid=?", (rowid,)).fetchone()
        c.close()
        blob = bytes(blob or b"")
        head["0x02+SILK" if blob[:1] == b"\x02" and blob[1:10] == SILK_MAGIC else
             "SILK" if blob.startswith(SILK_MAGIC) else "other"] += 1
        try:
            t0 = time.time()
            pcm = silk_to_pcm(blob)
            t1 = time.time()
            path = encode_audio(pcm, os.path.join(tmpdir, "test_%04d" % i))
            t2 = time.time()
            t_dec += t1 - t0
            t_enc += t2 - t1
            d = audio_file_duration(path)
            if d is not None and abs(d - len(pcm) / 2.0 / SAMPLE_RATE) < 0.2:
                frame_match += 1
            ok += 1
        except MediaDecodeError as e:
            errors[str(e)[:80]] += 1
    total = len(rows)
    print("[+] 语音: 测试 %d 条: %d 成功, %d 失败 (%.1f%%)" % (
        total, ok, total - ok, 100.0 * ok / total if total else 0))
    print("    数据头: %s" % dict(head))
    if ok:
        print("    平均每条: SILK 解码 %.1f ms, 编码 %.1f ms (%s)；输出时长与 PCM 一致 %d/%d" % (
            1000 * t_dec / ok, 1000 * t_enc / ok,
            "afconvert AAC" if afconvert_path() else "WAV", frame_match, ok))
    for k, v in errors.most_common(5):
        print("  %6d  %s" % (v, k))
    if not out_dir:
        shutil.rmtree(tmpdir, ignore_errors=True)

    base = cfg.get("wechat_base_dir")
    root = os.path.join(base or "", "msg", "video")
    if not os.path.isdir(root):
        print("[!] 未找到视频目录 msg/video")
        return
    kinds = Counter()
    for f in glob.glob(os.path.join(root, "*", "*")):
        name = os.path.basename(f)
        if name.endswith("_thumb.jpg"):
            kinds["thumb"] += 1
        elif name.endswith(".mp4"):
            kinds["mp4" if is_mp4(f) else "mp4 (incomplete)"] += 1
        else:
            kinds["other"] += 1
    print("[+] 视频目录: %s" % dict(kinds))


def main():
    ap = argparse.ArgumentParser(description="微信 4.x 语音 / 视频消息工具")
    ap.add_argument("--test", type=int, metavar="N", help="随机解码 N 条语音并统计视频文件")
    ap.add_argument("--voice", nargs=2, metavar=("CHAT_USERNAME", "SERVER_ID"),
                    help="把一条语音导出为 m4a")
    ap.add_argument("--wav", action="store_true", help="输出 WAV 而不是 m4a")
    ap.add_argument("-o", "--out", help="输出目录")
    args = ap.parse_args()
    cfg = _load_cfg()
    if args.out:
        os.makedirs(args.out, exist_ok=True)
    if args.test:
        _run_test(args.test, args.out, cfg)
        return
    if args.voice:
        username, sid = args.voice
        with VoiceStore(cfg["decrypted_dir"]) as store:
            data = store.get(username, int(sid))
        if data is None:
            print("[!] 未找到该语音")
            sys.exit(1)
        stem = os.path.join(args.out or ".", sid)
        path, dur = voice_to_file(data, stem, fmt="wav" if args.wav else "m4a")
        print("[+] %s (%.1f 秒)" % (path, dur))
        return
    ap.error("请指定 --test N 或 --voice")


if __name__ == "__main__":
    main()
