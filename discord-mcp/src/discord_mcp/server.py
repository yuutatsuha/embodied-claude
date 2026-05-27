"""discord-mcp — send and read Discord messages via the REST API (bot token, no gateway)."""

from __future__ import annotations

import json
import mimetypes
import os
import re
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from . import config

mcp = FastMCP("discord-mcp")

# Discord truncates message content at 2000 chars (non-nitro). Guard before POST.
_MAX_CONTENT = 2000

# Where downloaded image attachments are cached so Claude can Read them. Lives at the repo
# root (server.py -> discord_mcp -> src -> discord-mcp -> embodied-claude == parents[3]),
# next to .discord-gateway-logs. Git-ignored.
ATTACHMENT_DIR = Path(__file__).resolve().parents[3] / ".discord-attachments"


def _is_image_attachment(att: dict) -> bool:
    """True if a Discord attachment looks like an image (by content_type or extension)."""
    ct = (att.get("content_type") or "").lower()
    if ct.startswith("image/"):
        return True
    name = (att.get("filename") or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))


def _safe_attachment_name(message_id: str, att: dict) -> str:
    """Stable, filesystem-safe local filename for a Discord attachment."""
    raw = att.get("filename") or f"{att.get('id', 'att')}.bin"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    return f"{message_id}_{att.get('id', '0')}_{safe}"


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=config.API_BASE,
        headers=config.auth_headers(),
        timeout=config.HTTP_TIMEOUT,
    )


def _error_detail(resp: httpx.Response) -> str:
    """Extract a human-readable error from a non-2xx Discord response."""
    try:
        body = resp.json()
    except Exception:
        return resp.text[:500]
    # Discord error shape: {"message": "...", "code": 50001, "errors": {...}}
    msg = body.get("message", "")
    code = body.get("code", "")
    errors = body.get("errors")
    detail = f"{msg} (code {code})" if code else msg
    if errors:
        detail += f" — {json.dumps(errors, ensure_ascii=False)[:300]}"
    return detail or resp.text[:500]


def _message_link(channel_id: str, message_id: str) -> str:
    guild = config.get_guild_id()
    guild_part = guild if guild else "@me"
    return f"https://discord.com/channels/{guild_part}/{channel_id}/{message_id}"


@mcp.tool()
def send_message(text: str, channel_id: str = "", image_path: str = "") -> str:
    """Send a message to a Discord channel via the bot, optionally with an image.

    Args:
        text: Message text (max 2000 characters). May be empty if image_path is set.
        channel_id: Target channel ID. If empty, uses DISCORD_CHANNEL_ID from env.
        image_path: Optional path to an image file (jpg/png/gif/webp) to attach.

    Returns a success line with a clickable message link, or an "Error: ..." string.
    """
    try:
        cid = config.resolve_channel_id(channel_id)
    except RuntimeError as e:
        return f"Error: {e}"

    if len(text) > _MAX_CONTENT:
        return f"Error: message is {len(text)} chars (Discord max {_MAX_CONTENT}). Shorten it."
    if not text and not image_path:
        return "Error: nothing to send — provide text and/or image_path."

    url = f"/channels/{cid}/messages"

    try:
        with _client() as client:
            if image_path:
                if not os.path.isfile(image_path):
                    return f"Error: image_path does not exist: {image_path}"
                filename = os.path.basename(image_path)
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                # Discord multipart: payload_json carries the body, files[0] the binary.
                payload = {"content": text, "attachments": [{"id": 0, "filename": filename}]}
                with open(image_path, "rb") as fh:
                    files = {"files[0]": (filename, fh, content_type)}
                    data = {"payload_json": json.dumps(payload)}
                    resp = client.post(url, data=data, files=files)
            else:
                resp = client.post(url, json={"content": text})

            if not resp.is_success:
                return f"Error: Discord returned {resp.status_code}: {_error_detail(resp)}"

            msg = resp.json()
            return f"Sent! {_message_link(cid, msg['id'])}"
    except httpx.HTTPError as e:
        return f"Error: HTTP request failed: {e}"


