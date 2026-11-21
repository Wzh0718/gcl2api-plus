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


@pytest.mark.parametrize(("setting", "expected"), [(None, 100), ("6", 6)])
async def test_main_pool_capacity(monkeypatch, setting, expected):
    from src.storage.main_postgres import PostgresDatabase

    if setting is None:
        monkeypatch.delenv("MAIN_POSTGRES_POOL_MAX_SIZE", raising=False)
    else:
        monkeypatch.setenv("MAIN_POSTGRES_POOL_MAX_SIZE", setting)
    pool = PostgresDatabase("").pool
    assert pool.max_size == expected
    assert pool.min_size == 0
    await pool.close()


@pytest.mark.parametrize("setting", ["0", "-1", "invalid"])
async def test_invalid_main_pool_capacity(monkeypatch, setting):
    from src.storage.main_postgres import PostgresDatabase

    monkeypatch.setenv("MAIN_POSTGRES_POOL_MAX_SIZE", setting)
    with pytest.raises(ValueError, match="MAIN_POSTGRES_POOL_MAX_SIZE"):
        PostgresDatabase("")


@pytest_asyncio.fixture
async def pg_store(tmp_path, monkeypatch):
    dsn = os.getenv("TEST_MAIN_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_MAIN_POSTGRES_DSN is not set")
    monkeypatch.setenv("REDIS_URL", os.getenv("TEST_ACCOUNT_COOLDOWN_REDIS_URL", ""))
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
    monkeypatch.setattr("src.storage_adapter._storage_adapter", adapter)
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


async def test_pg_uniform_rotation_under_concurrent_load(pg_store):
    from collections import Counter

    names = [f"account-{index}.json" for index in range(38)]
    for name in names:
        assert await pg_store.store_credential(name, {"project_id": "test"}, mode="antigravity")
    selected = await asyncio.gather(*(
        pg_store._backend.get_next_available_credential("antigravity", "model")
        for _ in range(600)
    ))
    counts = Counter(row[0] for row in selected)
    assert sorted(counts.values()) == [15] * 8 + [16] * 30
    async with pg_store._backend._db_path.connect() as db:
        totals = await db.execute_fetchall(
            "SELECT filename, call_count, routing_window_count FROM antigravity_credentials"
        )
    assert all(calls == window == counts[name] for name, calls, window in totals)


async def test_pg_batch_skips_cancelled_waiters_and_keeps_filter_groups_separate(pg_store):
    backend = pg_store._backend
    for name in ("a.json", "b.json"):
        assert await pg_store.store_credential(name, {"project_id": "test"}, mode="antigravity")
    backend._claim_slots = asyncio.Semaphore(0)
    pending = [asyncio.create_task(backend._claim_next("test", excluded, None))
               for excluded in ({"b.json"}, {"b.json"}, {"a.json"})]
    await asyncio.sleep(0)
    pending[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending[0]
    backend._claim_slots.release()
    results = await asyncio.gather(*pending[1:])
    assert [result[0][0] for result in results] == ["a.json", "b.json"]
    async with backend._db_path.connect() as db:
        assert await db.execute_fetchall(
            "SELECT call_count FROM antigravity_credentials ORDER BY filename"
        ) == [(1,), (1,)]


async def test_pg_success_isolated_by_account_and_rechecks_cooldown(pg_store):
    import time
    from src.request_timing import RequestTiming, request_timing

    backend = pg_store._backend
    db = backend._db_path
    for name in ("a.json", "b.json"):
        assert await pg_store.store_credential(name, {"project_id": "test"}, mode="antigravity")
    active_until = time.time() + 600
    async with db.connect() as setup:
        await setup.execute("""UPDATE antigravity_credentials SET error_codes='[429]',
            model_cooldowns='{"m":1}', transient_error_count=2, transient_hold_until=?""", (active_until,))
        await setup.execute("""INSERT INTO antigravity_account_health (credential_name,binding_status)
            VALUES ('a.json','proxy_drift'), ('b.json','proxy_failed')""")
        await setup.commit()

    async with db.connect() as blocking:
        await blocking.execute("UPDATE antigravity_credentials SET model_cooldowns=? WHERE filename='a.json'",
                               (json.dumps({"m": active_until}),))
        pid = (await blocking.execute_fetchall("SELECT pg_backend_pid()"))[0][0]
        state = RequestTiming()
        token = request_timing.set(state)
        try:
            first = asyncio.create_task(backend.record_success("a.json", "m", "antigravity"))
        finally:
            request_timing.reset(token)
        try:
            async with asyncio.timeout(3):
                while True:
                    (blocked,) = await db.single_row(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid)))",
                        (pid,), operation="test_blocker",
                    )
                    if blocked:
                        break
                    await asyncio.sleep(.01)
            await asyncio.wait_for(backend.record_success("b.json", "m", "antigravity"), 2)
            assert not first.done()
        finally:
            await blocking.commit()
            await first
    async with db.connect() as session:
        rows = await session.execute_fetchall("""SELECT filename,error_codes,model_cooldowns,
            transient_error_count,transient_hold_until,binding_status,eligibility_status
            FROM antigravity_credentials JOIN antigravity_account_health ON filename=credential_name
            ORDER BY filename""")
    assert [(r[0],r[1],json.loads(r[2]),*r[3:]) for r in rows] == [
        ("a.json","[]",{"m": active_until},0,active_until,"healthy","eligible"),
        ("b.json","[]",{},0,active_until,"healthy","eligible"),
    ]
    assert state.phase_ms["success_lock_wait"] > 0


