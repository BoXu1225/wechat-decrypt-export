"""
配置加载器 - 从 config.json 读取路径配置
首次运行时自动检测微信数据目录，检测失败则提示手动配置
"""
import glob
import json
import os
import re
import stat
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")

_DEFAULT_TEMPLATE_DIR = "/Users/YOU/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/YOUR_WXID/db_storage"

_XWECHAT_FILES = os.path.expanduser(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")

_DEFAULT = {
    "db_dir": _DEFAULT_TEMPLATE_DIR,
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "decoded_image_dir": "decoded_images",
    "wechat_process": "WeChat",
}


def auto_detect_db_dir():
    """自动检测 macOS 微信数据目录: xwechat_files/<wxid>_<后缀>/db_storage"""
    candidates = sorted(
        d for d in glob.glob(os.path.join(_XWECHAT_FILES, "*", "db_storage"))
        if os.path.isdir(d)
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # 非交互环境直接取最近修改的那个
        if not sys.stdin.isatty():
            return max(candidates, key=os.path.getmtime)
        print("[!] 检测到多个微信账号数据目录（请选择当前正在运行的微信账号）:")
        for i, c in enumerate(candidates, 1):
            print(f"    {i}. {c}")
        print("    0. 跳过，稍后手动配置")
        try:
            while True:
                choice = input("请选择 [0-{}]: ".format(len(candidates))).strip()
                if choice == "0":
                    return None
                if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                    return candidates[int(choice) - 1]
                print("    无效输入，请重新选择")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    return None


def wxid_from_db_dir(db_dir):
    """从数据目录推导自己的微信 ID。
    目录名格式为 <wxid>_<4位后缀>，例如 wxid_abc123_1a2b -> wxid_abc123"""
    account_dir = os.path.basename(os.path.dirname(os.path.normpath(db_dir)))
    return re.sub(r"_[0-9a-f]{4}$", "", account_dir)


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        except json.JSONDecodeError:
            print(f"[!] {CONFIG_FILE} 格式损坏，将使用默认配置")
            cfg = {}

    # db_dir 缺失或仍为模板值时，尝试自动检测
    db_dir = cfg.get("db_dir", "")
    if not db_dir or db_dir == _DEFAULT_TEMPLATE_DIR or "your_wxid" in db_dir:
        detected = auto_detect_db_dir()
        if detected:
            print(f"[+] 自动检测到微信数据目录: {detected}")
            # 合并默认值并保存
            cfg = {**_DEFAULT, **cfg, "db_dir": detected}
            write_private(CONFIG_FILE, json.dumps(cfg, indent=4, ensure_ascii=False))
            print(f"[+] 已保存到: {CONFIG_FILE}")
        else:
            if not os.path.exists(CONFIG_FILE):
                write_private(CONFIG_FILE, json.dumps(_DEFAULT, indent=4))
            print("[!] 未能自动检测微信数据目录")
            print(f"    请手动编辑 {CONFIG_FILE} 中的 db_dir 字段")
            print(f"    路径位于 {_XWECHAT_FILES}/<你的微信ID>/db_storage")
            sys.exit(1)

    # 将相对路径转为绝对路径
    base = os.path.dirname(os.path.abspath(__file__))
    for key in ("keys_file", "decrypted_dir", "decoded_image_dir"):
        if key in cfg and not os.path.isabs(cfg[key]):
            cfg[key] = os.path.join(base, cfg[key])

    # 自动推导微信数据根目录（db_dir 的上级目录）和自己的微信 ID
    db_dir = cfg.get("db_dir", "")
    if db_dir and os.path.basename(db_dir) == "db_storage":
        cfg["wechat_base_dir"] = os.path.dirname(db_dir)
    else:
        cfg["wechat_base_dir"] = db_dir
    cfg["self_wxid"] = wxid_from_db_dir(db_dir)

    # decoded_image_dir 默认值
    if "decoded_image_dir" not in cfg:
        cfg["decoded_image_dir"] = os.path.join(base, "decoded_images")

    return cfg


# ---------------------------------------------------------------------------
# 文件权限：解密出的数据库、密钥、配置、日志、导出的聊天记录都只允许本人读写
# （目录 0700，文件 0600），其他本地用户不可读。
# ---------------------------------------------------------------------------

PRIVATE_UMASK = 0o077


def private_opener(path, flags):
    """open(..., opener=private_opener)：新建的文件为 0600（已存在的文件权限不变）"""
    return os.open(path, flags, 0o600)


def chmod_private(path):
    """去掉 group/other 的权限位（0644 -> 0600，0755 -> 0700）；不跟随符号链接，
    只处理自己拥有的文件。权限有变化时返回 True"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or not st.st_mode & 0o077:
        return False
    os.chmod(path, stat.S_IMODE(st.st_mode) & ~0o077)
    return True


def private_dir(path):
    """创建目录（连同缺失的上级目录都是 0700），已存在则收紧为 0700；返回 path"""
    old = os.umask(PRIVATE_UMASK)   # makedirs 的 mode 只作用于最后一级
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    finally:
        os.umask(old)
    chmod_private(path)
    return path


def write_private(path, text):
    """原子写入文本文件，权限 0600（替换后的文件属于当前用户）"""
    path = os.path.realpath(path)   # path 是符号链接时写入其目标
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", opener=private_opener) as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def tighten_tree(path):
    """收紧 path（文件或目录树）的权限。只有顶层权限过宽时才遍历整棵树，
    所以已处理过的目录每次只需一次 stat。返回修改的条目数"""
    if not chmod_private(path):
        return 0
    n = 1
    if os.path.isdir(path) and not os.path.islink(path):
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                n += chmod_private(os.path.join(root, name))
    return n


def _is_wechat_path(path, cfg):
    """微信自己的数据目录（绝不修改其权限）"""
    real = os.path.realpath(path)
    roots = [os.path.dirname(_XWECHAT_FILES)]   # .../com.tencent.xinWeChat/Data/Documents
    roots += [cfg[k] for k in ("db_dir", "wechat_base_dir") if cfg.get(k)]
    for r in roots:
        r = os.path.realpath(r)
        if real == r or real.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def output_paths(cfg=None):
    """本工具写出的路径（相对路径以项目目录为基准）：[(path, 是否目录树)]"""
    cfg = cfg or {}

    def resolve(p):
        return p if os.path.isabs(p) else os.path.join(PROJECT_ROOT, p)

    paths = [(CONFIG_FILE, False),
             (os.path.join(PROJECT_ROOT, "export"), True),
             (os.path.join(PROJECT_ROOT, "logs"), True)]
    for key in ("keys_file", "mcp_access_log"):
        if cfg.get(key):
            paths.append((resolve(cfg[key]), False))
    for key in ("decrypted_dir", "decoded_image_dir"):
        if cfg.get(key):
            paths.append((resolve(cfg[key]), True))
    idx = cfg.get("mcp_index_path")
    if idx:
        paths += [(resolve(idx) + s, False) for s in ("", "-wal", "-shm")]
    return paths


def secure_outputs(cfg=None):
    """入口调用一次：设置 umask 0o077（此后新建的文件/目录只有本人可访问），
    并收紧已有输出路径的权限（旧版本创建的 0755/0644）。
    不处理微信自己的目录，也不处理项目目录本身或其上级目录。返回修改的条目数"""
    os.umask(PRIVATE_UMASK)
    cfg = cfg or {}
    root = os.path.realpath(PROJECT_ROOT)
    home = os.path.realpath(os.path.expanduser("~"))
    n = 0
    for path, tree in output_paths(cfg):
        real = os.path.realpath(path)
        if (real == home or root == real or root.startswith(real.rstrip(os.sep) + os.sep)
                or _is_wechat_path(path, cfg)):
            continue
        try:
            n += tighten_tree(path) if tree else chmod_private(path)
        except OSError as e:
            print(f"[!] 无法修改权限 {path}: {e}", file=sys.stderr)
    if n:
        print(f"[+] 已将 {n} 个输出文件/目录的权限收紧为仅本人可访问（目录 700，文件 600）",
              file=sys.stderr)
    return n
