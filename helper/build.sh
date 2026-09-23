#!/bin/bash
# Build helper/build/WeChatSendHelper.app (swiftc + ad-hoc codesign).
#
# Skips the build when the sources and Info.plist are unchanged since the last
# build (a stamp file records their hash): every rebuild produces a new ad-hoc
# signature, and macOS may then ask for the Accessibility permission again.
# Use --force to rebuild anyway.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ID="local.wechat-decrypt-export.sendhelper"
APP="$HERE/build/WeChatSendHelper.app"
STAMP="$HERE/build/.source-hash"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

HASH="$(cat "$HERE/Info.plist" "$HERE"/Sources/*.swift | shasum -a 256 | cut -d' ' -f1)"
if [[ $FORCE == 0 && -x "$APP/Contents/MacOS/WeChatSendHelper" && -f "$STAMP" \
      && "$(cat "$STAMP")" == "$HASH" ]]; then
    echo "[+] WeChatSendHelper.app is up to date (sources unchanged)"
    exit 0
fi

command -v swiftc >/dev/null || { echo "[!] swiftc not found: run xcode-select --install"; exit 1; }

echo "[+] building WeChatSendHelper.app"
TMP="$HERE/build/WeChatSendHelper.app.tmp"
rm -rf "$TMP"
mkdir -p "$TMP/Contents/MacOS"
cp "$HERE/Info.plist" "$TMP/Contents/Info.plist"
swiftc -O -swift-version 5 -warnings-as-errors \
    -target "$(uname -m)-apple-macos14.0" \
    -framework AppKit -framework ApplicationServices \
    -o "$TMP/Contents/MacOS/WeChatSendHelper" "$HERE"/Sources/*.swift
codesign --force -s - -i "$BUNDLE_ID" "$TMP"
rm -rf "$APP"
mv "$TMP" "$APP"
echo "$HASH" > "$STAMP"
echo "[+] built $APP"
