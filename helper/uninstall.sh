#!/bin/bash
# Remove the WeChatSendHelper LaunchAgent, app, socket and log. Safe to rerun.
set -uo pipefail

LABEL="local.wechat-decrypt-export.sendhelper"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$HOME/Applications/WeChatSendHelper.app"
SOCK="$HOME/Library/Application Support/wechat-decrypt-export/sendhelper.sock"
LOG_DIR="$HOME/Library/Logs/wechat-decrypt-export"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL" && echo "[+] LaunchAgent stopped"
fi
[[ -f "$PLIST" ]] && rm -f "$PLIST" && echo "[+] removed $PLIST"
[[ -d "$APP" ]] && rm -rf "$APP" && echo "[+] removed $APP"
[[ -S "$SOCK" ]] && rm -f "$SOCK" && echo "[+] removed socket"
rmdir "$(dirname "$SOCK")" 2>/dev/null || true
rm -f "$LOG_DIR/sendhelper.log"
rmdir "$LOG_DIR" 2>/dev/null || true
echo "[+] done. You can also remove WeChatSendHelper from"
echo "    System Settings -> Privacy & Security -> Accessibility (select it, click -)."
exit 0
