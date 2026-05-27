"""Configuration for discord-mcp: .env loading, token/channel resolution, REST constants."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load discord-mcp/.env (config.py -> discord_mcp -> src -> discord-mcp == parents[2]),
# regardless of the current working directory.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

API_BASE = "https://discord.com/api/v10"

# Network timeout for all REST calls (seconds).
HTTP_TIMEOUT = 30.0


def get_token() -> str:
    """Return the bot token, or raise if missing."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. Add it to discord-mcp/.env or the MCP server env."
        )
    return token


def resolve_channel_id(channel_id: str = "") -> str:
    """Use the explicit channel_id if given, else fall back to DISCORD_CHANNEL_ID."""
    cid = (channel_id or os.environ.get("DISCORD_CHANNEL_ID", "")).strip()
    if not cid:
        raise RuntimeError(
            "No channel_id given and DISCORD_CHANNEL_ID is not set. "
            "Pass channel_id=... or set the env var."
        )
    return cid


def get_guild_id() -> str:
    """Optional guild id, used only to build clickable message links."""
    return os.environ.get("DISCORD_GUILD_ID", "").strip()


def auth_headers() -> dict[str, str]:
    """Authorization header for bot-token REST calls. Discord requires a User-Agent."""
    return {
        "Authorization": f"Bot {get_token()}",
        "User-Agent": "discord-mcp (embodied-claude, v0.1.0)",
    }
