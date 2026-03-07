# WeChat macOS Database Decryptor & Chat Exporter

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

- macOS (Apple Silicon / Intel)
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

### 1. Extract encryption keys

```bash
# Compile (one-time)
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation

# Run (WeChat must be open, requires root)
sudo ./find_all_keys_macos
```

Outputs `all_keys.json` mapping each database to its encryption key.

### 2. Decrypt databases

```bash
python decrypt_db.py
```

Decrypts all databases to `decrypted/`. Validates each with HMAC and SQLite integrity check.

### 3. Export a chat

```bash
python export_chat.py xxx
python export_chat.py xxx -o chats/output.txt
```

Options:
- `-o`, `--output` — output file path (default: `<contact>_chat.txt`)
- `-d`, `--decrypted-dir` — custom path to decrypted databases

The exporter:
- Searches across all `message_*.db` files (chats can span multiple DBs)
- Resolves per-DB Name2Id rowid mappings correctly
- Decompresses zstd-encoded messages (CT=4)
- Formats message types: text, images, stickers, links, quotes, files, mini programs, system messages

## Configuration

On first run, create `config.json` (or edit the existing one):

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

---

# 微信 macOS 数据库解密 & 聊天记录导出

我们的数据，我们做主！解密微信 4.x (macOS) 本地 SQLCipher 4 加密数据库，导出聊天记录为可读文本文件。

## 原理

微信 4.x 使用 SQLCipher 4 加密本地数据库：
- **加密算法**: AES-256-CBC + HMAC-SHA512
- **密钥派生**: PBKDF2-HMAC-SHA512, 256,000 次迭代
- **页面大小**: 4096 字节, 保留区 = 80 (IV 16 + HMAC 64)
- **每个数据库有独立的 salt 和加密密钥**

WCDB（微信的 SQLCipher 封装层）会在进程内存中缓存派生后的原始密钥，格式为 `x'<64位hex密钥><32位hex盐值>'`。本工具扫描进程内存中的该模式，通过盐值匹配数据库，提取正确的密钥。

消息内容可能经过 zstd 压缩（WCDB_CT=4，无需字典）。导出步骤会自动处理解压。

## 环境要求

- macOS (Apple Silicon / Intel)
- 微信 4.x 正在运行且已登录
- Xcode 命令行工具: `xcode-select --install`
- Python 3.10+，安装依赖: `pip install -r requirements.txt`
- 微信需要 **ad-hoc 签名**（用于内存访问）:
  ```bash
  # SIP 会阻止对 /Applications 下应用的签名，需先复制
  cp -R /Applications/WeChat.app ~/WeChat.app
  codesign --force --deep --sign - ~/WeChat.app
  # 之后启动 ~/WeChat.app 而非原版
  ```

## 快速开始

### 1. 提取加密密钥

```bash
# 编译（仅需一次）
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation

# 运行（微信必须打开，需要 root 权限）
sudo ./find_all_keys_macos
```

输出 `all_keys.json`，包含每个数据库与其加密密钥的映射。

### 2. 解密数据库

```bash
python decrypt_db.py
```

将所有数据库解密到 `decrypted/` 目录。每个数据库会进行 HMAC 校验和 SQLite 完整性检查。

### 3. 导出聊天记录

```bash
python export_chat.py xxx
python export_chat.py xxx -o chats/output.txt
```

参数：
- `-o`, `--output` — 输出文件路径（默认: `<联系人备注>_chat.txt`）
- `-d`, `--decrypted-dir` — 自定义解密数据库目录路径

导出功能：
- 自动搜索所有 `message_*.db`（聊天记录可能分布在多个数据库中）
- 正确处理每个数据库独立的 Name2Id rowid 映射
- 自动解压 zstd 压缩的消息（CT=4）
- 格式化各类消息：文字、图片、表情、链接、引用回复、文件、小程序、系统消息

## 配置

首次运行前，创建或编辑 `config.json`：

```json
{
    "db_dir": "/Users/你的用户名/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/你的微信ID/db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "WeChat"
}
```

`db_dir` 路径可在 `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/` 下找到。

## 文件说明

| 文件 | 说明 |
|------|------|
| `find_all_keys_macos.c` | C 源码 — 通过 Mach VM API 扫描微信进程内存提取 SQLCipher 密钥 |
| `decrypt_db.py` | 使用提取的密钥解密所有数据库 |
| `export_chat.py` | 导出单人聊天记录为文本文件 |
| `config.py` | 配置加载器 |
| `config.json` | 本地配置文件 |
| `requirements.txt` | Python 依赖 |

## 致谢

本项目受 [@ylytdeng](https://github.com/ylytdeng) 的 [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) 启发。

## 免责声明

本工具仅供个人使用，用于访问**自己的**微信数据。请遵守相关法律法规，不要用于未经授权的数据访问。