async def test_pg_readiness_uses_only_current_claim_evidence(pg_store, monkeypatch):
    import time
    from unittest.mock import AsyncMock
    from src.credential_manager import CredentialManager

    backend = pg_store._backend
    name = "test.json"
    assert await pg_store.store_credential(name, {"access_token": "t"}, mode="antigravity")
    async with backend._db_path.connect() as db:
        await db.execute("""INSERT INTO antigravity_account_health
            (credential_name,binding_status,eligibility_status,eligibility_checked_at)
            VALUES (?,'healthy','eligible',?)""", (name,time.time()))
        [(group,)] = await db.execute_fetchall("INSERT INTO proxy_groups (name) VALUES ('test') RETURNING id")
        [(node,)] = await db.execute_fetchall(
            "INSERT INTO proxy_group_nodes (group_id,proxy_url) VALUES (?,?) RETURNING id",
            (group, "http://example.invalid:3128"),
        )
        await db.execute("""UPDATE antigravity_credentials SET proxy_mode='group',proxy_group_id=?,
            bound_proxy_node_id=? WHERE filename=?""", (group,node,name))
        await db.commit()
    manager = CredentialManager()
    manager._storage_adapter = pg_store
    monkeypatch.setattr(manager, "_ensure_initialized", AsyncMock())
    monkeypatch.setattr("config.get_antigravity_network_check_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr("config.get_antigravity_network_check_ttl_seconds", AsyncMock(return_value=600))
    monkeypatch.setattr(pg_store, "resolve_credential_network", AsyncMock(side_effect=AssertionError("duplicate lookup")))
    monkeypatch.setattr(pg_store, "get_antigravity_account_health", AsyncMock(side_effect=AssertionError("duplicate lookup")))
    _, data = await backend.get_next_available_credential("antigravity","m")
    ready = await manager.ensure_antigravity_account_ready(name, data)
    assert ready["access_token"] == "t"
    assert ready["proxy_url"] == "http://example.invalid:3128"
    assert ready["proxy_node_id"] == node
    async with backend._db_path.connect() as db:
        await db.execute("UPDATE antigravity_account_health SET binding_status='proxy_drift' WHERE credential_name=?", (name,))
        await db.commit()
    _, data = await backend.get_next_available_credential("antigravity","m")
    assert await manager.ensure_antigravity_account_ready(name,data) is None


async def test_pg_geminicli_success_clears_only_its_model(pg_store):
    backend = pg_store._backend
    assert await pg_store.store_credential("gem.json", {"project_id": "test"}, mode="geminicli")
    async with backend._db_path.connect() as db:
        await db.execute("""UPDATE credentials SET error_codes='[403]', error_messages='{"m":"failed"}',
            model_cooldowns='{"m":9999999999,"other":9999999999}' WHERE filename='gem.json'""")
        await db.commit()
    await backend.record_success("gem.json", "m")
    async with backend._db_path.connect() as db:
        (row,) = await db.execute_fetchall(
            "SELECT error_codes,error_messages,model_cooldowns,last_success FROM credentials WHERE filename='gem.json'"
        )
    assert row[:2] == ("[]", "{}")
    assert json.loads(row[2]) == {"other": 9999999999}
    assert row[3] > 0


async def test_pg_503_backoff_coalesces_parallel_failures_and_resets(pg_store):
    import time

    name = "backoff.json"
    await pg_store.store_credential(name, {"project_id": "test"}, mode="antigravity")
    backend = pg_store._backend
    # 一次 503 就必须把账号移出轮转：首档 60 秒，之后逐级退避到配置上限。
    for seconds in (60, 120, 240, 480, 600, 600):
        start = time.time()
        deadlines = await asyncio.gather(*(
            backend.hold_antigravity_transient(name, 600, 503) for _ in range(4)
        ))
        assert max(deadlines) == min(deadlines)
        assert deadlines[0] - start == pytest.approx(seconds, abs=2)
        async with backend._db_path.connect() as conn:
            await conn.execute("UPDATE antigravity_credentials SET transient_hold_until=0 WHERE filename=?", (name,))
            await conn.commit()
    await backend.record_success(name, "m", "antigravity")
    assert await backend.hold_antigravity_transient(name, 600, 503) - time.time() == pytest.approx(60, abs=2)


@pytest.mark.parametrize("status", ["error", "geo_blocked"])
async def test_pg_skips_recent_exclusive_probe_failure_then_rechecks(pg_store, status):
    import time

    backend = pg_store._backend
    for name in ("probe.json", "ready.json"):
        await pg_store.store_credential(name, {"project_id": "test"}, mode="antigravity")
    async with backend._db_path.connect() as db:
        [(group,)] = await db.execute_fetchall("INSERT INTO proxy_groups (name,exclusive_assignment) VALUES ('probe',1) RETURNING id")
        [(node,)] = await db.execute_fetchall(
            "INSERT INTO proxy_group_nodes (group_id,proxy_url) VALUES (?,?) RETURNING id",
            (group, "http://example.invalid:3128"),
        )
        await db.execute("UPDATE antigravity_credentials SET proxy_mode='group',proxy_group_id=?,bound_proxy_node_id=? WHERE filename='probe.json'", (group, node))
        await db.execute("INSERT INTO antigravity_account_health (credential_name, eligibility_status,eligibility_checked_at) VALUES ('probe.json',?,?)", (status,time.time()))
        await db.commit()
    from src.credential_manager import rank_antigravity_image_candidates

    states = await backend.get_all_credential_states("antigravity")
    assert rank_antigravity_image_candidates(states, model_name="m") == ["ready.json"]
    for _ in range(3):
        picked = await backend.get_next_available_credential("antigravity", "m")
        assert picked[0] == "ready.json"
    async with backend._db_path.connect() as db:
        await db.execute("UPDATE antigravity_account_health SET eligibility_checked_at=? WHERE credential_name='probe.json'", (time.time()-61,))
        await db.commit()
    picked = await backend.get_next_available_credential("antigravity", "m")
    assert picked[0] == "probe.json"


@pytest.mark.parametrize("reason,binding", [
    ("IP check failed: ValueError: ReadError", "proxy_failed"),
    ("IP echo read failed: ReadError", "unchecked"),
])
async def test_pg_echo_read_error_allows_request_through_bound_node(pg_store, monkeypatch, reason, binding):
    import time
    from unittest.mock import AsyncMock
    from src.credential_manager import CredentialManager, rank_antigravity_image_candidates

    filename = "echo.json"
    await pg_store.store_credential(filename, {"access_token": "test"}, mode="antigravity")
    backend = pg_store._backend
    async with backend._db_path.connect() as db:
        [(group,)] = await db.execute_fetchall("INSERT INTO proxy_groups(name,exclusive_assignment) VALUES ('fixed',1) RETURNING id")
        [(node,)] = await db.execute_fetchall("INSERT INTO proxy_group_nodes(group_id,proxy_url) VALUES (?,?) RETURNING id", (group,"http://bound.invalid"))
        await db.execute("UPDATE antigravity_credentials SET proxy_mode='group', proxy_group_id=?, bound_proxy_node_id=? WHERE filename=?", (group,node,filename))
        await db.execute("INSERT INTO antigravity_account_health(credential_name,binding_status,eligibility_status,eligibility_reason,eligibility_checked_at) VALUES (?,?,'error',?,?)", (filename,binding,reason,time.time()))
        await db.commit()
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = pg_store
    monkeypatch.setattr("config.get_antigravity_egress_ip_check_url", AsyncMock(return_value="https://echo.invalid"))
    monkeypatch.setattr("config.get_antigravity_network_check_ttl_seconds", AsyncMock(return_value=600))
    name, selected = await backend.get_next_available_credential("antigravity", "m")
    ready = await manager.ensure_antigravity_account_ready(name, selected)
    assert ready["proxy_url"] == "http://bound.invalid"
    assert ready["proxy_node_id"] == node
    states = await backend.get_all_credential_states("antigravity")
    assert rank_antigravity_image_candidates(states, model_name="m") == [filename]


async def test_shared_cooldowns_keep_model_and_account_deadlines_separate(pg_store, monkeypatch):
    import time
    from unittest.mock import AsyncMock
    from src.rate_limits import selection_usage_resume_at
    from src.storage.account_cooldowns import AccountCooldowns
    from src.storage.main_postgres import main_dsn

    backend = pg_store._backend
    await pg_store.store_credential("a.json", {"project_id": "test"}, mode="antigravity")
    now = int(time.time())
    usage_until = now + 30
    async with backend._db_path.connect() as db:
        await db.execute("UPDATE antigravity_credentials SET usage_resume_at=?, model_cooldowns=?",
                         (usage_until, json.dumps({"m1": now + 90, "m2": now - 1})))
        await db.commit()
    claim = AsyncMock(wraps=backend._rotate_batch)
    monkeypatch.setattr(backend, "_rotate_batch", claim)

    assert await backend.get_next_available_credential("antigravity", "m1") is None
    assert selection_usage_resume_at.get() == 0
    assert claim.await_count == 1
    await backend._cooldown_refresh
    if os.getenv("TEST_ACCOUNT_COOLDOWN_REDIS_URL"):
        # Another worker has no process-local copy.
        old = backend._cooldowns
        backend._cooldowns = AccountCooldowns(main_dsn() + "|" + backend._db_path.schema)
        await old.close()
    assert await backend.get_next_available_credential("antigravity", "m2") is None
    assert selection_usage_resume_at.get() == usage_until
    assert await backend.get_next_available_credential("antigravity", "m1") is None
    assert selection_usage_resume_at.get() == 0
    assert claim.await_count == 1

    # Advancing only the account-wide deadline reopens m2. m1 stays blocked.
    _, rows = await backend._cooldowns.read("m2")
    assert AccountCooldowns.selection(rows, "m2", set(), None, usage_until) == (set(), None)
    _, rows = await backend._cooldowns.read("m1")
    from src.api.antigravity import _account_pool_429
    response = _account_pool_429()
    assert response.rate_limit.reason == "account_pool_unavailable"
    assert 88 <= int(response.headers["Retry-After"]) <= 90
    blocked, rejection = AccountCooldowns.selection(rows, "m1", set(), None, usage_until)
    assert blocked == {"a.json"}
    assert rejection.retry_at == now + 90
    assert AccountCooldowns.selection(rows, "m1", {"a.json"}, None, now) == (set(), None)
    # A transient account hold blocks both models even after quota recovery.
    held = [("a.json", usage_until, now + 60, {"m1": now + 90})]
    assert AccountCooldowns.selection(held, "m2", set(), None, usage_until)[1].retry_at == now + 60
    assert AccountCooldowns.selection(held, "m1", set(), None, usage_until)[1].retry_at == now + 90


async def test_partial_model_cooldown_keeps_other_models_and_accounts_available(pg_store, monkeypatch):
    import time
    from unittest.mock import AsyncMock

    backend = pg_store._backend
    for name in ("a.json", "b.json"):
        await pg_store.store_credential(name, {"project_id": "test"}, mode="antigravity")
    assert await backend.set_model_cooldown("a.json", "m1", time.time() + 60, "antigravity")
    select = backend.get_next_available_credential
    assert await select("antigravity", "m1", credential_name="a.json") is None
    await backend._cooldown_refresh
    claim = AsyncMock(wraps=backend._rotate_batch)
    monkeypatch.setattr(backend, "_rotate_batch", claim)
    assert (await select("antigravity", "m1"))[0] == "b.json"
    assert claim.await_args.args[-1] == {"a.json"}
    assert (await select("antigravity", "m2", credential_name="a.json"))[0] == "a.json"


async def test_cooldown_cache_outage_and_expiry_restore_claims(pg_store, monkeypatch):
    from redis.exceptions import ConnectionError
    from unittest.mock import AsyncMock
    import time

    backend = pg_store._backend
    cache = backend._cooldowns
    client = AsyncMock()
    client.eval.side_effect = ConnectionError("offline")
    monkeypatch.setattr(cache, "client", client)
    await pg_store.store_credential("a", {"project_id": "test"}, mode="antigravity")
    await backend.set_model_cooldown("a", "short", time.time() + .4, "antigravity")
    await backend.set_model_cooldown("a", "long", time.time() + 30, "antigravity")
    select = backend.get_next_available_credential
    assert await select("antigravity", "short") is None
    await backend._cooldown_refresh
    assert await select("antigravity", "long") is None
    await asyncio.sleep(.41)
    assert (await select("antigravity", "short"))[0] == "a"
    assert await select("antigravity", "long") is None
    client.eval.assert_awaited_once()


async def test_redis_marker_exists_only_for_its_model(pg_store):
    import time

    if not os.getenv("TEST_ACCOUNT_COOLDOWN_REDIS_URL"):
        pytest.skip("requires disposable Redis")
    cache = pg_store._backend._cooldowns
    now = time.time()
    rows = [("a.json", 0, 0, {"gemini-A": now + 60, "gemini-B": now + 120})]
    await cache.publish(now, rows)
    first = cache.key + ":a.json:model:gemini-A"
    second = cache.key + ":a.json:model:gemini-B"
    assert 58 <= await cache.client.ttl(first) <= 60
    assert 118 <= await cache.client.ttl(second) <= 120
    # Same value, different TTLs. Expiring A must not unlock B.
    assert await cache.client.mget(first, second) == [b"1", b"1"]
    await cache.client.pexpire(first, 1)
    await asyncio.sleep(.01)
    assert await cache.client.exists(first) == 0
    _, candidates = await cache.read("gemini-A")
    assert cache.selection(candidates, "gemini-A", set(), None, time.time()) == (set(), None)
    _, candidates = await cache.read("gemini-B")
    assert cache.selection(candidates, "gemini-B", set(), None, time.time())[0] == {"a.json"}
    # An early reset and an out-of-order refresh must not resurrect either marker.
    await cache.publish(time.time(), [("a.json", 0, 0, {})])
    await cache.publish(now, rows)
    assert await cache.client.exists(first, second) == 0


async def test_chain_uses_earliest_recovery_with_cold_and_warm_roster(pg_store, monkeypatch):
    import time
    from unittest.mock import AsyncMock
    from src.api import antigravity as api

    store = pg_store._backend
    models = ["gemini-A", "gemini-B", "gemini-C"]
    now = int(time.time())
    await pg_store.store_credential("shared.json", {"project_id": "test"}, mode="antigravity")
    async with store._db_path.connect() as db:
        await db.execute("UPDATE antigravity_credentials SET model_cooldowns=?, usage_resume_at=?",
                         (json.dumps(dict(zip(models, [now + 120, now + 15, now + 90]))), now + 30))
        await db.commit()
    monkeypatch.setattr(api, "get_antigravity_model_fallback_chain", AsyncMock(return_value=models))
    monkeypatch.setattr(api.credential_manager, "get_valid_credential", store.get_next_available_credential)

    # A single cold claim must know both deadlines before any Redis refresh finishes.
    assert await store.get_next_available_credential("antigravity", models[1]) is None
    assert 28 <= int(api._account_pool_429().headers["retry-after"]) <= 30
    await store._cooldown_refresh
    claim = AsyncMock(wraps=store._rotate_batch)
    monkeypatch.setattr(store, "_rotate_batch", claim)
    assert await api._select_text_credential(models[0]) == (models[1], None)
    assert 28 <= int(api._account_pool_429().headers["retry-after"]) <= 30
    claim.assert_not_awaited()

    # B becomes available on the same account while A and C stay blocked.
    await store.set_model_cooldown("shared.json", models[1], None, mode="antigravity")
    async with store._db_path.connect() as db:
        await db.execute("UPDATE antigravity_credentials SET usage_resume_at=0")
        await db.commit()
    await store._refresh_cooldowns()
    chosen, result = await api._select_text_credential(models[0])
    assert chosen == models[1] and result[0] == "shared.json"
    claim.assert_awaited_once()
