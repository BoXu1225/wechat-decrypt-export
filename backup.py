#!/usr/bin/env python3
"""
无人值守备份: 解密有变化的数据库 -> 增量导出全部聊天 -> 写运行记录 -> 失败时通知。

用法（通过 ./wechat backup）:
    ./wechat backup                      # 立即运行一次（非交互，从不调用 sudo / 密钥扫描器）
    ./wechat backup --dir /path/to/dir   # 临时指定导出目录（也可用环境变量 WECHAT_BACKUP_DIR）
    ./wechat backup --install            # 安装每日定时任务（LaunchAgent）
    ./wechat backup --uninstall          # 卸载定时任务
    ./wechat backup --status             # 是否已安装、下次运行时间、上次运行结果

config.json 中的可选配置（以下为默认值）:
    "backup": {"formats": ["html", "txt"], "media": true, "dir": "export", "time": "03:30"}
    formats  导出格式（txt / md / html / json / csv），每种格式各做一次 --all -i
    media    导出图片等媒体（导出命令支持 --media 时用 --media，否则用 --images）
    dir      导出目录，相对路径相对于项目目录
    time     每天运行的时间 HH:MM（修改后需重新 --install）

退出码:
    0  成功
    1  失败（解密或导出出错）
    2  配置或参数错误
    3  已有备份在运行（本次跳过）
    4  备份完成，但有数据库需要新密钥：请手动运行 ./wechat decrypt（需要 sudo）

每次运行在 logs/backup.jsonl 追加一行摘要（时间、耗时、更新的聊天数、新增消息数、
跳过的数据库、错误），不含任何消息内容或聊天名称。

密钥缺失或过期时绝不提权：对应数据库跳过（导出使用上次解密的数据），并发送一条通知。
只读文件，不需要图形界面，屏幕锁定时也能运行。
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import functools
import io
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import time

print = functools.partial(print, flush=True)

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, "config.json")
LOG_DIR = os.path.join(ROOT, "logs")
SUMMARY_FILE = os.path.join(LOG_DIR, "backup.jsonl")
LOCK_FILE = os.path.join(LOG_DIR, "backup.lock")
EXPORT_SCRIPT = os.path.join(ROOT, "export_chat.py")

LABEL = "local.wechat-decrypt-export.backup"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LAUNCHD_LOG_DIR = os.path.expanduser("~/Library/Logs/wechat-decrypt-export")
LAUNCHD_LOG = os.path.join(LAUNCHD_LOG_DIR, "backup.log")

ALL_FORMATS = ("txt", "md", "html", "json", "csv")
DEFAULTS = {"formats": ["html", "txt"], "media": True, "dir": "export", "time": "03:30"}
EXPORT_TIMEOUT = 4 * 3600  # 单个格式导出的上限（秒）

EXIT_OK, EXIT_FAIL, EXIT_CONFIG, EXIT_LOCKED, EXIT_NEEDS_KEYS = 0, 1, 2, 3, 4

NOTIFY_TITLE = "微信聊天备份"


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def read_raw_config(path=CONFIG_FILE):
    """config.json 原始内容（不做自动检测、不交互）；不存在时返回 {}。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"无法读取 {path}: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 顶层应为对象")
    return data


def parse_time(s):
    """'HH:MM' -> (hour, minute)"""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(s))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ConfigError(f"backup.time 应为 HH:MM（例如 03:30），实际为 {s!r}")
    return int(m.group(1)), int(m.group(2))


