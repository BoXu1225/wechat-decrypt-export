# WeChat macOS Database Decryptor & Chat Exporter

[中文](README.md)

Our data, we own it! Decrypt WeChat 4.x (macOS) local SQLCipher 4 databases and export chat history to readable text files.

## How it works

WeChat 4.x encrypts local databases with SQLCipher 4:
- **Encryption**: AES-256-CBC + HMAC-SHA512
- **KDF**: PBKDF2-HMAC-SHA512, 256,000 iterations
- **Page size**: 4096 bytes, reserve = 80 (IV 16 + HMAC 64)
- **Each database has its own salt and encryption key**

WCDB (WeChat's SQLCipher wrapper) caches derived raw keys in process memory as `x'<64hex_enc_key><32hex_salt>'`. This tool scans process memory for that pattern, matches keys to databases by salt, and decrypts them.

Message content may be zstd-compressed (WCDB_CT=4, no dictionary). The export step handles decompression automatically.

## Prerequisites

- **macOS** (Apple Silicon / Intel)
- WeChat 4.x running and logged in
- Xcode Command Line Tools: `xcode-select --install`
- Python 3.10+ with dependencies: `pip install -r requirements.txt`
- WeChat must be **ad-hoc signed** (required for memory access):
  ```bash
  # SIP blocks signing in /Applications, so copy first
  cp -R /Applications/WeChat.app ~/WeChat.app
  codesign --force --deep --sign - ~/WeChat.app
  # Launch ~/WeChat.app instead of the original
  ```

## Quick start

One command does everything; the first run handles all setup automatically:

```bash
python export_chat.py ning
```

It will:
1. **Extract keys** — on first run, when a new database appears, or when a key goes stale, it compiles and runs `find_all_keys_macos` (asks for your sudo password; WeChat must be open)
2. **Decrypt databases** — into `decrypted/`, only the ones that changed, validated with HMAC and a SQLite integrity check
3. **Export the chat**

You can also run decryption on its own: `python decrypt_db.py`

### Export options

```bash
# Fuzzy search for a contact (partial match, interactive selection)
python export_chat.py ning

# Specify output file
python export_chat.py xxx -o chats/output.txt

# Incremental export (outputs to export/<contact>/output_0.txt, output_1.txt, ...)
python export_chat.py xxx -i
```

Options:
- `-o`, `--output` — output file path (default: `export/<contact>_chat.txt`)
- `-d`, `--decrypted-dir` — custom path to decrypted databases
- `-i`, `--incremental` — incremental export, only exports messages newer than the last export

The exporter:
- Supports fuzzy contact search (case-insensitive partial matching)
- Searches across all `message_*.db` files (chats can span multiple DBs)
- Resolves per-DB Name2Id rowid mappings correctly
- Decompresses zstd-encoded messages (CT=4)
- Formats message types: text, images, stickers, links, quotes, files, mini programs, system messages

## Configuration

No setup needed: on first run the WeChat data folder is auto-detected and saved to `config.json` (you pick one if there are multiple accounts). Your own WeChat ID is derived from the folder name. To override, edit `config.json`:

```json
{
    "db_dir": "/Users/YOU/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/YOUR_WXID/db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "WeChat"
}
```

Find `db_dir` by browsing `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/`.

## Files

| File | Purpose |
|------|---------|
| `find_all_keys_macos.c` | C — scans WeChat process memory for SQLCipher keys (Mach VM API) |
| `decrypt_db.py` | Decrypts all databases using extracted keys |
| `export_chat.py` | Exports a 1-on-1 chat to a text file |
| `config.py` | Config loader |
| `config.json` | Your local configuration |
| `requirements.txt` | Python dependencies |

## Acknowledgments

Inspired by [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) by [@ylytdeng](https://github.com/ylytdeng).

## Disclaimer

This tool is for personal use only — to access **your own** WeChat data on your own machine. Respect applicable laws and do not use it for unauthorized data access.
