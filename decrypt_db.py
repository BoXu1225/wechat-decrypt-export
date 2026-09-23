"""
WeChat 4.0 数据库解密器

使用从进程内存提取的per-DB enc_key解密SQLCipher 4加密的数据库
参数: SQLCipher 4, AES-256-CBC, HMAC-SHA512, reserve=80, page_size=4096
密钥来源: all_keys.json (由find_all_keys.py从内存提取)

WAL: 微信以 WAL 模式写库，最近的写入可能还在 <name>.db-wal 里、尚未 checkpoint
回主文件。解密时一并读取 -wal，把其中已提交、校验通过的页面直接替换进输出，
得到一个包含最新数据、不依赖 WAL 的普通 SQLite 文件。微信的文件只以只读方式
打开（从不用 sqlite 打开，也不加锁）。
"""
import hashlib, struct, os, sys, json
import hmac as hmac_mod
from Crypto.Cipher import AES

import functools
print = functools.partial(print, flush=True)

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80  # IV(16) + HMAC(64)
SQLITE_HDR = b'SQLite format 3\x00'

# SQLite WAL 格式: 32 字节文件头 + 若干帧（24 字节帧头 + 一页）
WAL_HDR_SZ = 32
WAL_FRAME_HDR_SZ = 24
WAL_MAGIC_LE = 0x377f0682   # 校验和按小端 32 位字计算
WAL_MAGIC_BE = 0x377f0683   # 校验和按大端 32 位字计算

# 记录每个库上次解密时源文件（.db 和 -wal）的 mtime/大小，用于增量判断
STATE_FILE = ".decrypt_state.json"
# 源文件在解密过程中被修改（微信正在 checkpoint）时的重试次数
SNAPSHOT_RETRIES = 5

from config import load_config, private_dir, private_opener, secure_outputs, write_private
_cfg = load_config()
DB_DIR = _cfg["db_dir"]
OUT_DIR = _cfg["decrypted_dir"]
KEYS_FILE = _cfg["keys_file"]

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SCANNER_SRC = os.path.join(PROJECT_ROOT, "find_all_keys_macos.c")
SCANNER_BIN = os.path.join(PROJECT_ROOT, "find_all_keys_macos")


def list_db_files():
    """返回 db_dir 下所有数据库的相对路径（统一正斜杠）"""
    rels = []
    for root, dirs, files in os.walk(DB_DIR):
        for f in files:
            if f.endswith('.db'):
                rels.append(os.path.relpath(os.path.join(root, f), DB_DIR).replace('\\', '/'))
    return rels


def load_keys():
    if not os.path.exists(KEYS_FILE):
        return {}
    with open(KEYS_FILE) as f:
        keys = json.load(f)
    keys.pop("_db_dir", None)
    return keys


def run_key_scanner():
    """编译（如需要）并以 root 运行内存密钥扫描器，生成 all_keys.json"""
    import subprocess
    if (not os.path.exists(SCANNER_BIN)
            or os.path.getmtime(SCANNER_BIN) < os.path.getmtime(SCANNER_SRC)):
        print("[+] 编译密钥扫描器 ...")
        subprocess.run(["cc", "-O2", "-o", SCANNER_BIN, SCANNER_SRC,
                        "-framework", "Foundation"], check=True)
    print("[!] 提取密钥需要 root 权限（微信必须正在运行，且已 ad-hoc 签名）")
    subprocess.run(["sudo", SCANNER_BIN], cwd=PROJECT_ROOT, check=True)


def ensure_keys():
    """密钥文件缺失，或自上次提取后有数据库新增/变化且没有可用密钥时，自动运行扫描器"""
    keys = load_keys()
    last_scan = os.path.getmtime(KEYS_FILE) if keys else 0
    # 缺少密钥或密钥已过期（数据库被重建、salt 改变）。
    # 只看上次提取后有变化的数据库，避免对扫描器找不到密钥的库反复要求 sudo
    missing = [r for r in list_db_files()
               if os.path.getmtime(os.path.join(DB_DIR, r)) > last_scan
               and (r not in keys or not key_is_valid(r, keys[r]["enc_key"]))]
    if keys and not missing:
        return keys
    if keys:
        print(f"[!] {len(missing)} 个数据库缺少密钥或密钥已过期（例如 {missing[0]}），重新提取 ...")
    else:
        print(f"[!] 未找到密钥文件 {KEYS_FILE}，开始提取 ...")
    try:
        run_key_scanner()
    except Exception as e:
        print(f"[!] 密钥提取失败: {e}")
        if not keys:
            sys.exit(1)

    # 扫描器会覆盖密钥文件：保留本次没扫到、但仍然有效的旧密钥。
    # 写回同时刷新文件时间，本次仍未找到密钥的库不会在下次运行时再触发扫描
    new_keys = load_keys()
    for rel, entry in keys.items():
        if rel not in new_keys and os.path.exists(os.path.join(DB_DIR, rel)) \
                and key_is_valid(rel, entry["enc_key"]):
            new_keys[rel] = entry
    write_private(KEYS_FILE, json.dumps(new_keys, indent=2))
    return new_keys


