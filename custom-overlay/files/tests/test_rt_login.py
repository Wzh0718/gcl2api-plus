"""RT 登录（refresh_token 直接导入 Antigravity 账号）的测试"""

from unittest.mock import AsyncMock, patch

import pytest

from src import oauth_clients
from src.auth import add_antigravity_account_by_refresh_token
from src.google_oauth_api import (
    TokenError,
    refresh_access_token_with_clients,
)
from src.oauth_clients import reset_oauth_clients


class FakeResponse:
    """模拟 httpx Response 的最小实现"""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def reset_registry(monkeypatch):
    """每个测试前清空相关环境变量并重置注册表"""
    monkeypatch.delenv("ANTIGRAVITY_OAUTH_CLIENTS", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_OAUTH_CLIENT_KEY", raising=False)
    reset_oauth_clients()
    yield
    reset_oauth_clients()


@pytest.fixture
def proxy_urls(monkeypatch):
    """避免测试触碰真实配置/数据库"""
    monkeypatch.setattr(
        "src.google_oauth_api.get_oauth_proxy_url",
        AsyncMock(return_value="https://oauth2.googleapis.com"),
    )
    monkeypatch.setattr(
        "src.google_oauth_api.get_googleapis_proxy_url",
        AsyncMock(return_value="https://www.googleapis.com"),
    )
    monkeypatch.setattr(
        "src.auth.get_antigravity_api_url",
        AsyncMock(return_value="https://daily-cloudcode-pa.googleapis.com"),
    )


async def test_refresh_single_client_success(proxy_urls, monkeypatch):
    post_mock = AsyncMock(
        return_value=FakeResponse(200, {"access_token": "at-1", "expires_in": 3600})
    )
    monkeypatch.setattr("src.google_oauth_api.post_async", post_mock)

    token_data, client_key = await refresh_access_token_with_clients("rt-1")

    assert token_data["access_token"] == "at-1"
    assert client_key == oauth_clients.BUILTIN_CLIENT_KEY
    assert post_mock.await_count == 1
    # token 请求带 Antigravity UA header
    headers = post_mock.await_args.kwargs["headers"]
    assert "Antigravity/" in headers["User-Agent"]


async def test_refresh_fallback_on_invalid_client(proxy_urls, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_CLIENTS", "secondary|sid|ssec")
    reset_oauth_clients()

    post_mock = AsyncMock(
        side_effect=[
            FakeResponse(401, text='{"error": "invalid_client"}'),
            FakeResponse(200, {"access_token": "at-2", "expires_in": 3600}),
        ]
    )
    monkeypatch.setattr("src.google_oauth_api.post_async", post_mock)

    token_data, client_key = await refresh_access_token_with_clients(
        "rt-1", preferred_client_key="secondary"
    )

    assert token_data["access_token"] == "at-2"
    assert client_key == oauth_clients.BUILTIN_CLIENT_KEY
    assert post_mock.await_count == 2
    # 第一次用 preferred client，降级后用 active（内置）client
    first_call_data = post_mock.await_args_list[0].kwargs["data"]
    second_call_data = post_mock.await_args_list[1].kwargs["data"]
    assert first_call_data["client_id"] == "sid"
    assert second_call_data["client_id"] != "sid"


async def test_refresh_network_error_no_fallback(proxy_urls, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_CLIENTS", "secondary|sid|ssec")
    reset_oauth_clients()

    post_mock = AsyncMock(side_effect=Exception("connect timeout"))
    monkeypatch.setattr("src.google_oauth_api.post_async", post_mock)

    with pytest.raises(TokenError):
        await refresh_access_token_with_clients(
            "rt-1", preferred_client_key="secondary"
        )
    # 网络错误不降级，直接失败
    assert post_mock.await_count == 1


async def test_rt_login_full_flow(proxy_urls, monkeypatch):
    """全流程：保存的凭证含 oauth_client_key、email 正确、缺新 RT 时保留原 RT"""
    post_mock = AsyncMock(
        return_value=FakeResponse(200, {"access_token": "at-1", "expires_in": 3599})
    )
    get_mock = AsyncMock(
        return_value=FakeResponse(200, {"email": "user@example.com"})
    )
    monkeypatch.setattr("src.google_oauth_api.post_async", post_mock)
    monkeypatch.setattr("src.google_oauth_api.get_async", get_mock)
    monkeypatch.setattr(
        "src.auth.fetch_project_id_and_tier",
        AsyncMock(return_value=("proj-1", "pro")),
    )
    save_mock = AsyncMock(return_value="ag_proj-1-1.json")
    monkeypatch.setattr("src.auth.save_credentials", save_mock)

    result = await add_antigravity_account_by_refresh_token("rt-original")

    assert result["success"] is True
    assert result["email"] == "user@example.com"
    assert result["oauth_client_key"] == oauth_clients.BUILTIN_CLIENT_KEY
    assert result["file_path"] == "ag_proj-1-1.json"

    creds_data = result["credentials"]
    assert creds_data["oauth_client_key"] == oauth_clients.BUILTIN_CLIENT_KEY
    assert creds_data["project_id"] == "proj-1"
    # Google 未返回新 RT，保留原 RT
    assert creds_data["refresh_token"] == "rt-original"

    # save_credentials 收到的凭证对象同样带 oauth_client_key 与原 RT
    saved_creds = save_mock.await_args.args[0]
    assert saved_creds.oauth_client_key == oauth_clients.BUILTIN_CLIENT_KEY
    assert saved_creds.refresh_token == "rt-original"
    assert save_mock.await_args.kwargs["mode"] == "antigravity"
    assert save_mock.await_args.kwargs["subscription_tier"] == "pro"


async def test_rt_login_uses_new_refresh_token_when_returned(proxy_urls, monkeypatch):
    post_mock = AsyncMock(
        return_value=FakeResponse(
            200,
            {"access_token": "at-1", "expires_in": 3599, "refresh_token": "rt-new"},
        )
    )
    get_mock = AsyncMock(
        return_value=FakeResponse(200, {"email": "user@example.com"})
    )
    monkeypatch.setattr("src.google_oauth_api.post_async", post_mock)
    monkeypatch.setattr("src.google_oauth_api.get_async", get_mock)
    monkeypatch.setattr(
        "src.auth.fetch_project_id_and_tier",
        AsyncMock(return_value=("proj-1", "pro")),
    )
    monkeypatch.setattr(
        "src.auth.save_credentials", AsyncMock(return_value="ag_proj-1-1.json")
    )

    result = await add_antigravity_account_by_refresh_token("rt-original")

    assert result["success"] is True
    assert result["credentials"]["refresh_token"] == "rt-new"


async def test_rt_login_fails_without_email(proxy_urls, monkeypatch):
    post_mock = AsyncMock(
        return_value=FakeResponse(200, {"access_token": "at-1", "expires_in": 3599})
    )
    get_mock = AsyncMock(return_value=FakeResponse(200, {}))
    monkeypatch.setattr("src.google_oauth_api.post_async", post_mock)
    monkeypatch.setattr("src.google_oauth_api.get_async", get_mock)

    result = await add_antigravity_account_by_refresh_token("rt-1")

    assert result["success"] is False
    assert "error" in result
