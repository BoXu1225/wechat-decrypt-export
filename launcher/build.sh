#!/bin/bash
# Build launcher/build/WeChatBackup.app for this repo (C + ad-hoc codesign).
#
# The repo path is compiled into the binary, so the app can only ever run this
# repo's backup.py. Skips the build when main.c, Info.plist, this script and the repo path are
# unchanged since the last build (a stamp file records their hash): every rebuild
# produces a new ad-hoc signature, and macOS then needs the Full Disk Access
# grant again. Use --force to rebuild anyway.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BUNDLE_ID="local.wechat-decrypt-export.backup"
APP="$HERE/build/WeChatBackup.app"
STAMP="$HERE/build/.source-hash"
CC=/usr/bin/cc   # not plain `cc`: it may be aliased in interactive shells

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

case "$ROOT" in *\"*|*\\*) echo "[!] repo path must not contain \" or \\: $ROOT" >&2; exit 1 ;; esac

HASH="$( (cat "$HERE/Info.plist" "$HERE/main.c" "$HERE/build.sh"; printf '%s' "$ROOT") | shasum -a 256 | cut -d' ' -f1)"
if [[ $FORCE == 0 && -x "$APP/Contents/MacOS/WeChatBackup" && -f "$STAMP" \
      && "$(cat "$STAMP")" == "$HASH" ]]; then
    echo "[+] WeChatBackup.app is up to date"
    exit 0
fi

[[ -x "$CC" ]] || { echo "[!] $CC not found: run xcode-select --install" >&2; exit 1; }

echo "[+] building WeChatBackup.app"
TMP="$HERE/build/WeChatBackup.app.tmp"
rm -rf "$TMP"
mkdir -p "$TMP/Contents/MacOS"
cp "$HERE/Info.plist" "$TMP/Contents/Info.plist"
"$CC" -O2 -Wall -Wextra -Werror -mmacosx-version-min=13.0 \
    -DREPO_ROOT="\"$ROOT\"" -o "$TMP/Contents/MacOS/WeChatBackup" "$HERE/main.c"
# Hardened runtime: dyld ignores DYLD_* variables, so the Full Disk Access grant
# cannot be borrowed by injecting a library into this binary.
codesign --force -s - -i "$BUNDLE_ID" -o runtime "$TMP"
rm -rf "$APP"
mv "$TMP" "$APP"
echo "$HASH" > "$STAMP"
echo "[+] built $APP"
