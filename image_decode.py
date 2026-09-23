"""
image_decode.py - 解码微信 4.x (macOS) 聊天图片 .dat 文件

WeChat 4.x stores chat images under
    <wechat_base_dir>/msg/attach/<md5(chat username)>/<YYYY-MM>/Img/<md5>[_t|_h].dat
      <md5>.dat    full image (may be missing if never downloaded)
      <md5>_h.dat  HD variant (only some messages)
      <md5>_t.dat  thumbnail

Supported .dat formats
  * V2  header 07 08 56 32 08 07 ("\\x07\\x08V2\\x08\\x07")  - AES-128-ECB + XOR, per-account key
  * V1  header 07 08 56 31 08 07 ("\\x07\\x08V1\\x08\\x07")  - same layout, fixed AES key
  * legacy (WeChat 3.x): whole file XOR'ed with a single byte

V1/V2 layout:
    [6B magic][u32 LE aes_size][u32 LE xor_size][1B pad]
    [AES-128-ECB ciphertext of the first aes_size bytes, PKCS7 padded
     -> aes_size rounded up to the next multiple of 16 (+16 when already aligned)]
    [plain bytes]
    [last xor_size bytes XOR'ed with a single byte]

Decrypted payloads are JPEG / PNG / GIF / WEBP / BMP, or "wxgf" (Tencent's
HEVC-in-a-wrapper format).  wxgf is converted to a real image by wrapping the
HEVC bitstream into a minimal HEIC container (pure Python) and converting that
with macOS' built-in `sips`: JPEG by default, PNG with transparency when the
wxgf carries a (non-opaque) second, monochrome HEVC stream - the alpha plane,
stored in the HEIC as an auxiliary alpha image (see wxgf_streams()).

Keys (per account, no memory scanning required on macOS):
    uin      = the number in  ~/Library/Containers/com.tencent.xinWeChat/Data/
               Documents/app_data/net/kvcomm/key_<uin>_*.statistic
    aes_key  = md5(str(uin) + wxid).hexdigest()[:16]   (16 ASCII chars used as the key)
    xor_key  = uin & 0xFF
The derived AES key is validated against real V2 files (the first AES block
must decrypt to an image magic) before it is used.  Keys can be overridden in
config.json with "image_aes_key" (16-char string) and "image_xor_key" (int or
"0x.." string).

CLI:
    python image_decode.py <file.dat> [more.dat ...] -o out_dir
    python image_decode.py --test 200 [-o out_dir]
    python image_decode.py --show-keys
"""
import argparse
import fnmatch
import glob
import hashlib
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter

from Crypto.Cipher import AES

V1_MAGIC = b"\x07\x08V1\x08\x07"
V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_AES_KEY = b"cfcd208495d565ef"  # md5("0")[:16], fixed for V1 files

KVCOMM_DIRS = [
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/kvcomm",
]

_IMAGE_MAGICS = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG", "png"),
    (b"GIF8", "gif"),
    (b"RIFF", "webp"),  # refined below
    (b"BM", "bmp"),
    (b"wxgf", "wxgf"),
    (b"II*\x00", "tif"),
    (b"MM\x00*", "tif"),
]


class ImageDecodeError(Exception):
    pass


class KeyRequiredError(ImageDecodeError):
    pass


def detect_ext(data):
    for magic, ext in _IMAGE_MAGICS:
        if data.startswith(magic):
            if ext == "webp" and data[8:12] != b"WEBP":
                continue
            return ext
    return None


def dat_format(head):
    """Return 'v2', 'v1' or 'xor' for the first bytes of a .dat file."""
    if head[:6] == V2_MAGIC:
        return "v2"
    if head[:6] == V1_MAGIC:
        return "v1"
    return "xor"


# --------------------------------------------------------------------------
# .dat decryption
# --------------------------------------------------------------------------

def _normalize_aes_key(aes_key):
    if aes_key is None:
        return None
    if isinstance(aes_key, str):
        aes_key = aes_key.encode()
    if len(aes_key) != 16:
        raise ValueError("AES key must be 16 bytes/characters, got %d" % len(aes_key))
    return aes_key


