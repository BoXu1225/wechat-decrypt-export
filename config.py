"""
配置加载器 - 从 config.json 读取路径配置
首次运行时自动检测微信数据目录，检测失败则提示手动配置
"""
import glob
import json
import os
import re
import sys

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

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
            with open(CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            print(f"[+] 已保存到: {CONFIG_FILE}")
        else:
            if not os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "w") as f:
                    json.dump(_DEFAULT, f, indent=4)
            print(f"[!] 未能自动检测微信数据目录")
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