def load_backup_config(raw, root=ROOT):
    """从 config.json 内容中取出并校验 "backup" 配置，填充默认值。"""
    b = raw.get("backup", {})
    if b is None:
        b = {}
    if not isinstance(b, dict):
        raise ConfigError('config.json 中 "backup" 应为对象')
    unknown = set(b) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"backup 中有未知配置项: {', '.join(sorted(unknown))}")
    cfg = {**DEFAULTS, **b}

    fmts = cfg["formats"]
    if isinstance(fmts, str):
        fmts = [fmts]
    if not isinstance(fmts, list) or not fmts:
        raise ConfigError("backup.formats 应为非空列表，例如 [\"html\", \"txt\"]")
    out = []
    for f in fmts:
        f = str(f).lower().strip()
        if f not in ALL_FORMATS:
            raise ConfigError(f"backup.formats 中的 {f!r} 不支持（可选: {', '.join(ALL_FORMATS)}）")
        if f not in out:
            out.append(f)
    cfg["formats"] = out

    if not isinstance(cfg["media"], bool):
        raise ConfigError("backup.media 应为 true 或 false")

    d = cfg["dir"]
    if not isinstance(d, str) or not d.strip():
        raise ConfigError("backup.dir 应为目录路径")
    d = os.path.expanduser(d.strip())
    cfg["dir"] = d if os.path.isabs(d) else os.path.normpath(os.path.join(root, d))

    cfg["hour"], cfg["minute"] = parse_time(cfg["time"])
    cfg["time"] = f"{cfg['hour']:02d}:{cfg['minute']:02d}"
    return cfg


# ---------------------------------------------------------------------------
# 锁
# ---------------------------------------------------------------------------

class Locked(Exception):
    pass


@contextlib.contextmanager
def run_lock(path=None):
    """独占锁，防止两次备份重叠运行。进程退出（包括崩溃）时内核自动释放。"""
    path = path or LOCK_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Locked(path)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# 解密（不提权）
# ---------------------------------------------------------------------------

def _forbidden(*_a, **_kw):
    raise RuntimeError("backup 不允许运行密钥扫描器 / sudo")


def import_decrypt_db():
    """导入 decrypt_db（导入时会读取 config.json）。"""
    try:
        import decrypt_db as D
    except SystemExit:
        raise ConfigError("config.json 中的 db_dir 未配置（请先交互运行一次 ./wechat decrypt）")
    return D


@contextlib.contextmanager
def no_key_scanner(D):
    """在解密期间把 decrypt_db 中会触发 sudo 的函数替换为直接报错（纵深防御）。"""
    saved = {n: getattr(D, n) for n in ("run_key_scanner", "ensure_keys") if hasattr(D, n)}
    for n in saved:
        setattr(D, n, _forbidden)
    try:
        yield
    finally:
        for n, f in saved.items():
            setattr(D, n, f)


def decrypt_changed(D, verbose=False):
    """解密有变化（.db 或 -wal）的数据库，只使用 all_keys.json 中现有的密钥。

    返回 {"decrypted", "unchanged", "failed": [rel], "needs_key": [rel], "no_key": [rel]}:
      needs_key  自上次提取密钥后有变化、但没有可用密钥（缺失或已过期）的库 -> 需要手动 ./wechat decrypt
      no_key     从未有过密钥、且自上次提取后未变化的库（例如微信从不打开的库），不算问题
    """
    res = {"decrypted": 0, "unchanged": 0, "failed": [], "needs_key": [], "no_key": []}
    secure = getattr(D, "secure_outputs", None)  # 部分版本会收紧输出目录权限
    if secure:
        secure(D._cfg)
    keys = D.load_keys()
    last_scan = os.path.getmtime(D.KEYS_FILE) if keys else 0
    state = D.load_state(D.OUT_DIR)
    os.makedirs(D.OUT_DIR, exist_ok=True)

    dbs = []
    for rel in D.list_db_files():
        path = os.path.join(D.DB_DIR, rel)
        try:
            dbs.append((os.path.getsize(path), rel, path))
        except OSError:
            continue
    dbs.sort()

    for _sz, rel, path in dbs:
        out_path = os.path.join(D.OUT_DIR, rel)
        try:
            changed_since_scan = os.path.getmtime(path) > last_scan
        except OSError:
            continue
        if rel not in keys:
            (res["needs_key"] if changed_since_scan else res["no_key"]).append(rel)
            continue
        if D.is_current(state, rel, path, out_path):
            res["unchanged"] += 1
            continue
        try:
            valid = D.key_is_valid(rel, keys[rel]["enc_key"])
        except Exception:
            valid = False
        if not valid:
            res["needs_key"].append(rel)   # 数据库被重建，密钥已过期
            continue
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sig = D.decrypt_database(path, out_path, bytes.fromhex(keys[rel]["enc_key"]))
        except Exception as e:
            sig = False
            buf.write(f"  [!] {type(e).__name__}: {e}\n")
        if verbose:
            print(f"  解密 {rel}: {buf.getvalue().strip()}")
        if not sig:
            res["failed"].append(rel)
            continue
        D.save_state(D.OUT_DIR, {rel: sig})
        try:
            conn = sqlite3.connect(f"file:{out_path}?mode=ro", uri=True)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            conn.close()
        except sqlite3.Error:
            res["failed"].append(rel)
            continue
        res["decrypted"] += 1
    return res


