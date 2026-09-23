# WeChat macOS Database Decryptor & Chat Exporter

[中文](README.md)

Our data, we own it! Decrypt WeChat 4.x (macOS) local SQLCipher 4 databases and export your chats — 1-on-1 and group — as text, Markdown, HTML, JSON or CSV, with images.

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
- **Old incremental layout**: if you used the previous `export/<name>/output_N.txt` layout, the first incremental export merges those files into `export/<name>.txt`; the old folder can then be deleted

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

Each chat's messages live in tables named `Msg_<md5(username)>`; message content may be zstd-compressed (WCDB_CT=4). Chat images are stored as `.dat` files under `msg/attach/<md5(username)>/<YYYY-MM>/Img/`, the first 1 KB AES-128-ECB encrypted and the rest XOR'd.

## Files

| File | Purpose |
|------|---------|
| `setup.sh` | One-time environment setup (venv, scanner, WeChat signing) |
| `wechat` | Launcher: `./wechat …` → export, `./wechat decrypt` → decrypt, `./wechat moments` / `favorites` |
| `find_all_keys_macos.c` | C — scans WeChat process memory for SQLCipher keys (Mach VM API) |
| `decrypt_db.py` | Decrypts changed databases; extracts keys automatically when missing or stale |
| `export_chat.py` | Command line: search, list, export |
| `chats.py` | Chat discovery, contacts, group sender names, message parsing |
| `formatters.py` | txt / md / html / json / csv writers |
| `moments.py`, `favorites.py`, `social_common.py` | Moments / Favorites parsing, media lookup and export |
| `image_decode.py` | Decodes WeChat `.dat` images and maps messages to image files |
| `config.py` | Config loader and auto-detection |
| `tests/` | Unit tests (synthetic data): `./venv/bin/python -m unittest discover -s tests` |

## Acknowledgments

Inspired by [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) by [@ylytdeng](https://github.com/ylytdeng).

## Disclaimer

This tool is for personal use only — to access **your own** WeChat data on your own machine. Respect applicable laws and do not use it for unauthorized data access.
