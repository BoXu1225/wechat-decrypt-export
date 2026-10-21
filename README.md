# 面向 AI 助手的微信本地聊天记录工具

[![CI](https://github.com/BoXu1225/wechat-decrypt-export/actions/workflows/ci.yml/badge.svg)](https://github.com/BoXu1225/wechat-decrypt-export/actions/workflows/ci.yml)

[English](README_EN.md)

通过一个 MCP 服务器，让 AI 助手读取和搜索 Mac 上的微信 4.x 本地聊天记录。
支持数据库解密、聊天与媒体导出、朋友圈、收藏和自动备份。
本项目不发送消息，也不操作微信界面。

## 配置 MCP

需要 macOS、Python 3.10+、已登录的微信 4.x，以及你自己的本地微信数据。
在仓库根目录运行：

```bash
./setup.sh
./wechat decrypt
claude mcp add --scope user wechat -- "$PWD/venv/bin/python" "$PWD/mcp_server.py"
```

`setup.sh` 创建 Python 环境、编译扫描器，并询问是否在 `~/WeChat.app`
创建临时签名的微信副本。解密时提取密钥可能需要 sudo。
请先在终端完成初始化；MCP 只使用已保存的密钥刷新解密快照。
详见[完整安装说明](docs/usage.zh-CN.md#安装)。

其他 MCP 客户端可配置 stdio 服务器，路径须为绝对路径：

```json
{"mcpServers":{"wechat":{
  "command":"/ABS/PATH/wechat-decrypt-export/venv/bin/python",
  "args":["/ABS/PATH/wechat-decrypt-export/mcp_server.py"]
}}}
```

## AI 助手能做什么

| 需求 | MCP 工具 |
| --- | --- |
| 找到聊天、确认联系人 | `list_chats`、`get_contact` |
| 读取聊天记录、向前或向后翻页 | `get_messages` |
| 搜索话题并查看上下文 | `search_messages`、`get_message_context` |
| 查看图片、表情或语音 | `get_image`、`get_voice` |
| 读取本地缓存的朋友圈与收藏 | `get_moments`、`search_favorites`、`get_favorite` |
| 使用现有密钥刷新解密快照 | `refresh` |

建议先按聊天名或关键词缩小范围，再使用返回的 username 读取上下文。
例如：“上周我们最后决定去哪旅行？”或“找一下家人群里发过的地址”。
[AI 助手使用指南](docs/agents.md)包含调用示例、分页、媒体读取、错误处理与数据新鲜度说明。

## 命令行与详细文档

```bash
./wechat --list
./wechat 张三 -f html --media
./wechat --all -i
./wechat moments
./wechat favorites
./wechat backup --status
```

普通导出在需要时会提取密钥；无人值守备份只使用已保存的密钥。
[完整使用与备份指南](docs/usage.zh-CN.md) · [文档目录](docs/README.md) ·
[项目结构](docs/architecture.md)。

## 开发

修改代码的 AI 助手请先阅读 [AGENTS.md](AGENTS.md)。核心 Python 模块保留在
根目录，保持已有导入和本地路径稳定；详细文档集中在 `docs/`。

配置、密钥、解密数据库、导出文件和日志只保存在本地，不提交到 Git。
请仅访问自己的微信数据。[MIT 许可证](LICENSE)。
本项目受 [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) 启发。
