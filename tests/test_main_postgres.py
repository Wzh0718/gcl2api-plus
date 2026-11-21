"""Run against a disposable PostgreSQL server using TEST_MAIN_POSTGRES_DSN."""

import asyncio
import json
from datetime import datetime
import os
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiosqlite
import psycopg
from psycopg import sql
import pytest
import pytest_asyncio

from src import antigravity_usage as usage
from src.api_keys import ApiKeyAuthError, ApiKeyService
from src.storage_adapter import StorageAdapter


@pytest_asyncio.fixture
async def pg_store(tmp_path, monkeypatch):
    dsn = os.getenv("TEST_MAIN_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_MAIN_POSTGRES_DSN is not set")
    schema = "gcli_test_" + uuid4().hex
    monkeypatch.setenv("MAIN_POSTGRES_DSN", dsn)
    monkeypatch.setenv("MAIN_POSTGRES_SCHEMA", schema)
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.delenv("MODEL_STATS_POSTGRES_DSN", raising=False)

    def unexpected(*args, **kwargs):
        pytest.fail("PG mode opened SQLite")

    monkeypatch.setattr(sqlite3, "connect", unexpected)
    monkeypatch.setattr(aiosqlite, "connect", unexpected)
    adapter = StorageAdapter()
    try:
        await adapter.initialize()
        assert adapter.get_backend_type() == "postgresql"
        yield adapter
    finally:
        await adapter.close()
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
    assert not list(tmp_path.glob("*.db"))


async def test_pg_auth_billing_dedupe_and_quota(pg_store):
    service = ApiKeyService(pg_store)
    created = await service.create_key(name="Accounting", quota_amount="1.00")
    key = created["api_key"]
    principal = await service.authenticate(key)
    date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

    async def record(i):
        return await pg_store.record_billing_usage(
            request_id=f"req-{i}",
            billing_date=date,
            credential_name="account.json",
            model="test",
            currency="CNY",
            input_tokens=2,
            output_tokens=3,
            cache_tokens=0,
            thought_tokens=0,
            total_tokens=5,
            input_cost="0",
            output_cost="0.10",
            cache_cost="0",
            total_cost="0.10",
            success=True,
            unknown_usage=False,
            dedupe_ttl_seconds=120,
            api_key_id=principal.api_key_id,
        )

    writes = await asyncio.gather(*(record(i % 10) for i in range(40)))
    assert sum(writes) == 10
    stored = await pg_store.get_api_key_record(principal.api_key_id)
    assert stored["quota_used"] == "1.00000000"
    assert stored["quota_tokens_used"] == 50
    with pytest.raises(ApiKeyAuthError) as rejected:
        await service.authenticate(key)
    assert rejected.value.status_code == 429
    db = pg_store._backend._db_path
    async with db.connect() as session:
        assert await session.execute_fetchall(
            "SELECT total_tokens,total_cost,success_count FROM billing_daily"
        ) == [(50, "1.00000000", 10)]
    await pg_store.get_billing_summary("today")
    with pytest.raises(ValueError, match="名称"):
        await service.create_key(name="ACCOUNTING")


async def test_pg_round_robin_and_account_admission(pg_store, monkeypatch):
    a, b = "first.json", "second.json"
    for name in (a, b):
        assert await pg_store.store_credential(name, {"project_id": "test"}, mode="antigravity")
    other = StorageAdapter()
    await other.initialize()
    try:
        chosen = await asyncio.gather(
            *(
                (other if i % 2 else pg_store)._backend.get_next_available_credential(
                    "antigravity", "model"
                )
                for i in range(12)
            )
        )
        assert sorted(x[0] for x in chosen) == [a] * 6 + [b] * 6
        policy = dict(usage.DEFAULT_USAGE_LIMITS, enabled=True, requests_per_minute=3)

        async def limits(**kwargs):
            return policy

        monkeypatch.setattr(usage, "get_usage_settings", limits)
        attempts = await asyncio.gather(
            *(usage.begin_account_usage(a) for _ in range(10)), return_exceptions=True
        )
        allowed = [r for r in attempts if isinstance(r, usage.AccountUsageAttempt)]
        assert len(allowed) == 3
        assert sum(isinstance(r, usage.AccountUsageLimitReached) for r in attempts) == 7
        await allowed[0].confirm({"candidatesTokenCount": 9})
        snap = await usage.get_account_usage_snapshots([a])
        payload = json.loads(json.dumps(snap))
        assert payload[a]["windows"][0]["generated_tokens"]["used"] == 9
        for window in payload[a]["windows"]:
            assert type(window["requests"]["used"]) is int
            assert type(window["generated_tokens"]["used"]) is int
        assert (await other._backend.get_next_available_credential("antigravity", "model"))[0] == b
    finally:
        await other.close()


