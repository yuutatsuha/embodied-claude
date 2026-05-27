"""discord-voice — join a voice channel, speak one VOICEVOX line, then leave.

Stage 1 of Discord voice presence: kokone enters the shared voice room and talks
(one-way for now — listening/transcription is a later stage). The bot connects, plays
a single synthesized line, waits for playback to finish, then disconnects. Short-lived,
so it fits the same on-demand model as the rest of discord-mcp.

Usage:
    uv run python -m discord_mcp.voice <voice_channel_id> "喋りたいこと"

Requires PyNaCl (installed via the discord.py[voice] extra) and ffmpeg on PATH. The
bot must have Connect + Speak permissions on the target voice channel.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import discord

from . import config

VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://localhost:50021").rstrip("/")
VOICEVOX_SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "3"))


def synth_wav(text: str, speaker: int = VOICEVOX_SPEAKER) -> bytes:
    """Synthesize text to WAV bytes via the local VOICEVOX HTTP API (stdlib only).

    Mirrors tts-mcp's VoicevoxEngine.synthesize: a two-step audio_query -> synthesis
    call. Kept self-contained here to avoid a cross-package import.
    """
    params = urllib.parse.urlencode({"text": text, "speaker": speaker})
    req = urllib.request.Request(f"{VOICEVOX_URL}/audio_query?{params}", method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        query = json.loads(resp.read())

    req = urllib.request.Request(
        f"{VOICEVOX_URL}/synthesis?speaker={speaker}",
        data=json.dumps(query).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


async def say_in_voice(channel_id: int, text: str) -> None:
    """Connect to the voice channel, speak one line, then leave."""
    intents = discord.Intents.default()  # includes voice_states; no privileged intent needed
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        vc: discord.VoiceClient | None = None
        wav_path: str | None = None
        try:
            channel = await client.fetch_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                raise RuntimeError(f"channel {channel_id} is not a voice channel")

            vc = await channel.connect()

            wav = synth_wav(text)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                fh.write(wav)
                wav_path = fh.name

            finished = asyncio.Event()
            vc.play(
                discord.FFmpegPCMAudio(wav_path),
                after=lambda _exc: client.loop.call_soon_threadsafe(finished.set),
            )
            await finished.wait()
            await asyncio.sleep(0.5)  # let the audio tail flush before disconnecting
        finally:
            if vc is not None:
                await vc.disconnect()
            if wav_path:
                Path(wav_path).unlink(missing_ok=True)
            await client.close()

    await client.start(config.get_token())


def main() -> None:
    if len(sys.argv) < 3:
        print('usage: python -m discord_mcp.voice <voice_channel_id> "text"', file=sys.stderr)
        raise SystemExit(2)
    channel_id = int(sys.argv[1])
    text = sys.argv[2]
    asyncio.run(say_in_voice(channel_id, text))


if __name__ == "__main__":
    main()
