import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import config
from src import antigravity_usage as usage


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("WORKERS", "1")
    monkeypatch.setattr(config, "_config_initialized", True)
    policy = {}
    monkeypatch.setattr(config, "_config_cache", {usage.CONFIG_KEY: policy})
    with sqlite3.connect(usage._db_path()) as db:
        db.execute("""CREATE TABLE antigravity_credentials (
            filename TEXT PRIMARY KEY, usage_limit_disabled INTEGER NOT NULL DEFAULT 0
        )""")
    return policy


@pytest.mark.asyncio
async def test_panel_prefills_recommended_limits_and_preserves_custom_values(tmp_path, monkeypatch):
    from src.panel import config_routes
    from src.storage.sqlite_manager import SQLiteManager

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("WORKERS", "2")
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {})
    clock = SimpleNamespace(now=10000)
    monkeypatch.setattr(usage, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now))
    storage = SQLiteManager()
    await storage.initialize()
    monkeypatch.setattr(config_routes, "get_storage_adapter", AsyncMock(return_value=storage))

    async def reload_config():
        config._config_cache.update(await storage.get_all_config())

    monkeypatch.setattr(config, "reload_config", reload_config)
    app = FastAPI()
    app.include_router(config_routes.router)
    app.dependency_overrides[config_routes.verify_panel_token] = lambda: "test-token"

    expected = {
        "requests_per_minute": 160,
        "requests_per_hour": 0,
        "generated_tokens_per_minute": 100_000,
        "generated_tokens_per_hour": 1_250_000,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        initial = await client.get("/config/get")
        assert initial.status_code == 200
        assert initial.json()["account_usage_defaults"] == expected
        defaults = {"enabled": False, **expected}
        assert initial.json()["config"][usage.CONFIG_KEY] == defaults
        assert await usage.get_usage_settings() == defaults
        assert await usage.get_usage_settings(for_admission=True) == defaults
        assert await usage.begin_account_usage("shared") is None

        await storage.set_config(usage.CONFIG_KEY, {"enabled": True, "requests_per_minute": 160})
        legacy = await client.get("/config/get")
        assert legacy.json()["config"][usage.CONFIG_KEY]["generated_tokens_per_hour"] == 0

        custom = {
            "enabled": True,
            "requests_per_minute": 0,
            "requests_per_hour": 0,
            "generated_tokens_per_minute": 80_000,
            "generated_tokens_per_hour": 900_000,
        }
        rejected = {**custom, "generated_tokens_per_minute": 1.5}
        assert (await client.post("/config/save", json={"config": {usage.CONFIG_KEY: rejected}})).status_code == 400
        assert (await client.post("/config/save", json={"config": {usage.CONFIG_KEY: custom}})).status_code == 200
        clock.now += 1
        updated = (await client.get("/config/get")).json()
        assert updated["config"][usage.CONFIG_KEY] == custom
        assert updated["account_usage_defaults"] == expected
        assert await usage.get_usage_settings(for_admission=True) == custom
        snapshot = (await usage.get_account_usage_snapshots(["shared"]))["shared"]
        assert [window["generated_tokens"]["limit"] for window in snapshot["windows"]] == [80_000, 900_000]
    await storage.close()


@pytest.mark.asyncio
async def test_switch_and_rolling_account_windows(settings, monkeypatch):
    clock = SimpleNamespace(now=10000)
    monkeypatch.setattr(usage, "time", SimpleNamespace(time=lambda: clock.now))
    settings.update(requests_per_minute=1, requests_per_hour=2)
    assert await usage.begin_account_usage("first") is None
    with sqlite3.connect(usage._db_path()) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'account_usage'").fetchone()

    settings["enabled"] = True
    assert await usage.begin_account_usage("first")
    with pytest.raises(usage.AccountUsageLimitReached) as limited:
        await usage.begin_account_usage("first")
    assert limited.value.retry_after == 60
    assert await usage.begin_account_usage("second")

    clock.now += 60
    assert await usage.begin_account_usage("first")
    with pytest.raises(usage.AccountUsageLimitReached) as limited:
        await usage.begin_account_usage("first")
    assert limited.value.retry_after == 3540
    clock.now += 3540
    assert await usage.begin_account_usage("first")

    settings["enabled"] = False
    assert await usage.begin_account_usage("first") is None


@pytest.mark.asyncio
async def test_parallel_attempts_cannot_overshoot_request_cap(settings):
    settings.update(enabled=True, requests_per_hour=2)
    results = await asyncio.gather(
        *(usage.begin_account_usage("shared") for _ in range(8)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, usage.AccountUsageAttempt) for result in results) == 2
    assert all(isinstance(result, (usage.AccountUsageAttempt, usage.AccountUsageLimitReached)) for result in results)


@pytest.mark.asyncio
async def test_credential_snapshot_matches_rolling_gate(settings, monkeypatch):
    from src.panel import creds

    clock = SimpleNamespace(now=10000)
    monkeypatch.setattr(usage, "time", SimpleNamespace(time=lambda: clock.now))
    accounts = ["first.json", "second.json"]
    items = [
        {"filename": account, "user_email": "", "disabled": False, "error_codes": [], "last_success": None}
        for account in accounts
    ]
    storage = SimpleNamespace(
        get_backend_info=AsyncMock(return_value={"backend_type": "sqlite"}),
        _backend=SimpleNamespace(get_credentials_summary=AsyncMock(return_value={"items": items, "total": 2})),
    )
    monkeypatch.setattr(creds, "get_storage_adapter", AsyncMock(return_value=storage))
    with sqlite3.connect(usage._db_path()):
        pass

    assert await usage.get_account_usage_snapshots(accounts) == {
        account: {"state": "disabled", "mode": "inherit"} for account in accounts
    }
    settings["enabled"] = True
    assert (await usage.get_account_usage_snapshots(accounts))[accounts[0]]["state"] == "unconfigured"
    settings.update(requests_per_minute=2, generated_tokens_per_hour=10)
    empty_windows = (await usage.get_account_usage_snapshots(accounts))[accounts[0]]["windows"]
    assert all(window["requests"]["used"] == window["generated_tokens"]["used"] == 0 for window in empty_windows)
    with sqlite3.connect(usage._db_path()) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'account_usage'").fetchone()

    first = await usage.begin_account_usage(accounts[0])
    await first.confirm({"candidatesTokenCount": 7, "thoughtsTokenCount": 3})
    clock.now += 60
    response = await creds.get_creds_status_common(0, 20, "all", mode="antigravity")
    first_row, second_row = json.loads(response.body)["items"]
    assert first_row["account_usage"] == {
        "state": "active",
        "mode": "inherit",
        "as_of": clock.now,
        "windows": [
            {"seconds": 60, "requests": {"used": 0, "limit": 2}, "generated_tokens": {"used": 0, "limit": 0}},
            {"seconds": 3600, "requests": {"used": 1, "limit": 0}, "generated_tokens": {"used": 10, "limit": 10}},
        ],
    }
    assert second_row["account_usage"]["windows"] == empty_windows
    with pytest.raises(usage.AccountUsageLimitReached):
        await usage.begin_account_usage(accounts[0])
    response = await creds.get_creds_status_common(0, 20, "all", mode="geminicli")
    assert all("account_usage" not in row for row in json.loads(response.body)["items"])


@pytest.mark.asyncio
async def test_worker_reads_saved_policy_from_shared_database(settings, monkeypatch):
    monkeypatch.setenv("WORKERS", "2")
    clock = SimpleNamespace(now=10000)
    monkeypatch.setattr(usage, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now))
    with sqlite3.connect(usage._db_path()) as db:
        db.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO config VALUES (?, ?)", (
            usage.CONFIG_KEY, json.dumps({"enabled": True, "requests_per_hour": 1}),
        ))
    assert await usage.begin_account_usage("shared")
    with pytest.raises(usage.AccountUsageLimitReached):
        await usage.begin_account_usage("shared")
    with sqlite3.connect(usage._db_path()) as db:
        db.execute("UPDATE config SET value = '{}' WHERE key = ?", (usage.CONFIG_KEY,))
    clock.now += 1
    assert await usage.begin_account_usage("shared") is None


