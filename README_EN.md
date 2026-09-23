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
| `--images` | Decode images into `export/<name>_files/` and embed them in md/html/json (txt/csv show `[图片]`) |
| `--voice` | Convert voice messages to `export/<name>_files/<id>.m4a`, playable in html (`<audio>`) and linked from md/json; txt/csv show `[语音 12″]` |
| `--media` | `--images` + `--voice` + copy downloaded videos (`<md5>.mp4`, thumbnail `<md5>_thumb.jpg`) for html `<video>` / md / json; txt/csv show `[视频 0:35]` |
| `--since` / `--until` | Date range, `YYYY-MM-DD`, inclusive |
| `-o`, `--output` | Output file (default `export/<name>_chat.<ext>`) |
| `--export-dir` | Export folder (default `export/`) |
| `-d`, `--decrypted-dir` | Decrypted database folder |
| `--no-decrypt` | Skip the automatic decrypt step |

What the exporter handles:
- **Group chats**: each message shows the sender's group nickname, else your remark/their nickname
- **Chats spanning several databases** (`message_0.db`, `message_1.db`, … — roughly one per year)
- **Message types**: text, images, voice, video, stickers, links, files, quotes, mini programs, system messages; zstd-compressed messages are decompressed
- **Images**: WeChat 4.x encrypted `.dat` images (including the HEVC-based wxgf format, converted to JPEG with macOS's built-in `sips`). The image key is derived from local account files — no extra `sudo` needed. When WeChat only has a thumbnail (full image never downloaded), the thumbnail is used and upgraded on a later export once the full image exists
- **Voice / video**: voice messages (SILK v3 stored in `message/media_0.db`) are decoded with the `silk-python` package and encoded to AAC `.m4a` with macOS's built-in `afconvert` (WAV if unavailable). Videos WeChat has downloaded are plain MP4s and are copied (an APFS clone on the same volume, so no extra space); videos never downloaded show their thumbnail. Durations come from the message. Already exported files are reused, so incremental exports only convert new messages. Audio/video use `preload="none"` so large HTML pages open quickly
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
- **Permission**: when started by launchd, macOS requires its own grant to read WeChat's data (Terminal's grant does not apply). Add the Python path printed by `--install` under System Settings > Privacy & Security > **Full Disk Access** (again after a Homebrew Python upgrade), then test with `launchctl kickstart gui/$(id -u)/local.wechat-decrypt-export.backup` and `./wechat backup --status`. Without it the backup does not hang on the consent prompt: after ~45 s it skips decryption, exports the already-decrypted data and notifies you.

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
| `wechat` | Launcher: `./wechat …` → export, `./wechat decrypt` → decrypt, `./wechat backup` → backup |
| `find_all_keys_macos.c` | C — scans WeChat process memory for SQLCipher keys (Mach VM API) |
| `decrypt_db.py` | Decrypts changed databases; extracts keys automatically when missing or stale |
| `backup.py` | Unattended backup (`./wechat backup`) and LaunchAgent install |
| `export_chat.py` | Command line: search, list, export |
| `chats.py` | Chat discovery, contacts, group sender names, message parsing |
| `formatters.py` | txt / md / html / json / csv writers |
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
