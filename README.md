# 微信 macOS 数据库解密 & 聊天记录导出

[![CI](https://github.com/BoXu1225/wechat-decrypt-export/actions/workflows/ci.yml/badge.svg)](https://github.com/BoXu1225/wechat-decrypt-export/actions/workflows/ci.yml)

[English](README_EN.md)

我们的数据，我们做主！解密微信 4.x (macOS) 本地 SQLCipher 4 加密数据库，把单聊和群聊导出为 txt、Markdown、HTML、JSON 或 CSV，支持图片、可播放的语音和视频。

## 安装

环境要求：macOS (Apple Silicon / Intel)，微信 4.x 已登录。

```bash
./setup.sh
```

`setup.sh` 可重复运行。它会检查 Xcode 命令行工具和 Python 3.10+，创建 `venv/` 并安装依赖，编译密钥扫描器，并在征得你同意后在 `~/WeChat.app` 创建一个 **ad-hoc 签名的微信副本**（扫描器读取微信内存需要此签名；SIP 不允许对 `/Applications` 下的应用签名）。之后请退出原版微信，改为启动 `~/WeChat.app`。

微信更新后，照常安装到 `/Applications`，再运行一次 `./setup.sh`：它会检测到版本变化并提示重新复制和签名。

## 快速开始

```bash
./wechat ning                  # 导出一个聊天（名称模糊匹配，多个结果时交互选择）
./wechat --list                # 列出所有聊天：名称、类型、消息数、最后消息日期
./wechat --all                 # 导出所有聊天（增量）
```

`./wechat` 会用项目自带的 venv 运行 `export_chat.py`，可在任意目录使用（也可以软链接到 `~/bin`）。每次运行会自动：

1. **提取密钥**（仅在需要时）— 首次运行、微信新建了数据库、或密钥过期时，用 `sudo` 运行扫描器（需要输入密码，微信必须打开）。其余情况直接跳过。
2. **解密** — 只解密自上次以来有变化的数据库（`.db` 或其 `-wal` 有变化），输出到 `decrypted/`，并进行 HMAC 校验和 SQLite 完整性检查。微信尚未写回主文件、还在 `-wal` 里的最新消息也会合并进去。
3. **导出**。

单独解密：`./wechat decrypt`

## 导出

```bash
./wechat ning -f html --images           # 带图片的聊天气泡风格 HTML
./wechat ning -f html --media            # 再加上可直接播放的语音和视频
./wechat ning -i                         # 增量：新消息追加到 export/<名称>.txt
./wechat ning --since 2026-01-01 --until 2026-06-30
./wechat --all --type group -f md        # 所有群聊导出为 Markdown
./wechat --list 同学                      # 列出名称包含“同学”的聊天
```

| 参数 | 说明 |
|---|---|
| `contact` | 备注 / 昵称 / 群名，支持部分匹配；配合 `--list` / `--all` 时作为过滤词 |
| `-f`, `--format` | `txt`（默认）、`md`、`html`、`json`、`csv` |
| `-i`, `--incremental` | 导出到 `export/<名称>.<扩展名>`；txt/md/csv 只追加新消息，html/json 重新生成 |
| `--all` | 增量导出所有聊天 |
| `--list [过滤词]` | 列出聊天 |
| `--type` | `all`（默认）、`single`（单聊）、`group`（群聊） |
| `--images` | 解码图片到 `export/<名称>_files/`，在 md/html/json 中直接显示（txt/csv 仍显示 `[图片]`）；已缓存的表情也会一起显示 |
| `--voice` | 把语音转换为 `export/<名称>_files/<id>.m4a`，html 中可直接播放（`<audio>`），md/json 中为链接；txt/csv 显示 `[语音 12″]` |
| `--media` | `--images` + `--voice` + 复制已下载的视频（`<md5>.mp4`，封面 `<md5>_thumb.jpg`），html 中用 `<video>` 播放；txt/csv 显示 `[视频 0:35]` |
| `--download-emoji` | 配合 `--images` / `--media`：本地没有的表情从消息里的微信 CDN 地址下载（默认关闭，缓存到 `decrypted/emoji_cache/`） |
| `--since` / `--until` | 日期范围 `YYYY-MM-DD`，包含当天 |
| `-o`, `--output` | 输出文件（默认 `export/<名称>_chat.<扩展名>`） |
| `--export-dir` | 导出目录（默认 `export/`） |
| `-d`, `--decrypted-dir` | 已解密数据库目录 |
| `--no-decrypt` | 跳过自动解密 |

导出功能：
- **群聊**：每条消息显示发送者的群昵称，没有则显示你的备注或对方昵称
- **跨数据库的聊天**（`message_0.db`、`message_1.db`……大约每年一个）
- **消息类型**：文字、图片、语音、视频、表情、链接、文件、引用、小程序、系统消息；自动解压 zstd 压缩的消息
- **图片**：解码微信 4.x 加密的 `.dat` 图片（包括基于 HEVC 的 wxgf 格式，用 macOS 自带的 `sips` 转为 JPEG；带透明通道的 wxgf 转为透明 PNG）。图片密钥从本地账号文件推导，不需要额外的 `sudo`。如果微信只有缩略图（原图从未下载），先使用缩略图，之后原图出现时再次导出会自动替换
- **语音 / 视频**：语音（存放在 `message/media_0.db` 中的 SILK v3 数据）用 `silk-python` 解码，再用 macOS 自带的 `afconvert` 编码为 AAC `.m4a`（没有 afconvert 时保存为 WAV）。微信已下载的视频是普通 MP4，直接复制（同一磁盘卷上为 APFS 克隆，不额外占空间）；未下载的视频显示封面。时长取自消息本身。已导出的文件会复用，增量导出只转换新消息。音视频使用 `preload="none"`，大型 HTML 也能快速打开
- **表情**：微信本地的表情缓存是加密的（密钥未知），所以表情图片需要用 `--download-emoji` 从消息中的 CDN 地址下载一次（只访问微信 CDN 域名，有超时和大小限制；失败的一周内不再重试）。`./venv/bin/python emoticon.py` 可统计有多少表情可下载（只输出数量）
- **旧的增量格式**：如果之前用过 `export/<名称>/output_N.txt` 格式，第一次增量导出时会把这些文件合并到 `export/<名称>.txt`，之后可以删除旧文件夹

## 自动备份

```bash
./wechat backup              # 立即备份一次：解密有变化的数据库 + 增量导出全部聊天
./wechat backup --install    # 安装每日定时任务（默认 03:30）
./wechat backup --status     # 是否已安装、下次运行时间、上次运行结果
./wechat backup --uninstall  # 卸载
```

- **从不使用 sudo**：只用 `all_keys.json` 里现有的密钥。密钥缺失或过期的数据库会被跳过（导出使用上次解密的数据），并弹出通知提醒你手动运行 `./wechat decrypt`。
- 按 `config.json` 的 `backup` 配置（均可省略，下面是默认值）对每种格式运行一次 `--all -i`：
  ```json
  "backup": {"formats": ["html", "txt"], "media": true, "dir": "export", "time": "03:30"}
  ```
  `media` 为 true 时导出图片（导出命令支持 `--media` 时用 `--media`，否则用 `--images`）；`dir` 可用 `--dir` 或环境变量 `WECHAT_BACKUP_DIR` 临时覆盖；修改 `time` 后需重新 `--install`。
- 每次运行在 `logs/backup.jsonl` 追加一行摘要（时间、耗时、更新的聊天数、新增消息数、跳过的数据库、错误），不含消息内容。定时任务的输出在 `~/Library/Logs/wechat-decrypt-export/backup.log`。
- 只在失败或需要手动提取密钥时发送 macOS 通知；成功时不打扰。有锁文件，不会重叠运行。
- 退出码：0 成功，1 失败，2 配置错误，3 已有备份在运行，4 完成但有数据库需要新密钥。
- 定时任务是 LaunchAgent（`~/Library/LaunchAgents/local.wechat-decrypt-export.backup.plist`），以低优先级运行；屏幕锁定时照常运行（只读写文件）。到点时电脑在睡眠，会在**唤醒后补跑**一次（错过多次也只补一次）；关机或未登录时不运行。
- **权限**：由 launchd 启动时，macOS 要求单独授权读取微信的数据（终端的授权不适用）。请在 系统设置 > 隐私与安全性 > **完全磁盘访问权限** 中添加 `--install` 打印的 Python 路径（Homebrew 升级 Python 后需重新添加），然后用 `launchctl kickstart gui/$(id -u)/local.wechat-decrypt-export.backup` 试运行、`./wechat backup --status` 查看结果。没有权限时备份不会卡在授权弹窗上：约 45 秒后放弃解密，只导出已解密的数据，并发送通知。

## 朋友圈与收藏

```bash
./wechat moments                         # 本地缓存的全部朋友圈 -> export/moments.html
./wechat moments 张三 --images            # 某人的朋友圈，附本地缓存的图片/视频
./wechat moments 我 -f md --since 2025-01-01
./wechat favorites                       # 全部收藏 -> export/favorites.html
./wechat favorites 发票 --type file -f json
```

两者都支持 `-f html|md|json|txt`（默认 html）、`--since` / `--until`、`--images`、`-o`、`--export-dir`、`--no-decrypt`。`moments` 可按作者过滤（备注/昵称/微信号，部分匹配；`我` = 自己），`-q` 按内容搜索；`favorites` 可带搜索词、`--type` 和 `--tag`。输出为 `export/moments[_<名称>].<扩展名>` / `export/favorites.<扩展名>`，媒体文件在同名 `_files/` 目录。

- **朋友圈**（`sns/sns.db`）只包含本机微信加载过的动态（自己的和刷到过的好友动态），含文字、链接、位置、点赞和评论（按通讯录显示名称）。图片/视频只有在微信本地缓存（`cache/<月份>/Sns/`）中时才能导出；CDN 链接保留在 JSON 中，不会联网下载。
- **收藏**（`favorite/favorite.db`）：文字、链接、图片、文件、聊天记录、笔记、位置等，含来源聊天/发送者和标签。只有微信已保存在本地的文件能导出。

## 配置

无需配置：首次运行时会自动检测微信数据目录并保存到 `config.json`（有多个账号时让你选择），你自己的微信 ID 从目录名自动推导。如需手动指定，编辑 `config.json`：

```json
{
    "db_dir": "/Users/你的用户名/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/你的微信ID/db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "WeChat"
}
```

可选：`image_aes_key`（16 个字符）和 `image_xor_key` 可覆盖自动推导的图片密钥。

## 原理

微信 4.x 使用 SQLCipher 4 加密本地数据库：
- **加密算法**: AES-256-CBC + HMAC-SHA512
- **密钥派生**: PBKDF2-HMAC-SHA512, 256,000 次迭代
- **页面大小**: 4096 字节, 保留区 = 80 (IV 16 + HMAC 64)
- **每个数据库有独立的 salt 和密钥**

WCDB（微信的 SQLCipher 封装层）会在进程内存中缓存派生后的原始密钥，格式为 `x'<64位hex密钥><32位hex盐值>'`。扫描器在微信内存中查找该模式，并通过盐值匹配到对应的数据库。解密前会用第 1 页的 HMAC 校验每个密钥，因此过期的密钥会被自动发现并重新提取。

每个聊天的消息存放在名为 `Msg_<md5(用户名)>` 的表中，内容可能经过 zstd 压缩（WCDB_CT=4）。聊天图片以 `.dat` 文件存放在 `msg/attach/<md5(用户名)>/<年-月>/Img/` 下，前 1 KB 用 AES-128-ECB 加密，其余部分做了 XOR。语音是 `message/media_0.db` 中 `VoiceInfo` 表里的 SILK 数据（按聊天 + server id，或 create_time + local_id 对应）；视频是未加密的 `msg/video/<年-月>/<md5>.mp4`（及 `_thumb.jpg`），md5 在消息的 `packed_info_data` 中。

## 文件说明

| 文件 | 说明 |
|------|------|
| `setup.sh` | 一次性环境配置（venv、扫描器、微信签名） |
| `wechat` | 启动脚本：`./wechat …` 导出，`./wechat decrypt` 解密，`./wechat moments` / `favorites` 朋友圈/收藏，`./wechat backup` 备份 |
| `find_all_keys_macos.c` | C 源码 — 通过 Mach VM API 扫描微信进程内存提取 SQLCipher 密钥 |
| `decrypt_db.py` | 解密有变化的数据库；密钥缺失或过期时自动提取 |
| `backup.py` | 无人值守备份（`./wechat backup`）与定时任务安装 |
| `export_chat.py` | 命令行：搜索、列出、导出 |
| `chats.py` | 聊天发现、联系人、群成员名称、消息解析 |
| `formatters.py` | txt / md / html / json / csv 输出 |
| `moments.py`、`favorites.py`、`social_common.py` | 朋友圈 / 收藏解析、媒体查找和导出 |
| `image_decode.py` | 解码微信 `.dat` 图片，并把消息对应到图片文件 |
| `media_decode.py` | 语音（SILK → m4a）和视频查找；`--test N` 在你的数据上检查解码 |
| `config.py` | 配置加载与自动检测 |
| `tests/` | 单元测试（使用合成数据）：`./venv/bin/python -m unittest discover -s tests` |

## 致谢

本项目受 [@ylytdeng](https://github.com/ylytdeng) 的 [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) 启发。

## 免责声明

本工具仅供个人使用，用于在自己的电脑上访问**自己的**微信数据。请遵守相关法律法规，不要用于未经授权的数据访问。

## 许可证

[MIT](LICENSE)
