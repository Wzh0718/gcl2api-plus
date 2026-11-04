import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest


def test_antigravity_profile_matches_official_endpoint_split():
    from src.httpx_client import antigravity_profile_for_url

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
async def test_oauth_refresh_selects_antigravity_transport():
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
    assert call["data"]["grant_type"] == "refresh_token"


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
