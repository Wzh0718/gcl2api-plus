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
async def test_default_off_and_cancelled_warmup_allow_normal_requests(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)

    initial = await manager.get_antigravity_warmup("a.json")
    assert initial["status"] == "off"
    assert initial["selectable"] is True

    started = await manager.set_antigravity_warmup(["a.json"], "start", TEST_CONFIG)
    assert started["a.json"]["state"]["status"] == "warming"

    cancelled = await manager.set_antigravity_warmup(["a.json"], "cancel")
    assert cancelled["a.json"]["state"]["status"] == "cancelled"
    assert cancelled["a.json"]["state"]["selectable"] is True
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


@pytest.mark.asyncio
async def test_selection_and_image_rank_include_cancelled_accounts(tmp_path, monkeypatch):
    from src.credential_manager import rank_antigravity_image_candidates

    manager = await make_manager(tmp_path, monkeypatch)
    await manager.set_antigravity_warmup(["a.json"], "start", TEST_CONFIG)
    await manager.set_antigravity_warmup(["a.json"], "cancel")

    selected = [
        await manager.get_next_available_credential(mode="antigravity")
        for _ in range(2)
    ]
    assert {item[0] for item in selected} == {"a.json", "b.json"}

    states = await manager.get_all_credential_states(mode="antigravity")
    ranked = rank_antigravity_image_candidates(
        states, model_name="gemini-3.1-flash-image"
    )
    assert set(ranked) == {"a.json", "b.json"}


@pytest.mark.asyncio
async def test_cancelled_warmup_bypasses_warmup_limits_and_leases(tmp_path, monkeypatch):
    import aiosqlite
    from src.antigravity_dynamic_warmup import acquire_dynamic_warmup_slot

    manager = await make_manager(tmp_path, monkeypatch)
    await manager.set_antigravity_warmup(["a.json"], "start", TEST_CONFIG)
    await manager.set_antigravity_warmup(["a.json"], "cancel")

    class Adapter:
        _backend = manager

    async def get_adapter():
        return Adapter()

    monkeypatch.setattr("src.antigravity_dynamic_warmup.get_storage_adapter", get_adapter)
    expected = {"enabled": False, "acquired": True, "lease_id": None}
    assert await acquire_dynamic_warmup_slot("a.json") == expected
    assert await manager.acquire_antigravity_warmup_lease("a.json", "test-ip") == expected
    async with aiosqlite.connect(manager._db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM antigravity_warmup_leases") as cursor:
            assert (await cursor.fetchone())[0] == 0
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "account_forbidden"])
async def test_blocking_warmup_statuses_still_exclude_normal_and_image_requests(
    tmp_path, monkeypatch, status
):
    import aiosqlite
    from src.credential_manager import rank_antigravity_image_candidates

    manager = await make_manager(tmp_path, monkeypatch)
    await manager.set_antigravity_warmup(["a.json"], "start", TEST_CONFIG)
    async with aiosqlite.connect(manager._db_path) as db:
        await db.execute(
            "UPDATE antigravity_warmup_accounts SET status = ? WHERE credential_name = ?",
            (status, "a.json"),
        )
        await db.commit()
    assert (await manager.get_next_available_credential(mode="antigravity"))[0] == "b.json"
    states = await manager.get_all_credential_states(mode="antigravity")
    assert rank_antigravity_image_candidates(
        states, model_name="gemini-3.1-flash-image"
    ) == ["b.json"]
    assert (await manager.acquire_antigravity_warmup_lease("a.json", "test-ip"))["acquired"] is False
    await manager.close()


@pytest.mark.asyncio
async def test_pacing_lifecycle_records_success_and_releases_lease(tmp_path, monkeypatch):
    from src import antigravity_pacing as pacing
    from src.antigravity_dynamic_warmup import (
        note_dynamic_warmup_outcome,
        release_dynamic_warmup_slot,
    )

    manager = await make_manager(tmp_path, monkeypatch)
    await manager.set_antigravity_warmup(["a.json"], "start", TEST_CONFIG)

    class Adapter:
        _backend = manager

        async def get_credential_network(self, filename, mode="antigravity"):
            return {"proxy_mode": "group", "bound_proxy_node_id": 1}

        async def get_antigravity_account_health(self, filename):
            return {}

    adapter = Adapter()

    async def get_adapter():
        return adapter

    monkeypatch.setattr("src.antigravity_dynamic_warmup.get_storage_adapter", get_adapter)
    pacing.reset_state()
    try:
        await pacing.acquire_credential_slot("a.json")
        with pytest.raises(pacing.CredentialSlotWaitTimeout):
            await pacing.acquire_credential_slot("a.json")

        note_dynamic_warmup_outcome("a.json", "success")
        await release_dynamic_warmup_slot("a.json")
        await pacing.acquire_credential_slot("a.json")
        await release_dynamic_warmup_slot("a.json")
    finally:
        await release_dynamic_warmup_slot("a.json")
        pacing.reset_state()

    assert (await manager.get_antigravity_warmup("a.json"))["success_count"] == 1