def _normalize_xor_key(xor_key):
    if xor_key is None:
        return None
    if isinstance(xor_key, str):
        xor_key = int(xor_key, 0)
    return int(xor_key) & 0xFF


def _decode_v12(data, aes_key, xor_key):
    if len(data) < 15:
        raise ImageDecodeError("file too short")
    aes_size, xor_size = struct.unpack("<II", data[6:14])
    body = data[15:]
    aligned = (aes_size // 16 + 1) * 16  # PKCS7 always adds padding
    if aligned > len(body):
        # small files: whole body is AES
        aligned = len(body) - len(body) % 16
    if xor_size > len(body) - aligned:
        raise ImageDecodeError("corrupt header (xor_size too large)")
    try:
        plain = AES.new(aes_key, AES.MODE_ECB).decrypt(body[:aligned])
    except ValueError as e:
        raise ImageDecodeError("AES decrypt failed: %s" % e)
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    else:
        raise ImageDecodeError("bad PKCS7 padding - wrong AES key?")
    end = len(body) - xor_size
    middle = body[aligned:end]
    if xor_size:
        if xor_key is None:
            raise KeyRequiredError("this file has an XOR-encrypted tail; xor_key is required")
        tail = bytes(b ^ xor_key for b in body[end:])
    else:
        tail = b""
    return plain + middle + tail


def guess_legacy_xor(data):
    """Infer the single XOR byte of a legacy (3.x) .dat file from image magics."""
    for magic, _ in _IMAGE_MAGICS:
        if len(data) < len(magic):
            continue
        k = data[0] ^ magic[0]
        if all(data[i] ^ k == magic[i] for i in range(len(magic))):
            return k
    return None


def decrypt_dat(data, aes_key=None, xor_key=None):
    """Decrypt raw .dat bytes -> (payload bytes, format) without wxgf conversion."""
    fmt = dat_format(data[:6])
    if fmt == "v2":
        if aes_key is None:
            raise KeyRequiredError(
                "V2 .dat needs the per-account AES key; see derive_image_keys()")
        payload = _decode_v12(data, _normalize_aes_key(aes_key), _normalize_xor_key(xor_key))
    elif fmt == "v1":
        payload = _decode_v12(data, V1_AES_KEY, _normalize_xor_key(xor_key))
    else:
        k = _normalize_xor_key(xor_key)
        if k is None or detect_ext(bytes(b ^ k for b in data[:12])) is None:
            k = guess_legacy_xor(data)
        if k is None:
            raise ImageDecodeError("unknown .dat format (not V1/V2 and no XOR key fits)")
        payload = bytes(b ^ k for b in data)
    if detect_ext(payload) is None:
        raise ImageDecodeError("%s decrypted to unknown data (wrong key?)" % fmt)
    return payload, fmt


def decode_dat(path, aes_key=None, xor_key=None, wxgf_to="auto"):
    """Decode a WeChat .dat image file.

    Args:
        path:    path to a .dat file.
        aes_key: 16-char str/bytes AES key (required for V2 files).
        xor_key: int 0-255 (required for V1/V2 files with an XOR tail;
                 inferred automatically for legacy XOR files).
        wxgf_to: what to do with wxgf (HEVC) payloads (converted via macOS sips):
                 "auto" (default) - PNG with transparency when the file has a
                                    non-opaque alpha stream, JPEG otherwise
                 "jpg" | "png" | "heic" (png/heic keep alpha) | None (raw wxgf).
    Returns:
        (image_bytes, ext) where ext is e.g. "jpg", "png", "gif", "heic", "wxgf".
    Raises:
        KeyRequiredError if a needed key is missing, ImageDecodeError otherwise.
    """
    with open(path, "rb") as f:
        data = f.read()
    payload, _ = decrypt_dat(data, aes_key, xor_key)
    return convert_payload(payload, wxgf_to)


def convert_payload(payload, wxgf_to="auto"):
    """Turn a decrypted image payload into (bytes, ext); wxgf is converted
    according to wxgf_to (see decode_dat)."""
    ext = detect_ext(payload)
    if ext != "wxgf" or not wxgf_to:
        return payload, ext
    return wxgf_convert(payload, wxgf_to)


def wxgf_convert(payload, wxgf_to="auto"):
    """Convert a wxgf payload to (bytes, ext); see decode_dat for wxgf_to."""
    color, alpha = wxgf_streams(payload)
    if wxgf_to == "auto":
        if alpha is not None:
            try:
                if not alpha_is_opaque(alpha):
                    return heic_convert(hevc_to_heic(color, alpha), "png"), "png"
            except ImageDecodeError:
                pass
        return heic_convert(hevc_to_heic(color), "jpg"), "jpg"
    if wxgf_to == "jpg":
        return heic_convert(hevc_to_heic(color), "jpg"), "jpg"
    heic = hevc_to_heic(color, alpha)
    if wxgf_to == "heic":
        return heic, "heic"
    try:
        return heic_convert(heic, wxgf_to), wxgf_to
    except ImageDecodeError:
        if alpha is None:
            raise
        return heic_convert(hevc_to_heic(color), wxgf_to), wxgf_to  # drop alpha


# --------------------------------------------------------------------------
# wxgf (HEVC) -> HEIC
# --------------------------------------------------------------------------

def _split_annexb(stream):
    """Split an Annex-B byte stream into NAL units (without start codes)."""
    starts = [m.start() for m in re.finditer(b"\x00\x00\x01", stream)]
    nals = []
    for i, s in enumerate(starts):
        begin = s + 3
        end = starts[i + 1] if i + 1 < len(starts) else len(stream)
        nal = stream[begin:end]
        if i + 1 < len(starts):
            nal = nal.rstrip(b"\x00") if nal.endswith(b"\x00") else nal
        if nal:
            nals.append(nal)
    return nals


def _wxgf_partitions(data):
    """Return the HEVC partitions (Annex-B streams) of a wxgf file.

    Each partition is preceded by its 4-byte big-endian length and starts
    with a VPS NAL (00 00 00 01 40 01)."""
    parts = []
    for m in re.finditer(b"\x00\x00\x00\x01\x40\x01", data):
        pos = m.start()
        if pos < 4:
            continue
        length = struct.unpack(">I", data[pos - 4:pos])[0]
        if 0 < length <= len(data) - pos:
            parts.append(data[pos:pos + length])
    if not parts:
        # fall back: everything from the first VPS
        i = data.find(b"\x00\x00\x00\x01\x40\x01")
        if i < 0:
            raise ImageDecodeError("wxgf: no HEVC stream found")
        parts.append(data[i:])
    return parts


def _unescape_rbsp(nal):
    return re.sub(b"\x00\x00\x03", b"\x00\x00", nal)


class _BitReader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self):
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ImageDecodeError("bad exp-golomb in SPS")
        return (1 << zeros) - 1 + self.u(zeros)