def derive_mac_key(enc_key, salt):
    """从enc_key派生HMAC密钥"""
    mac_salt = bytes(b ^ 0x3a for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def page_hmac_ok(page, pgno, mac_key):
    """校验单个加密页面的 HMAC-SHA512（页面数据 + IV + 小端页号）。
    page 1 的前 16 字节是 salt，不参与 HMAC（WAL 里的 page 1 也一样带 salt 前缀）"""
    start = SALT_SZ if pgno == 1 else 0
    hm = hmac_mod.new(mac_key, page[start : PAGE_SZ - RESERVE_SZ + IV_SZ], hashlib.sha512)
    hm.update(struct.pack('<I', pgno))
    return hmac_mod.compare_digest(hm.digest(), page[PAGE_SZ - HMAC_SZ : PAGE_SZ])


def page1_hmac_ok(page1, enc_key):
    """用 page 1 的 HMAC 校验密钥是否匹配该数据库"""
    return page_hmac_ok(page1, 1, derive_mac_key(enc_key, page1[:SALT_SZ]))


def key_is_valid(rel, enc_key_hex):
    with open(os.path.join(DB_DIR, rel), 'rb') as f:
        page1 = f.read(PAGE_SZ)
    return len(page1) == PAGE_SZ and page1_hmac_ok(page1, bytes.fromhex(enc_key_hex))


def decrypt_page(enc_key, page_data, pgno):
    """解密单个页面，输出4096字节的标准SQLite页面"""
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]

    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        page = bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ)
        # 保留 reserve=80, B-tree 基于 usable_size=4016 构建
        return bytes(page)
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def _wal_checksum(data, s0, s1, big_endian):
    """SQLite WAL 累积校验和（对加密后的原始字节计算）"""
    words = struct.unpack(('>' if big_endian else '<') + '%dI' % (len(data) // 4), data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def parse_wal(wal, mac_key):
    """解析 SQLCipher WAL 的原始字节，返回 (pages, db_pages, info)。

    pages: {页号: 加密页面}，只包含已提交事务里的页面，同一页取最后一次写入。
    db_pages: 最后一个有效提交帧记录的数据库总页数；没有有效提交时为 None。
    帧必须依次满足: salt 与文件头一致、累积校验和正确、页面 HMAC 正确；
    遇到第一个不满足的帧即视为日志结束（之后是旧的或写了一半的帧）。
    最后一个提交帧之后的帧属于未提交事务，忽略。"""
    info = {"frames": 0, "commits": 0, "uncommitted": 0, "stop": "eof"}
    if len(wal) < WAL_HDR_SZ:
        info["stop"] = "no wal" if not wal else "short header"
        return {}, None, info
    magic, _ver, page_sz, _ckpt, salt1, salt2, ck1, ck2 = struct.unpack('>8I', wal[:WAL_HDR_SZ])
    if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE) or page_sz != PAGE_SZ:
        info["stop"] = "bad header"
        return {}, None, info
    big = bool(magic & 1)
    cks = _wal_checksum(wal[:WAL_HDR_SZ - 8], 0, 0, big)
    if cks != (ck1, ck2):
        info["stop"] = "bad header checksum"
        return {}, None, info

    frame_sz = WAL_FRAME_HDR_SZ + PAGE_SZ
    committed, pending = {}, {}
    db_pages = None
    off = WAL_HDR_SZ
    while True:
        if off + frame_sz > len(wal):
            if off < len(wal):
                info["stop"] = "partial frame"
            break
        fh = wal[off : off + WAL_FRAME_HDR_SZ]
        pgno, commit, fs1, fs2, fc1, fc2 = struct.unpack('>6I', fh)
        page = wal[off + WAL_FRAME_HDR_SZ : off + frame_sz]
        if (fs1, fs2) != (salt1, salt2):
            info["stop"] = "salt mismatch"   # 上一轮 WAL 残留的旧帧
            break
        if pgno == 0:
            info["stop"] = "bad page number"
            break
        cks = _wal_checksum(fh[:8], cks[0], cks[1], big)
        cks = _wal_checksum(page, cks[0], cks[1], big)
        if cks != (fc1, fc2):
            info["stop"] = "bad checksum"
            break
        if not page_hmac_ok(page, pgno, mac_key):
            info["stop"] = "bad hmac"
            break
        pending[pgno] = page
        info["frames"] += 1
        if commit:
            committed.update(pending)
            pending.clear()
            db_pages = commit
            info["commits"] += 1
            info["uncommitted"] = 0
        else:
            info["uncommitted"] += 1
        off += frame_sz
    info["frames"] -= info["uncommitted"]
    return committed, db_pages, info


def source_sig(db_path):
    """源数据库 (.db) 及其 -wal 的 (mtime_ns, size)；.db 不存在时返回 None"""
    try:
        st = os.stat(db_path)
    except FileNotFoundError:
        return None
    try:
        w = os.stat(db_path + "-wal")
        wal = [w.st_mtime_ns, w.st_size]
    except FileNotFoundError:
        wal = None
    return {"db": [st.st_mtime_ns, st.st_size], "wal": wal}


def load_state(out_dir):
    """读取 out_dir 下的解密状态 {rel: source_sig}"""
    try:
        with open(os.path.join(out_dir, STATE_FILE)) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(out_dir, updates):
    """把 {rel: source_sig} 合并进状态文件（重新读取后合并、原子替换，
    这样 decrypt 和 MCP 刷新同时写也不会丢掉对方的大部分记录）"""
    if not updates:
        return
    state = load_state(out_dir)
    state.update({rel: {"db": sig["db"], "wal": sig["wal"]} for rel, sig in updates.items()})
    private_dir(out_dir)
    write_private(os.path.join(out_dir, STATE_FILE), json.dumps(state, indent=1, sort_keys=True))


def is_current(state, rel, db_path, out_path):
    """解密结果存在，且源 .db 和 -wal 自上次解密后都没有变化"""
    return os.path.exists(out_path) and state.get(rel) == source_sig(db_path)


def _decrypt_snapshot(db_path, out_path, enc_key):
    """读取 .db 和 -wal 并写出合并后的明文数据库；成功返回 WAL 统计信息，失败返回 None"""
    file_size = os.path.getsize(db_path)
    main_pages = (file_size + PAGE_SZ - 1) // PAGE_SZ
    if file_size % PAGE_SZ != 0:
        print(f"  [!] 文件大小 {file_size} 不是 {PAGE_SZ} 的倍数")

    with open(db_path, 'rb') as fin:
        page1 = fin.read(PAGE_SZ)
    if len(page1) < PAGE_SZ:
        print(f"  [!] 文件太小")
        return None

    # 验证page 1
    if not page1_hmac_ok(page1, enc_key):
        print(f"  [!] Page 1 HMAC 验证失败! salt: {page1[:SALT_SZ].hex()}")
        return None
    mac_key = derive_mac_key(enc_key, page1[:SALT_SZ])

    # 先整体读入 -wal（最多几 MB），再读主文件
    try:
        with open(db_path + "-wal", 'rb') as f:
            wal = f.read()
    except FileNotFoundError:
        wal = b''
    wal_pages, wal_db_pages, info = parse_wal(wal, mac_key)
    total_pages = wal_db_pages or main_pages
    info["pages"] = len(wal_pages)
    info["total_pages"] = total_pages

    msg = f"  HMAC OK, {total_pages} pages"
    if wal_pages:
        msg += f", WAL: {info['frames']} 帧/{info['commits']} 个事务 ({len(wal_pages)} 页)"
    if info["uncommitted"]:
        msg += f", 忽略 {info['uncommitted']} 个未提交帧"
    print(msg)

    missing = 0
    with open(db_path, 'rb') as fin, open(out_path, 'wb', opener=private_opener) as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ) if pgno <= main_pages else b''
            if pgno in wal_pages:
                page = wal_pages[pgno]
            elif len(page) < PAGE_SZ:
                if not page:
                    # 提交帧说库更大、但这页既不在主文件也不在 WAL 里（不应发生）
                    missing += 1
                    fout.write(b'\x00' * PAGE_SZ)
                    continue
                page = page + b'\x00' * (PAGE_SZ - len(page))

            decrypted = decrypt_page(enc_key, page, pgno)
            if pgno == 1:
                if decrypted[:16] != SQLITE_HDR:
                    print(f"  [!] 解密后 header 不匹配!")
                hdr = bytearray(decrypted)
                hdr[18] = hdr[19] = 1                         # 普通 rollback 模式，不需要 -wal/-shm
                hdr[28:32] = struct.pack('>I', total_pages)   # 库大小以最后一次提交为准
                hdr[92:96] = hdr[24:28]                       # 让上面的库大小生效
                decrypted = bytes(hdr)
            fout.write(decrypted)

            if pgno % 10000 == 0:
                print(f"  进度: {pgno}/{total_pages} ({100*pgno/total_pages:.1f}%)")
    if missing:
        print(f"  [!] {missing} 个页面缺失，已填零")
    return info


