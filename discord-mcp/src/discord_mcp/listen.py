"""discord-voice listen — record one turn from the voice channel and transcribe it (ja).

Stage 2 (first half) of Discord voice: kokone's ear. Joins the voice channel, records
for a fixed window, then transcribes with faster-whisper (CPU, int8 — WSL2 has no CUDA
here). Prints the recognized text. Pairs with voice.py (the mouth) for a manual
conversation turn:

    listen.py  ->  (read transcript)  ->  voice.py reply

Usage:
    uv run python -m discord_mcp.listen <voice_channel_id> [seconds]

Requires discord-ext-voice-recv + PyNaCl + ffmpeg. The bot needs Connect on the channel.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
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


def transcribe(wav_path: str) -> str:
    """Transcribe a WAV file to Japanese text with faster-whisper (CPU)."""
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(wav_path, language="ja")
    return "".join(seg.text for seg in segments).strip()


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m discord_mcp.listen <voice_channel_id> [seconds]", file=sys.stderr)
        raise SystemExit(2)
    channel_id = int(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0

    wav_path = asyncio.run(record_once(channel_id, seconds))
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