# ---------------------------------------------------------------------------
# 导出（子进程调用 export_chat.py --all -i）
# ---------------------------------------------------------------------------

def python_bin():
    venv = os.path.join(ROOT, "venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def export_capabilities(runner=subprocess.run):
    """export_chat.py 支持的选项（从 --help 中探测，兼容新旧版本）。"""
    p = runner([python_bin(), EXPORT_SCRIPT, "--help"], cwd=ROOT, stdin=subprocess.DEVNULL,
               capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"export_chat.py --help 退出码 {p.returncode}")
    return set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", p.stdout))


def media_flag(caps, want_media):
    if not want_media:
        return None
    if "--media" in caps:
        return "--media"
    if "--images" in caps:
        return "--images"
    return None


_DONE_RE = re.compile(r"完成: (\d+) 个聊天有新消息，共新增 (\d+) 条")
_CHAT_RE = re.compile(r"^\s*\[\d+/\d+\] .*: \+(\d+)\s*$", re.M)


def parse_export_output(out):
    """从 export_chat.py --all 的输出中取 (更新的聊天数, 新增消息数)；无法解析时为 (None, None)。"""
    m = None
    for m in _DONE_RE.finditer(out):
        pass
    if m:
        return int(m.group(1)), int(m.group(2))
    per_chat = [int(n) for n in _CHAT_RE.findall(out)]
    if per_chat:
        return len(per_chat), sum(per_chat)
    if "没有找到" in out and "记录" in out:
        return 0, 0
    return None, None


def build_export_cmd(fmt, export_dir, mflag):
    cmd = [python_bin(), EXPORT_SCRIPT, "--all", "-i", "-f", fmt,
           "--no-decrypt", "--export-dir", export_dir]
    if mflag:
        cmd.append(mflag)
    return cmd


def run_export(fmt, export_dir, mflag, runner=subprocess.run, verbose=False):
    t0 = time.monotonic()
    cmd = build_export_cmd(fmt, export_dir, mflag)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        p = runner(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=EXPORT_TIMEOUT, env=env)
        rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        rc, out, err = -1, "", f"超时（{EXPORT_TIMEOUT} 秒）"
    chats, msgs = parse_export_output(out)
    res = {"format": fmt, "exit_code": rc, "chats_updated": chats, "messages_added": msgs,
           "seconds": round(time.monotonic() - t0, 1)}
    if verbose and out:
        print(out.rstrip())
    if rc != 0:
        # 只打印到本地日志（不写入 backup.jsonl），便于排查
        tail = "\n".join(err.strip().splitlines()[-15:])
        if tail:
            print(f"  [{fmt}] stderr:\n{tail}", file=sys.stderr)
    return res


# ---------------------------------------------------------------------------
# 摘要与通知
# ---------------------------------------------------------------------------

def append_summary(entry, path=None):
    path = path or SUMMARY_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def last_summary(path=None):
    path = path or SUMMARY_FILE
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(message, title=NOTIFY_TITLE, runner=subprocess.run):
    """macOS 通知（osascript）；失败时忽略。"""
    script = f"display notification {_applescript_str(message)} with title {_applescript_str(title)}"
    try:
        runner(["/usr/bin/osascript", "-e", script], stdin=subprocess.DEVNULL,
               capture_output=True, timeout=15)
    except Exception:
        pass


def notification_text(entry):
    """需要通知时返回文本，否则返回 None（成功时不打扰）。"""
    parts = []
    if entry["status"] == "error":
        parts.append(f"备份失败（{len(entry['errors'])} 个错误），详见 logs/backup.jsonl")
    n = len(entry.get("needs_key") or [])
    if n:
        parts.append(f"{n} 个数据库需要新密钥，请手动运行 ./wechat decrypt")
    return "；".join(parts) or None


# ---------------------------------------------------------------------------
# 备份主流程
# ---------------------------------------------------------------------------

def run_backup(cfg, *, notify_enabled=True, verbose=False, trigger="manual",
               runner=subprocess.run, decrypt_module=None):
    """执行一次备份（调用方负责加锁），返回 (退出码, 摘要)。"""
    start = time.time()
    t0 = time.monotonic()
    entry = {
        "time": dt.datetime.fromtimestamp(start).astimezone().isoformat(timespec="seconds"),
        "trigger": trigger, "status": "ok", "exit_code": EXIT_OK,
        "formats": cfg["formats"], "media": None, "dir": cfg["dir"],
        "chats_updated": 0, "messages_added": 0,
        "decrypt": None, "needs_key": [], "skipped_dbs": [], "exports": [], "errors": [],
    }
    print(f"[+] {entry['time']} 开始备份 -> {cfg['dir']}（格式: {', '.join(cfg['formats'])}）")

    # 1. 解密（只用现有密钥）
    td = time.monotonic()
    try:
        D = decrypt_module or import_decrypt_db()
        with no_key_scanner(D):
            dres = decrypt_changed(D, verbose=verbose)
        entry["decrypt"] = {"decrypted": dres["decrypted"], "unchanged": dres["unchanged"],
                            "failed": len(dres["failed"]), "needs_key": len(dres["needs_key"]),
                            "no_key": len(dres["no_key"]),
                            "seconds": round(time.monotonic() - td, 1)}
        entry["needs_key"] = dres["needs_key"]
        entry["skipped_dbs"] = sorted(dres["needs_key"] + dres["failed"])
        for rel in dres["failed"]:
            entry["errors"].append(f"解密失败: {rel}")
        print(f"[+] 解密: {dres['decrypted']} 个已更新, {dres['unchanged']} 个未变化, "
              f"{len(dres['failed'])} 个失败, {len(dres['needs_key'])} 个需要新密钥 "
              f"({entry['decrypt']['seconds']}s)")
        if dres["needs_key"]:
            print(f"[!] 以下数据库需要新密钥（请手动运行 ./wechat decrypt）: {', '.join(dres['needs_key'])}")
    except ConfigError as e:
        entry["errors"].append(str(e))
    except Exception as e:
        entry["errors"].append(f"解密出错: {type(e).__name__}: {e}")

    # 2. 导出
    try:
        caps = export_capabilities(runner)
        if "--no-decrypt" not in caps or "--export-dir" not in caps:
            raise RuntimeError("export_chat.py 缺少 --no-decrypt / --export-dir 选项")
    except Exception as e:
        caps = None
        entry["errors"].append(f"无法调用导出命令: {e}")
    if caps is not None:
        mflag = media_flag(caps, cfg["media"])
        entry["media"] = mflag
        os.makedirs(cfg["dir"], exist_ok=True)
        for fmt in cfg["formats"]:
            r = run_export(fmt, cfg["dir"], mflag, runner=runner, verbose=verbose)
            entry["exports"].append(r)
            if r["exit_code"] != 0:
                entry["errors"].append(f"导出 {fmt} 失败（退出码 {r['exit_code']}）")
            print(f"[+] 导出 {fmt}: {r['chats_updated']} 个聊天更新, 新增 "
                  f"{r['messages_added']} 条 ({r['seconds']}s)"
                  + ("" if r["exit_code"] == 0 else f" [退出码 {r['exit_code']}]"))
        ok = [r for r in entry["exports"] if r["exit_code"] == 0]
        entry["chats_updated"] = max((r["chats_updated"] or 0 for r in ok), default=0)
        entry["messages_added"] = max((r["messages_added"] or 0 for r in ok), default=0)

    # 3. 结果
    if entry["errors"]:
        entry["status"], entry["exit_code"] = "error", EXIT_FAIL
    elif entry["needs_key"]:
        entry["status"], entry["exit_code"] = "needs_keys", EXIT_NEEDS_KEYS
    entry["duration_s"] = round(time.monotonic() - t0, 1)
    append_summary(entry)
    print(f"[+] 备份{'完成' if entry['status'] != 'error' else '失败'}: "
          f"{entry['chats_updated']} 个聊天更新, 新增 {entry['messages_added']} 条, "
          f"耗时 {entry['duration_s']}s")
    for e in entry["errors"]:
        print(f"[!] {e}")

    text = notification_text(entry)
    if text and notify_enabled:
        notify(text, runner=runner)
    return entry["exit_code"], entry


# ---------------------------------------------------------------------------
# LaunchAgent
# ---------------------------------------------------------------------------

def build_plist(hour, minute, export_dir=None, python=None, root=ROOT):
    """生成 LaunchAgent plist（bytes）。export_dir 只在明确指定时写入参数。"""
    args = [python or python_bin(), os.path.join(root, "backup.py"), "--scheduled"]
    if export_dir:
        args += ["--dir", export_dir]
    d = {
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": root,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "Nice": 10,
        "LowPriorityIO": True,
        "StandardOutPath": LAUNCHD_LOG,
        "StandardErrorPath": LAUNCHD_LOG,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
                                 "PYTHONIOENCODING": "utf-8"},
    }
    return plistlib.dumps(d)