def _parse_sps(sps_nal):
    rbsp = _unescape_rbsp(sps_nal)[2:]
    r = _BitReader(rbsp)
    r.u(4)
    max_sub_layers_minus1 = r.u(3)
    r.u(1)
    ptl = rbsp[1:13]  # general profile/tier/compat/constraint/level (12 bytes)
    r.u(96)
    sub_prof, sub_lvl = [], []
    for _ in range(max_sub_layers_minus1):
        sub_prof.append(r.u(1))
        sub_lvl.append(r.u(1))
    if max_sub_layers_minus1 > 0:
        for _ in range(max_sub_layers_minus1, 8):
            r.u(2)
    for i in range(max_sub_layers_minus1):
        if sub_prof[i]:
            r.u(88)
        if sub_lvl[i]:
            r.u(8)
    r.ue()  # sps_id
    chroma = r.ue()
    if chroma == 3:
        r.u(1)
    width, height = r.ue(), r.ue()
    if r.u(1):
        left, right, top, bottom = r.ue(), r.ue(), r.ue(), r.ue()
        sub_w = 2 if chroma in (1, 2) else 1
        sub_h = 2 if chroma == 1 else 1
        width -= sub_w * (left + right)
        height -= sub_h * (top + bottom)
    bd_luma, bd_chroma = r.ue(), r.ue()
    return {"ptl": ptl, "chroma": chroma, "width": width, "height": height,
            "bd_luma": bd_luma, "bd_chroma": bd_chroma,
            "max_sub_layers_minus1": max_sub_layers_minus1}


