import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.credential_manager import CredentialManager
from src import credential_manager as module


@pytest.fixture
def harness(monkeypatch):
    data = {'access_token': 'old', 'refresh_token': 'old-rt', 'proxy_mode': 'direct',
            'expiry': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    async def get(*args, **kwargs):
        return dict(data)
    async def store(filename, expected, value, **kwargs):
        if expected != data:
            return False
        data.update(value)
        return True
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = SimpleNamespace(get_credential=AsyncMock(side_effect=get),
                                                acquire_refresh_lease=AsyncMock(return_value='acquired'),
                                                renew_refresh_lease=AsyncMock(return_value=True),
                                                release_refresh_lease=AsyncMock(),
                                                compare_and_swap_credential=AsyncMock(side_effect=store))
    creds = SimpleNamespace(access_token='new', refresh_token='rotated', oauth_client_key='client',
                            expires_at=datetime.now(timezone.utc) + timedelta(hours=1), refresh=AsyncMock())
    monkeypatch.setattr(module.Credentials, 'from_dict', lambda value: creds)
    return manager, creds, data


@pytest.mark.asyncio
async def test_concurrent_refreshes_share_one_request_and_persist_rotation(harness):
    manager, creds, data = harness
    stale = dict(data)
    results = await asyncio.gather(*(manager._refresh_token(stale, 'a', 'antigravity') for _ in range(20)))
    creds.refresh.assert_awaited_once()
    manager._storage_adapter.compare_and_swap_credential.assert_awaited_once()
    assert data['refresh_token'] == 'rotated'
    assert data['oauth_client_key'] == 'client'
    assert all(r['access_token'] == 'new' for r in results)
    results[0]['access_token'] = 'changed'
    assert results[1]['access_token'] == 'new'
    await manager._refresh_token(stale, 'a', 'antigravity')
    creds.refresh.assert_awaited_once()  # stale caller reloads saved result


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_abort_rotation(harness):
    manager, creds, data = harness
    started, release = asyncio.Event(), asyncio.Event()
    async def refresh():
        started.set()
        await release.wait()
    creds.refresh.side_effect = refresh
    first = asyncio.create_task(manager._refresh_token(data, 'a', 'antigravity'))
    await started.wait()
    second = asyncio.create_task(manager._refresh_token(data, 'a', 'antigravity'))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second)['refresh_token'] == 'rotated'
    creds.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_refresh_is_shared_then_can_be_retried(harness):
    manager, creds, data = harness
    creds.refresh.side_effect = RuntimeError('temporary')
    manager._cool_dead_proxy_node = AsyncMock()
    results = await asyncio.gather(*(manager._refresh_token(data, 'a', 'antigravity') for _ in range(5)))
    assert results == [None] * 5
    creds.refresh.assert_awaited_once()
    assert not manager._refresh_tasks
    creds.refresh.side_effect = None
    assert await manager._refresh_token(data, 'a', 'antigravity')
    assert creds.refresh.await_count == 2


@pytest.mark.asyncio
async def test_deleted_credential_is_not_recreated(harness):
    manager, creds, data = harness
    manager._storage_adapter.get_credential.side_effect = None
    manager._storage_adapter.get_credential.return_value = None
    assert await manager._refresh_token(data, 'a', 'antigravity') is None
    creds.refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining,expected', [(600, False), (301, False), (299, True)])
async def test_official_five_minute_refresh_window(remaining, expected):
    manager = CredentialManager()
    data = {'access_token': 'token', 'expiry': (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat()}
    assert await manager._should_refresh_token(data, 'antigravity') is expected
