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
- **权限**：由 launchd 启动时，macOS 要求单独授权读取微信的数据（终端的授权不适用）。定时任务通过 `~/Applications/WeChatBackup.app` 启动（`--install` 自动构建安装；这是一个极小的 ad-hoc 签名启动器，仓库路径编译在里面，只能以子进程运行本仓库的 `backup.py`），macOS 把访问归属到它，所以**只需为 WeChatBackup.app 授权**，不要为 Python 或终端授权：系统设置 > 隐私与安全性 > **完全磁盘访问权限** > 点「+」> 按 ⌘⇧G 输入 `~/Applications/WeChatBackup.app` > 打开，并确认开关已打开。然后用 `launchctl kickstart gui/$(id -u)/local.wechat-decrypt-export.backup` 试运行、`./wechat backup --status` 查看结果。重复 `--install` 时只有启动器源码或仓库路径变化才会重新构建（重新构建后需删除旧条目并重新授权）。没有权限时备份不会卡在授权弹窗上：约 45 秒后放弃解密，只导出已解密的数据，并发送通知。

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

## AI 助手（MCP 服务器）

`mcp_server.py` 让 Claude Code、Claude Desktop 等 MCP 客户端读取你的聊天记录（并在你每次同意后发送消息）。先解密一次（`./wechat decrypt`），然后注册：

```bash
claude mcp add --scope user wechat -- "$PWD/venv/bin/python" "$PWD/mcp_server.py"
```

Claude Desktop（`~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{"mcpServers": {"wechat": {"command": "/绝对路径/venv/bin/python", "args": ["/绝对路径/mcp_server.py"]}}}
```

然后就可以问："上周我和张三关于旅行最后怎么定的？"、"找一下家庭群里有人发过的那个地址"。

| 工具 | 功能 |
|------|------|
| `list_chats` | 聊天列表（名称、类型、最近活动），可按名称、单聊/群聊过滤 |
| `get_messages` | 某个聊天的消息，按时间范围或游标分页，正序或倒序 |
| `search_messages` | 全文搜索（支持中文），可限定聊天、发送者和时间 |
| `get_message_context` | 某条消息或某个时间点前后的消息 |
| `get_contact` | 联系人或群的备注、昵称、微信号和共同群聊 |
| `get_image` / `get_voice` | 聊天图片（以图片返回）或语音（以音频返回） |
| `get_moments` | 本地缓存的朋友圈，可按作者、时间、内容筛选 |
| `search_favorites` / `get_favorite` | 搜索和读取收藏 |
| `refresh` | 立即重新解密有变化的数据库 |
| `send_message` | 发送文字消息（每次先征求同意，见「发送消息」） |

- **除 `send_message` 外只读。** 读取类工具都标记为只读；`send_message` 标记为破坏性操作，客户端每次发送前都会征求同意。
- **访问范围：** 默认所有聊天可见。要隐藏某些聊天，在 `config.json` 中加 `"mcp_blocklist": ["名称或 wxid", …]`；`"mcp_allowlist"` 非空时只显示列出的聊天。每次调用都记录在 `logs/mcp_access.jsonl`（工具、参数、结果数量，不含消息内容）。
- **数据新鲜度：** 读取前会用已有密钥重新解密有变化的数据库，最多每 `mcp_auto_refresh_minutes` 分钟一次（默认 5，`0` 关闭）。它不会运行密钥扫描器；密钥过期时会提示你运行 `./wechat decrypt`。微信会把最新写入暂存在 WAL 中，最新消息可能略有延迟。
- **紧凑输出：** 结果是纯文本行——日期行下每条消息一行 `HH:MM 发送者: 内容`，聊天名只出现一次——比 JSON 小约 60–70%，助手可以读更长的聊天记录。需要 JSON 时在 `config.json` 中设置 `"mcp_output": "json"`。
- **搜索索引：** 首次使用时在 `decrypted/mcp_index.db` 建立（约 20 万条消息需几秒），之后增量更新。
- **自检：** `./venv/bin/python mcp_server.py --selftest` 输出统计数字（不含消息内容）后退出。

## 发送消息

`./wechat send`（以及 MCP 工具 `send_message`）像你本人一样操作本机的微信来发送文字消息：搜索聊天、确认打开的是对的聊天、粘贴、核对文字、按回车，最后在数据库里确认消息已发出。

