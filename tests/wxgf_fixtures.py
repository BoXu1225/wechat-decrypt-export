"""Synthetic wxgf/HEVC fixtures for tests (no real data).

* make_sps() writes a minimal HEVC SPS NAL (enough fields for
  image_decode._parse_sps) so parsing/splitting logic can be tested anywhere.
* heic_to_annexb() pulls the HEVC stream out of a HEIC made by macOS `sips`,
  used for end-to-end tests (skipped when sips is unavailable).
* make_bmp() writes a 24-bit BMP (sips input / bmp_min_sample tests).
"""
import struct


class BitWriter:
    def __init__(self):
        self.bits = []

    def u(self, n, v):
        self.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def ue(self, v):
        v += 1
        n = v.bit_length()
        self.u(n - 1, 0)
        self.u(n, v)

    def bytes(self):
        bits = self.bits + [1]  # rbsp_stop_one_bit
        bits += [0] * (-len(bits) % 8)
        return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))


def make_sps(width, height, chroma=1):
    w = BitWriter()
    w.u(4, 0)        # sps_video_parameter_set_id
    w.u(3, 0)        # max_sub_layers_minus1
    w.u(1, 1)        # temporal_id_nesting
    w.u(8, 0x01)     # profile space/tier/idc (Main)
    w.u(32, 0x60000000)
    w.u(48, 0x900000000000)
    w.u(8, 90)       # level
    w.ue(0)          # sps_id
    w.ue(chroma)
    if chroma == 3:
        w.u(1, 0)
    w.ue(width)
    w.ue(height)
    w.u(1, 0)        # conformance_window_flag
    w.ue(0)          # bit_depth_luma_minus8
    w.ue(0)          # bit_depth_chroma_minus8
    return bytes([33 << 1, 1]) + w.bytes()


def nal(t, payload=b"\x00"):
    return bytes([t << 1, 1]) + payload


def annexb(*nals):
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


def fake_stream(width, height, chroma=1, slice_payload=b"\x80\x11\x22"):
    """VPS/SPS/PPS + one IDR slice (first_slice_segment_in_pic_flag set)."""
    return annexb(nal(32, b"\x0c\x01"), make_sps(width, height, chroma), nal(34, b"\xc1"),
                  nal(19, slice_payload))


def make_wxgf(color, alpha=None):
    """wxgf-like container: header then [u32 len][stream] partitions
    (alpha first, as observed in real files)."""
    head = b"wxgf" + bytes([0x12 if alpha else 0x13]) + b"\x00" * 19
    parts = ([alpha] if alpha else []) + [color]
    return head + b"".join(struct.pack(">I", len(p)) + p for p in parts)


def make_bmp(w, h, pixel):
    """pixel(x, y) -> (r, g, b); top-down rows written bottom-up."""
    row = (w * 3 + 3) & ~3
    data = b"".join(
        b"".join(bytes(pixel(x, y)[::-1]) for x in range(w)).ljust(row, b"\0")
        for y in reversed(range(h)))
    return (b"BM" + struct.pack("<IHHI", 54 + len(data), 0, 0, 54) +
            struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(data), 2835, 2835, 0, 0) + data)


def _boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack(">I4s", data[i:i + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        yield typ, i + hdr, i + size
        i += size


def heic_to_annexb(heic):
    """Primary item's HEVC stream (parameter sets from hvcC + item data) as
    Annex-B. Handles the simple single-image HEICs sips writes."""
    meta = next((s, e) for t, s, e in _boxes(heic) if t == b"meta")
    boxes = {t: (s, e) for t, s, e in _boxes(heic, meta[0] + 4, meta[1])}
    ps, pe = boxes[b"pitm"]
    primary = struct.unpack(">H", heic[ps + 4:ps + 6])[0]
    s, e = boxes[b"iprp"]
    ipco = next((a, b) for t, a, b in _boxes(heic, s, e) if t == b"ipco")
    hvcc = next(heic[a:b] for t, a, b in _boxes(heic, *ipco) if t == b"hvcC")
    nals = []
    i, n_arrays = 23, hvcc[22]
    for _ in range(n_arrays):
        count = struct.unpack(">H", hvcc[i + 1:i + 3])[0]
        i += 3
        for _ in range(count):
            ln = struct.unpack(">H", hvcc[i:i + 2])[0]
            nals.append(hvcc[i + 2:i + 2 + ln])
            i += 2 + ln
    s, e = boxes[b"iloc"]
    ver = heic[s]
    b1, b2 = heic[s + 4], heic[s + 5]
    off_sz, len_sz, base_sz = b1 >> 4, b1 & 15, b2 >> 4
    idx_sz = b2 & 15 if ver in (1, 2) else 0
    j = s + 6
    count = struct.unpack(">H", heic[j:j + 2])[0]
    j += 2

    def rd(n):
        nonlocal j
        v = int.from_bytes(heic[j:j + n], "big") if n else 0
        j += n
        return v
    data = None
    for _ in range(count):
        item = rd(2)
        if ver in (1, 2):
            rd(2)
        rd(2)  # data_reference_index
        base = rd(base_sz)
        chunks = []
        for _ in range(rd(2)):
            rd(idx_sz)
            off, ln = rd(off_sz), rd(len_sz)
            chunks.append(heic[base + off:base + off + ln])
        if item == primary:
            data = b"".join(chunks)
    k = 0
    while k + 4 <= len(data):
        ln = struct.unpack(">I", data[k:k + 4])[0]
        nals.append(data[k + 4:k + 4 + ln])
        k += 4 + ln
    return annexb(*nals)
