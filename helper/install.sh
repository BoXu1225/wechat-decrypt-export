#!/bin/bash
# Build and install WeChatSendHelper as a per-user LaunchAgent.
#
# Touches only:
#   ~/Applications/WeChatSendHelper.app
#   ~/Library/LaunchAgents/local.wechat-decrypt-export.sendhelper.plist
#   ~/Library/Logs/wechat-decrypt-export/sendhelper.log
#   (the helper itself creates ~/Library/Application Support/wechat-decrypt-export/)
# Safe to rerun. The installed app is replaced only when the build differs,
# so an existing Accessibility grant survives reruns.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="local.wechat-decrypt-export.sendhelper"
SRC_APP="$HERE/build/WeChatSendHelper.app"
DEST_DIR="$HOME/Applications"
DEST_APP="$DEST_DIR/WeChatSendHelper.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/wechat-decrypt-export"
DOMAIN="gui/$(id -u)"

# A stable local signing identity keeps the permissions across rebuilds
# (macOS may ask once for your login password: choose "Always Allow").
"$HERE/make-cert.sh" || echo "[i] no signing identity; the helper will be signed ad-hoc"
"$HERE/build.sh"

cdhash() { codesign -dvvv "$1" 2>&1 | sed -n 's/^CDHash=//p'; }

mkdir -p "$DEST_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
CHANGED=0
if [[ ! -d "$DEST_APP" || "$(cdhash "$SRC_APP")" != "$(cdhash "$DEST_APP")" ]]; then
    CHANGED=1
fi

# Stop the running agent (if any) before replacing its files.
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
        sleep 0.3
    done
fi

if [[ $CHANGED == 1 ]]; then
    rm -rf "$DEST_APP.new"
    cp -R "$SRC_APP" "$DEST_APP.new"
    rm -rf "$DEST_APP"
    mv "$DEST_APP.new" "$DEST_APP"
    echo "[+] installed $DEST_APP"
else
    echo "[+] $DEST_APP is unchanged"
fi

cat > "$PLIST.tmp" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$DEST_APP/Contents/MacOS/WeChatSendHelper</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ThrottleInterval</key>
	<integer>10</integer>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>StandardErrorPath</key>
	<string>$LOG_DIR/sendhelper.log</string>
	<key>StandardOutPath</key>
	<string>$LOG_DIR/sendhelper.log</string>
</dict>
</plist>
PLIST
mv "$PLIST.tmp" "$PLIST"
chmod 644 "$PLIST"

launchctl bootstrap "$DOMAIN" "$PLIST"
echo "[+] LaunchAgent $LABEL loaded"

SOCK="$HOME/Library/Application Support/wechat-decrypt-export/sendhelper.sock"
for _ in $(seq 1 20); do [[ -S "$SOCK" ]] && break; sleep 0.25; done
if [[ -S "$SOCK" ]]; then
    echo "[+] helper is listening on $SOCK"
else
    echo "[!] helper socket did not appear; see $LOG_DIR/sendhelper.log"
fi

cat <<MSG

Next step (once): grant the helper two permissions in
System Settings -> Privacy & Security:
  1. Accessibility                    (to press keys in WeChat)
  2. Screen & System Audio Recording  (to read WeChat's window: WeChat 4 has no
                                       accessibility tree, so the chat title and
                                       message are checked by on-device OCR)
Turn ON "WeChatSendHelper" in both. If it is not listed, click "+", press
Cmd+Shift+G, enter  $DEST_APP  and add it. After granting Screen Recording run
  launchctl kickstart -k $DOMAIN/$LABEL
Then check:  ./venv/bin/python wechat_send.py --check
  ("trusted": true, "screen_capture": true)
MSG
if ! security find-certificate -c "WeChatSendHelper Local Signing" >/dev/null 2>&1; then
    echo "Note: the helper is ad-hoc signed, so each rebuild needs the permissions again"
    echo "(remove the old entries with \"-\" and add the app again)."
fi
exit 0
