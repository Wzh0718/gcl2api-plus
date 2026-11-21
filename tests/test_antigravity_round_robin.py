"""Persistent round robin with a bounded bonus for lower hourly generation."""
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


@pytest.mark.asyncio
async def test_post_claim_work_does_not_hold_the_selection_lock(manager, monkeypatch):
    group = await manager.create_proxy_group(name="test-group")
    await manager.import_proxy_group_nodes(group["id"], [{"proxy_url": "http://local.example:8080"}])
    await manager.update_credential_network(NAMES[0], proxy_mode="group", proxy_group_id=group["id"])
    original = manager.resolve_credential_network
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_resolution(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(manager, "resolve_credential_network", held_resolution)
    pending = asyncio.create_task(select(manager, credential_name=NAMES[0]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await asyncio.wait_for(select(manager, credential_name=NAMES[1]), 2) == NAMES[1]
    finally:
        release.set()
        assert await pending == NAMES[0]


@pytest.mark.asyncio
async def test_claim_timeout_preserves_turn_and_next_claim_succeeds(manager, monkeypatch):
    import sqlite3
    from src.storage import sqlite_manager

    monkeypatch.setattr(sqlite_manager, "BUSY_TIMEOUT_MS", 35)
    async with aiosqlite.connect(manager._db_path) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as error:
            await asyncio.wait_for(asyncio.to_thread(
                manager._claim_antigravity_row, None, set(), NAMES[0]
            ), timeout=2)
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        await writer.rollback()
    assert await select(manager, credential_name=NAMES[0]) == NAMES[0]
    states = await manager.get_all_credential_states(mode="antigravity")
    assert states[NAMES[0]]["call_count"] == 1


@pytest.mark.asyncio
async def test_unchanged_group_binding_can_be_read_while_another_writer_is_active(manager):
    group = await manager.create_proxy_group(name="group")
    await manager.import_proxy_group_nodes(group["id"], [{"proxy_url": "http://local.example:8080"}])
    await manager.update_credential_network(NAMES[0], proxy_mode="group", proxy_group_id=group["id"])
    bound = await manager.resolve_credential_network(NAMES[0])
    assert bound["proxy_url"]
    async with aiosqlite.connect(manager._db_path) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        current = await asyncio.wait_for(manager.resolve_credential_network(NAMES[0]), 1)
        assert current["proxy_node_id"] == bound["proxy_node_id"]
        assert current["proxy_url"] == bound["proxy_url"]
        await writer.rollback()

@pytest.mark.asyncio
async def test_76_concurrent_claims_split_evenly_across_38_accounts(manager):
    names = [f"account-{index}.json" for index in range(38)]
    for name in names[len(NAMES):]:
        await manager.store_credential(name, {"access_token": "test"}, mode="antigravity")
    selected = await asyncio.gather(*(select(manager) for _ in range(76)))
    assert Counter(selected) == dict.fromkeys(names, 2)


@pytest.mark.asyncio
async def test_low_usage_gets_one_extra_turn_even_before_any_request_settles(manager):
    from src.antigravity_usage import begin_account_usage

    attempts = [await begin_account_usage(name, reserve_routing=True) for name in NAMES[1:]]
    for attempt in attempts:
        await attempt.confirm({"candidatesTokenCount": 7000, "thoughtsTokenCount": 1192})

    selected = await asyncio.gather(*(select(manager) for _ in range(50)))
    assert Counter(selected) == {NAMES[0]: 20, **dict.fromkeys(NAMES[1:], 10)}
    assert all(set(selected[i:i+5]) == set(NAMES) for i in range(0, 50, 5))

    # Four unfinished attempts have the same routing weight as 8192 confirmed
    # tokens. Equal weights revert to the regular queue.
    pending = [await begin_account_usage(NAMES[0], reserve_routing=True) for _ in range(4)]
    assert_complete_rounds([await select(manager) for _ in range(8)])
    for attempt in pending:
        await attempt.confirm(None)
    assert Counter([await select(manager) for _ in range(5)])[NAMES[0]] == 2


@pytest.mark.asyncio
async def test_only_recent_generation_and_eligible_accounts_affect_weights(manager):
    from src.antigravity_usage import begin_account_usage

    attempt = await begin_account_usage(NAMES[0], reserve_routing=True)
    await attempt.confirm({"promptTokenCount": 2_000_000, "candidatesTokenCount": 8191})
    await attempt.confirm({"candidatesTokenCount": 2_000_000})
    async with aiosqlite.connect(manager._db_path) as db:
        await db.execute("INSERT INTO account_usage VALUES (?, unixepoch()-3600, 0, 999999)", (NAMES[2],))
        await db.execute("INSERT INTO account_usage VALUES (?, unixepoch(), 0, 999999)", (NAMES[3],))
        await db.commit()
    selected = [await select(manager, exclude_filenames={NAMES[3]}) for _ in range(12)]
    assert_complete_rounds(selected, NAMES[:3])


@pytest.mark.asyncio
async def test_image_and_text_share_weighted_tickets(manager):
    from src.antigravity_usage import begin_account_usage
    from src.credential_manager import _rank_image_tickets

    attempt = await begin_account_usage(NAMES[3], reserve_routing=True)
    await attempt.confirm({"candidatesTokenCount": 9000})
    selected = []
    for i in range(28):
        if i % 2:
            selected.append(await select(manager))
        else:
            states = await manager.get_all_credential_states("antigravity")
            name, bonus = _rank_image_tickets(states, model_name="image")[0]
            assert await manager.mark_credential_selected(
                name, expected_call_count=states[name]["call_count"], bonus=bonus)
            selected.append(name)
    assert Counter(selected) == dict(zip(NAMES, [8, 8, 8, 4]))


@pytest.mark.asyncio
async def test_legacy_schema_keeps_regular_cursor_on_upgrade(manager):
    first = await select(manager)
    async with aiosqlite.connect(manager._db_path) as db:
        await db.execute("DROP INDEX idx_ag_bonus_turn")
        await db.execute("ALTER TABLE antigravity_credentials DROP COLUMN routing_bonus_turn")
        await db.execute("DROP TABLE routing_pending")
        await db.execute("DROP TABLE account_usage")
        await db.commit()
    await manager.close()
    await manager.initialize()
    assert_complete_rounds([first] + [await select(manager) for _ in range(3)])