@mcp.tool()
def read_recent(channel_id: str = "", limit: int = 10) -> str:
    """Read recent messages from a Discord channel via the bot.

    Args:
        channel_id: Channel ID to read. If empty, uses DISCORD_CHANNEL_ID from env.
        limit: How many recent messages to fetch (1-100, default 10).

    Returns lines formatted "author: content (timestamp)", oldest first (newest last).
    """
    try:
        cid = config.resolve_channel_id(channel_id)
    except RuntimeError as e:
        return f"Error: {e}"

    limit = max(1, min(int(limit), 100))
    url = f"/channels/{cid}/messages"

    try:
        with _client() as client:
            resp = client.get(url, params={"limit": limit})
            if not resp.is_success:
                return f"Error: Discord returned {resp.status_code}: {_error_detail(resp)}"
            messages = resp.json()
    except httpx.HTTPError as e:
        return f"Error: HTTP request failed: {e}"

    if not messages:
        return "(no messages)"

    # Discord returns newest-first; reverse so output reads top-to-bottom chronologically.
    lines: list[str] = []
    for m in reversed(messages):
        author = m.get("author", {}).get("username", "unknown")
        content = (m.get("content") or "").replace("\n", " ").strip()
        if len(content) > 500:
            content = content[:497] + "..."
        # Always surface attachments/embeds, even when there is also a text body, so an
        # image sent with a caption is never silently dropped. Images get a hint to fetch.
        if m.get("attachments"):
            names = ", ".join(a.get("filename", "?") for a in m["attachments"])
            hint = " — 画像。fetch_recent_images で取得して Read" if any(
                _is_image_attachment(a) for a in m["attachments"]
            ) else ""
            content = f"{content} [添付: {names}{hint}]".strip()
        elif not content:
            content = "[embed]" if m.get("embeds") else "[no text]"
        ts = m.get("timestamp", "")
        lines.append(f"{author}: {content} ({ts})")

    return "\n".join(lines)


@mcp.tool()
def fetch_recent_images(channel_id: str = "", limit: int = 10) -> str:
    """Download image attachments from recent messages so they can be viewed with Read.

    Scans the last `limit` messages for image attachments (png/jpg/gif/webp/bmp), downloads
    each to the local .discord-attachments/ cache, and returns one line per image:
    "saved <abs path> (from <author>, <filename>)". Use the built-in Read tool on a returned
    path to actually SEE the image. Already-cached files are reused (not re-downloaded).

    Args:
        channel_id: Channel to scan. If empty, uses DISCORD_CHANNEL_ID from env.
        limit: How many recent messages to scan (1-100, default 10).

    Returns the saved paths, "(no images in recent messages)", or an "Error: ..." string.
    """
    try:
        cid = config.resolve_channel_id(channel_id)
    except RuntimeError as e:
        return f"Error: {e}"

    limit = max(1, min(int(limit), 100))

    try:
        with _client() as client:
            resp = client.get(f"/channels/{cid}/messages", params={"limit": limit})
            if not resp.is_success:
                return f"Error: Discord returned {resp.status_code}: {_error_detail(resp)}"
            messages = resp.json()
    except httpx.HTTPError as e:
        return f"Error: HTTP request failed: {e}"

    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    # Discord CDN URLs are pre-signed and public — download with a plain client (no bot auth).
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as dl:
            for m in reversed(messages):
                author = m.get("author", {}).get("username", "unknown")
                for att in m.get("attachments", []):
                    if not _is_image_attachment(att):
                        continue
                    url = att.get("url")
                    if not url:
                        continue
                    dest = ATTACHMENT_DIR / _safe_attachment_name(str(m.get("id", "0")), att)
                    if not dest.exists():
                        r = dl.get(url)
                        if not r.is_success:
                            fn = att.get("filename", "?")
                            saved.append(f"(failed {fn}: HTTP {r.status_code})")
                            continue
                        dest.write_bytes(r.content)
                    saved.append(f"saved {dest} (from {author}, {att.get('filename', '?')})")
    except httpx.HTTPError as e:
        return f"Error: download failed: {e}"

    return "\n".join(saved) if saved else "(no images in recent messages)"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
