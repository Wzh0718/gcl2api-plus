"""Health probes must never queue inference behind the IP echo limiter."""
import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(params=["sqlite", "postgresql", "mysql"])
async def health_store(request, tmp_path, monkeypatch):
    from src.storage_adapter import StorageAdapter

    backend = request.param
    name = "health_test_" + uuid4().hex
    dsn = None
    if backend != "sqlite":
        setting = "MAIN_POSTGRES_DSN" if backend == "postgresql" else "MAIN_MYSQL_DSN"
        dsn = os.getenv("TEST_" + setting)
        if not dsn:
            pytest.skip("Requires TEST_" + setting)
        monkeypatch.setenv(setting, dsn)
    monkeypatch.setenv("MAIN_STORAGE_BACKEND", backend)
    monkeypatch.setenv("MAIN_POSTGRES_SCHEMA", name)
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    if backend == "mysql":
        import pymysql
        from src.storage.main_mysql import connection_args
        from urllib.parse import urlsplit, urlunsplit
        with pymysql.connect(**connection_args()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE {name}")
        url = urlsplit(dsn)
        monkeypatch.setenv("MAIN_MYSQL_DSN", urlunsplit(url._replace(path="/" + name)))
    store = StorageAdapter()
    monkeypatch.setattr("src.storage_adapter._storage_adapter", store)
    try:
        await store.initialize()
        yield store._backend
    finally:
        await store.close()
        if backend == "postgresql":
            import psycopg
            from psycopg import sql
            async with await psycopg.AsyncConnection.connect(dsn) as connection:
                await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
        elif backend == "mysql":
            monkeypatch.setenv("MAIN_MYSQL_DSN", dsn)
            with pymysql.connect(**connection_args()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE {name}")


@pytest.mark.asyncio
async def test_concurrent_stale_requests_do_not_probe(monkeypatch):
    from src.credential_manager import CredentialManager
    from src.rate_limits import selection_retry_at

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = SimpleNamespace(
        resolve_credential_network=AsyncMock(return_value={"proxy_mode": "direct"}),
        get_antigravity_account_health=AsyncMock(return_value={}),
    )
    probe = AsyncMock(side_effect=AssertionError("request awaited health probe"))
    monkeypatch.setattr(manager, "check_antigravity_account_health", probe)
    monkeypatch.setattr("config.get_antigravity_network_check_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr("config.get_antigravity_network_check_ttl_seconds", AsyncMock(return_value=300))

    async def request():
        result = await manager.ensure_antigravity_account_ready("a", {"access_token": "x"})
        assert result is None
        assert selection_retry_at.get() > time.time()

    await asyncio.wait_for(asyncio.gather(*(request() for _ in range(20))), timeout=0.5)
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_selection_skips_more_than_three_stale_accounts(health_store):
    for i in range(6):
        await health_store.store_credential(str(i), {"access_token": "x"}, mode="antigravity")
    await health_store.update_antigravity_account_health(
        "5", binding_status="healthy", eligibility_status="eligible",
        eligibility_checked_at=time.time(),
    )
    result = await health_store.get_next_available_credential(
        mode="antigravity", health_policy=(True, 300, True),
    )
    assert result[0] == "5"
    # Administrative/raw storage callers retain their existing unchecked claims.
    result = await health_store.get_next_available_credential(mode="antigravity")
    assert result[0] == "0"


@pytest.mark.asyncio
async def test_refresh_due_before_expiry_and_skips_disabled(health_store):
    from src.antigravity_health_refresh import refresh_due_accounts

    now = time.time()
    for name, age in (("stale", 400), ("due", 250), ("fresh", 10), ("disabled", 400)):
        await health_store.store_credential(name, {"access_token": "x"}, mode="antigravity")
        await health_store.update_antigravity_account_health(
            name, binding_status="healthy", eligibility_status="eligible",
            eligibility_checked_at=now - age,
        )
    await health_store.update_credential_state("disabled", {"disabled": True}, mode="antigravity")
    manager = SimpleNamespace(_storage_adapter=SimpleNamespace(_backend=health_store))

    async def check(name):
        return await health_store.update_antigravity_account_health(
            name, binding_status="healthy", eligibility_status="eligible",
            eligibility_checked_at=time.time(),
        )

    manager.check_antigravity_account_health = AsyncMock(side_effect=check)
    await refresh_due_accounts(manager, (True, 300, True))
    assert [call.args[0] for call in manager.check_antigravity_account_health.await_args_list] == ["stale", "due"]
    assert (await health_store.get_next_available_credential(
        mode="antigravity", credential_name="stale", health_policy=(True, 300, True),
    ))[0] == "stale"


@pytest.mark.asyncio
async def test_redis_lease_loss_cancels_inflight_probe():
    from src.antigravity_health_refresh import run_with_lease

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def scan():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def renew(*args):
        await started.wait()
        return 0

    client = SimpleNamespace(eval=AsyncMock(side_effect=renew))
    with pytest.raises(RuntimeError, match="lease"):
        await run_with_lease(client, "lease", "owner", scan, heartbeat_seconds=0.001)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_old_probe_cannot_validate_replaced_credential(health_store):
    from src.credential_revision import REVISION_KEY

    await health_store.store_credential("a", {"access_token": "old"}, mode="antigravity")
    credential = await health_store.get_credential("a", mode="antigravity")
    network = await health_store.resolve_credential_network("a", mode="antigravity")
    started = time.time()
    await health_store.store_credential("a", {"access_token": "replacement"}, mode="antigravity")
    await health_store.update_antigravity_account_health(
        "a", expected_network=network, expected_revision=credential[REVISION_KEY],
        check_started_at=started, binding_status="healthy", eligibility_status="eligible",
        eligibility_checked_at=time.time(),
    )
    assert (await health_store.get_antigravity_account_health("a"))["eligibility_status"] == "unchecked"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["route", "newer_health", "newer_403"])
async def test_old_probe_cannot_overwrite_newer_route_or_evidence(health_store, change):
    from src.credential_revision import REVISION_KEY

    await health_store.store_credential("a", {"access_token": "x"}, mode="antigravity")
    credential = await health_store.get_credential("a", mode="antigravity")
    network = await health_store.resolve_credential_network("a", mode="antigravity")
    started = time.time()
    if change == "route":
        await health_store.batch_update_credential_network(["a"], proxy_mode="direct")
    elif change == "newer_health":
        await health_store.update_antigravity_account_health(
            "a", binding_status="proxy_drift", eligibility_checked_at=started + 1,
        )
    else:
        await health_store.update_antigravity_account_health(
            "a", eligibility_status="account_blocked", last_403_at=started + 1,
        )
    before = await health_store.get_antigravity_account_health("a")
    await health_store.update_antigravity_account_health(
        "a", expected_network=network, expected_revision=credential[REVISION_KEY],
        check_started_at=started, binding_status="healthy", eligibility_status="eligible",
        eligibility_checked_at=started,
    )
    assert await health_store.get_antigravity_account_health("a") == before


@pytest.mark.asyncio
async def test_redis_outage_does_not_fall_back_to_local_consumption(monkeypatch):
    from src import antigravity_health_refresh as refresh

    monkeypatch.setattr(refresh, "get_health_policy", AsyncMock(return_value=(True, 300, True)))
    scan = AsyncMock()
    monkeypatch.setattr(refresh, "refresh_due_accounts", scan)
    client = SimpleNamespace(set=AsyncMock(side_effect=ConnectionError))
    with pytest.raises(ConnectionError):
        await refresh._refresh_once(object(), client, "key")
    scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_coordinates_contenders_and_recovers(monkeypatch):
    from src import antigravity_health_refresh as refresh
    from redis.asyncio import Redis

    url = os.getenv("TEST_HEALTH_REDIS_URL")
    if not url:
        pytest.skip("Requires TEST_HEALTH_REDIS_URL")
    client = Redis.from_url(url, decode_responses=True)
    key = "health-test:" + uuid4().hex
    started, finish = asyncio.Event(), asyncio.Event()

    async def scan(*args, **kwargs):
        if before_probe := kwargs.get("before_probe"):
            await before_probe()
        started.set()
        await finish.wait()

    probe = AsyncMock(side_effect=scan)
    monkeypatch.setattr(refresh, "refresh_due_accounts", probe)
    monkeypatch.setattr(refresh, "get_health_policy", AsyncMock(return_value=(True, 300, True)))
    first = asyncio.create_task(refresh._refresh_once(object(), client, key))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await refresh._refresh_once(object(), client, key)
        assert probe.await_count == 1
        finish.set()
        await first
        await refresh._refresh_once(object(), client, key)
        assert probe.await_count == 2
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await client.delete(key)
        await client.aclose()


@pytest.mark.asyncio
async def test_lifecycle_detaches_request_context_and_drains_work(health_store, monkeypatch):
    from contextvars import ContextVar
    from src import antigravity_health_refresh as refresh

    context = ContextVar("request_marker", default=None)
    context.set("request-only")
    started, cancelled = asyncio.Event(), asyncio.Event()
    manager = SimpleNamespace(_storage_adapter=SimpleNamespace(_backend=health_store))

    async def scan(*args):
        assert context.get() is None
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("src.credential_manager.credential_manager._get_or_create", AsyncMock(return_value=manager))
    monkeypatch.setattr("src.redis_config.create_redis_client_from_env", lambda **kwargs: None)
    monkeypatch.setattr(refresh, "get_health_policy", AsyncMock(return_value=(True, 300, True)))
    monkeypatch.setattr(refresh, "refresh_due_accounts", scan)
    try:
        task = await refresh.start_health_refresh()
        assert await refresh.start_health_refresh() is task
        await asyncio.wait_for(started.wait(), 1)
    finally:
        await refresh.stop_health_refresh()
    assert task.done() and cancelled.is_set()
    assert refresh._TASK is None and refresh._WAKE is None


@pytest.mark.asyncio
async def test_background_check_persists_current_route(health_store, monkeypatch):
    from src.credential_manager import CredentialManager
    from src.antigravity_health_refresh import refresh_due_accounts
    from src.egress_probe import Observation
    from src.storage_adapter import get_storage_adapter

    await health_store.store_credential("a", {"access_token": "x"}, mode="antigravity")
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = await get_storage_adapter()
    monkeypatch.setattr(manager, "_should_refresh_token", AsyncMock(return_value=False))
    monkeypatch.setattr("config.get_antigravity_egress_ip_check_url", AsyncMock(return_value="https://echo.invalid"))
    monkeypatch.setattr("src.antigravity_account_health.observe_egress", AsyncMock(return_value=Observation(200, "203.0.113.8", "US")))
    monkeypatch.setattr("src.antigravity_account_health.post_async", AsyncMock(return_value=SimpleNamespace(
        status_code=200, json=lambda: {"currentTier": {"id": "free-tier"}},
    )))
    monkeypatch.setattr("src.google_oauth_api.warm_antigravity_environment", AsyncMock())
    await refresh_due_accounts(manager, (True, 300, True))
    health = await health_store.get_antigravity_account_health("a")
    assert health["binding_status"] == "healthy"
    assert health["eligibility_status"] == "eligible"
    assert "a" in await health_store.health_ready_credential_names((True, 300, True))


@pytest.mark.asyncio
async def test_exclusive_readiness_preserves_strict_gate_and_echo_exception(health_store):
    from src.storage.sqlite_manager import sqlite_connect

    group = await health_store.create_proxy_group("exclusive")
    await health_store.import_proxy_group_nodes(group["id"], [{"name": "one", "proxy_url": "http://proxy.invalid:80"}])
    await health_store.store_credential("a", {"access_token": "x"}, mode="antigravity")
    await health_store.batch_update_credential_network(["a"], proxy_mode="group", proxy_group_id=group["id"])
    await health_store.resolve_credential_network("a", mode="antigravity")
    policy = (False, 300, True)  # Exclusive checks remain required with global gate off.
    assert "a" not in await health_store.health_ready_credential_names(policy)
    for binding, eligibility, reason, age, ready in (
        ("healthy", "eligible", None, 0, True),
        ("healthy", "geo_blocked", None, 0, False),
        ("proxy_drift", "eligible", None, 0, False),
        ("healthy", "account_blocked", None, 0, False),
        ("unchecked", "error", "IP echo read failed: ReadTimeout", 0, True),
        ("unchecked", "error", "IP echo read failed: ReadTimeout", 61, False),
        ("healthy", "eligible", None, 301, False),
        ("healthy", "eligible", None, -30, False),
    ):
        await health_store.update_antigravity_account_health("a", binding_status=binding,
            eligibility_status=eligibility, eligibility_reason=reason, eligibility_checked_at=time.time() - age)
        assert ("a" in await health_store.health_ready_credential_names(policy)) is ready
    assert "a" not in await health_store.health_ready_credential_names((False, 300, False))