def _domain():
    return f"gui/{os.getuid()}"


def _launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True)


def is_loaded():
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def _bootout():
    if not is_loaded():
        return
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    for _ in range(20):
        if not is_loaded():
            return
        time.sleep(0.25)


def install(cfg, export_dir=None):
    os.makedirs(LAUNCHD_LOG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    data = build_plist(cfg["hour"], cfg["minute"], export_dir)
    _bootout()
    tmp = PLIST_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, PLIST_PATH)
    p = _launchctl("bootstrap", _domain(), PLIST_PATH)
    if p.returncode != 0:
        print(f"[!] launchctl bootstrap 失败: {p.stderr.strip()}")
        return EXIT_FAIL
    print(f"[+] 已安装定时备份: 每天 {cfg['time']}（{PLIST_PATH}）")
    print(f"    导出目录: {export_dir or cfg['dir']}，格式: {', '.join(cfg['formats'])}")
    print(f"    日志: {LAUNCHD_LOG}，摘要: {SUMMARY_FILE}")
    print("    电脑在该时间处于睡眠时，会在唤醒后补跑一次；关机或未登录时不会运行。")
    print("    立即试运行: launchctl kickstart " + f"{_domain()}/{LABEL}")
    return EXIT_OK


def uninstall():
    was = os.path.exists(PLIST_PATH) or is_loaded()
    _bootout()
    if os.path.exists(PLIST_PATH):
        os.remove(PLIST_PATH)
    print("[+] 已卸载定时备份" if was else "[+] 定时备份未安装")
    return EXIT_OK


