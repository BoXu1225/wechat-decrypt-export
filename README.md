# 微信 macOS 数据库解密 & 聊天记录导出

[English](README_EN.md)

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

- **macOS** (Apple Silicon / Intel)
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
# 模糊搜索联系人（支持部分匹配，多个结果时交互选择）
python export_chat.py ning

# 指定输出文件
python export_chat.py xxx -o chats/output.txt

# 增量导出（输出到 export/<联系人>/output_0.txt, output_1.txt, ...）
python export_chat.py xxx -i
```

参数：
- `-o`, `--output` — 输出文件路径（默认: `export/<联系人备注>_chat.txt`）
- `-d`, `--decrypted-dir` — 自定义解密数据库目录路径
- `-i`, `--incremental` — 增量导出，仅导出上次导出之后的新消息

导出功能：
- 支持联系人模糊搜索（大小写不敏感的部分匹配）
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
