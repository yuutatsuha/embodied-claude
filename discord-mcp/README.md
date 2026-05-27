# discord-mcp

MCP server that sends and reads Discord messages via the REST API using a bot token.
No persistent gateway connection — it fits the short-lived stdio MCP model.

## Tools

- `send_message(text, channel_id="", image_path="")` — post a message to a channel (optional image attachment).
- `read_recent(channel_id="", limit=10)` — read recent messages from a channel, oldest first.

If `channel_id` is omitted, `DISCORD_CHANNEL_ID` from the environment is used.

## Setup

1. Create an application + bot in the [Discord Developer Portal](https://discord.com/developers/applications) and copy the bot token.
2. Enable the **Message Content Intent** (privileged) on the Bot page — required so `read_recent` returns message text.
3. Invite the bot with **View Channel + Send Messages + Read Message History + Attach Files**
   (OAuth2 → URL Generator, scope `bot`; or `permissions=52224`).
4. Enable Developer Mode in Discord and copy the target **Channel ID** (right-click channel → Copy Channel ID).
5. Copy `.env.example` to `.env` and fill in `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, and optionally `DISCORD_GUILD_ID`.

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest -v
uv run discord-mcp
```