@pytest.mark.asyncio
async def test_account_override_is_persistent_scoped_and_keeps_measuring(tmp_path, monkeypatch):
    from src.panel import creds
    from src.storage.sqlite_manager import SQLiteManager

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("WORKERS", "2")
    clock = SimpleNamespace(now=10000)
    monkeypatch.setattr(usage, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now))
    storage = SQLiteManager()
    await storage.initialize()
    first, second = "first.json", "second.json"
    for account in (first, second):
        assert await storage.store_credential(account, {"refresh_token": account}, mode="antigravity")
    assert await storage.set_config(usage.CONFIG_KEY, {"enabled": True, "requests_per_minute": 1})

    app = FastAPI()
    app.include_router(creds.router)
    app.dependency_overrides[creds.verify_panel_token] = lambda: "test-token"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for account in (first, second):
            assert await usage.begin_account_usage(account)
        assert (await client.patch("/creds/missing.json/usage-policy", json={"mode": "off"})).status_code == 404
        assert (await client.patch(f"/creds/{first}/usage-policy", json={"mode": "custom"})).status_code == 422
        response = await client.patch(f"/creds/{first}/usage-policy", json={"mode": "off"})
        assert response.status_code == 200
        assert response.json()["account_usage"]["state"] == "exempt"

        attempt = await usage.begin_account_usage(first)
        await attempt.confirm({"candidatesTokenCount": 7, "thoughtsTokenCount": 3})
        snapshots = await usage.get_account_usage_snapshots([first, second])
        assert snapshots[first]["mode"] == "off"
        assert snapshots[first]["windows"][0]["requests"] == {"used": 2, "limit": 0}
        assert snapshots[first]["windows"][0]["generated_tokens"] == {"used": 10, "limit": 0}
        assert snapshots[second]["windows"][0]["requests"] == {"used": 1, "limit": 1}
        with pytest.raises(usage.AccountUsageLimitReached):
            await usage.begin_account_usage(second)

        assert await storage.set_config(usage.CONFIG_KEY, {"enabled": False})
        clock.now += 1
        assert (await usage.get_account_usage_snapshots([first]))[first] == {"state": "disabled", "mode": "off"}
        assert await usage.begin_account_usage(second) is None

        assert await storage.set_config(usage.CONFIG_KEY, {"enabled": True, "requests_per_minute": 1})
        clock.now += 1
        assert (await usage.get_account_usage_snapshots([first]))[first]["state"] == "exempt"
        response = await client.patch(f"/creds/{first}/usage-policy", json={"mode": "inherit"})
        assert response.json()["account_usage"]["windows"][0]["requests"] == {"used": 2, "limit": 1}
        with pytest.raises(usage.AccountUsageLimitReached):
            await usage.begin_account_usage(first)
    await storage.close()


@pytest.mark.asyncio
async def test_image_retry_rechecks_limit_before_sending(settings, monkeypatch):
    from src.api import antigravity

    settings.update(enabled=True, requests_per_minute=1)
    upstream = AsyncMock(return_value=httpx.Response(
        503, json={"error": {"details": [{"reason": "MODEL_CAPACITY_EXHAUSTED"}]}},
    ))
    monkeypatch.setattr(antigravity, "post_async", upstream)
    monkeypatch.setattr(antigravity, "get_antigravity_api_url", AsyncMock(return_value="https://test"))
    monkeypatch.setattr(antigravity, "_image_capacity_retry_delay_seconds", lambda response: 0)

    with pytest.raises(usage.AccountUsageLimitReached):
        await antigravity._post_image_generate(
            headers={}, json_body={"requestType": "image_gen"}, credential_name="shared",
        )
    upstream.assert_awaited_once()
