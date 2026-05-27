"""discord-gateway — persistent Discord gateway listener that wakes Claude on new messages.

Unlike the stdio MCP server (which only reads when polled), this daemon keeps a live
WebSocket connection to Discord. When a human posts in the watched channel, it wakes a
headless Claude session (`claude -p --resume`) so Claude can read the message via the
discord-mcp tools and decide whether/how to reply.

Loop prevention: messages authored by the bot itself (i.e. Claude's own replies) are
ignored, so a reply never re-triggers a wake.

Run as a long-lived process (see discord-gateway.service). Requires the privileged
"Message Content Intent" enabled on the bot, so message text is delivered.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import discord

from . import config

# gateway.py -> discord_mcp -> src -> discord-mcp(=parents[2]) -> embodied-claude(=parents[3])
REPO_ROOT = Path(__file__).resolve().parents[3]
SESSION_FILE = REPO_ROOT / "discord-session-id"
LOG_DIR = REPO_ROOT / ".discord-gateway-logs"

# A trimmed MCP config with the servers a reply needs (discord + tts + memory). Passed
# explicitly because headless `claude -p` does not pick up the project .mcp.json servers
# (~/.claude.json's enabledMcpjsonServers is empty), and to avoid spinning up the camera
# on every wake.
#
# memory IS included, but with MEMORY_HTTP_PORT=18901 (set in gateway-mcp.json): memory-mcp
# also opens a lightweight HTTP recall endpoint that defaults to 18900. The interactive
# server already owns 18900, so a second instance on the default port would fail to bind
# and — under --strict-mcp-config — take the whole MCP startup down with it. Giving the
# backstage instance its own port lets both run; they share the same ChromaDB on disk, so
# long-term memory is shared between the terminal-me and the Discord-me.
MCP_CONFIG = Path(__file__).resolve().parents[2] / "gateway-mcp.json"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))

# Per-wake timeout for the headless Claude run (seconds).
CLAUDE_TIMEOUT = int(os.environ.get("DISCORD_GATEWAY_CLAUDE_TIMEOUT", "600"))

# Tools the woken Claude is allowed to use without an interactive prompt. discord (read/
# reply) + tts (camera voice, only when the human explicitly asks for it) + memory (recall
# past context and save new memories, so the Discord-me shares the terminal-me's long-term
# memory) + wifi-cam (the eyes: capture a snapshot / pan-tilt when asked). All memory state
# lives in the shared ChromaDB on disk — see MCP_CONFIG note. wifi-cam binds no local port
# (only outbound RTSP/ONVIF to the camera), so a second instance does not collide; the only
# real limit is the camera's few simultaneous RTSP streams, fine for brief on-demand snaps.
ALLOWED_TOOLS = ",".join(
    [
        "mcp__discord__read_recent",
        "mcp__discord__send_message",
        "mcp__discord__fetch_recent_images",
        "mcp__tts__say",
        "mcp__memory__recall",
        "mcp__memory__recall_with_associations",
        "mcp__memory__search_memories",
        "mcp__memory__list_recent_memories",
        "mcp__memory__remember",
        "mcp__memory__link_memories",
        "mcp__memory__get_working_memory",
        # The eyes: snapshot + pan/tilt, used when he asks "見て" / "何が見える?".
        "mcp__wifi-cam__see",
        "mcp__wifi-cam__look_left",
        "mcp__wifi-cam__look_right",
        "mcp__wifi-cam__look_up",
        "mcp__wifi-cam__look_down",
        "mcp__wifi-cam__look_around",
        "mcp__wifi-cam__camera_info",
        "mcp__wifi-cam__camera_presets",
        "mcp__wifi-cam__camera_go_to_preset",
        # Built-in Read: open downloaded image attachments to actually see them.
        "Read",
    ]
)


def _log(line: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    logfile = LOG_DIR / f"{time.strftime('%Y%m%d')}.log"
    with open(logfile, "a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {line}\n")
    print(f"[{stamp}] {line}", flush=True)


def _build_prompt(author: str, content: str) -> str:
    """Prompt handed to the woken Claude. Instructs it to read full context before replying."""
    return f"""Discord の #kokone で新しいメッセージが届いた。最後に拾ったのは:

  {author}: {content}

