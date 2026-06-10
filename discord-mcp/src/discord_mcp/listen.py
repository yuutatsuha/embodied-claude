"""discord-voice listen — record one turn from the voice channel and transcribe it (ja).

Stage 2 (first half) of Discord voice: kokone's ear. Joins the voice channel, records
one turn, then transcribes with faster-whisper (CPU, int8 — WSL2 has no CUDA here).
By default it records until the speaker goes quiet (silence-based end-of-turn) instead
of cutting off at a fixed time, so a turn can be as long or short as the speaker needs.
Prints the recognized text. Pairs with voice.py (the mouth) for a manual conversation
turn:

    listen.py  ->  (read transcript)  ->  voice.py reply

Usage:
    uv run python -m discord_mcp.listen <voice_channel_id> [max_seconds]   # stop on silence
    uv run python -m discord_mcp.listen <voice_channel_id> fixed <seconds> # fixed window

Requires discord-ext-voice-recv + PyNaCl + ffmpeg. The bot needs Connect on the channel.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

import discord
from discord.ext import voice_recv

from . import config


def _ensure_opus() -> None:
    """Load libopus explicitly. discord.py doesn't auto-load it on this WSL2 setup, and
    without it received audio can't be decoded — the decoded callback silently gets
    nothing even though raw RTP packets arrive fine."""
    if discord.opus.is_loaded():
        return
    for name in ("libopus.so.0", "/lib/x86_64-linux-gnu/libopus.so.0", "opus"):
        try:
            discord.opus.load_opus(name)
            return
        except Exception:
            continue


def _patch_voice_recv_for_dave() -> None:
    """Teach voice_recv to strip Discord's voice E2EE (DAVE) layer before opus decode.

    discord-ext-voice-recv (0.5.x) is unaware of DAVE. On a DAVE-enabled channel the
    RTP-decrypted payload is still E2EE-wrapped, so opus decode raises 'corrupted stream'
    and the receive loop dies — no audio ever reaches the sink. discord.py ships `davey`
    (it uses it to *encrypt* our outgoing voice); here we use the same per-connection
    dave_session to *decrypt* each sender's frames first. Idempotent monkeypatch.
    """
    from discord.ext.voice_recv import opus as _vr_opus
    from discord.ext.voice_recv.rtp import OPUS_SILENCE

    if getattr(_vr_opus.PacketDecoder, "_dave_patched", False):
        return

    import davey

    _orig_decode = _vr_opus.PacketDecoder._decode_packet

    def _decode_with_dave(self, packet):  # noqa: ANN001, ANN202
        if packet and getattr(packet, "decrypted_data", None):
            vc = self.sink.voice_client
            sess = getattr(getattr(vc, "_connection", None), "dave_session", None)
            if sess is not None and sess.ready:
                uid = vc._get_id_from_ssrc(self.ssrc)
                if uid is not None and not sess.can_passthrough(uid):
                    try:
                        packet.decrypted_data = sess.decrypt(
                            uid, davey.MediaType.audio, packet.decrypted_data
                        )
                    except Exception:
                        # Key not negotiated yet for this sender; substitute a valid opus
                        # silence frame so the receive loop survives instead of feeding
                        # opus a corrupt (or empty) frame, which raises and kills it.
                        packet.decrypted_data = OPUS_SILENCE
        try:
            return _orig_decode(self, packet)
        except Exception:
            # Even a "successfully" decrypted frame can be unparseable by opus right after
            # join, while the DAVE key/epoch is still settling. Swap in a silence frame and
            # retry so one bad packet never kills the whole receive loop.
            if packet:
                packet.decrypted_data = OPUS_SILENCE
                return _orig_decode(self, packet)
            raise

    _vr_opus.PacketDecoder._decode_packet = _decode_with_dave
    _vr_opus.PacketDecoder._dave_patched = True


async def record_once(channel_id: int, seconds: float) -> str | None:
    """Join the voice channel, capture `seconds` of audio to a WAV, then leave."""
    _ensure_opus()
    _patch_voice_recv_for_dave()
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    captured: dict[str, str] = {}

    @client.event
    async def on_ready() -> None:
        vc: voice_recv.VoiceRecvClient | None = None
        try:
            channel = await client.fetch_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                raise RuntimeError(f"channel {channel_id} is not a voice channel")

            vc = await channel.connect(cls=voice_recv.VoiceRecvClient)

            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            vc.listen(voice_recv.WaveSink(tmp.name))
            await asyncio.sleep(seconds)
            vc.stop_listening()
            await asyncio.sleep(0.2)  # flush the last frames into the file
            captured["path"] = tmp.name
        finally:
            if vc is not None:
                await vc.disconnect()
            await client.close()

    await client.start(config.get_token())
    return captured.get("path")


class _SilenceAwareWaveSink(voice_recv.WaveSink):
    """WaveSink that also stamps when audio last arrived, for end-of-turn detection.

    A plain WaveSink (no SilenceGenerator) calls write() only when a real RTP packet is
    received. Discord stops sending packets shortly after a speaker goes quiet, so
    'time since last write' is a usable voice-activity signal. The stamp is a single
    float written from the receive thread and read from the event loop — atomic enough
    under CPython's GIL that no lock is needed.
    """

    def __init__(self, destination: str) -> None:
        super().__init__(destination)
        self.last_write: float | None = None

    def write(self, user, data) -> None:  # noqa: ANN001
        super().write(user, data)
        self.last_write = time.monotonic()


async def record_until_silence(
    channel_id: int,
    *,
    max_seconds: float = 45.0,
    silence_timeout: float = 2.0,
    start_timeout: float = 20.0,
) -> str | None:
    """Join the voice channel and record one turn, stopping when the speaker goes quiet.

    Ends when no audio packet has arrived for `silence_timeout` seconds (end of a turn)
    or after `max_seconds` total, whichever comes first. If nobody speaks within
    `start_timeout`, gives up so the caller can retry instead of hanging.
    """
    _ensure_opus()
    _patch_voice_recv_for_dave()
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    captured: dict[str, str] = {}

    @client.event
    async def on_ready() -> None:
        vc: voice_recv.VoiceRecvClient | None = None
        try:
            channel = await client.fetch_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                raise RuntimeError(f"channel {channel_id} is not a voice channel")

            vc = await channel.connect(cls=voice_recv.VoiceRecvClient)

            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            sink = _SilenceAwareWaveSink(tmp.name)
            vc.listen(sink)

            start = time.monotonic()
            while True:
                await asyncio.sleep(0.2)
                now = time.monotonic()
                if now - start >= max_seconds:
                    break
                if sink.last_write is None:
                    if now - start >= start_timeout:
                        break  # nobody spoke
                    continue
                if now - sink.last_write >= silence_timeout:
                    break  # speaker went quiet -> end of turn

            vc.stop_listening()
            await asyncio.sleep(0.2)  # flush the last frames into the file
            captured["path"] = tmp.name
        finally:
            if vc is not None:
                await vc.disconnect()
            await client.close()

    await client.start(config.get_token())
    return captured.get("path")


def transcribe(wav_path: str) -> str:
    """Transcribe a WAV file to Japanese text with faster-whisper (CPU)."""
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(wav_path, language="ja")
    return "".join(seg.text for seg in segments).strip()


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(
            "usage:\n"
            "  python -m discord_mcp.listen <voice_channel_id> [max_seconds]   "
            "# stop when the speaker goes quiet (default)\n"
            "  python -m discord_mcp.listen <voice_channel_id> fixed <seconds> "
            "# fixed-length window (fallback)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    channel_id = int(args[0])
    if len(args) >= 2 and args[1] == "fixed":
        seconds = float(args[2]) if len(args) > 2 else 6.0
        wav_path = asyncio.run(record_once(channel_id, seconds))
    else:
        max_seconds = float(args[1]) if len(args) > 1 else 45.0
        wav_path = asyncio.run(record_until_silence(channel_id, max_seconds=max_seconds))

    if not wav_path or not Path(wav_path).exists():
        print("[no audio captured]", file=sys.stderr)
        raise SystemExit(1)
    try:
        text = transcribe(wav_path)
    finally:
        Path(wav_path).unlink(missing_ok=True)
    print(text or "[silence]")


if __name__ == "__main__":
    main()