```bash
helper/install.sh                                   # 一次性：构建并启动 WeChatSendHelper.app
./wechat send --check                               # 检查权限和微信状态
./wechat send --to 张三 --text "晚上七点见" --dry-run   # 输入后清空，不发送
./wechat send --to 张三 --text "晚上七点见"             # 确认后发送
```

- **一次性设置：** `helper/install.sh` 安装 `~/Applications/WeChatSendHelper.app`（一个小的后台助手），并创建本地代码签名证书，使重新构建后权限不丢失（macOS 可能会要求输入一次登录密码，请选「始终允许」）。只给这个助手——不要给别的程序——在 系统设置 → 隐私与安全性 中授予两项权限：**辅助功能**（在微信里按键）和 **录屏与系统录音**（读取微信窗口），然后运行 `launchctl kickstart -k gui/$(id -u)/local.wechat-decrypt-export.sendhelper`。
- **为什么要截屏：** 微信 4 自绘界面，辅助功能里什么都读不到，所以助手用 Apple 本地 OCR（Vision）读取聊天标题、搜索结果和输入框。数据不会离开本机。
- **安全检查：** 必须给出准确的名称或微信 ID（模糊名称会返回候选）；只点击「联系人 / 群聊」分组下名称完全一致的搜索结果；输入前标题必须匹配；不会动已有草稿；按回车前会读回文字，并在按键前用同一张截图再次核对标题和文字；发送后在数据库确认（`sent`，消息尚未出现时为 `unverified`）。频率限制：两次发送间隔 3 秒，每分钟最多 6 条（`config.json` 中的 `send_rate_limit`）。每次发送记录在 `logs/send_log.jsonl`（聊天、时间、文字哈希，不含文字）。
- **要求：** 微信已运行并登录、Mac 未锁屏。仅支持文字，不支持文件和图片。
- **和助手共用电脑：** 发送时约 10 秒占用真实的键盘焦点。开始前会等到你 2 秒没有打字、点击或滚动（移动鼠标没关系）（最多等 30 秒，否则放弃并返回 `user_busy`，什么都没发生）。进行中屏幕顶部会显示提示「正在发送给 …——请不要操作键盘鼠标」，结束时显示「已发送」或「已停止」。期间如果你打字、点击、滚动或切换应用，它会在按回车前停止（`user_activity`），不会发送；如果文字已经输入，会作为未发送的草稿留在该聊天中，结果里会说明。读取聊天记录完全不涉及屏幕操作。相关设置：`send_idle_s`、`send_idle_timeout_s`。
- **AI 助手：** `send_message` 标记为破坏性操作，Claude Code 每次发送前都会征求你的同意。被 `mcp_blocklist` / `mcp_allowlist` 隐藏的聊天会被拒绝；在 `config.json` 设置 `"mcp_send": false` 可移除该工具。
- **限制：** 文件传输助手在微信 4 的联系人搜索里搜不到，因此无法作为发送目标。如果把输入框上方的分隔线拖得很高，文字核对会失败，不会发送。

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
| `wechat` | 启动脚本：`./wechat …` 导出，`./wechat decrypt` 解密，`./wechat moments` / `favorites` 朋友圈/收藏，`./wechat backup` 备份，`./wechat send` 发送 |
| `find_all_keys_macos.c` | C 源码 — 通过 Mach VM API 扫描微信进程内存提取 SQLCipher 密钥 |
| `decrypt_db.py` | 解密有变化的数据库；密钥缺失或过期时自动提取 |
| `backup.py` | 无人值守备份（`./wechat backup`）与定时任务安装 |
| `launcher/` | 定时备份的启动器 WeChatBackup.app（C，ad-hoc 签名；完全磁盘访问权限只授予它） |
| `export_chat.py` | 命令行：搜索、列出、导出 |
| `wechat_send.py` | 发送：聊天解析、安全检查、频率限制、发送确认（`./wechat send`） |
| `helper/` | WeChatSendHelper.app（Swift）：发送时的按键、截屏和 OCR；只有它被授予辅助功能 / 录屏权限 |
| `mcp_server.py` | 供 AI 助手使用的 MCP 服务器（只读：聊天、搜索、媒体、朋友圈、收藏） |
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
