# Coding agent guide

## Scope and starting points

This public repository reads, decrypts, searches, exports and backs up local
WeChat data. It has no message sender or UI automation. Preserve that scope.
For agents using an installed MCP, see `docs/agents.md`; this file is for agents
modifying the repository.

Read `docs/architecture.md` for the module map. Start with:

- `mcp_server.py` for tools, server instructions, access policy and search.
- `chats.py` / `msg_parse.py` for history and message formats.
- `decrypt_db.py` for snapshots and WAL; `config.py` for paths/permissions.
- `export_chat.py` / `formatters.py` for exports; `backup.py` / `launcher/` for scheduling.
- `image_decode.py`, `media_decode.py`, `emoticon.py` for media.
- `moments.py`, `favorites.py`, `social_common.py` for social data.

Keep root modules and existing entry points compatible. Most paths derive from
the module's directory; moving a module can silently relocate config, keys or
outputs. Put detailed prose under `docs/` and link it from `docs/README.md`.
Update Chinese and English entry-point docs together for user-facing changes.

## Runtime contracts

- Stdout on the MCP entry point carries protocol frames only. Diagnostics go to stderr.
- Keep tool names, parameter meanings and `N:local_id` chat-scoped IDs stable.
- Respect allowlist/blocklist policy across chat, contact, search and media tools.
- Preserve compact text output and JSON error fields; document schema changes.
- MCP refresh and backup use saved keys only. Never add scanner/sudo fallback there.
- Source WeChat databases are inputs; generated output belongs in local snapshot/export paths.
- Keep bounded, validated sticker downloads and owner-only output permissions.

## Private data and development

Use synthetic fixtures from `tests/`, never personal chats or database extracts.
Do not print, copy into fixtures, commit or upload local config, keys, databases,
media, exports or logs. Keep the existing ignore rules and permission helpers.
Do not run setup, key extraction, real-data exports, media downloads or launchd
installation merely to inspect or lint code; those commands affect a real account
or machine. Use the user's authorization and task context for operational work.

Run commands from the repository root with the repository virtual environment.
When the user asks for tests or verification, the existing suite is:

```bash
./venv/bin/python -m unittest discover -s tests -v
```

CI also checks Python syntax and pyflakes on Python 3.10/3.13, compiles the key
scanner and backup launcher on macOS, and runs shellcheck. CI uses synthetic
fixtures; it must not require WeChat, real account keys or elevated privileges.
Do not add or run tests unless requested. Existing CLI help is available with
`./wechat --help`; CLI usage is in `docs/usage.en.md` and `docs/usage.zh-CN.md`.
