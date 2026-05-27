"""Tests for discord-mcp tools. All HTTP is mocked; no real network calls."""

from __future__ import annotations

import json

import pytest

from discord_mcp import config, server


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._json


class FakeClient:
    """Stand-in for httpx.Client; records the last call and returns a queued response."""

    last_call: dict = {}

    def __init__(self, response: FakeResponse):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, data=None, files=None):
        FakeClient.last_call = {
            "method": "POST",
            "url": url,
            "json": json,
            "data": data,
            "files": list(files.keys()) if files else None,
        }
        return self._response

    def get(self, url, params=None):
        FakeClient.last_call = {"method": "GET", "url": url, "params": params}
        return self._response


def _patch_client(monkeypatch, response: FakeResponse):
    monkeypatch.setattr(server, "_client", lambda: FakeClient(response))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "111")
    monkeypatch.setenv("DISCORD_GUILD_ID", "999")


def test_send_text_success(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(200, {"id": "555"}))
    out = server.send_message("hello")
    assert "Sent!" in out
    assert "https://discord.com/channels/999/111/555" in out
    assert FakeClient.last_call["json"] == {"content": "hello"}
    assert FakeClient.last_call["url"] == "/channels/111/messages"


def test_send_uses_explicit_channel(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(200, {"id": "1"}))
    server.send_message("hi", channel_id="222")
    assert FakeClient.last_call["url"] == "/channels/222/messages"


def test_send_image_multipart(monkeypatch, tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n")
    _patch_client(monkeypatch, FakeResponse(200, {"id": "777"}))
    out = server.send_message("with pic", image_path=str(img))
    assert "Sent!" in out
    call = FakeClient.last_call
    assert call["files"] == ["files[0]"]
    payload = json.loads(call["data"]["payload_json"])
    assert payload["content"] == "with pic"
    assert payload["attachments"][0]["filename"] == "pic.png"


def test_send_missing_image(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(200, {"id": "1"}))
    out = server.send_message("x", image_path="/nope/none.png")
    assert out.startswith("Error:")
    assert "does not exist" in out


def test_send_non_2xx(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(403, {"message": "Missing Access", "code": 50001}))
    out = server.send_message("hi")
    assert out.startswith("Error: Discord returned 403")
    assert "Missing Access" in out
    assert "50001" in out


def test_send_too_long(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(200, {"id": "1"}))
    out = server.send_message("a" * 2001)
    assert out.startswith("Error:") and "max 2000" in out


def test_send_nothing(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(200, {"id": "1"}))
    out = server.send_message("")
    assert out.startswith("Error:") and "nothing to send" in out


def test_get_token_missing(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        config.get_token()


def test_send_missing_channel(monkeypatch):
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    out = server.send_message("hi")
    assert out.startswith("Error:") and "channel" in out.lower()


def test_read_recent_orders_oldest_first(monkeypatch):
    # Discord returns newest-first; tool reverses to oldest-first.
    data = [
        {"author": {"username": "bob"}, "content": "second", "timestamp": "2026-05-24T10:01:00Z"},
        {"author": {"username": "alice"}, "content": "first", "timestamp": "2026-05-24T10:00:00Z"},
    ]
    _patch_client(monkeypatch, FakeResponse(200, data))
    out = server.read_recent(limit=2)
    lines = out.splitlines()
    assert lines[0].startswith("alice: first")
    assert lines[1].startswith("bob: second")
    assert FakeClient.last_call["params"] == {"limit": 2}


def test_read_recent_empty(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(200, []))
    assert server.read_recent() == "(no messages)"


def test_read_recent_attachment_only(monkeypatch):
    data = [
        {
            "author": {"username": "a"},
            "content": "",
            "attachments": [{"id": "1", "filename": "doc.pdf"}],
            "timestamp": "t",
        }
    ]
    _patch_client(monkeypatch, FakeResponse(200, data))
    out = server.read_recent()
    assert "[添付: doc.pdf]" in out


def test_read_recent_image_attachment_with_caption(monkeypatch):
    # An image sent with a text caption must surface BOTH the text and a fetch hint.
    data = [
        {
            "author": {"username": "a"},
            "content": "見て！",
            "attachments": [{"id": "1", "filename": "cat.png", "content_type": "image/png"}],
            "timestamp": "t",
        }
    ]
    _patch_client(monkeypatch, FakeResponse(200, data))
    out = server.read_recent()
    assert "見て！" in out
    assert "cat.png" in out
    assert "fetch_recent_images" in out


def test_read_recent_clamps_limit(monkeypatch):
    _patch_client(monkeypatch, FakeResponse(200, []))
    server.read_recent(limit=500)
    assert FakeClient.last_call["params"]["limit"] == 100


def test_is_image_attachment():
    assert server._is_image_attachment({"content_type": "image/png"})
    assert server._is_image_attachment({"filename": "photo.JPG"})
    assert not server._is_image_attachment({"filename": "notes.txt"})
    assert not server._is_image_attachment({"id": "1"})


def test_safe_attachment_name_sanitizes():
    # The safety property is that the result is a single path component: no separators,
    # so it can never escape ATTACHMENT_DIR.
    name = server._safe_attachment_name("123", {"id": "9", "filename": "my pic/../x.png"})
    assert name.startswith("123_9_")
    assert "/" not in name and "\\" not in name


class _FakeDownload:
    """Stand-in for httpx.Client used only for CDN attachment downloads."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        resp = FakeResponse(200)
        resp.content = b"\x89PNG\r\n"
        return resp


def test_fetch_recent_images_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ATTACHMENT_DIR", tmp_path)
    img = {"id": "9", "filename": "cat.png", "content_type": "image/png", "url": "https://cdn/x"}
    txt = {"id": "10", "filename": "notes.txt", "content_type": "text/plain", "url": "https://cdn/y"}
    messages = [{"author": {"username": "yuutatsuha"}, "attachments": [img, txt]}]
    _patch_client(monkeypatch, FakeResponse(200, messages))
    monkeypatch.setattr(server.httpx, "Client", _FakeDownload)

    out = server.fetch_recent_images()
    # Only the image is saved, not the .txt
    assert "cat.png" in out
    assert "notes.txt" not in out
    saved = list(tmp_path.iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"\x89PNG\r\n"


def test_fetch_recent_images_none(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ATTACHMENT_DIR", tmp_path)
    _patch_client(monkeypatch, FakeResponse(200, [{"author": {"username": "a"}, "content": "hi"}]))
    assert server.fetch_recent_images() == "(no images in recent messages)"
