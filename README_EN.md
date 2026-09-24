# WeChat macOS Database Decryptor & Chat Exporter

[![CI](https://github.com/BoXu1225/wechat-decrypt-export/actions/workflows/ci.yml/badge.svg)](https://github.com/BoXu1225/wechat-decrypt-export/actions/workflows/ci.yml)

[中文](README.md)

Our data, we own it! Decrypt WeChat 4.x (macOS) local SQLCipher 4 databases and export your chats — 1-on-1 and group — as text, Markdown, HTML, JSON or CSV, with images, playable voice messages and videos.

## Setup

Requirements: macOS (Apple Silicon / Intel), WeChat 4.x logged in.

```bash
./setup.sh
```

`setup.sh` is safe to rerun. It checks for Xcode Command Line Tools and Python 3.10+, creates `venv/` and installs dependencies, compiles the key scanner, and — after asking you — makes an **ad-hoc signed copy of WeChat** at `~/WeChat.app` (required so the scanner can read WeChat's memory; SIP prevents signing the copy in `/Applications`). Quit WeChat and launch `~/WeChat.app` from then on.

After a WeChat update, install it to `/Applications` as usual and rerun `./setup.sh`: it detects the version change and offers to re-copy and re-sign.

## Quick start

```bash
./wechat ning                  # export one chat (fuzzy name match, pick from a list if several)
./wechat --list                # list all chats: name, type, message count, last message
./wechat --all                 # export every chat (incremental)
```

`./wechat` runs `export_chat.py` with the project's venv from any directory (you can symlink it into `~/bin`). Each run automatically:

1. **Extracts keys** when needed — on first run, when WeChat creates a new database, or when a key goes stale. This runs the scanner with `sudo` (asks for your password; WeChat must be open). Otherwise it's skipped.
2. **Decrypts** only the databases that changed since last time (the `.db` or its `-wal`), into `decrypted/`, checking HMAC and SQLite integrity. Recent messages WeChat still holds in the `-wal` file (not yet checkpointed) are merged in.
3. **Exports**.

Decrypt on its own with `./wechat decrypt`.

## Exporting

```bash
./wechat ning -f html --images           # chat-style HTML page with images
./wechat ning -f html --media            # ... plus playable voice messages and videos
./wechat ning -i                         # incremental: append new messages to export/<name>.txt
./wechat ning --since 2026-01-01 --until 2026-06-30
./wechat --all --type group -f md        # all group chats as Markdown
./wechat --list 同学                      # list chats whose name contains 同学
```

| Option | Meaning |
|---|---|
| `contact` | Remark / nickname / group name, partial match. With `--list` / `--all` it filters by name |
| `-f`, `--format` | `txt` (default), `md`, `html`, `json`, `csv` |
| `-i`, `--incremental` | Export to `export/<name>.<ext>`; txt/md/csv append only new messages, html/json are regenerated |
| `--all` | Incremental export of every chat |
| `--list [filter]` | List chats |
| `--type` | `all` (default), `single` (1-on-1), `group` |
| `--images` | Decode images into `export/<name>_files/` and embed them in md/html/json (txt/csv show `[图片]`); cached stickers are shown too |
| `--voice` | Convert voice messages to `export/<name>_files/<id>.m4a`, playable in html (`<audio>`) and linked from md/json; txt/csv show `[语音 12″]` |
| `--media` | `--images` + `--voice` + copy downloaded videos (`<md5>.mp4`, thumbnail `<md5>_thumb.jpg`) for html `<video>` / md / json; txt/csv show `[视频 0:35]` |
| `--download-emoji` | With `--images` / `--media`: download stickers that are not available locally from the WeChat CDN URL in the message (off by default; cached in `decrypted/emoji_cache/`) |
| `--since` / `--until` | Date range, `YYYY-MM-DD`, inclusive |
| `-o`, `--output` | Output file (default `export/<name>_chat.<ext>`) |
| `--export-dir` | Export folder (default `export/`) |
| `-d`, `--decrypted-dir` | Decrypted database folder |
| `--no-decrypt` | Skip the automatic decrypt step |

What the exporter handles:
- **Group chats**: each message shows the sender's group nickname, else your remark/their nickname
- **Chats spanning several databases** (`message_0.db`, `message_1.db`, … — roughly one per year)
- **Message types**: text, images, voice, video, stickers, links, files, quotes, mini programs, system messages; zstd-compressed messages are decompressed
- **Images**: WeChat 4.x encrypted `.dat` images (including the HEVC-based wxgf format, converted to JPEG with macOS's built-in `sips`; wxgf images with an alpha channel become transparent PNGs). The image key is derived from local account files — no extra `sudo` needed. When WeChat only has a thumbnail (full image never downloaded), the thumbnail is used and upgraded on a later export once the full image exists
- **Voice / video**: voice messages (SILK v3 stored in `message/media_0.db`) are decoded with the `silk-python` package and encoded to AAC `.m4a` with macOS's built-in `afconvert` (WAV if unavailable). Videos WeChat has downloaded are plain MP4s and are copied (an APFS clone on the same volume, so no extra space); videos never downloaded show their thumbnail. Durations come from the message. Already exported files are reused, so incremental exports only convert new messages. Audio/video use `preload="none"` so large HTML pages open quickly
- **Stickers**: WeChat's local sticker cache is encrypted (unknown key), so sticker images are fetched once with `--download-emoji` from the CDN URL in the message (WeChat CDN hosts only, with timeout and size limits; failures are not retried for a week). `./venv/bin/python emoticon.py` reports how many stickers are downloadable (counts only)
- **Old incremental layout**: if you used the previous `export/<name>/output_N.txt` layout, the first incremental export merges those files into `export/<name>.txt`; the old folder can then be deleted

## Automatic backup

```bash
./wechat backup              # back up now: decrypt changed DBs + incremental export of every chat
./wechat backup --install    # install the daily LaunchAgent (default 03:30)
./wechat backup --status     # installed? next run, last run summary
./wechat backup --uninstall
```

- **Never uses sudo**: only the keys already in `all_keys.json`. DBs whose key is missing or stale are skipped (the export uses the last decrypted copy) and a notification asks you to run `./wechat decrypt` by hand.
- Runs `--all -i` once per format from the optional `backup` section of `config.json` (defaults shown):
  ```json
  "backup": {"formats": ["html", "txt"], "media": true, "dir": "export", "time": "03:30"}
  ```
  `media` exports images (`--media` if the export CLI has it, otherwise `--images`); `dir` can be overridden with `--dir` or `WECHAT_BACKUP_DIR`; re-run `--install` after changing `time`.
- Each run appends one line to `logs/backup.jsonl` (time, duration, chats updated, messages added, skipped DBs, errors; no message content). Scheduled output goes to `~/Library/Logs/wechat-decrypt-export/backup.log`.
- macOS notification only on failure or when keys need a manual `./wechat decrypt`. A lock file prevents overlapping runs.
- Exit codes: 0 ok, 1 failed, 2 config error, 3 another run in progress, 4 done but some DBs need new keys.
- Low-priority LaunchAgent (`~/Library/LaunchAgents/local.wechat-decrypt-export.backup.plist`); runs while the screen is locked (files only). If the Mac is asleep at the scheduled time, launchd runs the job **once after wake** (missed runs are coalesced); it does not run while shut down or logged out.
- **Permission**: when started by launchd, macOS requires its own grant to read WeChat's data (Terminal's grant does not apply). The job is started through `~/Applications/WeChatBackup.app` (built and installed by `--install`: a tiny ad-hoc signed launcher with the repo path compiled in that can only run this repo's `backup.py` as a child process), and macOS attributes the access to it, so **grant only WeChatBackup.app**, not Python or Terminal: System Settings > Privacy & Security > **Full Disk Access** > + > press ⌘⇧G, enter `~/Applications/WeChatBackup.app` > Open, and make sure its switch is on. Then test with `launchctl kickstart gui/$(id -u)/local.wechat-decrypt-export.backup` and `./wechat backup --status`. Re-running `--install` rebuilds the app only when the launcher sources or the repo path change (after a rebuild, remove the old entry and add the app again). Without the grant the backup does not hang on the consent prompt: after ~45 s it skips decryption, exports the already-decrypted data and notifies you.

## Moments & Favorites

```bash
./wechat moments                         # all locally cached Moments posts -> export/moments.html
./wechat moments 张三 --images            # one person's posts with cached photos/videos
./wechat moments 我 -f md --since 2025-01-01
./wechat favorites                       # all Favorites -> export/favorites.html
./wechat favorites 发票 --type file -f json
```

Both take `-f html|md|json|txt` (default html), `--since` / `--until`, `--images`, `-o`, `--export-dir`, `--no-decrypt`. `moments` takes an optional author filter (remark / nickname / WeChat ID, partial; `我` = you) and `-q` text search; `favorites` takes an optional search query, `--type` and `--tag`. Output: `export/moments[_<name>].<ext>` / `export/favorites.<ext>`, media in a `_files/` folder next to it.

- **Moments** (`sns/sns.db`) holds only posts WeChat has loaded on this Mac — your own and friends' posts you scrolled past — with text, links, location, likes and comments (names resolved through your contacts). Photos and videos are exported only when they are in WeChat's local cache (`cache/<month>/Sns/`); CDN URLs are kept in JSON but never downloaded.
- **Favorites** (`favorite/favorite.db`): text, links, images, files, chat records, notes, locations, with source chat/sender and tags. Only files WeChat has saved locally can be exported.

## AI agents (MCP server)

`mcp_server.py` lets MCP clients such as Claude Code or Claude Desktop read your chats (and, with your approval each time, send messages). Decrypt once first (`./wechat decrypt`), then register it:

```bash
claude mcp add --scope user wechat -- "$PWD/venv/bin/python" "$PWD/mcp_server.py"
```

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{"mcpServers": {"wechat": {"command": "/ABS/PATH/venv/bin/python", "args": ["/ABS/PATH/mcp_server.py"]}}}
```

Then ask things like "what did 张三 and I decide about the trip last week?" or "find the address someone sent in the family group".

| Tool | What it does |
|------|--------------|
| `list_chats` | Chats with names, type and last activity; filter by name, single/group |
| `get_messages` | Messages of one chat, by time range or cursor, oldest or newest first |
| `search_messages` | Full-text search (Chinese works) across all chats or one chat, by sender and time |
| `get_message_context` | Messages around a message id or a time |
| `get_contact` | Remark, nickname, alias and shared groups of a person or group |
| `get_image` / `get_voice` | A chat image (as an image) or voice message (as audio) |
| `get_moments` | Locally cached Moments posts, by author, time or text |
| `search_favorites` / `get_favorite` | Search and read Favorites |
| `refresh` | Re-decrypt changed databases now |
| `send_message` | Send a text message (asks you first; see [Sending messages](#sending-messages)) |

- **Read-only except `send_message`.** All reading tools are marked read-only; `send_message` is marked destructive, so clients ask before each send.
- **Access:** all chats are visible by default. To hide some, add `"mcp_blocklist": ["name or wxid", …]` to `config.json`; a non-empty `"mcp_allowlist"` shows only the chats listed. Every call is logged to `logs/mcp_access.jsonl` (tool, arguments, result count — no message content).
- **Freshness:** before reads the server re-decrypts changed databases with the existing keys, at most every `mcp_auto_refresh_minutes` (default 5, `0` disables). It never runs the key scanner; if keys are stale it tells you to run `./wechat decrypt`. The newest messages can lag a little because WeChat holds recent writes in its WAL until it checkpoints.
- **Compact output:** results are plain lines — `HH:MM sender: text` under a date line, the chat named once — about 60–70% smaller than JSON, so agents can read long histories. Set `"mcp_output": "json"` in `config.json` for JSON objects instead.
- **Search index:** built on first use at `decrypted/mcp_index.db` (a few seconds for ~200k messages) and updated incrementally.
- **Check:** `./venv/bin/python mcp_server.py --selftest` prints counts (no message content) and exits.

## Sending messages

`./wechat send` (and the MCP tool `send_message`) sends a text message by driving the WeChat app on this Mac, the way you would: search the chat, check it opened the right one, paste, check the text, press Enter, then confirm the message landed in the database.

```bash
helper/install.sh                                   # once: builds and starts WeChatSendHelper.app
./wechat send --check                               # permissions / WeChat state
./wechat send --to 张三 --text "晚上七点见" --dry-run   # types and clears, sends nothing
./wechat send --to 张三 --text "晚上七点见"             # asks for confirmation, then sends
```

- **Setup (once):** `helper/install.sh` installs `~/Applications/WeChatSendHelper.app`, a small background helper, and a local code-signing certificate so rebuilds keep their permissions (macOS may ask once for your login password: choose *Always Allow*). Grant the helper — and nothing else — two permissions in System Settings → Privacy & Security: **Accessibility** (to press keys in WeChat) and **Screen & System Audio Recording** (to read WeChat's window), then run `launchctl kickstart -k gui/$(id -u)/local.wechat-decrypt-export.sendhelper`.
- **Why screenshots:** WeChat 4 draws its own UI and exposes nothing to Accessibility, so the helper reads the chat title, search results and input box with Apple's on-device OCR (Vision). Nothing leaves the Mac.
- **Safety checks:** the chat must be given by exact name or username (fuzzy names return candidates); only a search result with exactly that name under *Contacts* / *Group Chats* is clicked; the title must match before anything is typed; an existing draft is never touched; the text is read back before Enter, and the title and text are re-checked in the same screenshot right before the key press; afterwards the database is checked (`sent`, or `unverified` if the message hasn't appeared yet). Rate limit: 3 s between sends, 6 per minute (`send_rate_limit` in `config.json`). Every send is logged to `logs/send_log.jsonl` (chat, time, text hash — not the text).
- **Requirements:** WeChat running and logged in, the Mac unlocked. Text only — no files or images.
- **Sharing the Mac with the helper:** a send uses the real keyboard focus for ~10 s. Before starting it waits until you haven't touched the keyboard or mouse for 2 s (up to 30 s, else it gives up with `user_busy` — nothing happened). While it works a banner at the top of the screen says *Sending to … — please don't use the keyboard or mouse*, then *Sent to …* or *Stopped*. If you type, click, scroll or switch apps meanwhile, it stops before pressing Enter (`user_activity`) and nothing is sent; if the text was already typed it stays as an unsent draft in that chat, and the result says so. Reading chats never touches the screen. Settings: `send_idle_s`, `send_idle_timeout_s`.
- **Agents:** `send_message` is marked destructive, so Claude Code asks you before every send. It refuses chats hidden by `mcp_blocklist` / `mcp_allowlist`; set `"mcp_send": false` in `config.json` to remove the tool.
- **Limits:** File Transfer (文件传输助手) doesn't appear in WeChat 4's contact search, so it can't be a target. If you drag the divider above the input box much higher, the text check fails and nothing is sent.

## Configuration

Nothing to configure: on first run the WeChat data folder is auto-detected and saved to `config.json` (you pick one if there are several accounts), and your own WeChat ID is derived from the folder name. To override, edit `config.json`:

```json
{
    "db_dir": "/Users/YOU/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/YOUR_WXID/db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "WeChat"
}
```

Optional: `image_aes_key` (16 characters) and `image_xor_key` override the automatically derived image keys.

## How it works

WeChat 4.x encrypts local databases with SQLCipher 4:
- **Encryption**: AES-256-CBC + HMAC-SHA512
- **KDF**: PBKDF2-HMAC-SHA512, 256,000 iterations
- **Page size**: 4096 bytes, reserve = 80 (IV 16 + HMAC 64)
- **Each database has its own salt and key**

WCDB (WeChat's SQLCipher wrapper) caches derived raw keys in process memory as `x'<64hex_enc_key><32hex_salt>'`. The scanner finds that pattern in WeChat's memory and matches keys to databases by salt. Before decrypting, each key is checked against page 1's HMAC, so stale keys are detected and re-extracted automatically.

Each chat's messages live in tables named `Msg_<md5(username)>`; message content may be zstd-compressed (WCDB_CT=4). Chat images are stored as `.dat` files under `msg/attach/<md5(username)>/<YYYY-MM>/Img/`, the first 1 KB AES-128-ECB encrypted and the rest XOR'd. Voice audio is a SILK blob in `message/media_0.db` (`VoiceInfo`, keyed by chat + server id, or create time + local id); videos are unencrypted `msg/video/<YYYY-MM>/<md5>.mp4` (+ `_thumb.jpg`), with the md5 in the message's `packed_info_data`.

## Files

| File | Purpose |
|------|---------|
| `setup.sh` | One-time environment setup (venv, scanner, WeChat signing) |
| `wechat` | Launcher: `./wechat …` → export, `./wechat decrypt` → decrypt, `./wechat moments` / `favorites` → Moments / Favorites, `./wechat backup` → backup, `./wechat send` → send |
| `find_all_keys_macos.c` | C — scans WeChat process memory for SQLCipher keys (Mach VM API) |
| `decrypt_db.py` | Decrypts changed databases; extracts keys automatically when missing or stale |
| `backup.py` | Unattended backup (`./wechat backup`) and LaunchAgent install |
| `launcher/` | WeChatBackup.app, the backup job's launcher (C, ad-hoc signed; the only thing granted Full Disk Access) |
| `export_chat.py` | Command line: search, list, export |
| `wechat_send.py` | Sending: chat resolution, safety checks, rate limit, verification (`./wechat send`) |
| `helper/` | WeChatSendHelper.app (Swift): keyboard, screenshots and OCR for sending; the only app granted Accessibility / Screen Recording |
| `mcp_server.py` | MCP server for AI agents (read-only chat, search, media, Moments, Favorites) |
| `chats.py` | Chat discovery, contacts, group sender names, message parsing |
| `formatters.py` | txt / md / html / json / csv writers |
| `moments.py`, `favorites.py`, `social_common.py` | Moments / Favorites parsing, media lookup and export |
| `image_decode.py` | Decodes WeChat `.dat` images and maps messages to image files |
| `media_decode.py` | Voice (SILK → m4a) and video lookup; `--test N` checks decoding on your data |
| `config.py` | Config loader and auto-detection |
| `tests/` | Unit tests (synthetic data): `./venv/bin/python -m unittest discover -s tests` |

## Acknowledgments

Inspired by [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) by [@ylytdeng](https://github.com/ylytdeng).

## Disclaimer

This tool is for personal use only — to access **your own** WeChat data on your own machine. Respect applicable laws and do not use it for unauthorized data access.

## License

[MIT](LICENSE)
