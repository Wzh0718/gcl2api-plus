import time

import pytest


async def _make_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage_adapter import StorageAdapter

    adapter = StorageAdapter()
    await adapter.initialize()
    return adapter


def _record_kwargs(request_id, *, created_at=None, success=True, api_key_id="key-1",
                   credential_name="acc-a.json", model_name="gemini-3-pro",
                   request_model="", upstream_model="", total_tokens=100):
    return {
        "request_id": request_id,
        "created_at": created_at if created_at is not None else time.time(),
        "api_key_id": api_key_id,
        "credential_name": credential_name,
        "channel": "antigravity",
        "model_name": model_name,
        "request_model": request_model,
        "upstream_model": upstream_model,
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
async def test_aggregate_key_account_usage_nests_by_key_and_account(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    backend = adapter._backend
    now = time.time()

    await backend.insert_call_record(
        **_record_kwargs("ka1", created_at=now - 40, upstream_model="gemini-3-pro-high")
    )
    await backend.insert_call_record(
        **_record_kwargs("ka2", created_at=now - 30, success=False,
                         upstream_model="gemini-3-pro-high")
    )
    await backend.insert_call_record(
        **_record_kwargs("kb1", created_at=now - 20, credential_name="acc-b.json",
                         model_name="gemini-3-flash", upstream_model="gemini-3-flash-n",
                         total_tokens=50)
    )
    await backend.insert_call_record(
        **_record_kwargs("kc1", created_at=now - 10, api_key_id="key-2",
                         upstream_model="gemini-3-pro-high")
    )

    rows = await backend.aggregate_key_account_usage(since_ts=now - 100)
    pairs = {(row["api_key_id"], row["credential_name"]): row for row in rows}
    assert set(pairs) == {
        ("key-1", "acc-a.json"),
        ("key-1", "acc-b.json"),
        ("key-2", "acc-a.json"),
    }

    acc_a = pairs[("key-1", "acc-a.json")]
    assert acc_a["group_key"] == "key-1"
    assert acc_a["total"] == 2
    assert acc_a["success_count"] == 1 and acc_a["failed_count"] == 1
    assert acc_a["success_rate"] == 50.0
    assert acc_a["total_tokens"] == 200
    # 最近一次调用是失败（ka2）
    assert acc_a["last_success"] is False and acc_a["last_status_code"] == 429
    # 模型不拆行，只回填调用量最大的请求模型 / 上游物理模型
    assert acc_a["top_model"] == "gemini-3-pro"
    assert acc_a["top_model_calls"] == 2
    assert acc_a["top_upstream_model"] == "gemini-3-pro-high"
    assert acc_a["top_upstream_calls"] == 2

    acc_b = pairs[("key-1", "acc-b.json")]
    assert acc_b["total"] == 1
    assert acc_b["top_upstream_model"] == "gemini-3-flash-n"

    # 排序：组内按账号调用量降序，组间按调用量降序
    assert (rows[0]["api_key_id"], rows[0]["credential_name"]) == ("key-1", "acc-a.json")
    assert (rows[1]["api_key_id"], rows[1]["credential_name"]) == ("key-1", "acc-b.json")

    failed = await backend.aggregate_key_account_usage(since_ts=now - 100, failed_only=True)
    assert [(row["api_key_id"], row["credential_name"]) for row in failed] == [
        ("key-1", "acc-a.json")
    ]
    await adapter.close()


@pytest.mark.asyncio
async def test_overview_key_view_nests_key_and_account(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    import src.storage_adapter as sa
    from src.call_monitor import get_call_monitor

    monkeypatch.setattr(sa, "_storage_adapter", adapter)
    monitor = await get_call_monitor(adapter)
    now = time.time()
    await monitor.record(**_record_kwargs("o1", created_at=now - 20))
    await monitor.record(**_record_kwargs("o2", created_at=now - 10,
                                          credential_name="acc-b.json"))
    await monitor.record(**_record_kwargs("o3", created_at=now - 5, api_key_id="key-2",
                                          credential_name="acc-b.json"))

    from src.panel.call_monitor import call_monitor_overview

    resp = await call_monitor_overview(
        view="key", range="all", failed_only=False, _token=None
    )
    assert resp["view"] == "key"
    assert resp["degraded"] is False
    assert len(resp["rows"]) == 3
    assert {(row["api_key_id"], row["credential_name"]) for row in resp["rows"]} == {
        ("key-1", "acc-a.json"),
        ("key-1", "acc-b.json"),
        ("key-2", "acc-b.json"),
    }
    assert all(row["group_key"] == row["api_key_id"] for row in resp["rows"])
    assert all("tps" in row for row in resp["rows"])

    # 账号视图继续沿用「账号 × 模型」口径
    account = await call_monitor_overview(
        view="account", range="all", failed_only=False, _token=None
    )
    assert account["view"] == "account"
    assert len(account["rows"]) == 2
    assert all("model_name" in row for row in account["rows"])
    await adapter.close()


def test_aggregate_from_buffer_key_account_dims():
    from src.panel.call_monitor import _aggregate_from_buffer

    now = time.time()

    def _row(request_id, *, created_at, api_key_id="key-1", credential_name="acc-a.json",
             model_name="gemini-3-pro", upstream_model="", success=True, total_cost="0.00100000"):
        return {
            "request_id": request_id,
            "created_at": created_at,
            "api_key_id": api_key_id,
            "credential_name": credential_name,
            "channel": "antigravity",
            "model_name": model_name,
            "request_model": "",
            "upstream_model": upstream_model,
            "task_type": "chat",
            "success": success,
            "status_code": 200 if success else 500,
            "gateway_seconds": 2.0,
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_tokens": 0,
            "thought_tokens": 0,
            "total_tokens": 15,
            "total_cost": total_cost,
            "currency": "CNY",
        }

    buffer_rows = [
        _row("b1", created_at=now - 30, upstream_model="gemini-3-pro-high"),
        _row("b2", created_at=now - 20, upstream_model="gemini-3-pro-high", success=False),
        _row("b3", created_at=now - 10, credential_name="acc-b.json",
             model_name="gemini-3-flash", upstream_model="gemini-3-flash-n"),
    ]

    rows = _aggregate_from_buffer(
        buffer_rows,
        dims=("api_key_id", "credential_name"),
        with_model=False,
        since_ts=0,
        failed_only=False,
    )
    assert [row["credential_name"] for row in rows] == ["acc-a.json", "acc-b.json"]
    assert rows[0]["group_key"] == "key-1"
    assert rows[0]["api_key_id"] == "key-1"
    assert rows[0]["total"] == 2 and rows[0]["failed_count"] == 1
    assert rows[0]["top_model"] == "gemini-3-pro"
    assert rows[0]["top_upstream_model"] == "gemini-3-pro-high"
    assert rows[0]["last_success"] is False

    # 账号视图仍按模型拆行
    model_rows = _aggregate_from_buffer(
        buffer_rows,
        dims=("credential_name",),
        with_model=True,
        since_ts=0,
        failed_only=False,
    )
    assert [(row["group_key"], row["model_name"]) for row in model_rows] == [
        ("acc-a.json", "gemini-3-pro"),
        ("acc-b.json", "gemini-3-flash"),
    ]
    assert model_rows[0]["total"] == 2


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
async def test_list_call_records_offset_pagination(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    backend = adapter._backend
    now = time.time()
    for i in range(5):
        await backend.insert_call_record(**_record_kwargs(f"p{i}", created_at=now + i))

    page1 = await backend.list_call_records(since_ts=0, limit=2)
    page2 = await backend.list_call_records(since_ts=0, limit=2, offset=2)
    page3 = await backend.list_call_records(since_ts=0, limit=2, offset=4)
    page4 = await backend.list_call_records(since_ts=0, limit=2, offset=6)
    assert [r["request_id"] for r in page1] == ["p4", "p3"]
    assert [r["request_id"] for r in page2] == ["p2", "p1"]
    assert [r["request_id"] for r in page3] == ["p0"]
    assert page4 == []
    # offset 与筛选条件叠加
    await backend.insert_call_record(
        **_record_kwargs("p5", created_at=now + 5, api_key_id="key-2")
    )
    key_rows = await backend.list_call_records(since_ts=0, api_key_id="key-2", limit=10, offset=0)
    assert [r["request_id"] for r in key_rows] == ["p5"]
    counters = await backend.count_call_records(since_ts=0, api_key_id="key-1")
    assert counters["logs"] == 5 and counters["keys"] == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_monitor_realtime_page(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    from src.call_monitor import CallMonitor

    monitor = CallMonitor(adapter)
    now = time.time()
    for i in range(7):
        await monitor.record(**_record_kwargs(f"rp{i}", created_at=now + i))

    page1 = await monitor.realtime_page(page=1, page_size=3)
    assert [r["request_id"] for r in page1["records"]] == ["rp6", "rp5", "rp4"]
    assert page1["total"] == 7 and page1["page"] == 1 and page1["page_size"] == 3
    assert page1["degraded"] is False

    page3 = await monitor.realtime_page(page=3, page_size=3)
    assert [r["request_id"] for r in page3["records"]] == ["rp0"]

    # 越界页返回空但 total 不变
    page99 = await monitor.realtime_page(page=99, page_size=3)
    assert page99["records"] == [] and page99["total"] == 7

    # 失败筛选下的分页
    await monitor.record(**_record_kwargs("rp-fail", success=False, created_at=now + 10))
    failed = await monitor.realtime_page(page=1, page_size=20, failed_only=True)
    assert [r["request_id"] for r in failed["records"]] == ["rp-fail"]
    assert failed["total"] == 1

    # 降级模式：无 _backend 的对象走内存缓冲切片
    class _BareStorage:
        pass

    bare = CallMonitor(_BareStorage())
    await bare.record(**_record_kwargs("b1"))
    await bare.record(**_record_kwargs("b2"))
    degraded = await bare.realtime_page(page=2, page_size=1)
    assert degraded["degraded"] is True
    assert degraded["total"] == 2
    assert [r["request_id"] for r in degraded["records"]] == ["b1"]
    await adapter.close()


@pytest.mark.asyncio
async def test_realtime_endpoint_pagination_shape(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    import src.storage_adapter as sa
    from src.call_monitor import get_call_monitor

    monkeypatch.setattr(sa, "_storage_adapter", adapter)
    monitor = await get_call_monitor(adapter)
    now = time.time()
    for i in range(45):
        await monitor.record(**_record_kwargs(f"ep{i:02d}", created_at=now + i))

    from src.panel.call_monitor import call_monitor_realtime

    resp = await call_monitor_realtime(
        page=2, page_size=20, range="all", failed_only=False, api_key_id=None, _token=None
    )
    assert resp["page"] == 2 and resp["page_size"] == 20
    assert resp["total"] == 45 and resp["total_pages"] == 3
    assert len(resp["records"]) == 20
    assert resp["records"][0]["request_id"] == "ep24"
    assert resp["counters"]["realtime"] == 45
    assert resp["degraded"] is False

    last = await call_monitor_realtime(
        page=3, page_size=20, range="all", failed_only=False, api_key_id=None, _token=None
    )
    assert len(last["records"]) == 5
    assert last["records"][-1]["request_id"] == "ep00"
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
