from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.panel import creds as panel


@pytest.mark.asyncio
async def test_quota_delegates_refresh_and_never_writes_an_old_snapshot(monkeypatch):
    adapter = SimpleNamespace(get_credential=AsyncMock(return_value={'access_token': 'old'}),
                              store_credential=AsyncMock())
    current = {'access_token': 'new', 'token': 'new', 'refresh_token': 'new-rt', 'project_id': 'project'}
    credentials = SimpleNamespace(proxy_url=None)
    coordinator = SimpleNamespace(get_account_credentials=AsyncMock(return_value=(current, credentials)))
    monkeypatch.setattr(panel, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(panel, 'credential_manager', coordinator)
    quota = AsyncMock(return_value={'success': False})
    monkeypatch.setattr(panel, 'fetch_quota_info', quota)
    await panel._collect_quota_for_filename('test.json', 'antigravity')
    coordinator.get_account_credentials.assert_awaited_once_with('test.json', 'antigravity')
    adapter.store_credential.assert_not_awaited()
    assert quota.await_args.args[0] == 'new'
