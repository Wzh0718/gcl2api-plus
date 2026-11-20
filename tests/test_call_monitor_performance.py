import sqlite3
import time
from contextlib import asynccontextmanager

import pytest


@pytest.mark.parametrize("view", ["account", "key"])
async def test_bounded_overview_does_not_scan_old_history(tmp_path, monkeypatch, view):
    from src.storage.sqlite_manager import SQLiteManager
    from src.storage_adapter import StorageAdapter
    import aiosqlite

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    adapter = StorageAdapter()
    await adapter.initialize()
    backend = adapter._backend
    now = time.time()
    db = sqlite3.connect(backend._db_path)
    db.execute(
        """WITH RECURSIVE n(x) AS (
        SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<60000
    ) INSERT INTO call_records(request_id,created_at,credential_name,api_key_id,model_name,success)
      SELECT 'old-'||x, ?, 'account-'||(x%10), 'key-'||(x%3), 'model', 1 FROM n""",
        (now - 100 * 86400,),
    )
    db.executemany(
        """INSERT INTO call_records(
        request_id,created_at,credential_name,api_key_id,model_name,success
    ) VALUES(?,?,?,?,?,?)""",
        [(f"new-{i}", now, "recent", "recent-key", "model", 1) for i in range(20)],
    )
    db.commit()
    db.close()
    steps = 0
    original_connect = aiosqlite.connect

    def progress():
        nonlocal steps
        steps += 1000
        return 0

    @asynccontextmanager
    async def measured_connect(*args, **kwargs):
        async with original_connect(*args, **kwargs) as connection:
            await connection.set_progress_handler(progress, 1000)
            yield connection

    monkeypatch.setattr(aiosqlite, "connect", measured_connect)
    try:
        if view == "account":
            rows = await backend.aggregate_call_records(
                group_by="credential_name", since_ts=now - 60
            )
        else:
            rows = await backend.aggregate_key_account_usage(since_ts=now - 60)
        assert sum(row["total"] for row in rows) == 20
        # Bound database work, rather than relying on hardware-dependent timing.
        assert steps < 30000, f"Recent overview scanned old history ({steps} VM steps)"
    finally:
        await adapter.close()


async def test_realtime_reads_once_without_flushing(tmp_path, monkeypatch):
    from src.call_monitor import CallMonitor
    from src.storage_adapter import StorageAdapter

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    adapter = StorageAdapter()
    await adapter.initialize()
    backend = adapter._backend
    count = backend.count_call_records
    count_calls = []

    async def count_records(**filters):
        count_calls.append(filters)
        return await count(**filters)

    async def unexpected_flush():
        pytest.fail("Reading the monitor must not wait for queued writes")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(backend, "count_call_records", count_records)
            patch.setattr(backend, "flush_call_records", unexpected_flush)
            monitor = CallMonitor(adapter)
            await monitor.preload()
            page = await monitor.realtime_page(failed_only=True)
            assert page["total"] == page["counters"]["failed"] == 0
            assert len(count_calls) == 1
    finally:
        await adapter.close()