@pytest.mark.asyncio
async def test_stream_and_non_stream_requests_release_warmup_leases(tmp_path, monkeypatch):
    from tests.test_antigravity_pacing_slots import _FakeResponse, _body, _patch_common
    from src.api import antigravity as agy
    from src.antigravity_dynamic_warmup import release_dynamic_warmup_slot

    manager = await make_manager(tmp_path, monkeypatch)
    await manager.store_credential(
        "cred1.json", {"access_token": "t", "project_id": "p"}, mode="antigravity"
    )
    await manager.set_antigravity_warmup(["cred1.json"], "start", TEST_CONFIG)
    _patch_common(monkeypatch)

    class Adapter:
        _backend = manager

        async def get_credential_network(self, filename, mode="antigravity"):
            return {"proxy_mode": "group", "bound_proxy_node_id": 1}

        async def get_antigravity_account_health(self, filename):
            return {}

    adapter = Adapter()

    async def get_adapter():
        return adapter

    monkeypatch.setattr("src.antigravity_dynamic_warmup.get_storage_adapter", get_adapter)

    async def success(_manager, name, **kwargs):
        from src.antigravity_dynamic_warmup import note_dynamic_warmup_outcome

        note_dynamic_warmup_outcome(name, "success")

    monkeypatch.setattr(agy, "record_api_call_success", success)

    async def fake_stream(**kwargs):
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)
    _ = [item async for item in agy.stream_request(_body())]

    async def fake_post(**kwargs):
        return _FakeResponse(200, b'{"candidates":[]}')

    monkeypatch.setattr(agy, "post_async", fake_post)
    response = await agy.non_stream_request(_body())
    assert response.status_code == 200
    assert (await manager.get_antigravity_warmup("cred1.json"))["success_count"] == 2

    lease = await manager.acquire_antigravity_warmup_lease("cred1.json", "node:1")
    assert lease["acquired"] is True
    await release_dynamic_warmup_slot("cred1.json")


@pytest.mark.asyncio
async def test_tos_response_permanently_excludes_warmup_account(tmp_path, monkeypatch):
    from src import antigravity_pacing as pacing
    from src.api import antigravity as agy
    from src.antigravity_dynamic_warmup import release_dynamic_warmup_slot

    manager = await make_manager(tmp_path, monkeypatch)
    await manager.set_antigravity_warmup(["a.json"], "start", TEST_CONFIG)

    class Adapter:
        _backend = manager

        async def get_credential_network(self, filename, mode="antigravity"):
            return {"proxy_mode": "group", "bound_proxy_node_id": 1}

        async def get_antigravity_account_health(self, filename):
            return {}

    adapter = Adapter()

    async def get_adapter():
        return adapter

    async def empty_health(filename):
        return {}

    async def noop(*args, **kwargs):
        return True

    monkeypatch.setattr("src.antigravity_dynamic_warmup.get_storage_adapter", get_adapter)
    monkeypatch.setattr(agy.credential_manager, "get_antigravity_account_health", empty_health)
    monkeypatch.setattr(agy.credential_manager, "update_antigravity_account_health", noop)
    monkeypatch.setattr(agy.credential_manager, "set_cred_disabled", noop)
    monkeypatch.setattr(agy, "check_should_auto_ban", noop)

    await pacing.acquire_credential_slot("a.json")
    result = await agy._handle_antigravity_403(
        credential_name="a.json",
        credential_data={},
        model_name="gemini-2.5-flash",
        error_text="Account disabled due to violation of terms of service",
    )
    await release_dynamic_warmup_slot("a.json")

    assert result.decision.disable_account is True
    state = await manager.get_antigravity_warmup("a.json")
    assert state["status"] == "account_forbidden"
    assert state["selectable"] is False
    blocked = await manager.acquire_antigravity_warmup_lease("a.json", "node:1")
    assert blocked["acquired"] is False
    pacing.reset_state()
