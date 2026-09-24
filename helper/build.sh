#!/bin/bash
# Build helper/build/WeChatSendHelper.app (swiftc + codesign).
#
# Signs with the local "WeChatSendHelper Local Signing" identity when it exists
# (create it once with helper/make-cert.sh): its designated requirement is
# stable, so Accessibility / Screen Recording grants survive rebuilds.
# Otherwise signs ad-hoc; each ad-hoc rebuild needs the grants again, so the
# build is skipped when the sources and Info.plist are unchanged (a stamp file
# records their hash). Use --force to rebuild anyway.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ID="local.wechat-decrypt-export.sendhelper"
APP="$HERE/build/WeChatSendHelper.app"
STAMP="$HERE/build/.source-hash"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

HASH="$( (cat "$HERE/Info.plist" "$HERE"/Sources/*.swift; security find-certificate -c "WeChatSendHelper Local Signing" -Z 2>/dev/null | grep SHA-1) | shasum -a 256 | cut -d' ' -f1)"
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
    -framework ScreenCaptureKit -framework Vision \
    -o "$TMP/Contents/MacOS/WeChatSendHelper" "$HERE"/Sources/*.swift
IDENTITY="WeChatSendHelper Local Signing"
if security find-certificate -c "$IDENTITY" >/dev/null 2>&1; then
    codesign --force -s "$IDENTITY" -i "$BUNDLE_ID" "$TMP"
else
    echo "[i] signing ad-hoc (run helper/make-cert.sh so rebuilds keep permissions)"
    codesign --force -s - -i "$BUNDLE_ID" "$TMP"
fi
rm -rf "$APP"
mv "$TMP" "$APP"
echo "$HASH" > "$STAMP"
echo "[+] built $APP"
