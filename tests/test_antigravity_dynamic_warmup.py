import asyncio

import pytest

from src.storage.sqlite_manager import SQLiteManager


TEST_CONFIG = {
    "account_initial_concurrency": 1,
    "account_target_concurrency": 2,
    "account_initial_interval_ms": 0,
    "account_target_interval_ms": 0,
    "ip_initial_concurrency": 1,
    "ip_target_concurrency": 1,
    "ip_initial_interval_ms": 0,
    "ip_target_interval_ms": 0,
    "success_threshold": 2,
    "window_seconds": 10,
    "max_level": 2,
}


async def make_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    await manager.initialize()
    await manager.store_credential("a.json", {"access_token": "a"}, mode="antigravity")
    await manager.store_credential("b.json", {"access_token": "b"}, mode="antigravity")
    return manager


@pytest.mark.asyncio
async def test_default_off_is_normal_and_cancel_is_not_selectable(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)

    initial = await manager.get_antigravity_warmup("a.json")
    assert initial["status"] == "off"
    assert initial["selectable"] is True

    started = await manager.set_antigravity_warmup(["a.json"], "start", TEST_CONFIG)
    assert started["a.json"]["state"]["status"] == "warming"

    cancelled = await manager.set_antigravity_warmup(["a.json"], "cancel")
    assert cancelled["a.json"]["state"]["status"] == "cancelled"
    assert cancelled["a.json"]["state"]["selectable"] is False
    assert cancelled["a.json"]["state"]["level"] == 0

    repeated = await manager.set_antigravity_warmup(["a.json"], "cancel")
    assert repeated["a.json"]["changed"] is False

    resumed = await manager.set_antigravity_warmup(["a.json"], "resume")
    assert resumed["a.json"]["state"]["status"] == "warming"

    completed = await manager.set_antigravity_warmup(["a.json"], "complete")
    assert completed["a.json"]["state"]["status"] == "active"
    assert completed["a.json"]["state"]["selectable"] is True


@pytest.mark.asyncio
async def test_batch_start_is_atomic_when_account_is_missing(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)

    result = await manager.set_antigravity_warmup(
        ["a.json", "missing.json"], "start", TEST_CONFIG
    )

    assert result["missing.json"]["status"] == "missing"
    assert (await manager.get_antigravity_warmup("a.json"))["status"] == "off"
    assert (await manager.get_antigravity_warmup("b.json"))["status"] == "off"


@pytest.mark.asyncio
async def test_same_ip_group_and_account_limits_are_atomic(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)
    await manager.set_antigravity_warmup(["a.json", "b.json"], "start", TEST_CONFIG)

    first = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=100.0)
    assert first["acquired"] is True

    group_blocked = await manager.acquire_antigravity_warmup_lease("b.json", "node:1", now=100.0)
    assert group_blocked["acquired"] is False
    assert group_blocked["reason"] == "ip_concurrency"

    account_blocked = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=100.0)
    assert account_blocked["acquired"] is False
    assert account_blocked["reason"] == "account_concurrency"

    await manager.release_antigravity_warmup_lease(first["lease_id"])
    second = await manager.acquire_antigravity_warmup_lease("b.json", "node:1", now=100.0)
    assert second["acquired"] is True
    await manager.release_antigravity_warmup_lease(second["lease_id"])


@pytest.mark.asyncio
async def test_concurrent_workers_cannot_exceed_ip_budget(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)
    await manager.set_antigravity_warmup(["a.json", "b.json"], "start", TEST_CONFIG)

    attempts = await asyncio.gather(*(
        manager.acquire_antigravity_warmup_lease(
            "a.json" if index % 2 == 0 else "b.json", "node:shared", now=200.0
        )
        for index in range(12)
    ))
    acquired = [item for item in attempts if item["acquired"]]
    assert len(acquired) == 1
    await manager.release_antigravity_warmup_lease(acquired[0]["lease_id"])


@pytest.mark.asyncio
async def test_interval_is_shared_across_releases(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)
    config = {**TEST_CONFIG, "account_initial_interval_ms": 1000, "account_target_interval_ms": 1000}
    await manager.set_antigravity_warmup(["a.json"], "start", config)

    first = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=100.0)
    await manager.release_antigravity_warmup_lease(first["lease_id"])
    blocked = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=100.5)
    assert blocked["acquired"] is False
    assert blocked["reason"] == "account_interval"
    assert blocked["retry_after"] == pytest.approx(0.5)

    allowed = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=101.0)
    assert allowed["acquired"] is True


@pytest.mark.asyncio
async def test_success_requires_count_and_observation_window(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)
    await manager.set_antigravity_warmup(["a.json"], "start", TEST_CONFIG)

    first = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=100.0)
    await manager.record_antigravity_warmup_result(first["lease_id"], "success", now=100.0)
    early = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=101.0)
    await manager.record_antigravity_warmup_result(early["lease_id"], "success", now=109.0)
    assert (await manager.get_antigravity_warmup("a.json"))["level"] == 0

    final = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=110.0)
    await manager.record_antigravity_warmup_result(final["lease_id"], "success", now=110.0)
    state = await manager.get_antigravity_warmup("a.json")
    assert state["level"] == 1
    assert state["success_count"] == 0


@pytest.mark.asyncio
async def test_failures_demote_pause_and_permanently_exclude(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)
    await manager.set_antigravity_warmup(["a.json"], "start", TEST_CONFIG)
    first = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=100.0)
    await manager.record_antigravity_warmup_result(first["lease_id"], "success", now=100.0)
    second = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=101.0)
    await manager.record_antigravity_warmup_result(second["lease_id"], "success", now=111.0)
    assert (await manager.get_antigravity_warmup("a.json"))["level"] == 1

    limited = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=112.0)
    await manager.record_antigravity_warmup_result(limited["lease_id"], "rate_limited", now=112.0)
    state = await manager.get_antigravity_warmup("a.json")
    assert state["level"] == 0
    assert state["success_count"] == 0
    blocked = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=113.0)
    assert blocked["acquired"] is False

    paused = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=173.0)
    await manager.record_antigravity_warmup_result(paused["lease_id"], "paused", now=173.0)
    assert (await manager.get_antigravity_warmup("a.json"))["status"] == "paused"
    assert (await manager.get_antigravity_warmup("a.json"))["selectable"] is False

    await manager.set_antigravity_warmup(["a.json"], "resume")
    forbidden = await manager.acquire_antigravity_warmup_lease("a.json", "node:1", now=174.0)
    await manager.record_antigravity_warmup_result(forbidden["lease_id"], "forbidden", now=174.0)
    state = await manager.get_antigravity_warmup("a.json")
    assert state["status"] == "account_forbidden"
    assert state["selectable"] is False