def decrypt_database(db_path, out_path, enc_key):
    """解密整个数据库（含 -wal 中已提交的内容），原子替换 out_path。

    微信可能同时在写：解密前后各取一次 .db 的 mtime/大小，变化了（说明期间发生了
    checkpoint）就重试。-wal 在读主文件之前一次性读入，只追加新帧不影响已读到的
    快照；读到写了一半的帧会因校验失败被截断。
    成功返回解密前的 source_sig（真值，供 save_state 记录），失败返回 False。"""
    if os.path.dirname(out_path):
        private_dir(os.path.dirname(out_path))
    tmp = out_path + ".tmp"
    try:
        for _ in range(SNAPSHOT_RETRIES):
            before = source_sig(db_path)
            if before is None:
                print(f"  [!] 文件不存在")
                return False
            info = _decrypt_snapshot(db_path, tmp, enc_key)
            if info is None:
                return False
            after = source_sig(db_path)
            if after is not None and after["db"] == before["db"]:
                os.replace(tmp, out_path)
                before["wal_info"] = info
                return before
            print(f"  [!] 解密期间数据库被修改，重试 ...")
        print(f"  [!] 数据库持续变化，放弃本次解密")
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main():
    print("=" * 60)
    print("  WeChat 4.0 数据库解密器")
    print("=" * 60)
    secure_outputs(_cfg)

    keys = ensure_keys()
    print(f"\n[+] 加载 {len(keys)} 个数据库密钥")
    print(f"[+] 输出目录: {OUT_DIR}")
    private_dir(OUT_DIR)

    # 收集所有DB文件
    db_files = []
    for root, dirs, files in os.walk(DB_DIR):
        for f in files:
            if f.endswith('.db') and not f.endswith('-wal') and not f.endswith('-shm'):
                path = os.path.join(root, f)
                rel = os.path.relpath(path, DB_DIR).replace('\\', '/')
                sz = os.path.getsize(path)
                db_files.append((rel, path, sz))

    db_files.sort(key=lambda x: x[2])  # 从小到大

    print(f"[+] 找到 {len(db_files)} 个数据库文件\n")

    success = 0
    skipped = 0
    no_key = 0
    failed = 0
    total_bytes = 0
    wal_frames = 0
    state = load_state(OUT_DIR)

    for rel, path, sz in db_files:
        # 统一用正斜杠查找key
        rel_key = rel.replace('\\', '/')
        if rel_key not in keys:
            # 微信从未打开过的库（如 migrate/unspportmsg.db）内存里没有密钥，不算失败
            print(f"[!] 跳过: {rel}（无密钥）")
            no_key += 1
            continue

        enc_key = bytes.fromhex(keys[rel_key]["enc_key"])
        out_path = os.path.join(OUT_DIR, rel)

        # 跳过已解密且 .db / -wal 都未变化的数据库
        if is_current(state, rel_key, path, out_path):
            skipped += 1
            continue

        print(f"解密: {rel} ({sz/1024/1024:.1f}MB) ...", end=" ")

        sig = decrypt_database(path, out_path, enc_key)
        if sig:
            wal_frames += sig["wal_info"]["frames"]
            save_state(OUT_DIR, {rel_key: sig})
            # SQLite验证
            try:
                import sqlite3
                conn = sqlite3.connect(out_path)
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                conn.close()
                table_names = [t[0] for t in tables]
                print(f"  OK! 表: {', '.join(table_names[:5])}", end="")
                if len(table_names) > 5:
                    print(f" ...共{len(table_names)}个", end="")
                print()
                success += 1
                total_bytes += sz
            except Exception as e:
                print(f"  [!] SQLite 验证失败: {e}")
                failed += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"[+] 结果: {success} 成功, {skipped} 跳过(未变化), {no_key} 跳过(无密钥), {failed} 失败, 共 {len(db_files)} 个")
    if total_bytes > 0:
        print(f"[+] 本次解密: {total_bytes/1024/1024/1024:.1f}GB")
    if wal_frames:
        print(f"[+] 从 WAL 合并了 {wal_frames} 个已提交帧")
    print(f"[+] 解密文件在: {OUT_DIR}")


if __name__ == '__main__':
    main()
