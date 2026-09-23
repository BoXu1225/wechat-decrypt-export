#!/usr/bin/env bash
# setup.sh - one-time (and safe to rerun) environment setup for wechat-decrypt-export.
#   - checks macOS / Xcode Command Line Tools / Python >= 3.10
#   - creates ./venv and installs requirements
#   - compiles the memory key scanner
#   - checks that WeChat is ad-hoc signed (~/WeChat.app), offers to copy + sign it
#   - makes existing outputs (keys, config, decrypted/, export/, logs/) owner-only
# It never runs sudo or the key scanner; decryption does that on demand.
set -euo pipefail

ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

# Overridable for testing
WECHAT_SRC="${WECHAT_SRC:-/Applications/WeChat.app}"
WECHAT_DST="${WECHAT_DST:-${HOME}/WeChat.app}"

ok()   { echo "[+] $*"; }
info() { echo "    $*"; }
warn() { echo "[!] $*"; }
die()  { echo "[!] $*" >&2; exit 1; }

# Ask a y/N question; anything but y/yes (including EOF / no tty) means no
confirm() {
    local ans=""
    read -r -p "$1 [y/N] " ans || true
    [[ "${ans}" =~ ^[Yy]([Ee][Ss])?$ ]]
}

app_version() {  # CFBundleShortVersionString of an .app, empty if unavailable
    local plist="$1/Contents/Info.plist"
    [[ -f "${plist}" ]] || return 0
    plutil -extract CFBundleShortVersionString raw -o - "${plist}" 2>/dev/null || true
}

is_adhoc_signed() {
    local out
    out="$(codesign -dv "$1" 2>&1 || true)"  # capture first: grep -q + pipefail could SIGPIPE codesign
    grep -q '^Signature=adhoc' <<<"${out}"
}

# Full executable paths of running WeChat main processes
wechat_procs() {
    local pid
    for pid in $(pgrep -x WeChat 2>/dev/null || true); do
        ps -o comm= -p "${pid}" 2>/dev/null || true
    done
}

echo "============================================================"
echo "  wechat-decrypt-export 环境配置"
echo "============================================================"

# ---- 1. macOS --------------------------------------------------------------
[[ "$(uname -s)" == "Darwin" ]] || die "本工具仅支持 macOS"
ok "macOS $(sw_vers -productVersion) ($(uname -m))"

# ---- 2. Xcode Command Line Tools (needed to compile the scanner) -----------
if xcode-select -p >/dev/null 2>&1; then
    ok "Xcode Command Line Tools: $(xcode-select -p)"
else
    warn "未安装 Xcode Command Line Tools（编译密钥扫描器需要）"
    if confirm "    现在安装吗？（会弹出系统安装窗口）"; then
        xcode-select --install || true
        info "请在弹出的窗口中完成安装，然后重新运行 ./setup.sh"
    else
        info "请手动运行: xcode-select --install，然后重新运行 ./setup.sh"
    fi
    exit 1
fi

# ---- 3. Python >= 3.10 and venv ---------------------------------------------
py_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; }

if [[ -x venv/bin/python ]]; then
    py_ok venv/bin/python || die "venv 中的 Python 版本低于 3.10，请删除后重试: rm -rf venv && ./setup.sh"
    ok "使用已有虚拟环境 venv ($(venv/bin/python --version 2>&1))"
else
    PY=""
    for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "${cand}" >/dev/null 2>&1 && py_ok "${cand}"; then
            PY="$(command -v "${cand}")"
            break
        fi
    done
    [[ -n "${PY}" ]] || die "需要 Python 3.10+（系统自带的 python3 可能过旧），可用 Homebrew 安装: brew install python"
    ok "创建虚拟环境 venv（$("${PY}" --version 2>&1)）..."
    "${PY}" -m venv venv
fi

ok "安装 Python 依赖 ..."
venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
ok "依赖已就绪"

# ---- 4. Compile the key scanner --------------------------------------------
if [[ ! -x find_all_keys_macos || find_all_keys_macos -ot find_all_keys_macos.c ]]; then
    ok "编译密钥扫描器 find_all_keys_macos ..."
    cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
fi
ok "密钥扫描器已就绪"

# ---- 5. Owner-only permissions on our outputs --------------------------------
# Keys, decrypted databases and exported chats must not be readable by other
# local users. Only our own output paths (not symlinks, owned by us); the
# Python entry points do the same on every run and create new files as 0600.
tightened=()
for p in all_keys.json config.json decrypted export logs decoded_images; do
    [[ -e "${p}" && ! -L "${p}" && -O "${p}" ]] || continue
    if [[ -n "$(find "${p}" -maxdepth 0 -perm +077)" ]]; then
        find "${p}" \( -type f -o -type d \) -user "$(id -u)" -perm +077 -exec chmod go-rwx {} +
        tightened+=("${p}")
    fi
