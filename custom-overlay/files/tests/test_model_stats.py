"""Tests for per-credential per-model call statistics."""

import os
from datetime import datetime, timedelta, timezone

import pytest


def _beijing_today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


@pytest.mark.asyncio
async def test_sqlite_increment_and_get_model_stats(tmp_path, monkeypatch):
    """SQLite 后端累计并读取「凭证 × 模型」统计。"""
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()

    await manager.increment_model_stats("a.json", "gemini-3.1-flash-image", True, mode="antigravity")
    await manager.increment_model_stats("a.json", "gemini-3.1-flash-image", True, mode="antigravity")
    await manager.increment_model_stats(
        "a.json", "gemini-3.1-flash-image", False, status_code=429, mode="antigravity"
    )
    await manager.increment_model_stats("b.json", "gemini-3.1-flash-image", False, status_code=503, mode="antigravity")
    # 不同 mode / 不同模型互不干扰
    await manager.increment_model_stats("a.json", "gemini-2.5-flash", True, mode="geminicli")

    rows = await manager.get_model_stats(mode="antigravity")
    by_key = {(r["credential_name"], r["model_name"]): r for r in rows}

    assert len(rows) == 2
    row_a = by_key[("a.json", "gemini-3.1-flash-image")]
    assert row_a["success_count"] == 2
    assert row_a["failed_count"] == 1
    assert row_a["last_status"] == 429
    row_b = by_key[("b.json", "gemini-3.1-flash-image")]
    assert row_b["success_count"] == 0
    assert row_b["failed_count"] == 1
    assert row_b["last_status"] == 503

    geminicli_rows = await manager.get_model_stats(mode="geminicli")
    assert len(geminicli_rows) == 1
    assert geminicli_rows[0]["model_name"] == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_sqlite_daily_stats_and_date_filter(tmp_path, monkeypatch):
    """按日统计：当天有数据、其他日期为空、全量累计不受日期筛选影响。"""
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()

    await manager.increment_model_stats("a.json", "gemini-3.1-flash-image", True, mode="antigravity")
    await manager.increment_model_stats(
        "a.json", "gemini-3.1-flash-image", False, status_code=429, mode="antigravity"
    )

    today = _beijing_today()
    today_rows = await manager.get_model_stats(mode="antigravity", date=today)
    assert len(today_rows) == 1
    assert today_rows[0]["success_count"] == 1
    assert today_rows[0]["failed_count"] == 1
    assert today_rows[0]["last_status"] == 429

    empty_rows = await manager.get_model_stats(mode="antigravity", date="2020-01-01")
    assert empty_rows == []

    all_time_rows = await manager.get_model_stats(mode="antigravity")
    assert len(all_time_rows) == 1
    assert all_time_rows[0]["success_count"] == 1
    assert all_time_rows[0]["failed_count"] == 1


@pytest.mark.asyncio
async def test_sqlite_stats_join_user_email(tmp_path, monkeypatch):
    """统计行通过 LEFT JOIN 附带账号邮箱；无邮箱凭证为 None。"""
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    import aiosqlite

    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()

    async with aiosqlite.connect(str(tmp_path / "credentials.db")) as db:
        await db.execute(
            "INSERT INTO antigravity_credentials (filename, credential_data, user_email) "
            "VALUES ('a.json', '{}', 'owner@example.com')"
        )
        await db.commit()

    await manager.increment_model_stats("a.json", "gemini-3.1-flash-image", True, mode="antigravity")
    await manager.increment_model_stats("b.json", "gemini-3.1-flash-image", True, mode="antigravity")

    rows = await manager.get_model_stats(mode="antigravity")
    by_cred = {r["credential_name"]: r for r in rows}
    assert by_cred["a.json"]["user_email"] == "owner@example.com"
    assert by_cred["b.json"]["user_email"] is None

    daily_rows = await manager.get_model_stats(mode="antigravity", date=_beijing_today())
    daily_by_cred = {r["credential_name"]: r for r in daily_rows}
    assert daily_by_cred["a.json"]["user_email"] == "owner@example.com"