async def test_pg_monitor_stats_config_cache(pg_store, tmp_path):
    from src.prompt_fusion import _cache, _lease

    db = pg_store._backend
    assert await db.store_credential("one.json", {}, mode="antigravity")
    await db.insert_call_record(
        request_id="one", created_at=123, success=True, timing_details={"a": 2}
    )
    rows = await db.list_call_records(since_ts=100)
    assert rows[0]["request_id"] == "one"
    await db.aggregate_call_records(group_by="api_key_id", since_ts=100)
    await db.aggregate_key_account_usage(since_ts=100)
    path = tmp_path / "cache.sqlite3"
    assert await asyncio.to_thread(_lease, path, "key", "owner", True)
    assert not await asyncio.to_thread(_lease, path, "key", "another", True)
    await asyncio.to_thread(_cache, path, "key", "result")
    assert await asyncio.to_thread(_cache, path, "key") == "result"
    assert not path.exists()


async def test_pg_failed_settlement_rolls_back_and_can_retry(pg_store):
    store = pg_store._backend
    data = dict(
        request_id="atomic",
        billing_date="2026-11-21",
        credential_name="a",
        model="m",
        currency="CNY",
        input_tokens=1,
        output_tokens=0,
        cache_tokens=0,
        thought_tokens=0,
        total_tokens=1,
        input_cost="0.01",
        output_cost="0",
        cache_cost="0",
        total_cost="0.01",
        success=True,
        unknown_usage=False,
        dedupe_ttl_seconds=600,
    )
    async with store._db_path.connect() as db:
        await db.execute("ALTER TABLE api_keys ADD CHECK (quota_tokens_used < 1)")
        await db.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        await store.record_billing_usage(**data)
    async with store._db_path.connect() as db:
        assert await db.execute_fetchall("SELECT request_id FROM billing_request_dedupe") == []
        assert await db.execute_fetchall("SELECT total_cost FROM billing_daily") == []
        assert await db.execute_fetchall("SELECT quota_used FROM api_keys") == [("0.00000000",)]
        await db.execute("ALTER TABLE api_keys DROP CONSTRAINT api_keys_quota_tokens_used_check")
        await db.commit()
    assert await store.record_billing_usage(**data)
    assert not await store.record_billing_usage(**data)


