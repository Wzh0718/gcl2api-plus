import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest


def test_antigravity_profile_matches_official_endpoint_split():
    from src.httpx_client import antigravity_profile_for_url

    assert antigravity_profile_for_url("https://play.googleapis.com/log") == "telemetry"
    assert antigravity_profile_for_url("https://oauth2.googleapis.com/token") == "oauth"
    assert antigravity_profile_for_url("https://www.googleapis.com/oauth2/v2/userinfo") == "cloudcode"
    assert antigravity_profile_for_url(
        "https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent"
    ) == "cloudcode"


def test_native_body_encoding_preserves_json_and_form_contracts():
    from src.agy_native_transport import encode_request_body

    assert encode_request_body(None, {"中文": 1}) == '{"中文":1}'.encode()
    assert encode_request_body({"grant_type": "refresh_token"}, None) == b"grant_type=refresh_token"
    assert encode_request_body(None, None) is None


@pytest.mark.asyncio
async def test_native_transport_failure_does_not_silently_fallback(monkeypatch):
    from src import httpx_client

    class FailingNative:
        request = AsyncMock(side_effect=RuntimeError("helper unavailable"))

    monkeypatch.setattr(httpx_client, "agy_native_transport", FailingNative())
    monkeypatch.setattr(
        httpx_client,
        "get_antigravity_native_transport_enabled_sync",
        lambda: True,
    )
    with pytest.raises(RuntimeError, match="helper unavailable"):
        await httpx_client.post_async(
            "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            json={},
            transport="antigravity",
        )


@pytest.mark.asyncio
async def test_oauth_refresh_uses_native_profile_and_official_default_ua():
    from src.google_oauth_api import refresh_access_token_with_clients

    post_mock = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"access_token": "dummy", "token_type": "Bearer", "expires_in": 3600},
        )
    )
    with patch("src.google_oauth_api.post_async", post_mock):
        token, client_key = await refresh_access_token_with_clients("dummy-refresh")

    assert token["access_token"] == "dummy"
    assert client_key == "antigravity_enterprise"
    call = post_mock.await_args.kwargs
    assert call["transport"] == "antigravity"
    assert "User-Agent" not in call["headers"]
    assert call["data"]["grant_type"] == "refresh_token"


@pytest.mark.asyncio
async def test_oauth_refresh_does_not_rotate_clients_on_error():
    from src.google_oauth_api import TokenError, refresh_access_token_with_clients
    from src.oauth_clients import reset_oauth_clients

    reset_oauth_clients()
    post_mock = AsyncMock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    with patch("src.google_oauth_api.post_async", post_mock):
        with pytest.raises(TokenError):
            await refresh_access_token_with_clients("dummy-refresh")
    assert post_mock.await_count == 1


@pytest.mark.asyncio
async def test_agy_user_info_uses_native_transport():
    from datetime import datetime, timezone
    from src.google_oauth_api import Credentials, get_user_info

    credentials = Credentials(
        access_token="dummy",
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        proxy_url=None,
    )
    get_mock = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"email": "dummy@example.invalid"},
            request=httpx.Request("GET", "https://www.googleapis.com/oauth2/v2/userinfo"),
        )
    )
    with patch("src.google_oauth_api.get_async", get_mock):
        result = await get_user_info(credentials, antigravity=True)

    assert result["email"] == "dummy@example.invalid"
    assert get_mock.await_args.kwargs["transport"] == "antigravity"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upstream_error, expected_reason",
    [("invalid_grant", "invalid_grant"), ("invalid_client", "invalid_client"),
     ("invalid_request", "invalid_request")],
)
async def test_fixed_client_refresh_preserves_sanitized_oauth_diagnosis(
    upstream_error, expected_reason,
):
    from src.google_oauth_api import TokenError, refresh_access_token_with_clients

    response = httpx.Response(400, json={
        "error": upstream_error,
        "error_description": "private-upstream-response",
    })
    post_mock = AsyncMock(return_value=response)
    with patch("src.google_oauth_api.post_async", post_mock):
        with pytest.raises(TokenError) as caught:
            await refresh_access_token_with_clients("dummy-refresh")
    assert caught.value.status_code == 400
    assert caught.value.error_code == expected_reason
    assert "private-upstream-response" not in str(caught.value)
    assert post_mock.await_count == 1


class TestListenerHealth:
    """进程活着 ≠ listener 可用：本次生产事故（11-20 11:30）就是这个形态。"""

    @pytest.mark.asyncio
    async def test_listener_ok_false_when_socket_missing(self, tmp_path, monkeypatch):
        from src.agy_native_transport import AgyNativeTransport

        transport = AgyNativeTransport()
        monkeypatch.setattr(transport, "_socket", lambda: tmp_path / "missing.sock")
        assert await transport._listener_ok() is False

    @pytest.mark.asyncio
    async def test_ensure_ready_rebuilds_when_process_alive_but_listener_dead(self, monkeypatch):
        from src.agy_native_transport import AgyNativeTransport

        transport = AgyNativeTransport()

        class FakeProcess:
            returncode = None

        transport._process = FakeProcess()
        calls = []

        async def fake_listener_ok():
            return False

        async def fake_stop():
            calls.append("stop")

        async def fake_start():
            calls.append("start")

        monkeypatch.setattr(transport, "_listener_ok", fake_listener_ok)
        monkeypatch.setattr(transport, "stop", fake_stop)
        monkeypatch.setattr(transport, "start", fake_start)
        transport._last_probe_at = 0.0
        transport._listener_healthy = True

        await transport.ensure_ready()
        assert calls == ["stop", "start"]

    @pytest.mark.asyncio
    async def test_ensure_ready_skips_probe_when_recently_healthy(self, monkeypatch):
        import asyncio as _asyncio

        from src.agy_native_transport import AgyNativeTransport

        transport = AgyNativeTransport()

        class FakeProcess:
            returncode = None

        transport._process = FakeProcess()
        transport._listener_healthy = True
        transport._last_probe_at = _asyncio.get_running_loop().time()

        async def boom():
            raise AssertionError("不该探活")

        monkeypatch.setattr(transport, "_listener_ok", boom)
        await transport.ensure_ready()   # 5 秒内健康 → 直接返回

    @pytest.mark.asyncio
    async def test_request_retries_once_after_rebuild(self, monkeypatch):
        from src.agy_native_transport import AgyNativeTransport

        transport = AgyNativeTransport()
        attempts = []

        async def fake_ensure(force=False):
            attempts.append(("ensure", force))

        async def fake_once(method, url, **kwargs):
            attempts.append(("once", None))
            if sum(1 for a in attempts if a[0] == "once") == 1:
                raise httpx.ConnectError("[Errno 111] Connection refused")
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(transport, "ensure_ready", fake_ensure)
        monkeypatch.setattr(transport, "_request_once", fake_once)

        response = await transport.request("POST", "https://x.test/v1internal:generateContent")
        assert response.status_code == 200
        assert ("ensure", True) in attempts
        assert sum(1 for a in attempts if a[0] == "once") == 2
