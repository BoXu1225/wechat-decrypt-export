"""
WeChat 4.0 数据库解密器

使用从进程内存提取的per-DB enc_key解密SQLCipher 4加密的数据库
参数: SQLCipher 4, AES-256-CBC, HMAC-SHA512, reserve=80, page_size=4096
密钥来源: all_keys.json (由find_all_keys.py从内存提取)
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

from config import load_config
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
        print("编译密钥扫描器 ...")
        subprocess.run(["cc", "-O2", "-o", SCANNER_BIN, SCANNER_SRC,
                        "-framework", "Foundation"], check=True)
    print("提取密钥需要 root 权限（微信必须正在运行，且已 ad-hoc 签名）")
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
        print(f"{len(missing)} 个数据库缺少密钥或密钥已过期（例如 {missing[0]}），重新提取 ...")
    else:
        print(f"未找到密钥文件 {KEYS_FILE}，开始提取 ...")
    try:
        run_key_scanner()
    except Exception as e:
        print(f"[ERROR] 密钥提取失败: {e}")
        if not keys:
            sys.exit(1)

    # 扫描器会覆盖密钥文件：保留本次没扫到、但仍然有效的旧密钥。
    # 写回同时刷新文件时间，本次仍未找到密钥的库不会在下次运行时再触发扫描
    new_keys = load_keys()
    for rel, entry in keys.items():
        if rel not in new_keys and os.path.exists(os.path.join(DB_DIR, rel)) \
                and key_is_valid(rel, entry["enc_key"]):
            new_keys[rel] = entry
    with open(KEYS_FILE, "w") as f:
        json.dump(new_keys, f, indent=2)
    return new_keys


def derive_mac_key(enc_key, salt):
    """从enc_key派生HMAC密钥"""
    mac_salt = bytes(b ^ 0x3a for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def page1_hmac_ok(page1, enc_key):
    """用 page 1 的 HMAC 校验密钥是否匹配该数据库"""
    mac_key = derive_mac_key(enc_key, page1[:SALT_SZ])
    hm = hmac_mod.new(mac_key, page1[SALT_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ], hashlib.sha512)
    hm.update(struct.pack('<I', 1))
    return hm.digest() == page1[PAGE_SZ - HMAC_SZ : PAGE_SZ]


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


def decrypt_database(db_path, out_path, enc_key):
    """解密整个数据库文件"""
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ

    if file_size % PAGE_SZ != 0:
        print(f"  [WARN] 文件大小 {file_size} 不是 {PAGE_SZ} 的倍数")
        total_pages += 1

    with open(db_path, 'rb') as fin:
        page1 = fin.read(PAGE_SZ)

    if len(page1) < PAGE_SZ:
        print(f"  [ERROR] 文件太小")
        return False

    # 验证page 1
    if not page1_hmac_ok(page1, enc_key):
        print(f"  [ERROR] Page 1 HMAC验证失败! salt: {page1[:SALT_SZ].hex()}")
        return False

    print(f"  HMAC OK, {total_pages} pages")

    # 解密所有页面
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(db_path, 'rb') as fin, open(out_path, 'wb') as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                else:
                    break

            decrypted = decrypt_page(enc_key, page, pgno)
            fout.write(decrypted)

            if pgno == 1:
                if decrypted[:16] != SQLITE_HDR:
                    print(f"  [WARN] 解密后header不匹配!")

            if pgno % 10000 == 0:
                print(f"  进度: {pgno}/{total_pages} ({100*pgno/total_pages:.1f}%)")

    return True


def main():
    print("=" * 60)
    print("  WeChat 4.0 数据库解密器")
    print("=" * 60)

    keys = ensure_keys()
    print(f"\n加载 {len(keys)} 个数据库密钥")
    print(f"输出目录: {OUT_DIR}")
    os.makedirs(OUT_DIR, exist_ok=True)

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

    print(f"找到 {len(db_files)} 个数据库文件\n")

    success = 0
    skipped = 0
    no_key = 0
    failed = 0
    total_bytes = 0

    for rel, path, sz in db_files:
        # 统一用正斜杠查找key
        rel_key = rel.replace('\\', '/')
        if rel_key not in keys:
            # 微信从未打开过的库（如 migrate/unspportmsg.db）内存里没有密钥，不算失败
            print(f"SKIP: {rel} (无密钥)")
            no_key += 1
            continue

        enc_key = bytes.fromhex(keys[rel_key]["enc_key"])
        out_path = os.path.join(OUT_DIR, rel)

        # 跳过已解密且未变化的数据库
        if os.path.exists(out_path) and os.path.getmtime(out_path) >= os.path.getmtime(path):
            skipped += 1
            continue

        print(f"解密: {rel} ({sz/1024/1024:.1f}MB) ...", end=" ")

        ok = decrypt_database(path, out_path, enc_key)
        if ok:
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
                print(f"  [WARN] SQLite验证失败: {e}")
                failed += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"结果: {success} 成功, {skipped} 跳过(未变化), {no_key} 跳过(无密钥), {failed} 失败, 共 {len(db_files)} 个")
    if total_bytes > 0:
        print(f"本次解密: {total_bytes/1024/1024/1024:.1f}GB")
    print(f"解密文件在: {OUT_DIR}")


if __name__ == '__main__':
    main()