@pytest.mark.asyncio
async def test_model_stats_endpoint_date_param(monkeypatch):
    """接口层：date 透传给后端并回显；非法日期返回 400；账号聚合带邮箱。"""
    from fastapi import HTTPException

    from src.panel import creds as creds_module

    calls = []

    class FakeBackend:
        async def get_model_stats(self, mode="geminicli", date=None):
            calls.append({"mode": mode, "date": date})
            return [
                {
                    "credential_name": "a.json",
                    "model_name": "m1",
                    "success_count": 3,
                    "failed_count": 1,
                    "last_status": 429,
                    "updated_at": 1.0,
                    "user_email": "owner@example.com",
                }
            ]

    class FakeAdapter:
        _backend = FakeBackend()

    async def fake_get_storage_adapter():
        return FakeAdapter()

    monkeypatch.setattr(creds_module, "get_storage_adapter", fake_get_storage_adapter)

    result = await creds_module.get_model_call_stats(
        token="x", mode="antigravity", date="2026-08-07"
    )
    assert calls == [{"mode": "antigravity", "date": "2026-08-07"}]
    assert result["date"] == "2026-08-07"
    assert result["by_credential"][0]["success_rate"] == 75.0
    assert result["by_credential"][0]["user_email"] == "owner@example.com"
    assert "user_email" not in result["by_model"][0]

    result = await creds_module.get_model_call_stats(token="x", mode="antigravity")
    assert calls[-1] == {"mode": "antigravity", "date": None}
    assert result["date"] is None

    with pytest.raises(HTTPException) as exc_info:
        await creds_module.get_model_call_stats(
            token="x", mode="antigravity", date="2026/08/07"
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_sqlite_stats_join_tier(tmp_path, monkeypatch):
    """统计行通过 LEFT JOIN 附带套餐级别；无凭证记录时为 None。"""
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    import aiosqlite

    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()

    async with aiosqlite.connect(str(tmp_path / "credentials.db")) as db:
        await db.execute(
            "INSERT INTO antigravity_credentials (filename, credential_data, user_email, tier) "
            "VALUES ('a.json', '{}', 'owner@example.com', 'ultra')"
        )
        await db.commit()

    await manager.increment_model_stats("a.json", "gemini-3.1-flash-image", True, mode="antigravity")
    await manager.increment_model_stats("b.json", "gemini-3.1-flash-image", True, mode="antigravity")

    rows = await manager.get_model_stats(mode="antigravity")
    by_cred = {r["credential_name"]: r for r in rows}
    assert by_cred["a.json"]["tier"] == "ultra"
    assert by_cred["b.json"]["tier"] is None

    daily_rows = await manager.get_model_stats(mode="antigravity", date=_beijing_today())
    daily_by_cred = {r["credential_name"]: r for r in daily_rows}
    assert daily_by_cred["a.json"]["tier"] == "ultra"


@pytest.mark.asyncio
async def test_model_stats_endpoint_aggregates_tier(monkeypatch):
    """接口层：by_credential 聚合附带套餐级别，by_model 不带。"""
    from src.panel import creds as creds_module

    class FakeBackend:
        async def get_model_stats(self, mode="geminicli", date=None):
            return [
                {
                    "credential_name": "a.json",
                    "model_name": "m1",
                    "success_count": 3,
                    "failed_count": 1,
                    "last_status": 429,
                    "updated_at": 1.0,
                    "user_email": "owner@example.com",
                    "tier": "ultra",
                },
                {
                    "credential_name": "a.json",
                    "model_name": "m2",
                    "success_count": 2,
                    "failed_count": 0,
                    "last_status": 200,
                    "updated_at": 1.0,
                    "user_email": "owner@example.com",
                    "tier": "ultra",
                },
            ]

    class FakeAdapter:
        _backend = FakeBackend()

    async def fake_get_storage_adapter():
        return FakeAdapter()

    monkeypatch.setattr(creds_module, "get_storage_adapter", fake_get_storage_adapter)

    result = await creds_module.get_model_call_stats(token="x", mode="antigravity")
    assert result["by_credential"][0]["tier"] == "ultra"
    assert "tier" not in result["by_model"][0]


@pytest.mark.asyncio
async def test_record_api_call_result_increments_stats():
    """record_api_call_result 成功/失败都会累计模型统计。"""
    from src.credential_manager import CredentialManager

    class FakeBackend:
        def __init__(self):
            self.stats_calls = []

        async def record_success(self, filename, model_name=None, mode="geminicli"):
            pass

        async def update_credential_state(self, *args, **kwargs):
            pass

        async def increment_model_stats(
            self, filename, model_name, success, status_code=None, mode="geminicli",
            upstream_seconds=None, gateway_seconds=None,
        ):
            self.stats_calls.append((filename, model_name, success, status_code, mode))

    class FakeAdapter:
        def __init__(self, backend):
            self._backend = backend

    backend = FakeBackend()
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter(backend)
    # update_credential_state 走 storage_adapter，直接打桩
    async def fake_update_state(*args, **kwargs):
        pass
    manager.update_credential_state = fake_update_state

    await manager.record_api_call_result(
        "a.json", True, mode="antigravity", model_name="gemini-3.1-flash-image"
    )
    await manager.record_api_call_result(
        "a.json", False, 429, mode="antigravity", model_name="gemini-3.1-flash-image"
    )

    assert backend.stats_calls == [
        ("a.json", "gemini-3.1-flash-image", True, None, "antigravity"),
        ("a.json", "gemini-3.1-flash-image", False, 429, "antigravity"),
    ]


@pytest.mark.asyncio
async def test_sqlite_stats_latency_accumulation(tmp_path, monkeypatch):
    """耗时统计：sum 与计数正确累计；不传耗时参数时分母不涨；daily 表一致。"""
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()

    await manager.increment_model_stats(
        "a.json", "gemini-3.1-flash-image", True, mode="antigravity",
        upstream_seconds=2.0, gateway_seconds=2.5,
    )
    await manager.increment_model_stats(
        "a.json", "gemini-3.1-flash-image", True, mode="antigravity",
        upstream_seconds=4.0, gateway_seconds=4.5,
    )
    # 不传耗时：只累计次数，不动耗时分母
    await manager.increment_model_stats("a.json", "gemini-3.1-flash-image", False, status_code=429, mode="antigravity")
    # 只传上游耗时
    await manager.increment_model_stats(
        "a.json", "gemini-3.1-flash-image", False, status_code=503, mode="antigravity",
        upstream_seconds=1.0,
    )

    rows = await manager.get_model_stats(mode="antigravity")
    assert len(rows) == 1
    row = rows[0]
    assert row["success_count"] == 2
    assert row["failed_count"] == 2
    assert row["upstream_total_seconds"] == pytest.approx(7.0)
    assert row["upstream_timed_count"] == 3
    assert row["gateway_total_seconds"] == pytest.approx(7.0)
    assert row["gateway_timed_count"] == 2

    daily_rows = await manager.get_model_stats(mode="antigravity", date=_beijing_today())
    assert len(daily_rows) == 1
    assert daily_rows[0]["upstream_total_seconds"] == pytest.approx(7.0)
    assert daily_rows[0]["gateway_timed_count"] == 2


@pytest.mark.asyncio
async def test_model_stats_endpoint_task_type_and_latency(monkeypatch):
    """接口层：by_task_type 按模型名分生图/普通，耗时字段正确聚合。"""
    from src.panel import creds as creds_module

    class FakeBackend:
        async def get_model_stats(self, mode="geminicli", date=None):
            return [
                {
                    "credential_name": "a.json",
                    "model_name": "gemini-3.1-flash-image",
                    "success_count": 2, "failed_count": 0,
                    "last_status": 200, "updated_at": 1.0,
                    "user_email": "owner@example.com", "tier": "ultra",
                    "upstream_total_seconds": 6.0, "upstream_timed_count": 2,
                    "gateway_total_seconds": 8.0, "gateway_timed_count": 2,
                },
                {
                    "credential_name": "a.json",
                    "model_name": "gemini-2.5-pro",
                    "success_count": 3, "failed_count": 1,
                    "last_status": 200, "updated_at": 1.0,
                    "user_email": "owner@example.com", "tier": "ultra",
                    "upstream_total_seconds": 4.0, "upstream_timed_count": 4,
                    "gateway_total_seconds": 6.0, "gateway_timed_count": 4,
                },
            ]

    class FakeAdapter:
        _backend = FakeBackend()

    async def fake_get_storage_adapter():
        return FakeAdapter()

    monkeypatch.setattr(creds_module, "get_storage_adapter", fake_get_storage_adapter)

    result = await creds_module.get_model_call_stats(token="x", mode="antigravity")

    by_type = {item["task_type"]: item for item in result["by_task_type"]}
    assert by_type["image"]["total"] == 2
    assert by_type["image"]["upstream_total_seconds"] == 6.0
    assert by_type["chat"]["total"] == 4
    assert by_type["chat"]["gateway_timed_count"] == 4

    # 按模型聚合也带耗时字段
    by_model = {item["model_name"]: item for item in result["by_model"]}
    assert by_model["gemini-2.5-pro"]["gateway_total_seconds"] == 6.0

    # 原始 stats 行不带 task_type 附加字段
    assert all("task_type" not in row for row in result["stats"])


@pytest.mark.asyncio
async def test_record_api_call_result_forwards_latency():
    """record_api_call_result 将耗时参数透传给 increment_model_stats。"""
    from src.credential_manager import CredentialManager

    class FakeBackend:
        def __init__(self):
            self.stats_calls = []

        async def record_success(self, filename, model_name=None, mode="geminicli"):
            pass

        async def update_credential_state(self, *args, **kwargs):
            pass

        async def increment_model_stats(
            self, filename, model_name, success, status_code=None, mode="geminicli",
            upstream_seconds=None, gateway_seconds=None,
        ):
            self.stats_calls.append(
                (filename, model_name, success, status_code, mode, upstream_seconds, gateway_seconds)
            )

    class FakeAdapter:
        def __init__(self, backend):
            self._backend = backend

    backend = FakeBackend()
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter(backend)

    async def fake_update_state(*args, **kwargs):
        pass
    manager.update_credential_state = fake_update_state

    await manager.record_api_call_result(
        "a.json", True, mode="antigravity", model_name="gemini-3.1-flash-image",
        upstream_seconds=1.5, gateway_seconds=2.0,
    )
    await manager.record_api_call_result(
        "a.json", False, 429, mode="antigravity", model_name="gemini-3.1-flash-image",
        upstream_seconds=0.5,
    )

    assert backend.stats_calls == [
        ("a.json", "gemini-3.1-flash-image", True, None, "antigravity", 1.5, 2.0),
        ("a.json", "gemini-3.1-flash-image", False, 429, "antigravity", 0.5, None),
    ]


@pytest.mark.asyncio
async def test_record_api_call_result_skips_stats_without_model_name():
    """未带 model_name 的调用不累计统计。"""
    from src.credential_manager import CredentialManager

    class FakeBackend:
        def __init__(self):
            self.stats_calls = []

        async def record_success(self, filename, model_name=None, mode="geminicli"):
            pass

        async def increment_model_stats(self, *args, **kwargs):
            self.stats_calls.append(args)

    class FakeAdapter:
        def __init__(self, backend):
            self._backend = backend

    backend = FakeBackend()
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter(backend)

    await manager.record_api_call_result("a.json", True, mode="antigravity")
    assert backend.stats_calls == []