async def test_pg_full_import_preserves_auth_monitor_and_cache(tmp_path, monkeypatch):
    import asyncpg
    from scripts.postgres.import_main import transfer, transfer_fusion
    from src.prompt_fusion import _cache
    from src.storage.sqlite_manager import SQLiteManager

    dsn = os.getenv("TEST_MAIN_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_MAIN_POSTGRES_DSN is not set")
    schema = "gcli_import_" + uuid4().hex
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.delenv("MAIN_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("MODEL_STATS_POSTGRES_DSN", raising=False)
    old = SQLiteManager()
    await old.initialize()
    key = (await ApiKeyService(old).create_key(name="Historical"))["api_key"]
    await old.insert_call_record(
        request_id="historical",
        created_at=123,
        success=True,
        timing_details={"stages": {"upstream": 12}},
    )
    await old.close()
    cache = tmp_path / "prompt_fusion_cache.sqlite3"
    _cache(cache, "historical", "cached")
    conn = await asyncpg.connect(dsn)
    adapter = StorageAdapter()
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        with sqlite3.connect(tmp_path / "credentials.db") as source:
            source.execute("BEGIN")
            async with conn.transaction():
                await transfer(source, conn, schema, True)
                await transfer_fusion(cache, conn, schema, True)
        monkeypatch.setenv("MAIN_POSTGRES_DSN", dsn)
        monkeypatch.setenv("MAIN_POSTGRES_SCHEMA", schema)
        await adapter.initialize()
        assert (await ApiKeyService(adapter).authenticate(key)).name == "Historical"
        rows = await adapter._backend.list_call_records()
        assert rows[0]["timing_details"]["stages"]["upstream"] == 12
        assert _cache(cache, "historical") == "cached"
    finally:
        await adapter.close()
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


async def test_pg_unrelated_keys_and_usage_continue_while_one_key_is_locked(pg_store):
    service = ApiKeyService(pg_store)
    key_a = (await service.create_key(name="first"))["item"]["id"]
    key_b = (await service.create_key(name="second"))["item"]["id"]
    manager = pg_store._backend
    async with manager._db_path.connect() as db:
        await db.execute("SELECT id FROM api_keys WHERE id=? FOR UPDATE", (key_a,))
        waiting = asyncio.create_task(manager.admit_api_key_request(key_a))
        try:
            assert await asyncio.wait_for(manager.admit_api_key_request(key_b), 2) is None
            assert not waiting.done()
        finally:
            await db.rollback()
            await asyncio.wait_for(waiting, 2)


async def test_pg_billing_entrypoint_flushes_one_monitor_row(pg_store, monkeypatch):
    from src.billing import BillingRecorder

    monkeypatch.setenv("REDIS_URL", "")
    recorder = BillingRecorder(pg_store)
    created = await ApiKeyService(pg_store).create_key(name="monitor")
    entry = dict(
        request_id="monitored",
        api_key_id=created["item"]["id"],
        model="test",
        credential_name="test.json",
        success=True,
        usage_metadata={"promptTokenCount": 12, "candidatesTokenCount": 4, "totalTokenCount": 16},
    )
    assert await recorder.record(**entry)
    assert not await recorder.record(**entry)
    await pg_store._backend.flush_call_records()
    rows = await pg_store._backend.list_call_records()
    assert [(r["request_id"], r["total_tokens"]) for r in rows] == [("monitored", 16)]


async def test_pg_preserves_historical_and_incremental_model_stats(pg_store, monkeypatch):
    import asyncpg

    dsn = os.environ["TEST_MAIN_POSTGRES_DSN"]
    # Deployed installations keep model-stat deltas in a separate existing schema.
    delta_schema = "gcli_delta_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    other = StorageAdapter()
    try:
        await admin.execute(f'CREATE SCHEMA "{delta_schema}"')
        await pg_store._backend.increment_model_stats("history.json", "m", True, mode="antigravity")
        separator = "&" if "?" in dsn else "?"
        monkeypatch.setenv(
            "MODEL_STATS_POSTGRES_DSN", f"{dsn}{separator}search_path={delta_schema}"
        )
        await other.initialize()
        await other._backend.increment_model_stats("history.json", "m", True, mode="antigravity")
        rows = await other._backend.get_model_stats(mode="antigravity")
        assert [(r["model_name"], r["success_count"]) for r in rows] == [("m", 2)]
    finally:
        await other.close()
        await admin.execute(f'DROP SCHEMA "{delta_schema}" CASCADE')
        await admin.close()


async def test_partial_pg_schema_fails_closed_and_retries(tmp_path, monkeypatch):
    dsn = os.getenv("TEST_MAIN_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_MAIN_POSTGRES_DSN is not set")
    schema = "gcli_partial_" + uuid4().hex
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("MAIN_POSTGRES_DSN", dsn)
    monkeypatch.setenv("MAIN_POSTGRES_SCHEMA", schema)
    monkeypatch.delenv("MODEL_STATS_POSTGRES_DSN", raising=False)
    adapter = StorageAdapter()
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as admin:
        prefix = sql.Identifier(schema)
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(prefix))
        try:
            await admin.execute(sql.SQL("CREATE TABLE {}.api_keys(id text)").format(prefix))
            with pytest.raises(RuntimeError, match="Incomplete PostgreSQL main schema"):
                await adapter.initialize()
            assert not (tmp_path / "credentials.db").exists()
            await admin.execute(sql.SQL("DROP TABLE {}.api_keys").format(prefix))
            await adapter.initialize()
            assert (await adapter.get_api_key_record("env"))["id"] == "env"
        finally:
            await adapter.close()
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(prefix))


