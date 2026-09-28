import asyncio
from contextlib import asynccontextmanager
import io
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet

from app import integrations as integration
from app import net
from app.db import db
from app.security import encrypt


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("MASTER_KEY", Fernet.generate_key().decode())
    db.init(str(tmp_path / "test.sqlite3"))


def connector(kind="telegram", config=None):
    return {"id": "connector-id", "kind": kind, "enabled": True,
            "config": config or {"allowed_user_ids": [42], "chat_id": "42"},
            "secret_encrypted": encrypt(json.dumps({"bot_token": "123456:abcdefghijklmnopqrstuvwxy123456789", "access_token": "PRIVATE_ACCESS_TOKEN",
                "client_secret": "PRIVATE_CLIENT_SECRET", "refresh_token": "PRIVATE_REFRESH", "bearer_token": "PRIVATE_BEARER"}))}


def publication(**kwargs):
    return {"id": "ab" * 16, "connector_id": "connector-id", "status": "approved",
            "approved_at": "2026-09-16T00:00:00Z", "text": "Approved text", **kwargs}


def update(**changes):
    message = {"chat": {"type": "private", "id": 42}, "from": {"id": 42, "is_bot": False},
               "text": "/task Prepare content", "date": int(time.time())}
    message.update(changes)
    return {"update_id": 3, "message": message}


class FakeEngine:
    def __init__(self):
        self.created = []
        self.actions = []

    def create_task(self, data):
        self.created.append(data)
        return db.put("task", {"id": "ab" * 16, "status": "backlog", **data})

    async def action(self, task_id, action):
        self.actions.append((task_id, action))
        return {"id": task_id, "status": "planning"}


@pytest.mark.parametrize("url", ["http://example.com", "https://localhost", "https://127.0.0.1", "https://169.254.169.254/latest/meta-data",
    "https://[::1]", "https://[::ffff:127.0.0.1]", "https://10.0.0.1", "https://100.64.0.1", "https://example.com:8443", "https://user:secret@example.com", "https://example.com/#secret"])
def test_ssrf_invalid_targets_blocked(url):
    with pytest.raises(net.NetworkError):
        asyncio.run(net.validate_public_url(url))


def test_ssrf_mixed_public_private_dns_is_rejected(monkeypatch):
    async def test():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(return_value=[
            (2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.2", 443))]))
        with pytest.raises(net.NetworkError):
            await net.validate_public_url("https://media.example.com/video.mp4")
    asyncio.run(test())


def test_public_transport_pins_address_and_keeps_tls_hostname(monkeypatch):
    seen = []
    monkeypatch.setattr(net, "_resolve_public", AsyncMock(return_value=("media.example.com", "93.184.216.34")))
    async def send(self, request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    async def test():
        async with net.PublicHTTPTransport() as transport:
            await transport.handle_async_request(httpx.Request("GET", "https://media.example.com/video.mp4"))
    asyncio.run(test())
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].extensions["sni_hostname"] == "media.example.com"
    assert seen[0].headers["host"] == "media.example.com"


def test_upstream_errors_never_echo_credentials(monkeypatch):
    monkeypatch.setattr(net, "_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
        httpx.Response(403, json={"error": "PRIVATE_ACCESS_TOKEN", "request": str(req.url)}))))
    with pytest.raises(net.NetworkError) as error:
        asyncio.run(net.request_json("GET", "https://example.com?access_token=PRIVATE_ACCESS_TOKEN"))
    assert "PRIVATE" not in str(error.value)
    assert error.value.status_code == 403


def test_redirects_not_followed(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})
    monkeypatch.setattr(net, "_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False))
    with pytest.raises(net.NetworkError):
        asyncio.run(net.request("GET", "https://example.com"))
    assert len(calls) == 1


def test_response_cap_and_download_cap(monkeypatch):
    monkeypatch.setattr(net, "_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
        httpx.Response(200, content=b"x" * 200, headers={"content-type": "video/mp4"}))))
    with pytest.raises(net.NetworkError):
        asyncio.run(net.request("GET", "https://example.com", max_bytes=50))
    async def test():
        async with net.download_media("https://example.com/video.mp4", max_bytes=50):
            pytest.fail("oversized video accepted")
    with pytest.raises(net.NetworkError):
        asyncio.run(test())


