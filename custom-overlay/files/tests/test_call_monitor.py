import time

import pytest


async def _make_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage_adapter import StorageAdapter

    adapter = StorageAdapter()
    await adapter.initialize()
    return adapter


def _record_kwargs(request_id, *, created_at=None, success=True, api_key_id="key-1",
                   credential_name="acc-a.json", model_name="gemini-3-pro", total_tokens=100):
    return {
        "request_id": request_id,
        "created_at": created_at if created_at is not None else time.time(),
        "api_key_id": api_key_id,
        "credential_name": credential_name,
        "channel": "antigravity",
        "model_name": model_name,
        "task_type": "chat",
        "success": success,
        "status_code": 200 if success else 429,
        "gateway_seconds": 1.5,
        "input_tokens": 60,
        "output_tokens": 40,
        "cache_tokens": 0,
        "thought_tokens": 0,
        "total_tokens": total_tokens,
        "total_cost": "0.00100000",
        "currency": "CNY",
    }


@pytest.mark.asyncio
async def test_call_records_table_created(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    import aiosqlite

    backend = adapter._backend
    async with aiosqlite.connect(backend._db_path) as db:
        tables = {
            row[0]
            for row in await (
                await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert "call_records" in tables
    await adapter.close()


@pytest.mark.asyncio
async def test_insert_and_list_call_records_filters(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    backend = adapter._backend
    now = time.time()

    await backend.insert_call_record(**_record_kwargs("r1", created_at=now - 100))
    await backend.insert_call_record(
        **_record_kwargs("r2", created_at=now - 50, success=False, api_key_id="key-2")
    )
    await backend.insert_call_record(**_record_kwargs("r3", created_at=now))

    all_rows = await backend.list_call_records(since_ts=now - 200)
    assert [row["request_id"] for row in all_rows] == ["r3", "r2", "r1"]
    assert all(isinstance(row["success"], bool) for row in all_rows)

    failed_rows = await backend.list_call_records(since_ts=now - 200, failed_only=True)
    assert [row["request_id"] for row in failed_rows] == ["r2"]

    key_rows = await backend.list_call_records(since_ts=now - 200, api_key_id="key-2")
    assert [row["request_id"] for row in key_rows] == ["r2"]

    window_rows = await backend.list_call_records(since_ts=now - 60, until_ts=now - 10)
    assert [row["request_id"] for row in window_rows] == ["r2"]
    await adapter.close()


@pytest.mark.asyncio
async def test_aggregate_call_records_by_key_and_account(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    backend = adapter._backend
    now = time.time()

    await backend.insert_call_record(**_record_kwargs("a1", created_at=now - 30))
    await backend.insert_call_record(
        **_record_kwargs("a2", created_at=now - 20, success=False)
    )
    await backend.insert_call_record(
        **_record_kwargs("b1", created_at=now - 10, api_key_id="key-2",
                         credential_name="acc-b.json", model_name="gemini-3-flash")
    )

    rows = await backend.aggregate_call_records(group_by="api_key_id", since_ts=now - 100)
    assert len(rows) == 2
    key1 = next(row for row in rows if row["group_key"] == "key-1")
    assert key1["total"] == 2
    assert key1["success_count"] == 1
    assert key1["failed_count"] == 1
    assert key1["success_rate"] == 50.0
    assert key1["total_tokens"] == 200
    # 最近一次调用是失败（a2）
    assert key1["last_success"] is False
    assert key1["last_status_code"] == 429

    rows = await backend.aggregate_call_records(
        group_by="credential_name", since_ts=now - 100, failed_only=True
    )
    assert [row["group_key"] for row in rows] == ["acc-a.json"]

    with pytest.raises(ValueError):
        await backend.aggregate_call_records(group_by="model_name", since_ts=0)
    await adapter.close()


@pytest.mark.asyncio
async def test_count_min_ts_and_delete_before(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    backend = adapter._backend
    now = time.time()

    await backend.insert_call_record(**_record_kwargs("d1", created_at=now - 10 * 86400))
    await backend.insert_call_record(
        **_record_kwargs("d2", created_at=now - 3600, success=False,
                         credential_name="acc-b.json")
    )

    counters = await backend.count_call_records(since_ts=0)
    assert counters == {"logs": 2, "failed": 1, "accounts": 2, "keys": 1}
    assert (await backend.get_call_records_min_ts()) == pytest.approx(now - 10 * 86400, abs=1)

    deleted = await backend.delete_call_records_before(now - 86400)
    assert deleted == 1
    remaining = await backend.list_call_records(since_ts=0)
    assert [row["request_id"] for row in remaining] == ["d2"]
    await adapter.close()


@pytest.mark.asyncio
async def test_monitor_record_buffer_and_preload(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    from src.call_monitor import CallMonitor

    monitor = CallMonitor(adapter)
    await monitor.record(**_record_kwargs("m1"))
    await monitor.record(**_record_kwargs("m2", success=False))

    realtime = monitor.realtime()
    assert [row["request_id"] for row in realtime] == ["m2", "m1"]
    assert [row["request_id"] for row in monitor.realtime(failed_only=True)] == ["m2"]
    counters = monitor.buffer_counters()
    assert counters["logs"] == 2 and counters["failed"] == 1

    # 模拟重启：新实例从 SQLite 预热后仍能读到历史
    monitor2 = CallMonitor(adapter)
    assert monitor2.realtime() == []
    await monitor2.preload()
    assert [row["request_id"] for row in monitor2.realtime()] == ["m2", "m1"]
    await adapter.close()


@pytest.mark.asyncio
async def test_monitor_cleanup_respects_retention_days(tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_RECORDS_RETENTION_DAYS", "1")
    adapter = await _make_adapter(tmp_path, monkeypatch)
    from src.call_monitor import CallMonitor

    monitor = CallMonitor(adapter)
    old_ts = time.time() - 3 * 86400
    # 写入即触发节流清理：3 天前的记录在保留期 1 天口径下写入后立刻被清掉
    await monitor.record(**_record_kwargs("old", created_at=old_ts))
    await monitor.record(**_record_kwargs("fresh"))

    assert [row["request_id"] for row in monitor.realtime()] == ["fresh"]
    remaining = await adapter._backend.list_call_records(since_ts=0)
    assert [row["request_id"] for row in remaining] == ["fresh"]
    # 再次显式清理已无过期记录
    assert await monitor.cleanup() == 0
    await adapter.close()


@pytest.mark.asyncio
async def test_billing_record_feeds_call_monitor_exactly_once(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    from src.billing import get_billing_recorder
    from src.call_monitor import get_call_monitor

    recorder = await get_billing_recorder(adapter)
    monitor = await get_call_monitor(adapter)

    kwargs = dict(
        request_id="req-dedupe",
        credential_name="acc-a.json",
        model="gemini-3-pro",
        usage_metadata={
            "promptTokenCount": 100,
            "candidatesTokenCount": 50,
            "totalTokenCount": 150,
        },
        success=True,
        api_key_id="key-1",
        status_code=200,
        gateway_seconds=0.8,
    )
    assert await recorder.record(**kwargs) is True
    # 同一 request_id 第二次被去重，监控不应重复记录
    assert await recorder.record(**kwargs) is False

    rows = await adapter._backend.list_call_records(since_ts=0)
    assert len(rows) == 1
    row = rows[0]
    assert row["request_id"] == "req-dedupe"
    assert row["api_key_id"] == "key-1"
    assert row["credential_name"] == "acc-a.json"
    assert row["channel"] == "antigravity"
    assert row["task_type"] == "chat"
    assert row["success"] is True
    assert row["status_code"] == 200
    assert row["total_tokens"] == 150
    assert row["gateway_seconds"] == pytest.approx(0.8)

    assert len(monitor.realtime()) == 1
    await adapter.close()