async def test_concurrent_key_limits_keep_exact_rpm_and_period_counts(pg_store):
    service = ApiKeyService(pg_store)
    key = (await service.create_key(name="Limited"))["api_key"]
    key_id = (await service.authenticate(key)).api_key_id
    await service.update_key(key_id, limit_enabled=True, day_rpm=3, night_rpm=3,
                             day_total=4, night_total=4)
    now = 1795228394.0
    admit = pg_store.admit_api_key_request
    results = await asyncio.gather(*(admit(key_id, now) for _ in range(16)))
    assert results.count(None) == 3
    assert results.count("已达每分钟请求上限") == 13
    assert await admit(key_id, now + 60) is None
    assert await admit(key_id, now + 61) == "时段总额度已耗尽"
    async with pg_store._backend._db_path.connect() as db:
        assert await db.execute_fetchall(
            "SELECT limit_period_used FROM api_keys WHERE id=?", (key_id,)
        ) == [(4,)]


async def test_pipelined_claim_serializes_workers_and_preserves_resume(pg_store, monkeypatch):
    from collections import Counter
    import time
    from src.request_timing import RequestTiming, request_timing

    monkeypatch.setattr("config.get_antigravity_dormant_enabled_sync", lambda: False)
    names = ["a.json", "b.json"]
    for name in names:
        assert await pg_store.store_credential(name, {"project_id": "test"}, mode="antigravity")
    other = StorageAdapter()
    await other.initialize()
    db = pg_store._backend
    try:
        workers = [db, other._backend]
        results = await asyncio.gather(*(
            workers[i % 2]._claim_next("model", set(), None) for i in range(40)
        ))
        assert Counter(row[0] for row, _, _ in results) == {name: 20 for name in names}
        until = int(time.time()) + 300
        async with db._db_path.connect() as session:
            await session.execute("UPDATE antigravity_credentials SET usage_resume_at=?", (until,))
            await session.commit()
        state = RequestTiming()
        token = request_timing.set(state)
        try:
            assert await db._claim_next("model", {"a.json"}, "b.json") == (None, until, False)
            assert state.phase_ms["credential_pg_advisory_server"] >= 0
            assert state.phase_ms["credential_pg_claim_sql_server"] >= 0
        finally:
            request_timing.reset(token)
    finally:
        await other.close()

@pytest.mark.asyncio
async def test_pg_single_select_releases_connection_and_reports_repository_operation(pg_store):
    backend = pg_store._backend
    db = backend._db_path
    assert (await backend.get_antigravity_warmup('absent'))['status'] == 'off'
    assert (await backend.get_credential_network('absent'))['proxy_mode'] == 'inherit'
    assert (await backend.get_antigravity_account_health('absent'))['binding_status'] == 'unchecked'
    for op in ('get_antigravity_warmup', 'get_credential_network', 'get_antigravity_account_health'):
        count, held_ms, longest_ms = db._holds[op]
        assert count >= 1 and held_ms >= longest_ms > 0
    async with db.connect('check_transaction_mode') as session:
        assert not session.conn.autocommit

@pytest.mark.asyncio
async def test_pg_settlement_resets_month_once_with_concurrent_requests(pg_store):
    service = ApiKeyService(pg_store)
    created = await service.create_key(name='month rollover')
    key = (await service.authenticate(created['api_key'])).api_key_id
    db = pg_store._backend._db_path
    async with db.connect() as session:
        await session.execute("UPDATE api_keys SET reset_mode='monthly', quota_used='3.00000000',quota_tokens_used=100,quota_period_start='2026-10-01' WHERE id=?", (key,))
        await session.commit()
    async def settle(i):
        return await pg_store.record_billing_usage(request_id=f'month-{i}', api_key_id=key,
            billing_date='2026-11-01', credential_name='a', model='m', currency='CNY',
            input_tokens=1, output_tokens=0, cache_tokens=0, thought_tokens=0, total_tokens=1,
            input_cost='0.00000001', output_cost='0', cache_cost='0', total_cost='0.00000001',
            success=False, unknown_usage=True, dedupe_ttl_seconds=600)
    assert sum(await asyncio.gather(*(settle(i % 4) for i in range(8)))) == 4
    async with db.connect() as session:
        assert await session.execute_fetchall('SELECT amount_before,amount_after FROM api_key_quota_events WHERE api_key_id=?', (key,)) == [('3.00000000','0.00000000')]
        assert await session.execute_fetchall('SELECT quota_used,quota_tokens_used,quota_period_start FROM api_keys WHERE id=?', (key,)) == [('0.00000004',4,'2026-11-01')]
        assert await session.execute_fetchall('SELECT total_cost,failed_count,unknown_usage_count FROM billing_daily WHERE api_key_id=?',(key,)) == [('0.00000004',4,4)]