done
if (( ${#tightened[@]} )); then
    ok "已收紧权限（目录 700，文件 600，仅本人可读）: ${tightened[*]}"
else
    ok "密钥、配置、解密数据、导出和日志的权限均为仅本人可读"
fi
info "（之后解密/导出/MCP 生成的文件也只有本人可读写）"

# ---- 6. WeChat ad-hoc signing ----------------------------------------------
# Reading process memory needs an ad-hoc signed WeChat. SIP prevents re-signing
# inside /Applications, so we keep a signed copy at ~/WeChat.app.
echo
src_ver="$(app_version "${WECHAT_SRC}")"
dst_ver="$(app_version "${WECHAT_DST}")"
wechat_ready=0

copy_and_sign() {
    if grep -qF "${WECHAT_DST}/" <<<"$(wechat_procs)"; then
        warn "${WECHAT_DST} 中的微信正在运行，请先退出微信，再重新运行 ./setup.sh"
        return 1
    fi
    if [[ ! -d "${WECHAT_SRC}" ]]; then
        warn "未找到 ${WECHAT_SRC}，不会删除现有副本"
        return 1
    fi
    if [[ -e "${WECHAT_DST}" ]]; then
        ok "删除旧的副本 ${WECHAT_DST} ..."
        rm -rf "${WECHAT_DST}"
    fi
    ok "复制 ${WECHAT_SRC} -> ${WECHAT_DST} ..."
    cp -R "${WECHAT_SRC}" "${WECHAT_DST}" || { warn "复制失败"; return 1; }
    ok "ad-hoc 签名 ${WECHAT_DST} ..."
    codesign --force --deep --sign - "${WECHAT_DST}" || { warn "签名失败"; return 1; }
    ok "签名完成"
}

if [[ ! -d "${WECHAT_DST}" && ! -d "${WECHAT_SRC}" ]]; then
    warn "未找到微信（${WECHAT_SRC} 或 ${WECHAT_DST}），请先安装微信 4.x 后重新运行 ./setup.sh"
elif [[ ! -d "${WECHAT_DST}" || ( -n "${src_ver}" && "${src_ver}" != "${dst_ver}" ) ]]; then
    if [[ ! -d "${WECHAT_DST}" ]]; then
        warn "未找到已签名的微信副本 ${WECHAT_DST}"
    else
        warn "微信已更新: ${WECHAT_SRC} 为 ${src_ver}，而 ${WECHAT_DST} 仍是 ${dst_ver:-未知版本}"
    fi
    info "读取微信内存中的密钥需要 ad-hoc 签名的微信；SIP 不允许修改 /Applications 中的应用，"
    info "所以需要复制一份到 ${WECHAT_DST} 再签名:"
    info "  cp -R ${WECHAT_SRC} ${WECHAT_DST}"
    info "  codesign --force --deep --sign - ${WECHAT_DST}"
    [[ -d "${WECHAT_DST}" ]] && info "（会先删除旧的 ${WECHAT_DST}；聊天数据不在 app 内，不受影响）"
    if confirm "    现在执行吗？"; then
        copy_and_sign && wechat_ready=1
    else
        info "已跳过。之后可重新运行 ./setup.sh，或手动执行上面的命令"
    fi
elif ! is_adhoc_signed "${WECHAT_DST}"; then
    warn "${WECHAT_DST} 不是 ad-hoc 签名"
    if confirm "    现在重新签名吗？(codesign --force --deep --sign - ${WECHAT_DST})"; then
        codesign --force --deep --sign - "${WECHAT_DST}" && ok "签名完成" && wechat_ready=1
    else
        info "已跳过"
    fi
else
    ok "已签名的微信副本: ${WECHAT_DST} (${dst_ver:-未知版本})"
    [[ -z "${src_ver}" ]] && info "（未找到 ${WECHAT_SRC}，无法检查微信是否有更新）"
    wechat_ready=1
fi

# Which WeChat is running?
procs="$(wechat_procs)"
if [[ -z "${procs}" ]]; then
    info "微信当前未运行。提取密钥前请打开 ${WECHAT_DST} 并登录"
elif grep -qF "${WECHAT_SRC}/" <<<"${procs}"; then
    warn "正在运行的是 ${WECHAT_SRC} 中未签名的微信，无法读取其内存中的密钥"
    info "请退出微信（⌘Q），然后打开 ${WECHAT_DST}: open ${WECHAT_DST}"
elif grep -qF "${WECHAT_DST}/" <<<"${procs}"; then
    if [[ ${wechat_ready} == 1 ]]; then
        ok "微信正在从 ${WECHAT_DST} 运行"
    fi
else
    warn "正在运行的微信来自其他位置: ${procs}"
    info "请退出后打开 ${WECHAT_DST}"
fi
if [[ ${wechat_ready} == 1 ]]; then
    info "提示: 以后请始终从 ${WECHAT_DST} 启动微信（可拖到 Dock），不要用 ${WECHAT_SRC}"
fi

echo
echo "============================================================"
ok "配置完成。用法:"
info "./wechat <联系人>        导出聊天（首次运行会自动提取密钥并解密，需要 sudo 密码）"
info "./wechat decrypt         仅解密数据库"
info "./wechat --help          查看全部选项"
