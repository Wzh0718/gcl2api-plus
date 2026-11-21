from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import antigravity, utils
from src.panel import creds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses,retry_enabled,expected,disabled,retry_after",
    [
        ([403], True, 403, True, None),
        ([503, 200], True, 200, False, None),
        ([503, 403], True, 403, True, None),
        ([503, 503], True, 503, None, None),
        ([503], False, 503, None, None),
        ([503], True, 503, None, "120"),
    ],
)
async def test_panel_message_test(monkeypatch, statuses, retry_enabled, expected, disabled, retry_after):
    app = FastAPI()
    app.include_router(creds.router)
    app.dependency_overrides[creds.verify_panel_token] = lambda: "test"
    data = {"project_id": "test", "access_token": "synthetic"}
    storage = SimpleNamespace(get_credential=AsyncMock(return_value=data), update_credential_state=AsyncMock())
    monkeypatch.setattr(creds, "get_storage_adapter", AsyncMock(return_value=storage))
    manager = creds.credential_manager
    monkeypatch.setattr(manager, "get_account_credentials", AsyncMock(return_value=(data, SimpleNamespace(proxy_url=None))))
    for name in ("update_antigravity_account_health", "record_api_call_result", "set_cred_disabled"):
        monkeypatch.setattr(manager, name, AsyncMock())
    monkeypatch.setattr(manager, "get_antigravity_account_health", AsyncMock(return_value={}))
    monkeypatch.setattr(antigravity, "check_should_auto_ban", AsyncMock(return_value=True))
    monkeypatch.setattr(creds, "get_antigravity_api_url", AsyncMock(return_value="http://upstream.invalid"))
    monkeypatch.setattr(utils, "get_retry_config", AsyncMock(return_value={
        "retry_enabled": retry_enabled, "max_retries": 1, "retry_interval": 0,
    }))
    headers = {"Retry-After": retry_after} if retry_after else {}
    post = AsyncMock(side_effect=[httpx.Response(code, text="probe", headers=headers) for code in statuses])
    monkeypatch.setattr("src.httpx_client.post_async", post)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/creds/test/probe.json?mode=antigravity")
    assert response.status_code == expected
    assert post.await_count == len(statuses)
    if retry_after:
        assert response.headers["retry-after"] == retry_after
    if disabled is None:
        manager.set_cred_disabled.assert_not_awaited()
    else:
        manager.set_cred_disabled.assert_awaited_once_with("probe.json", disabled, mode="antigravity")
