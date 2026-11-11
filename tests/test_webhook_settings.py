import os
import asyncio
from unittest.mock import AsyncMock

import config
import httpx
import pytest

from src import dingtalk_alert as alerts


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {})
    for key in list(os.environ):
        if key.startswith("DINGTALK_"):
            monkeypatch.delenv(key)
    alerts._reset_alert_throttle_for_tests()


async def test_webhook_sqlite_overrides_env_including_empty(monkeypatch):
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=env")
    assert "env" in await config.get_dingtalk_webhook_url()
    config._config_cache["dingtalk_webhook_url"] = "https://oapi.dingtalk.com/robot/send?access_token=sqlite"
    assert "sqlite" in await config.get_dingtalk_webhook_url()
    config._config_cache["dingtalk_webhook_url"] = ""
    assert await config.get_dingtalk_webhook_url() == ""


async def test_missing_webhook_and_disabled_account_alert_never_post(monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(alerts, "post_async", post)
    config._config_cache.update(
        dingtalk_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
        dingtalk_all_accounts_alert_enabled=False,
    )
    assert not await alerts.send_all_accounts_unavailable_alert("test", {"a": {"disabled": True}})
    post.assert_not_awaited()


async def test_frequency_combines_configured_codes_and_cools_down(monkeypatch):
    config._config_cache.update(
        dingtalk_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
        dingtalk_alert_enabled=True, dingtalk_alert_status_codes=[429, 503],
        dingtalk_alert_window_minutes=1, dingtalk_alert_threshold=2,
        dingtalk_alert_cooldown_minutes=5,
        dingtalk_alert_template="{status_codes}|{status_code}|{count}|{window_minutes}|{model}|{channel}",
    )
    post = AsyncMock(return_value=httpx.Response(200, json={"errcode": 0}))
    monkeypatch.setattr(alerts, "post_async", post)
    assert not await alerts.send_status_frequency_alert(status_code=429, model="test", now=100)
    assert await alerts.send_status_frequency_alert(status_code=503, model="test", now=110)
    await alerts.flush_status_alerts()
    assert post.await_count == 1
    assert "429,503|503|2|1|test" in post.await_args.kwargs["json"]["text"]["content"]
    assert not await alerts.send_status_frequency_alert(status_code=429, now=111)
    assert not await alerts.send_status_frequency_alert(status_code=503, now=400)


async def test_account_custom_template_and_cooldown(monkeypatch):
    config._config_cache.update(
        dingtalk_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
        dingtalk_all_accounts_alert_template="{keyword} {model} {account_count} {recovery_time}",
        dingtalk_all_accounts_alert_cooldown_minutes=1,
    )
    post = AsyncMock(return_value=httpx.Response(200, json={"errcode": 0}))
    monkeypatch.setattr(alerts, "post_async", post)
    states = {"a": {"disabled": True}}
    assert await alerts.send_all_accounts_unavailable_alert("test", states, now=100)
    assert "Gemini反代 test 1" in post.await_args.kwargs["json"]["text"]["content"]
    assert not await alerts.send_all_accounts_unavailable_alert("test", states, now=159)
    assert await alerts.send_all_accounts_unavailable_alert("test", states, now=160)


async def test_frequency_expiry_failure_retry_and_nonblocking_delivery(monkeypatch):
    config._config_cache.update(
        dingtalk_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
        dingtalk_alert_enabled=True, dingtalk_alert_status_codes=[429],
        dingtalk_alert_threshold=2, dingtalk_alert_window_minutes=1,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return httpx.Response(500)
    post = AsyncMock(side_effect=blocked)
    monkeypatch.setattr(alerts, "post_async", post)
    assert not await alerts.send_status_frequency_alert(status_code=429, now=10)
    assert not await alerts.send_status_frequency_alert(status_code=200, now=11)
    assert not await alerts.send_status_frequency_alert(status_code=429, now=70)
    assert await alerts.send_status_frequency_alert(status_code=429, now=71)
    await asyncio.wait_for(entered.wait(), 1)
    assert not await alerts.send_status_frequency_alert(status_code=429, now=72)
    release.set()
    await alerts.flush_status_alerts()
    monkeypatch.setattr(alerts, "post_async", AsyncMock(return_value=httpx.Response(200, json={"errcode": 0})))
    assert await alerts.send_status_frequency_alert(status_code=429, now=73)
    await alerts.flush_status_alerts()


@pytest.mark.parametrize("template", ["{model.__class__}", "{unknown}", "{", "{count:100000000}"])
def test_template_validation_rejects_invalid_fields(template):
    with pytest.raises(ValueError):
        alerts.validate_alert_template(template, alerts.STATUS_TEMPLATE_FIELDS)


async def test_sqlite_panel_save_reload_and_env_override(tmp_path, monkeypatch):
    from src.storage_adapter import StorageAdapter
    from src.panel import config_routes
    from src.models import ConfigSaveRequest
    from src.panel.utils import get_env_locked_keys
    import src.storage_adapter
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=env")
    adapter = StorageAdapter()
    await adapter.initialize()
    monkeypatch.setattr(config_routes, "get_storage_adapter", AsyncMock(return_value=adapter))
    monkeypatch.setattr(src.storage_adapter, "get_storage_adapter", AsyncMock(return_value=adapter))
    values = {"dingtalk_webhook_url": "", "dingtalk_all_accounts_alert_enabled": False,
              "dingtalk_alert_status_codes": "429,503", "dingtalk_alert_cooldown_minutes": "0"}
    try:
        assert "dingtalk_webhook_url" not in get_env_locked_keys()
        await config_routes.save_config(ConfigSaveRequest(config=values), token="test")
        assert await adapter.get_config("dingtalk_webhook_url") == ""
        assert await config.get_dingtalk_webhook_url() == ""
        assert not await config.get_dingtalk_all_accounts_alert_enabled()
        assert await config.get_dingtalk_alert_cooldown_minutes() == 0
        import json
        result = json.loads((await config_routes.get_config(token="test")).body)
        assert result["config"]["dingtalk_webhook_url"] == ""
        assert result["config"]["dingtalk_alert_status_codes"] == [429, 503]
    finally:
        await adapter.close()
