import asyncio
import json
import time

import aiosqlite
import pytest

from src.storage.sqlite_manager import SQLiteManager


async def make_manager(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    await manager.initialize()
    assert await manager.store_credential(
        "account.json",
        {"access_token": "token", "project_id": "project"},
        mode=mode,
    )
    return manager


async def read_cooldowns(manager, mode):
    table = manager._get_table_name(mode)
    async with aiosqlite.connect(manager._db_path) as db:
        async with db.execute(
            f"SELECT model_cooldowns FROM {table} WHERE filename = ?",
            ("account.json",),
        ) as cursor:
            row = await cursor.fetchone()
    return json.loads(row[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["geminicli", "antigravity"])
async def test_short_cooldown_does_not_replace_existing_long_deadline(
    tmp_path, monkeypatch, mode
):
    manager = await make_manager(tmp_path, monkeypatch, mode)
    try:
        now = int(time.time())
        long_deadline = now + 3600.1234567
        assert await manager.set_model_cooldown(
            "account.json", "gemini-2.5-flash", long_deadline, mode=mode
        )
        assert await manager.set_model_cooldown(
            "account.json", "gemini-2.5-flash", now + 60, mode=mode
        )
        assert await read_cooldowns(manager, mode) == {
            "gemini-2.5-flash": long_deadline
        }
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["geminicli", "antigravity"])
async def test_concurrent_model_cooldowns_preserve_both_keys(
    tmp_path, monkeypatch, mode
):
    manager = await make_manager(tmp_path, monkeypatch, mode)
    try:
        deadlines = {
            "gemini-2.5-flash": int(time.time()) + 60,
            "claude-sonnet-4.5": int(time.time()) + 3600,
        }
        original_fetchone = aiosqlite.Cursor.fetchone
        both_reads_finished = asyncio.Event()
        reads = 0

        async def synchronize_old_read_modify_write(cursor):
            nonlocal reads
            row = await original_fetchone(cursor)
            # Real SQLite reads are synchronized only to expose the old lost-update
            # window deterministically; atomic UPDATE implementations need no read.
            if tuple(column[0] for column in cursor.description or ()) == (
                "model_cooldowns",
            ):
                reads += 1
                if reads == 2:
                    both_reads_finished.set()
                await asyncio.wait_for(both_reads_finished.wait(), timeout=5)
            return row

        with monkeypatch.context() as patch:
            patch.setattr(aiosqlite.Cursor, "fetchone", synchronize_old_read_modify_write)
            results = await asyncio.gather(
                *(
                    manager.set_model_cooldown(
                        "account.json", model, deadline, mode=mode
                    )
                    for model, deadline in deadlines.items()
                )
            )
        assert results == [True, True]
        assert await read_cooldowns(manager, mode) == deadlines
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["geminicli", "antigravity"])
async def test_explicit_none_removes_only_requested_model(tmp_path, monkeypatch, mode):
    manager = await make_manager(tmp_path, monkeypatch, mode)
    try:
        deadline = int(time.time()) + 3600
        for model in ("gemini-2.5-flash", "claude-sonnet-4.5"):
            assert await manager.set_model_cooldown(
                "account.json", model, deadline, mode=mode
            )
        assert await manager.set_model_cooldown(
            "account.json", "gemini-2.5-flash", None, mode=mode
        )
        assert await read_cooldowns(manager, mode) == {
            "claude-sonnet-4.5": deadline
        }
        assert await manager.set_model_cooldown(
            "account.json", "already-absent", None, mode=mode
        )
        assert not await manager.set_model_cooldown(
            "missing.json", "claude-sonnet-4.5", deadline, mode=mode
        )
    finally:
        await manager.close()
