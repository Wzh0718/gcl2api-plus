import time

import pytest


@pytest.mark.asyncio
async def test_old_sqlite_schema_gets_proxy_and_billing_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    import aiosqlite

    db_path = tmp_path / "credentials.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE antigravity_credentials (filename TEXT UNIQUE NOT NULL, credential_data TEXT NOT NULL)"
        )
        await db.commit()
    manager = SQLiteManager()
    await manager.initialize()

    async with aiosqlite.connect(manager._db_path) as db:
        columns = {
            row[1]
            for row in await (
                await db.execute("PRAGMA table_info(antigravity_credentials)")
            ).fetchall()
        }
        tables = {
            row[0]
            for row in await (
                await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }

    assert {"proxy_mode", "proxy_url"}.issubset(columns)
    assert {"billing_prices", "billing_daily", "billing_request_dedupe"}.issubset(tables)
    await manager.close()


@pytest.mark.asyncio
async def test_storage_adapter_always_uses_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("POSTGRESQL_URI", "postgresql://invalid.invalid/db")
    monkeypatch.setenv("MONGODB_URI", "mongodb://invalid.invalid/db")
    from src.storage_adapter import StorageAdapter

    adapter = StorageAdapter()
    await adapter.initialize()
    assert adapter.get_backend_type() == "sqlite"
    await adapter.close()


@pytest.mark.asyncio
async def test_expired_antigravity_429_is_not_reported_as_active_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    await manager.store_credential(
        "account.json",
        {"access_token": "token", "project_id": "project"},
        mode="antigravity",
    )
    await manager.update_credential_state(
        "account.json",
        {
            "error_codes": [429],
            "model_cooldowns": {"gemini-2.5-flash": time.time() - 1},
        },
        mode="antigravity",
    )

    summary = await manager.get_credentials_summary(mode="antigravity", limit=20)
    state = await manager.get_credential_state("account.json", mode="antigravity")

    assert summary["items"][0]["error_codes"] == []
    assert summary["items"][0]["model_cooldowns"] == {}
    assert state["error_codes"] == []
    assert state["model_cooldowns"] == {}
    await manager.close()


@pytest.mark.asyncio
async def test_active_antigravity_429_remains_visible_during_short_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    await manager.store_credential(
        "account.json",
        {"access_token": "token", "project_id": "project"},
        mode="antigravity",
    )
    cooldown_until = time.time() + 60
    await manager.update_credential_state(
        "account.json",
        {
            "error_codes": [429],
            "model_cooldowns": {"gemini-2.5-flash": cooldown_until},
        },
        mode="antigravity",
    )

    summary = await manager.get_credentials_summary(mode="antigravity", limit=20)

    assert summary["items"][0]["error_codes"] == [429]
    assert summary["items"][0]["model_cooldowns"] == {"gemini-2.5-flash": cooldown_until}
    await manager.close()


@pytest.mark.asyncio
async def test_antigravity_credentials_include_network_context_and_switch_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    await manager.store_credential("one.json", {"access_token": "one", "project_id": "p1"}, mode="antigravity")
    await manager.store_credential("two.json", {"access_token": "two", "project_id": "p2"}, mode="antigravity")
    assert await manager.update_credential_network("one.json", "custom", "http://user:secret@proxy-a:8080")
    assert await manager.update_credential_network("two.json", "direct", None)

    first = await manager.get_credential("one.json", mode="antigravity")
    assert first == {"access_token": "one", "project_id": "p1"}
    await manager.update_credential_state("two.json", {"disabled": True}, mode="antigravity")
    first_selected = await manager.get_next_available_credential(mode="antigravity")
    assert first_selected[0] == "one.json"
    assert first_selected[1]["proxy_mode"] == "custom"
    assert first_selected[1]["proxy_url"] == "http://user:secret@proxy-a:8080"
    await manager.update_credential_state("two.json", {"disabled": False}, mode="antigravity")
    await manager.update_credential_state("one.json", {"disabled": True}, mode="antigravity")
    second_selected = await manager.get_next_available_credential(mode="antigravity")
    assert second_selected[0] == "two.json"
    assert second_selected[1]["proxy_mode"] == "direct"
    assert second_selected[1]["proxy_url"] is None
    await manager.close()


@pytest.mark.asyncio
async def test_credential_selection_filters_cooldowns_before_32_candidate_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("CREDENTIAL_CANDIDATE_LIMIT", "32")
    from src.storage.sqlite_manager import SQLiteManager
    import aiosqlite

    manager = SQLiteManager()
    await manager.initialize()
    async with aiosqlite.connect(manager._db_path) as db:
        await db.executemany(
            """INSERT INTO antigravity_credentials
               (filename, credential_data, model_cooldowns, disabled, rotation_order)
               VALUES (?, ?, ?, 0, ?)""",
            [
                (f"cooled-{index}.json", '{"access_token":"x","project_id":"p"}', '{"m":9999999999}', index)
                for index in range(9999)
            ] + [
                ("available.json", '{"access_token":"ok","project_id":"p"}', "{}", 10000)
            ],
        )
        await db.commit()

    selected = await manager.get_next_available_credential(mode="antigravity", model_name="m")
    assert selected[0] == "available.json"
    await manager.close()


@pytest.mark.asyncio
async def test_antigravity_selection_balances_and_increments_call_count(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    import aiosqlite

    manager = SQLiteManager()
    await manager.initialize()
    for index in range(3):
        await manager.store_credential(
            f"account-{index}.json",
            {"access_token": str(index), "project_id": "project"},
            mode="antigravity",
        )

    selected = [
        (await manager.get_next_available_credential(mode="antigravity"))[0]
        for _ in range(12)
    ]
    async with aiosqlite.connect(manager._db_path) as db:
        rows = await (
            await db.execute(
                "SELECT filename, call_count FROM antigravity_credentials"
            )
        ).fetchall()
    counts = {filename: count for filename, count in rows}

    assert len(selected) == 12
    assert sum(counts.values()) == 12
    assert max(counts.values()) - min(counts.values()) <= 1
    await manager.close()


@pytest.mark.asyncio
async def test_antigravity_selection_backfills_usage_from_billing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    import aiosqlite

    manager = SQLiteManager()
    await manager.initialize()
    await manager.store_credential(
        "busy.json",
        {"access_token": "busy", "project_id": "project"},
        mode="antigravity",
    )
    await manager.store_credential(
        "underused.json",
        {"access_token": "underused", "project_id": "project"},
        mode="antigravity",
    )
    async with aiosqlite.connect(manager._db_path) as db:
        await db.executemany(
            """INSERT INTO billing_daily(
                   billing_date, api_key_id, credential_name, model, currency,
                   success_count, failed_count
               ) VALUES (date('now', '+8 hours'), 'env', ?, 'model', 'CNY', ?, 0)""",
            [("busy.json", 9), ("underused.json", 2)],
        )
        await db.commit()
    await manager.close()

    reloaded = SQLiteManager()
    await reloaded.initialize()
    selected = await reloaded.get_next_available_credential(mode="antigravity")
    async with aiosqlite.connect(reloaded._db_path) as db:
        rows = await (
            await db.execute(
                "SELECT filename, call_count FROM antigravity_credentials"
            )
        ).fetchall()
    counts = {filename: count for filename, count in rows}

    assert selected[0] == "underused.json"
    assert counts == {"busy.json": 9, "underused.json": 3}
    await reloaded.close()


@pytest.mark.asyncio
async def test_antigravity_concurrent_selection_remains_balanced(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    import asyncio
    import aiosqlite

    manager = SQLiteManager()
    await manager.initialize()
    for index in range(4):
        await manager.store_credential(
            f"concurrent-{index}.json",
            {"access_token": str(index), "project_id": "project"},
            mode="antigravity",
        )

    selected = await asyncio.gather(*(
        manager.get_next_available_credential(mode="antigravity")
        for _ in range(40)
    ))
    async with aiosqlite.connect(manager._db_path) as db:
        rows = await (
            await db.execute(
                "SELECT filename, call_count FROM antigravity_credentials"
            )
        ).fetchall()
    counts = {filename: count for filename, count in rows}

    assert all(item is not None for item in selected)
    assert sum(counts.values()) == 40
    assert max(counts.values()) - min(counts.values()) <= 1
    await manager.close()


@pytest.mark.asyncio
async def test_concurrent_antigravity_selection_opens_one_sqlite_connection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage import sqlite_manager as sqlite_module
    from src.storage.sqlite_manager import SQLiteManager
    import asyncio

    manager = SQLiteManager()
    await manager.initialize()
    for index in range(3):
        await manager.store_credential(
            f"fd-{index}.json",
            {"access_token": str(index), "project_id": "project"},
            mode="antigravity",
        )

    original_connect = sqlite_module.aiosqlite.connect
    active_connections = 0
    peak_connections = 0

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            nonlocal active_connections, peak_connections
            opened = await self.connection.__aenter__()
            active_connections += 1
            peak_connections = max(peak_connections, active_connections)
            await asyncio.sleep(0)
            return opened

        async def __aexit__(self, exc_type, exc, traceback):
            nonlocal active_connections
            try:
                return await self.connection.__aexit__(
                    exc_type, exc, traceback
                )
            finally:
                active_connections -= 1

    def tracked_connect(*args, **kwargs):
        return TrackedConnection(original_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite_module.aiosqlite, "connect", tracked_connect)
    await asyncio.gather(*(
        manager.get_next_available_credential(mode="antigravity")
        for _ in range(20)
    ))

    assert peak_connections == 1
    await manager.close()


@pytest.mark.asyncio
async def test_new_antigravity_account_has_bounded_catchup_deficit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import (
        FAIR_ROUTING_CATCHUP_WINDOW,
        SQLiteManager,
    )
    import aiosqlite

    manager = SQLiteManager()
    await manager.initialize()
    await manager.store_credential(
        "existing.json",
        {"access_token": "existing", "project_id": "project"},
        mode="antigravity",
    )
    async with aiosqlite.connect(manager._db_path) as db:
        await db.execute(
            "UPDATE antigravity_credentials SET call_count = 100"
        )
        await db.commit()

    await manager.store_credential(
        "new.json",
        {"access_token": "new", "project_id": "project"},
        mode="antigravity",
    )
    async with aiosqlite.connect(manager._db_path) as db:
        row = await (
            await db.execute(
                "SELECT call_count FROM antigravity_credentials WHERE filename = 'new.json'"
            )
        ).fetchone()

    assert row[0] == 100 - FAIR_ROUTING_CATCHUP_WINDOW
    await manager.close()


@pytest.mark.asyncio
async def test_image_selection_counter_uses_optimistic_atomic_update(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    await manager.store_credential(
        "image.json",
        {"access_token": "image", "project_id": "project"},
        mode="antigravity",
    )

    first = await manager.mark_credential_selected(
        "image.json",
        mode="antigravity",
        expected_call_count=0,
    )
    stale = await manager.mark_credential_selected(
        "image.json",
        mode="antigravity",
        expected_call_count=0,
    )
    states = await manager.get_all_credential_states(mode="antigravity")

    assert first is True
    assert stale is False
    assert states["image.json"]["call_count"] == 1
    await manager.close()