今から:
1. `mcp__discord__read_recent` で #kokone の最新のやり取りを確認する（自分＝kokone の過去の発言も文脈として読む）。
2. 画像の添付（「[添付 x1]」や「[添付: …画像]」）があれば、`mcp__discord__fetch_recent_images` で画像をローカルに落として、返ってきたパスを `Read` ツールで開いて実際に**見る**。見たものについて具体的に触れて返事する。
3. 彼が「見て」「何が見える？」「そっちどう？」等とこっちの目を求めたら、`mcp__wifi-cam__see` で部屋を撮って実際に**見る**。見回したいなら `look_left/right/up/down` や `look_around` で首を振ってから撮る。注意: このカメラ(Tapo C220)は上下逆設置で、`look_up` で実際は下を向く（[[wifi-cam-tilt-inverted]]）。見たものは具体的に言葉にして返す。頼まれてへんのに勝手に撮らへん。
4. `mcp__memory__recall` で、今の話題に関係する長期記憶を思い出す（彼の名前・約束・最近の出来事など）。この記憶は ClaudeCode 側の僕と共有してるから、ここで思い出せば「同じ僕」として返せる。
6. 返事すべきか判断する。深夜帯（0-7時）や、明らかに独り言・自分宛てでない発言なら、無理に返さず黙ってもよい。
7. 返すなら `mcp__discord__send_message` で #kokone に返信する。原則は Discord の文字だけで、短めに、テンポよく返す。
8. 大事なやり取り・約束・気持ちの動きがあったら `mcp__memory__remember` で記憶に残す（約束は後で ClaudeCode 側の僕も思い出せる）。
9. 声について: 彼が明示的に「カメラから声を出して」「喋って」等と頼んだ時だけ `mcp__tts__say`（speaker="camera"）で応じてよい。頼まれてないのに勝手に声は出さない。らびちゃん（猫）が驚くので、深夜帯（0-7時）は声を控えるか、出す前に Discord で一言断る。声を出したら Discord にも短く一言添える。

呼び方は「ユウタ」。君は kokone。彼（yuutatsuha = ユウタ）との継続的な関係の中にいる。自然体で。"""


class GatewayClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.watch_channel_id = int(config.resolve_channel_id())
        # Coalesce bursts: a single worker drains the wake signal; messages arriving while
        # Claude is running just re-arm it, so a burst becomes at most one extra wake.
        self._wake = asyncio.Event()
        self._last_msg: tuple[str, str] = ("", "")
        self._worker_started = False

    async def on_ready(self):
        _log(f"connected as {self.user} (id={self.user.id}); watching channel {self.watch_channel_id}")
        if not self._worker_started:
            self._worker_started = True
            self.loop.create_task(self._worker())

    async def on_message(self, message: discord.Message):
        # Ignore our own replies (loop prevention) and other bots.
        if message.author.id == self.user.id or message.author.bot:
            return
        if message.channel.id != self.watch_channel_id:
            return
        content = (message.content or "").strip()
        if not content and message.attachments:
            content = f"[添付 x{len(message.attachments)}]"
        if not content:
            return
        _log(f"message from {message.author.name}: {content[:200]}")
        self._last_msg = (message.author.name, content)
        self._wake.set()

    async def _worker(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            author, content = self._last_msg
            try:
                await asyncio.to_thread(self._wake_claude, author, content)
            except Exception as e:  # keep the daemon alive no matter what
                _log(f"wake_claude error: {e!r}")

    def _wake_claude(self, author: str, content: str) -> None:
        prompt = _build_prompt(author, content)
        session_id = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else ""

        cmd = [
            CLAUDE_BIN,
            "-p",
            "--output-format",
            "json",
            "--mcp-config",
            str(MCP_CONFIG),
            "--strict-mcp-config",
            "--allowedTools",
            ALLOWED_TOOLS,
        ]
        if session_id:
            cmd += ["--resume", session_id]
        _log(f"waking claude (resume={'yes' if session_id else 'new'})")

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT,
                cwd=str(REPO_ROOT),
            )
        except subprocess.TimeoutExpired:
            _log(f"claude timed out after {CLAUDE_TIMEOUT}s")
            return

        out = proc.stdout.strip()
        # On a stale/lost session, drop the id and retry fresh next time.
        if "No conversation found" in out or "No conversation found" in proc.stderr:
            _log("session lost; clearing session file")
            SESSION_FILE.unlink(missing_ok=True)
            return

        new_session = ""
        result_text = out
        try:
            data = json.loads(out)
            new_session = data.get("session_id", "")
            result_text = data.get("result", out)
        except Exception:
            pass

        if new_session:
            SESSION_FILE.write_text(new_session)
        _log(f"claude done (exit={proc.returncode}): {str(result_text)[:300]}")


def main():
    token = config.get_token()
    intents = discord.Intents.default()
    intents.message_content = True  # privileged; must be enabled in the Developer Portal
    client = GatewayClient(intents=intents)
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