def _box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _fullbox(typ, version, flags, payload):
    return _box(typ, struct.pack(">I", (version << 24) | flags) + payload)


def _hevc_item(stream):
    """Parse a single-picture HEVC Annex-B stream -> (hvcC payload, sps info,
    mdat payload of length-prefixed VCL NALs)."""
    nals = _split_annexb(stream)
    ps = {32: [], 33: [], 34: []}
    vcl = []
    for n in nals:
        t = (n[0] >> 1) & 0x3F
        if t in ps:
            ps[t].append(n)
        elif t < 32:
            if vcl and (n[2] & 0x80):  # first_slice_segment_in_pic_flag -> next picture
                break
            vcl.append(n)
    if not (ps[32] and ps[33] and ps[34] and vcl):
        raise ImageDecodeError("wxgf: incomplete HEVC stream")
    sps = _parse_sps(ps[33][0])

    hvcc = bytes([1]) + sps["ptl"]
    hvcc += struct.pack(">HBBBBH", 0xF000, 0xFC, 0xFC | sps["chroma"],
                        0xF8 | sps["bd_luma"], 0xF8 | sps["bd_chroma"], 0)
    hvcc += bytes([((sps["max_sub_layers_minus1"] + 1) << 3) | 0x04 | 0x03, 3])
    for t in (32, 33, 34):
        hvcc += bytes([0x80 | t]) + struct.pack(">H", len(ps[t]))
        for n in ps[t]:
            hvcc += struct.pack(">H", len(n)) + n
    data = b"".join(struct.pack(">I", len(n)) + n for n in vcl)
    return hvcc, sps, data


# auxiliary image type for an HEVC alpha plane (ISO/IEC 23008-12, HEIF)
HEVC_ALPHA_URN = b"urn:mpeg:hevc:2015:auxid:1"


def hevc_to_heic(stream, alpha=None):
    """Wrap a single-picture HEVC Annex-B stream into a minimal HEIC file.

    alpha: optional second HEVC stream (monochrome, same size) stored as an
    auxiliary alpha image (auxC urn:mpeg:hevc:2015:auxid:1 + iref/auxl), which
    macOS ImageIO/sips composites as transparency."""
    hvcc, sps, color = _hevc_item(stream)
    items = [(hvcc, sps, color)]
    if alpha is not None:
        items.append(_hevc_item(alpha))

    ftyp = _box(b"ftyp", b"heic" + struct.pack(">I", 0) + b"mif1heic")
    hdlr = _fullbox(b"hdlr", 0, 0, b"\0" * 4 + b"pict" + b"\0" * 12 + b"\0")
    pitm = _fullbox(b"pitm", 0, 0, struct.pack(">H", 1))
    infes = b"".join(_fullbox(b"infe", 2, 0, struct.pack(">HH", i + 1, 0) + b"hvc1" + b"\0")
                     for i in range(len(items)))
    iinf = _fullbox(b"iinf", 0, 0, struct.pack(">H", len(items)) + infes)
    # properties: 1 hvcC(color) 2 ispe(color) [3 hvcC(alpha) 4 ispe(alpha) 5 auxC]
    props = _box(b"hvcC", hvcc) + _fullbox(b"ispe", 0, 0, struct.pack(
        ">II", sps["width"], sps["height"]))
    assoc = [(1, [0x81, 0x02])]
    iref = b""
    if alpha is not None:
        a_hvcc, a_sps, _ = items[1]
        props += _box(b"hvcC", a_hvcc)
        props += _fullbox(b"ispe", 0, 0, struct.pack(">II", a_sps["width"], a_sps["height"]))
        props += _fullbox(b"auxC", 0, 0, HEVC_ALPHA_URN + b"\0")
        assoc.append((2, [0x83, 0x04, 0x85]))
        iref = _fullbox(b"iref", 0, 0, _box(b"auxl", struct.pack(">HHH", 2, 1, 1)))
    ipma = _fullbox(b"ipma", 0, 0, struct.pack(">I", len(assoc)) + b"".join(
        struct.pack(">HB", item_id, len(a)) + bytes(a) for item_id, a in assoc))
    iprp = _box(b"iprp", _box(b"ipco", props) + ipma)
    mdat_payload = b"".join(it[2] for it in items)

    def build(offset):
        locs = b""
        for i, it in enumerate(items):
            locs += struct.pack(">HHHII", i + 1, 0, 1, offset, len(it[2]))
            offset += len(it[2])
        iloc = _fullbox(b"iloc", 0, 0, bytes([0x44, 0x00]) +
                        struct.pack(">H", len(items)) + locs)
        meta = _fullbox(b"meta", 0, 0, hdlr + pitm + iloc + iinf + iref + iprp)
        return ftyp + meta

    head = build(0)
    head = build(len(head) + 8)
    return head + _box(b"mdat", mdat_payload)