def next_run(hour, minute, now=None):
    now = now or dt.datetime.now()
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return t if t > now else t + dt.timedelta(days=1)


def launchd_info():
    """launchctl print 中的 state / runs / last exit code。"""
    p = _launchctl("print", f"{_domain()}/{LABEL}")
    if p.returncode != 0:
        return None
    info = {}
    for key in ("state", "runs", "last exit code"):
        m = re.search(rf"^\s*{key} = (.+)$", p.stdout, re.M)
        if m:
            info[key] = m.group(1).strip()
    return info


def status(cfg):
    installed = os.path.exists(PLIST_PATH)
    print(f"定时备份: {'已安装' if installed else '未安装'}（{PLIST_PATH}）")
    if installed:
        try:
            with open(PLIST_PATH, "rb") as f:
                pl = plistlib.load(f)
            sci = pl.get("StartCalendarInterval", {})
            h, m = sci.get("Hour", 0), sci.get("Minute", 0)
            args = pl.get("ProgramArguments", [])
            d = args[args.index("--dir") + 1] if "--dir" in args else cfg["dir"]
            info = launchd_info()
            print(f"  已加载: {'是' if info is not None else '否（请重新运行 ./wechat backup --install）'}")
            if info:
                print(f"  launchd: " + ", ".join(f"{k}={v}" for k, v in info.items()))
            print(f"  每天 {h:02d}:{m:02d}，下次运行: {next_run(h, m):%Y-%m-%d %H:%M}")
            print(f"  导出目录: {d}")
            if (h, m) != (cfg["hour"], cfg["minute"]):
                print(f"  [!] config.json 中的时间为 {cfg['time']}，请重新运行 ./wechat backup --install")
            if len(args) > 1 and os.path.dirname(os.path.abspath(args[1])) != ROOT:
                print(f"  [!] 定时任务指向另一个目录: {os.path.dirname(args[1])}")
        except Exception as e:
            print(f"  [!] 无法读取 plist: {e}")
    last = last_summary()
    if last:
        print(f"上次运行: {last.get('time')}（{last.get('trigger')}），状态 {last.get('status')}，"
              f"退出码 {last.get('exit_code')}，耗时 {last.get('duration_s')}s，"
              f"{last.get('chats_updated')} 个聊天更新，新增 {last.get('messages_added')} 条")
        if last.get("needs_key"):
            print(f"  [!] {len(last['needs_key'])} 个数据库需要新密钥: 请手动运行 ./wechat decrypt")
        for e in last.get("errors") or []:
            print(f"  [!] {e}")
    else:
        print("上次运行: 无记录")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="wechat backup",
                                description="无人值守备份：解密有变化的数据库（不使用 sudo）并增量导出全部聊天")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="安装每日定时任务（LaunchAgent）")
    g.add_argument("--uninstall", action="store_true", help="卸载定时任务")
    g.add_argument("--status", action="store_true", help="显示定时任务状态和上次运行结果")
    p.add_argument("--dir", help="导出目录（覆盖 config.json 的 backup.dir 和环境变量 WECHAT_BACKUP_DIR）")
    p.add_argument("--formats", help="导出格式，逗号分隔（覆盖 backup.formats）")
    p.add_argument("--no-media", action="store_true", help="不导出图片等媒体")
    p.add_argument("--no-notify", action="store_true", help="不发送 macOS 通知")
    p.add_argument("-v", "--verbose", action="store_true", help="显示详细输出（包括导出的聊天名称）")
    p.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    return p


