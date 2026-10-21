# WeChat local history for AI agents

[![CI](https://github.com/BoXu1225/wechat-decrypt-export/actions/workflows/ci.yml/badge.svg)](https://github.com/BoXu1225/wechat-decrypt-export/actions/workflows/ci.yml)

[中文](README.md)

Read and search WeChat 4.x history on your Mac through one MCP server.
Decrypt local databases and export chats, media, Moments and Favorites.
This project does not send messages or automate the WeChat interface.

## Set up MCP

Requirements: macOS, Python 3.10+, WeChat 4.x logged in, and access to your own
local account data. Run these commands from the repository root:

```bash
./setup.sh
./wechat decrypt
claude mcp add --scope user wechat -- "$PWD/venv/bin/python" "$PWD/mcp_server.py"
```

Setup creates the Python environment, builds the scanner, and offers to create
an ad-hoc signed WeChat copy at `~/WeChat.app`. Key extraction during decrypt can
ask for sudo. Complete this interactive setup before using the MCP; the server
only refreshes snapshots with saved keys. See [setup details](docs/usage.en.md#setup).

For another MCP client, register a stdio server using absolute paths:

```json
{"mcpServers":{"wechat":{
  "command":"/ABS/PATH/wechat-decrypt-export/venv/bin/python",
  "args":["/ABS/PATH/wechat-decrypt-export/mcp_server.py"]
}}}
```

## What agents can do

| Task | MCP tools |
| --- | --- |
| Discover conversations and resolve names | `list_chats`, `get_contact` |
| Read a conversation, page through history | `get_messages` |
| Find a topic and inspect its surrounding messages | `search_messages`, `get_message_context` |
| Inspect an image, sticker or voice message | `get_image`, `get_voice` |
| Read locally cached Moments and Favorites | `get_moments`, `search_favorites`, `get_favorite` |
| Update decrypted snapshots using saved keys | `refresh` |

Start with a narrow search or chat filter, reuse the returned username, and
read context before summarizing a decision. For example: “What did we decide
about the trip last week?” or “Find the address sent in the family group.”
See [agent workflows and error handling](docs/agents.md) for concrete calls,
pagination, media, access filters and freshness limits.

## CLI and detailed guides

```bash
./wechat --list
./wechat 张三 -f html --media
./wechat --all -i
./wechat moments
./wechat favorites
./wechat backup --status
```

Exports may extract keys when needed; unattended backups use saved keys only.
[Full usage, setup and backup guide](docs/usage.en.md) ·
[Documentation index](docs/README.md) · [Architecture](docs/architecture.md).

## Working on this repository

Coding agents: read [AGENTS.md](AGENTS.md) for module boundaries, commands and
private-data handling. The root Python modules retain stable imports and local
paths; detailed documentation lives in `docs/`.

Config, keys, decrypted databases, exports and logs are local and ignored by Git.
Use this tool for your own WeChat data. [MIT license](LICENSE).
Inspired by [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt).