def _hevc_is_monochrome(stream):
    for n in _split_annexb(stream):
        if (n[0] >> 1) & 0x3F == 33:
            return _parse_sps(n)["chroma"] == 0
    return False


def wxgf_streams(data):
    """Split a wxgf payload into (color_stream, alpha_stream_or_None).

    Layout (as observed in WeChat 4.x):
        "wxgf" + header (byte 4 = header version/flags: 0x13 single stream,
        0x12 with alpha), then one or two partitions, each preceded by a
        4-byte big-endian length and starting with an HEVC VPS:
          single:  [len][colour 4:2:0 stream]
          alpha:   [len][alpha 4:0:0 (monochrome) stream][len][colour stream]
    The colour stream is the largest non-monochrome partition; the alpha
    stream is a monochrome partition with the same dimensions."""
    parts = _wxgf_partitions(data)
    color = max(parts, key=len)
    if len(parts) < 2:
        return color, None
    mono = [p for p in parts if p is not color and _hevc_is_monochrome(p)]
    if not mono or _hevc_is_monochrome(color):
        return color, None
    try:
        c_sps = _parse_sps(next(n for n in _split_annexb(color) if (n[0] >> 1) & 0x3F == 33))
        a_sps = _parse_sps(next(n for n in _split_annexb(mono[0]) if (n[0] >> 1) & 0x3F == 33))
    except (StopIteration, ImageDecodeError, IndexError):
        return color, None
    if (c_sps["width"], c_sps["height"]) != (a_sps["width"], a_sps["height"]):
        return color, None
    return color, mono[0]


def wxgf_to_heic(data, alpha=True):
    """Convert wxgf payload to HEIC bytes. When the file carries an alpha
    stream (and alpha=True) it becomes an auxiliary alpha image."""
    color, a = wxgf_streams(data)
    return hevc_to_heic(color, a if alpha else None)


def wxgf_has_alpha(data):
    return wxgf_streams(data)[1] is not None


def _sips_convert(heic, sips_fmt, ext):
    sips = shutil.which("sips")
    if not sips:
        raise ImageDecodeError("sips not found; use wxgf_to='heic'")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.heic")
        dst = os.path.join(td, "out." + ext)
        with open(src, "wb") as f:
            f.write(heic)
        r = subprocess.run([sips, "-s", "format", sips_fmt, src, "--out", dst],
                           capture_output=True)
        if r.returncode != 0 or not os.path.exists(dst):
            raise ImageDecodeError("sips failed to convert HEIC: %s" % r.stderr.decode(errors="replace")[:200])
        with open(dst, "rb") as f:
            return f.read()


def heic_convert(heic, fmt="jpg"):
    """Convert HEIC bytes to jpg/png with macOS sips (png keeps an auxiliary
    alpha image as transparency)."""
    sips_fmt = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png"}[fmt]
    return _sips_convert(heic, sips_fmt, fmt)


