"""Persistent per-request round robin, independent of historical usage windows."""
import asyncio
from collections import Counter
import time

import aiosqlite
import pytest
import pytest_asyncio

from src.storage.sqlite_manager import SQLiteManager


NAMES = [f"account-{index}.json" for index in range(4)]


@pytest_asyncio.fixture
async def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    instance = SQLiteManager()
    await instance.initialize()
    for index, name in enumerate(NAMES):
        await instance.store_credential(
            name, {"access_token": f"test-{index}", "project_id": "test-project"},
            mode="antigravity",
        )
    try:
        yield instance
    finally:
        await instance.close()


async def select(manager, **kwargs):
    selected = await manager.get_next_available_credential(mode="antigravity", **kwargs)
    assert selected is not None
    return selected[0]


async def seed_unbalanced_windows(manager):
    now = time.time()
    async with aiosqlite.connect(manager._db_path) as db:
        for name, lifetime, window, started in zip(
            NAMES, [500000, 200, 999999, 0], [80, 1, 20, 0],
            [now - 5, now - 10, now - 20, now - 30],
        ):
            await db.execute(
                "UPDATE antigravity_credentials SET call_count=?, routing_window_count=?, routing_window_started_at=? WHERE filename=?",
                (lifetime, window, started, name),
            )
        await db.commit()


def assert_complete_rounds(selected, names=NAMES):
    for offset in range(0, len(selected), len(names)):
        round_items = selected[offset:offset + len(names)]
        assert len(round_items) == len(set(round_items)), selected
        if len(round_items) == len(names):
            assert set(round_items) == set(names), selected


@pytest.mark.asyncio
async def test_history_does_not_create_catchup_or_repeat_within_round(manager):
    await seed_unbalanced_windows(manager)

    selected = [await select(manager) for _ in range(12)]

    assert_complete_rounds(selected)
    assert Counter(selected) == dict.fromkeys(NAMES, 3)


@pytest.mark.asyncio
async def test_window_expiry_does_not_reset_an_accounts_turn(manager):
    first = await select(manager)
    async with aiosqlite.connect(manager._db_path) as db:
        await db.execute(
            "UPDATE antigravity_credentials SET routing_window_count=100, routing_window_started_at=?",
            (time.time(),),
        )
        await db.execute(
            "UPDATE antigravity_credentials SET routing_window_started_at=? WHERE filename=?",
            (time.time() - 3600, first),
        )
        await db.commit()

    selected = [first] + [await select(manager) for _ in range(3)]

    assert_complete_rounds(selected)


@pytest.mark.asyncio
async def test_ten_concurrent_requests_use_four_accounts_without_catchup(manager):
    await seed_unbalanced_windows(manager)

    selected = await asyncio.gather(*(select(manager) for _ in range(10)))

    assert sorted(Counter(selected).values()) == [2, 2, 3, 3]
    assert_complete_rounds(selected)


@pytest.mark.asyncio
async def test_multiple_managers_share_one_atomic_round_robin_cursor(manager):
    await seed_unbalanced_windows(manager)
    peers = [SQLiteManager() for _ in range(3)]
    try:
        for peer in peers:
            await peer.initialize()
        instances = [manager, *peers]
        selected = await asyncio.gather(*(
            select(instances[index % len(instances)]) for index in range(20)
        ))
        assert Counter(selected) == dict.fromkeys(NAMES, 5)
    finally:
        for peer in peers:
            await peer.close()


@pytest.mark.asyncio
async def test_round_robin_skips_disabled_cooled_and_explicitly_excluded_accounts(manager):
    await seed_unbalanced_windows(manager)
    await manager.update_credential_state(NAMES[0], {"disabled": True}, mode="antigravity")
    await manager.set_model_cooldown(NAMES[1], "test-model", time.time() + 3600, mode="antigravity")

    selected = [await select(manager, model_name="test-model", exclude_filenames={NAMES[2]}) for _ in range(4)]

    assert selected == [NAMES[3]] * 4
    await manager.update_credential_state(NAMES[0], {"disabled": False}, mode="antigravity")
    selected = [await select(manager, model_name="test-model", exclude_filenames={NAMES[2]}) for _ in range(6)]
    assert_complete_rounds(selected, [NAMES[0], NAMES[3]])


@pytest.mark.asyncio
async def test_restart_continues_the_same_cycle_despite_usage_counter_changes(manager):
    cycle = [await select(manager) for _ in range(4)]
    assert_complete_rounds(cycle)
    assert await select(manager) == cycle[0]
    # Usage statistics remain writable without becoming a routing cursor.
    async with aiosqlite.connect(manager._db_path) as db:
        await db.execute(
            "UPDATE antigravity_credentials SET routing_window_count=0, routing_window_started_at=0, call_count=0"
        )
        await db.commit()
    await manager.close()
    restarted = SQLiteManager()
    try:
        await restarted.initialize()
        assert await select(restarted) == cycle[1]
        assert await select(restarted) == cycle[2]
        assert await select(restarted) == cycle[3]
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_image_ranking_and_atomic_mark_advance_the_same_persistent_rotation(manager):
    from src.credential_manager import rank_antigravity_image_candidates

    await seed_unbalanced_windows(manager)
    selected = []
    for _ in range(12):
        states = await manager.get_all_credential_states(mode="antigravity")
        ranked = rank_antigravity_image_candidates(
            states, model_name="gemini-3.1-flash-image", runtime_health={},
        )
        assert ranked
        name = ranked[0]
        assert await manager.mark_credential_selected(
            name, mode="antigravity", expected_call_count=states[name]["call_count"],
        )
        selected.append(name)

    assert_complete_rounds(selected)
    assert Counter(selected) == dict.fromkeys(NAMES, 3)
    states = await manager.get_all_credential_states(mode="antigravity")
    assert all("routing_turn" in state for state in states.values())