def test_unsolicited_compression_rejected_before_reading(monkeypatch):
    class Bomb(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("Compressed body must not be decompressed/read")
            yield b""
    monkeypatch.setattr(net, "_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
        httpx.Response(200, stream=Bomb(), headers={"content-encoding": "gzip"}))))
    with pytest.raises(net.NetworkError, match="сжатый"):
        asyncio.run(net.request_json("GET", "https://example.com"))


@pytest.mark.parametrize("changes", [{"status": "draft"}, {"approved_at": None}, {"connector_id": "other"}])
def test_no_external_calls_without_approval(monkeypatch, changes):
    mock = AsyncMock()
    monkeypatch.setattr(integration, "request_json", mock)
    with pytest.raises(integration.IntegrationError):
        asyncio.run(integration.publish(publication(**changes), connector()))
    mock.assert_not_called()


def test_webhook_check_never_posts(monkeypatch):
    check = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(integration, "validate_public_url", check)
    monkeypatch.setattr(integration, "request_json", send)
    result = asyncio.run(integration.test_connector(connector("webhook", {"url": "https://example.com/receive"})))
    assert result["ok"] is True
    check.assert_awaited_once()
    send.assert_not_called()


def test_telegram_test_only_getme(monkeypatch):
    mock = AsyncMock(return_value={"ok": True, "result": {"id": 123456, "username": "owner_bot"}})
    monkeypatch.setattr(integration, "request_json", mock)
    result = asyncio.run(integration.test_connector(connector()))
    assert result["bot_id"] == 123456
    assert mock.call_args.args[1].endswith("/getMe")


def test_vk_text_publishing_request_and_confirmed_id(monkeypatch):
    mock = AsyncMock(return_value={"response": {"post_id": 54}})
    monkeypatch.setattr(integration, "request_json", mock)
    result = asyncio.run(integration.publish(publication(), connector("vk", {"owner_id": -987, "api_version": "5.199"})))
    assert result["external_id"] == "-987_54"
    assert mock.call_args.kwargs["data"]["guid"] == publication()["id"]
    assert mock.call_args.kwargs["data"]["from_group"] == 1