def bmp_min_sample(bmp):
    """Smallest 8-bit sample value in an uncompressed 24/32-bit BMP's pixel
    rows (row padding excluded)."""
    if bmp[:2] != b"BM" or len(bmp) < 54:
        raise ImageDecodeError("not a BMP")
    off = struct.unpack("<I", bmp[10:14])[0]
    w, h, _, bpp, comp = struct.unpack("<iiHHI", bmp[18:34])
    if comp not in (0, 3) or bpp not in (24, 32):
        raise ImageDecodeError("unsupported BMP (bpp=%d, compression=%d)" % (bpp, comp))
    row = w * bpp // 8
    stride = (row + 3) & ~3
    lo = 255
    for y in range(abs(h)):
        start = off + y * stride
        line = bmp[start:start + row]
        if bpp == 32:
            line = bytes(b for i, b in enumerate(line) if i % 4 != 3)
        if line:
            lo = min(lo, min(line))
            if lo == 0:
                break
    return lo


# sips renders the grey alpha plane through colour management, so pure white
# comes out as 254/255; anything at or above this counts as opaque.
OPAQUE_THRESHOLD = 250


def alpha_is_opaque(alpha_stream):
    """True if a monochrome HEVC alpha stream is (practically) fully opaque.
    WeChat attaches an alpha stream to many wxgf images whose plane is
    uniformly 255; those are written as JPEG instead of PNG."""
    return bmp_min_sample(_sips_convert(hevc_to_heic(alpha_stream), "bmp", "bmp")) >= OPAQUE_THRESHOLD


# --------------------------------------------------------------------------
# key discovery
# --------------------------------------------------------------------------

def find_uins(kvcomm_dirs=None):
    uins = []
    for d in kvcomm_dirs or KVCOMM_DIRS:
        for p in glob.glob(os.path.join(os.path.expanduser(d), "key_*.statistic")):
            m = re.match(r"key_(\d+)_", os.path.basename(p))
            if m and int(m.group(1)) not in uins:
                uins.append(int(m.group(1)))
    return uins


def _wxid_candidates(wechat_base_dir, self_wxid=None):
    name = os.path.basename(os.path.normpath(wechat_base_dir))
    cands = []
    for c in (self_wxid, re.sub(r"_[0-9a-fA-F]{4}$", "", name), name):
        if c and c not in cands:
            cands.append(c)
    return cands


def iter_dat_files(wechat_base_dir, pattern="*.dat"):
    root = os.path.join(wechat_base_dir, "msg", "attach")
    for dp, _, files in os.walk(root):
        for f in files:
            if fnmatch.fnmatch(f, pattern):
                yield os.path.join(dp, f)


def _sample_v2_heads(wechat_base_dir, n=8):
    heads = []
    for p in iter_dat_files(wechat_base_dir, "*_t.dat"):
        with open(p, "rb") as f:
            h = f.read(31)
        if h[:6] == V2_MAGIC and len(h) == 31:
            heads.append(h)
            if len(heads) >= n:
                break
    return heads


def verify_aes_key(aes_key, heads):
    """True if the key decrypts the first AES block of every sample to an image magic."""
    key = _normalize_aes_key(aes_key)
    return bool(heads) and all(
        detect_ext(AES.new(key, AES.MODE_ECB).decrypt(h[15:31])) for h in heads)


def infer_xor_key(wechat_base_dir, n=50):
    """Most common (last byte ^ 0xD9) over V2 thumbnails (JPEGs end with FF D9)."""
    c = Counter()
    for i, p in enumerate(iter_dat_files(wechat_base_dir, "*_t.dat")):
        if i >= n:
            break
        with open(p, "rb") as f:
            f.seek(-1, os.SEEK_END)
            c[f.read(1)[0] ^ 0xD9] += 1
    return c.most_common(1)[0][0] if c else None


def derive_image_keys(wechat_base_dir, self_wxid=None, kvcomm_dirs=None):
    """Derive (aes_key:str, xor_key:int) for an account; either may be None.

    aes_key = md5(str(uin)+wxid)[:16], xor_key = uin & 0xFF, validated against
    sample V2 thumbnails in the account's attach folder."""
    heads = _sample_v2_heads(wechat_base_dir)
    for uin in find_uins(kvcomm_dirs):
        for wxid in _wxid_candidates(wechat_base_dir, self_wxid):
            key = hashlib.md5((str(uin) + wxid).encode()).hexdigest()[:16]
            if not heads or verify_aes_key(key, heads):
                return key, uin & 0xFF
    return None, infer_xor_key(wechat_base_dir)


