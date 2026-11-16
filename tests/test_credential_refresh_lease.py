"""Real shared SQLite coordination; OAuth is simulated, never sent to Google."""
import asyncio
import multiprocessing
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from src.credential_manager import CredentialManager
from src.google_oauth_api import Credentials
from src.storage.sqlite_manager import SQLiteManager


def _data():
    return {'access_token': 'old', 'refresh_token': 'rt',
            'expiry': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}


def _open(db_path):
    # Schema is initialized by parent; each process has its own manager/connection.
    db = SQLiteManager()
    db._db_path = db_path
    db._initialized = True
    return db


def _worker(db_path, start, ready, results, calls):
    async def run():
        db = _open(db_path)
        manager = CredentialManager()
        manager._initialized = True
        manager._storage_adapter = db
        async def refresh(self):
            with calls.get_lock():
                calls.value += 1
            await asyncio.sleep(0.3)
            self.access_token = 'shared-result'
            self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        Credentials.refresh = refresh
        ready.put(True)
        await asyncio.to_thread(start.wait, 10)
        result = await manager._refresh_token(_data(), 'a')
        results.put(result and result['access_token'])
        await manager.close()
    asyncio.run(run())


def _crash_owner(db_path):
    async def run():
        assert await _open(db_path).acquire_refresh_lease('a', 'crashed', ttl=0.3) == 'acquired'
    asyncio.run(run())
    os._exit(0)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    backend = SQLiteManager()
    await backend.initialize()
    await backend.store_credential('a', _data())
    yield backend
    await backend.close()


@pytest.mark.asyncio
async def test_four_independent_processes_send_only_one_refresh(db):
    ctx = multiprocessing.get_context('spawn')
    start, ready, results, calls = ctx.Event(), ctx.Queue(), ctx.Queue(), ctx.Value('i', 0)
    workers = [ctx.Process(target=_worker, args=(db._db_path, start, ready, results, calls)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert await asyncio.to_thread(ready.get, True, 15)
        start.set()
        output = [await asyncio.to_thread(results.get, True, 15) for _ in workers]
        for worker in workers:
            await asyncio.to_thread(worker.join, 5)
            assert worker.exitcode == 0
        assert calls.value == 1
        assert output == ['shared-result'] * 4
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
            worker.join(5)
        ready.close()
        results.close()


@pytest.mark.asyncio
async def test_crashed_process_lease_expires(db):
    ctx = multiprocessing.get_context('spawn')
    worker = ctx.Process(target=_crash_owner, args=(db._db_path,))
    worker.start()
    try:
        await asyncio.to_thread(worker.join, 10)
        assert worker.exitcode == 0
        await asyncio.sleep(0.35)
        assert await db.acquire_refresh_lease('a', 'replacement') == 'acquired'
        assert not await db.renew_refresh_lease('a', 'crashed')
    finally:
        if worker.is_alive():
            worker.kill()
        worker.join(5)


@pytest.mark.asyncio
async def test_expiry_fences_old_success_and_error_and_release(db):
    expected = await db.get_credential('a')
    assert await db.acquire_refresh_lease('a', 'old', ttl=0.05) == 'acquired'
    await asyncio.sleep(0.08)
    assert not await db.renew_refresh_lease('a', 'old')
    assert not await db.compare_and_swap_credential('a', expected, {'access_token': 'bad'}, lease_owner='old')
    assert await db.acquire_refresh_lease('a', 'new') == 'acquired'
    assert not await db.compare_and_swap_credential('a', expected, expected, lease_owner='old', state_updates={'disabled': True})
    await db.release_refresh_lease('a', 'old')
    assert await db.acquire_refresh_lease('a', 'third') == 'busy'
    assert not (await db.get_credential_state('a'))['disabled']
    assert await db.compare_and_swap_credential('a', expected, expected, lease_owner='new')


@pytest.mark.asyncio
async def test_renewal_and_failure_cooldown(db):
    assert await db.acquire_refresh_lease('a', 'one', ttl=0.15) == 'acquired'
    assert await db.renew_refresh_lease('a', 'one', ttl=1)
    await asyncio.sleep(0.2)
    assert await db.acquire_refresh_lease('a', 'two') == 'busy'
    await db.release_refresh_lease('a', 'one', cooldown=0.1)
    assert await db.acquire_refresh_lease('a', 'two') == 'cooldown'
    await asyncio.sleep(0.15)
    assert await db.acquire_refresh_lease('a', 'two') == 'acquired'
    # Other accounts/modes do not block each other.
    assert await db.acquire_refresh_lease('b', 'three') == 'acquired'
    assert await db.acquire_refresh_lease('a', 'four', 'antigravity') == 'acquired'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['acquire', 'renew', 'timeout'])
async def test_coordination_failure_stops_oauth_and_prevents_writes(db, monkeypatch, failure):
    from src import credential_manager as module
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = db
    refresh = AsyncMock(side_effect=lambda: None)
    entered = asyncio.Event()
    async def slow_refresh(self):
        entered.set()
        await refresh()
        await asyncio.Event().wait()
    monkeypatch.setattr(Credentials, 'refresh', slow_refresh)
    monkeypatch.setattr(module, 'REFRESH_LEASE_RENEW_INTERVAL', 0.01)
    monkeypatch.setattr(module, 'REFRESH_OPERATION_TIMEOUT', 0.1)
    if failure == 'acquire':
        monkeypatch.setattr(db, 'acquire_refresh_lease', AsyncMock(side_effect=RuntimeError('database unavailable')))
    elif failure == 'renew':
        monkeypatch.setattr(db, 'renew_refresh_lease', AsyncMock(side_effect=[True, False]))
    expected = await db.get_credential('a')
    assert await manager._refresh_token(expected, 'a') is None
    assert await db.get_credential('a') == expected
    if failure == 'acquire':
        refresh.assert_not_awaited()
    else:
        refresh.assert_awaited_once()
    assert not manager._refresh_tasks
    await manager.close()


@pytest.mark.asyncio
async def test_multiple_managers_share_failed_attempt_cooldown(db, monkeypatch):
    calls = 0
    async def fail(self):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        raise RuntimeError('temporary upstream failure')
    monkeypatch.setattr(Credentials, 'refresh', fail)
    managers = [CredentialManager() for _ in range(5)]
    for manager in managers:
        manager._initialized = True
        manager._storage_adapter = db
    assert await asyncio.gather(*(m._refresh_token(_data(), 'a') for m in managers)) == [None] * 5
    assert calls == 1
    for manager in managers:
        await manager.close()