def resolve_config(args, raw=None, env=None):
    env = os.environ if env is None else env
    raw = read_raw_config() if raw is None else raw
    b = dict(raw.get("backup") or {}) if isinstance(raw.get("backup"), dict) else raw.get("backup")
    if isinstance(b, dict):
        if env.get("WECHAT_BACKUP_DIR"):
            b["dir"] = env["WECHAT_BACKUP_DIR"]
        if args.dir:
            b["dir"] = args.dir
        if args.formats:
            b["formats"] = [f for f in args.formats.split(",") if f.strip()]
        if args.no_media:
            b["media"] = False
    return load_backup_config({**raw, "backup": b})


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"[!] 配置错误: {e}", file=sys.stderr)
        return EXIT_CONFIG

    if args.install:
        # 只有明确指定（--dir 或环境变量）时才把目录写进 plist，否则运行时读取 config.json
        explicit = args.dir or os.environ.get("WECHAT_BACKUP_DIR")
        return install(cfg, cfg["dir"] if explicit else None)
    if args.uninstall:
        return uninstall()
    if args.status:
        return status(cfg)

    trigger = "scheduled" if args.scheduled else "manual"
    try:
        with run_lock():
            code, _ = run_backup(cfg, notify_enabled=not args.no_notify,
                                 verbose=args.verbose, trigger=trigger)
            return code
    except Locked:
        print("[!] 已有备份在运行，本次跳过")
        append_summary({"time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                        "trigger": trigger, "status": "locked", "exit_code": EXIT_LOCKED})
        return EXIT_LOCKED


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] 已取消")
        sys.exit(130)