def get_image_keys(cfg):
    """Keys from config ("image_aes_key", "image_xor_key") or derived automatically."""
    aes_key = cfg.get("image_aes_key") or None
    xor_key = cfg.get("image_xor_key")
    xor_key = _normalize_xor_key(xor_key) if xor_key not in (None, "") else None
    if aes_key is None or xor_key is None:
        d_aes, d_xor = derive_image_keys(cfg["wechat_base_dir"], cfg.get("self_wxid"))
        aes_key = aes_key or d_aes
        xor_key = xor_key if xor_key is not None else d_xor
    return aes_key, xor_key


# --------------------------------------------------------------------------
# message -> file mapping
# --------------------------------------------------------------------------

_MD5_RE = re.compile(rb"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])")


def image_md5_from_packed_info(packed_info_data):
    """Extract the image file md5 from Msg_<md5>.packed_info_data (local_type 3)
    or MessageResourceInfo.packed_info (a protobuf holding a 32-hex string)."""
    if not packed_info_data:
        return None
    m = _MD5_RE.search(bytes(packed_info_data))
    return m.group(1).decode() if m else None


def image_md5_from_resource_db(resource_db_path, username, local_id, create_time=None):
    """Fallback lookup in message/message_resource.db. local_id is not unique
    across message_N.db files, so create_time is used to disambiguate."""
    import sqlite3
    con = sqlite3.connect(resource_db_path)
    try:
        row = con.execute("SELECT rowid FROM ChatName2Id WHERE user_name=?", (username,)).fetchone()
        if not row:
            return None
        sql = ("SELECT packed_info FROM MessageResourceInfo WHERE chat_id=? AND "
               "message_local_id=? AND message_local_type=3")
        args = [row[0], local_id]
        if create_time is not None:
            sql += " AND message_create_time=?"
            args.append(create_time)
        for (p,) in con.execute(sql, args):
            md5 = image_md5_from_packed_info(p)
            if md5:
                return md5
    finally:
        con.close()
    return None


def find_image_for_message(wechat_base_dir, username, packed_info_data=None,
                           create_time=None, md5=None,
                           prefer=("_h", "", "_t")):
    """Locate the .dat file for an image message (Msg_* row with local_type == 3).

    Args:
        wechat_base_dir:  account folder (cfg["wechat_base_dir"]).
        username:         chat username (the one whose md5 names the Msg_<md5> table).
        packed_info_data: the row's packed_info_data blob (contains the file md5).
        create_time:      the row's create_time (selects the YYYY-MM folder, local time).
        md5:              file md5 if already known (overrides packed_info_data).
        prefer:           suffix preference order: "_h" HD original, "" full, "_t" thumbnail.
    Returns: path to the best available .dat, or None.
    """
    md5 = md5 or image_md5_from_packed_info(packed_info_data)
    if not md5:
        return None
    chat_dir = os.path.join(wechat_base_dir, "msg", "attach",
                            hashlib.md5(username.encode()).hexdigest())
    dirs = []
    if create_time:
        for ts in (create_time, create_time - 86400, create_time + 86400):
            d = os.path.join(chat_dir, time.strftime("%Y-%m", time.localtime(ts)), "Img")
            if d not in dirs:
                dirs.append(d)
    for suffix in prefer:
        for d in dirs:
            p = os.path.join(d, md5 + suffix + ".dat")
            if os.path.exists(p):
                return p
    for suffix in prefer:  # slow path: any month
        hits = glob.glob(os.path.join(chat_dir, "*", "Img", md5 + suffix + ".dat"))
        if hits:
            return hits[0]
    return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _load_cfg():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import load_config
    return load_config()


def _variant(p):
    return "_t" if p.endswith("_t.dat") else "_h" if p.endswith("_h.dat") else "full"