def test_vk_media_is_explicitly_unsupported(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(integration, "request_json", mock)
    with pytest.raises(integration.IntegrationError, match="не поддерживается"):
        asyncio.run(integration.publish(publication(media_url="https://example.com/photo.jpg"), connector("vk", {"owner_id": -987})))
    mock.assert_not_called()


def test_mutation_timeout_is_uncertain(monkeypatch):
    monkeypatch.setattr(integration, "request_json", AsyncMock(side_effect=net.NetworkError("Timeout")))
    with pytest.raises(integration.IntegrationError) as error:
        asyncio.run(integration.publish(publication(), connector()))
    assert error.value.uncertain


def test_vk_unconfirmed_write_is_uncertain(monkeypatch):
    monkeypatch.setattr(integration, "request_json", AsyncMock(return_value={"unexpected": "response"}))
    with pytest.raises(integration.IntegrationError) as error:
        asyncio.run(integration.publish(publication(), connector("vk", {"owner_id": -987})))
    assert error.value.uncertain


def test_instagram_container_ready_before_publish(monkeypatch):
    calls = []
    async def send(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/media"):
            return {"id": "111"}
        if method == "GET":
            return {"status_code": "FINISHED"}
        return {"id": "222"}
    monkeypatch.setattr(integration, "request_json", send)
    monkeypatch.setattr(integration, "validate_public_url", AsyncMock())
    p = publication(media_url="https://cdn.example.com/photo.jpg")
    db.put("publication", p)
    result = asyncio.run(integration.publish(p, connector("instagram", {"account_id": "987", "api_version": "v23.0", "media_type": "IMAGE"})))
    assert result["external_id"] == "222"
    assert [call[0] for call in calls] == ["POST", "GET", "POST"]
    assert calls[-1][2]["data"] == {"creation_id": "111"}
    assert db.get("publication", p["id"])["external_container_id"] == "111"


def test_youtube_upload_is_private_by_default_and_streamed(monkeypatch):
    calls = []
    async def send(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/token"):
            return {"access_token": "PRIVATE_OAUTH"}
        if method == "PUT":
            assert b"".join([chunk async for chunk in kwargs["content"]]) == b"VIDEO"
            return {"id": "abcdef12345"}
        raise AssertionError(url)
    async def raw(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, headers={"location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=one"})
    @asynccontextmanager
    async def download(url):
        yield io.BytesIO(b"VIDEO"), 5, "video/mp4"
    monkeypatch.setattr(integration, "request_json", send)
    monkeypatch.setattr(integration, "request", raw)
    monkeypatch.setattr(integration, "download_media", download)
    monkeypatch.setattr(integration, "validate_public_url", AsyncMock())
    result = asyncio.run(integration.publish(publication(title="My video", media_url="https://cdn.example.com/video.mp4"), connector("youtube", {"client_id": "client"})))
    assert result["external_id"] == "abcdef12345"
    assert calls[1][2]["json"]["status"] == {"privacyStatus": "private"}
    assert calls[2][2]["headers"]["Content-Length"] == "5"


@pytest.mark.parametrize("changes", [{"chat": {"type": "group", "id": 42}}, {"from": {"id": 99, "is_bot": False}},
    {"from": {"id": "42", "is_bot": False}}, {"from": {"id": 42, "is_bot": True}}, {"chat": {"type": "private", "id": 99}},
    {"date": int(time.time()) - 301}, {"forward_origin": {"type": "user"}}])
def test_unauthorized_telegram_is_silent_and_creates_nothing(changes):
    engine = FakeEngine()
    result = asyncio.run(integration.handle_telegram_update(update(**changes), connector(), engine))
    assert result is None
    assert not engine.created
    assert not engine.actions


def test_telegram_task_uses_real_provider_and_does_not_auto_run():
    db.put("provider", {"id": "real", "kind": "openai", "enabled": True})
    db.put("provider", {"id": "mock", "kind": "mock", "enabled": True})
    engine = FakeEngine()
    result = asyncio.run(integration.handle_telegram_update(update(), connector(), engine))
    assert "Создана" in result
    assert engine.created[0]["planner_provider_id"] == "real"
    assert engine.created[0]["auto_run"] is False
    assert engine.actions == []


def test_telegram_first_connection_discards_existing_commands(monkeypatch):
    stop = asyncio.Event()
    calls = []
    async def telegram(token, method, payload=None, **kwargs):
        calls.append((method, payload))
        if payload["offset"] == -1:
            return [{**update(), "update_id": 500}]
        stop.set()
        return []
    monkeypatch.setattr(integration, "_telegram", telegram)
    engine = FakeEngine()
    asyncio.run(integration._telegram_worker(connector(), stop, engine))
    assert calls[0][1]["offset"] == -1
    assert calls[1][1]["offset"] == 501
    assert not engine.created


def test_telegram_offset_saved_before_action_and_duplicate_ignored(monkeypatch):
    import hashlib
    connection = connector()
    token = integration._telegram_token(integration._secrets(connection))
    offset_key = "telegram_offset_" + connection["id"] + "_" + hashlib.sha256(token.encode()).hexdigest()[:12]
    db.set_setting(offset_key, 3)
    db.put("provider", {"id": "real", "kind": "openai", "enabled": True})
    stop = asyncio.Event()
    polls = 0
    async def telegram(token, method, payload=None, **kwargs):
        nonlocal polls
        if method == "sendMessage":
            return {"message_id": 1}
        polls += 1
        if polls == 1:
            return [update(), update()]
        stop.set()
        return []
    class Engine(FakeEngine):
        def create_task(self, data):
            assert db.setting(offset_key) == 4
            return super().create_task(data)
    monkeypatch.setattr(integration, "_telegram", telegram)
    engine = Engine()
    asyncio.run(integration._telegram_worker(connection, stop, engine))
    assert len(engine.created) == 1