def _run_test(n, out_dir, aes_key, xor_key, wechat_base_dir, wxgf_to):
    files = list(iter_dat_files(wechat_base_dir))
    random.shuffle(files)
    files = files[:n]
    ok, fail = Counter(), Counter()
    errors = Counter()
    for i, p in enumerate(files):
        with open(p, "rb") as f:
            fmt = dat_format(f.read(6))
        try:
            data, ext = decode_dat(p, aes_key, xor_key, wxgf_to=wxgf_to)
            payload, _ = decrypt_dat(open(p, "rb").read(), aes_key, xor_key)
            inner = detect_ext(payload)
            ok[(fmt, _variant(p), inner)] += 1
            if out_dir:
                with open(os.path.join(out_dir, "test_%04d.%s" % (i, ext)), "wb") as f:
                    f.write(data)
        except ImageDecodeError as e:
            fail[(fmt, _variant(p))] += 1
            errors[type(e).__name__ + ": " + str(e)[:80]] += 1
    total = sum(ok.values()) + sum(fail.values())
    print("[+] 测试 %d 个文件: %d 成功, %d 失败 (%.1f%%)" % (
        total, sum(ok.values()), sum(fail.values()),
        100.0 * sum(ok.values()) / total if total else 0))
    print("成功（dat 格式, 变体, 图片格式）:")
    for k, v in sorted(ok.items()):
        print("  %6d  %s" % (v, k))
    if fail:
        print("失败（dat 格式, 变体）:")
        for k, v in sorted(fail.items()):
            print("  %6d  %s" % (v, k))
        for k, v in errors.most_common(5):
            print("  %6d  %s" % (v, k))


def main():
    ap = argparse.ArgumentParser(description="解码微信 4.x 聊天图片 .dat 文件")
    ap.add_argument("files", nargs="*", help="要解码的 .dat 文件")
    ap.add_argument("-o", "--out", help="输出目录")
    ap.add_argument("--test", type=int, metavar="N", help="随机解码 N 个 .dat 文件并统计结果")
    ap.add_argument("--wxgf", default="auto", choices=["auto", "jpg", "png", "heic", "raw"],
                    help="wxgf (HEVC) 图片的输出格式（默认 auto：带透明通道时为 png，否则 jpg）")
    ap.add_argument("--aes-key", help="指定 16 字符的 V2 AES 密钥")
    ap.add_argument("--xor-key", help="指定 XOR 密钥（例如 0x5a）")
    ap.add_argument("--show-keys", action="store_true", help="显示推导出的密钥后退出")
    args = ap.parse_args()

    cfg = _load_cfg()
    if args.aes_key:
        cfg["image_aes_key"] = args.aes_key
    if args.xor_key:
        cfg["image_xor_key"] = args.xor_key
    aes_key, xor_key = get_image_keys(cfg)
    wxgf_to = None if args.wxgf == "raw" else args.wxgf

    if args.show_keys:
        print("image_aes_key:", aes_key)
        print("image_xor_key:", "0x%02x" % xor_key if xor_key is not None else None)
        return
    if aes_key is None:
        print("[!] 未找到 V2 AES 密钥（没有匹配的 kvcomm/key_<uin>_*.statistic），"
              "V2 格式图片将无法解码；可在 config.json 中设置 image_aes_key")
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    if args.test:
        _run_test(args.test, args.out, aes_key, xor_key, cfg["wechat_base_dir"], wxgf_to)
        return
    if not args.files:
        ap.error("请指定 .dat 文件或 --test N")
    out_dir = args.out or "."
    for p in args.files:
        try:
            data, ext = decode_dat(p, aes_key, xor_key, wxgf_to=wxgf_to)
        except ImageDecodeError as e:
            print("[!] %s: %s" % (os.path.basename(p), e))
            continue
        base = os.path.splitext(os.path.basename(p))[0]
        dst = os.path.join(out_dir, base + "." + ext)
        with open(dst, "wb") as f:
            f.write(data)
        print("[+] %s -> %s (%d 字节)" % (os.path.basename(p), dst, len(data)))


if __name__ == "__main__":
    main()
