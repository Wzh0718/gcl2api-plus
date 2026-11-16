import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from src.credential_manager import CredentialManager
from src.google_oauth_api import Credentials, TokenError
from src.storage.sqlite_manager import SQLiteManager
from src.credential_revision import REVISION_KEY


def credential(token='old', *, fresh=False):
    return {'access_token': token, 'token': token, 'refresh_token': token + '-rt',
            'project_id': 'project', 'custom_metadata': 'keep',
            'expiry': (datetime.now(timezone.utc) + timedelta(hours=1 if fresh else -1)).isoformat()}


@pytest_asyncio.fixture
async def account(tmp_path, monkeypatch):
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    db = SQLiteManager()
    await db.initialize()
    assert await db.store_credential('test.json', credential(), mode='antigravity')
    adapter = SimpleNamespace(get_credential=db.get_credential, store_credential=db.store_credential,
                              compare_and_swap_credential=db.compare_and_swap_credential,
                              acquire_refresh_lease=db.acquire_refresh_lease,
                              renew_refresh_lease=db.renew_refresh_lease,
                              release_refresh_lease=db.release_refresh_lease,
                              resolve_credential_network=AsyncMock(return_value={'proxy_mode': 'direct'}))
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = adapter
    try:
        yield db, manager, adapter
    finally:
        await manager.close()
        await db.close()


@pytest.mark.asyncio
async def test_only_one_of_two_storage_writers_can_commit(account):
    db, _, _ = account
    expected = await db.get_credential('test.json', 'antigravity')
    results = await asyncio.gather(*(db.compare_and_swap_credential(
        'test.json', expected, credential(f'new-{i}', fresh=True), 'antigravity'
    ) for i in range(2)))
    assert sum(results) == 1
    saved = await db.get_credential('test.json', 'antigravity')
    assert saved[REVISION_KEY] != expected[REVISION_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['reauthorize', 'delete', 'delete_recreate_same'])
async def test_old_refresh_cannot_overwrite_new_authorization_or_recreate(account, monkeypatch, action):
    db, manager, _ = account
    before = await db.get_credential('test.json', 'antigravity')
    entered, release = asyncio.Event(), asyncio.Event()
    async def refresh(self):
        entered.set()
        await release.wait()
        self.access_token = 'stale-refresh-result'
        self.refresh_token = 'stale-rotated-rt'
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(Credentials, 'refresh', refresh)
    task = asyncio.create_task(manager._refresh_token(before, 'test.json', 'antigravity'))
    await entered.wait()
    if action.startswith('delete'):
        await db.delete_credential('test.json', 'antigravity')
    if action == 'reauthorize':
        await db.store_credential('test.json', credential('reauthorized', fresh=True), 'antigravity')
    elif action == 'delete_recreate_same':
        await db.store_credential('test.json', before, 'antigravity')
    current = await db.get_credential('test.json', 'antigravity')
    release.set()
    result = await task
    assert await db.get_credential('test.json', 'antigravity') == current
    if action == 'reauthorize':
        assert result['access_token'] == 'reauthorized'
    else:
        assert result is None


@pytest.mark.asyncio
async def test_old_invalid_grant_cannot_disable_reauthorized_account(account, monkeypatch):
    db, manager, _ = account
    before = await db.get_credential('test.json', 'antigravity')
    entered, release = asyncio.Event(), asyncio.Event()
    async def refresh(self):
        entered.set()
        await release.wait()
        raise TokenError('revoked', status_code=400, error_code='invalid_grant')
    monkeypatch.setattr(Credentials, 'refresh', refresh)
    task = asyncio.create_task(manager._refresh_token(before, 'test.json', 'antigravity'))
    await entered.wait()
    await db.store_credential('test.json', credential('reauthorized', fresh=True), 'antigravity')
    current = await db.get_credential('test.json', 'antigravity')
    release.set()
    assert (await task)['access_token'] == 'reauthorized'
    assert await db.get_credential('test.json', 'antigravity') == current
    state = await db.get_credential_state('test.json', 'antigravity')
    assert not state['disabled']
    assert not state['error_codes']


@pytest.mark.asyncio
async def test_current_invalid_grant_is_disabled_and_diagnosed_atomically(account, monkeypatch):
    db, manager, _ = account
    async def refresh(self):
        raise TokenError('revoked', status_code=400, error_code='invalid_grant')
    monkeypatch.setattr(Credentials, 'refresh', refresh)
    before = await db.get_credential('test.json', 'antigravity')
    assert await manager._refresh_token(before, 'test.json', 'antigravity') is None
    state = await db.get_credential_state('test.json', 'antigravity')
    assert state['disabled']
    assert state['error_codes'] == [400]
    import aiosqlite
    import json
    async with aiosqlite.connect(db._db_path) as conn:
        async with conn.execute("SELECT error_messages FROM antigravity_credentials WHERE filename = ?", ('test.json',)) as cursor:
            row = await cursor.fetchone()
    assert '400' in json.loads(row[0])


@pytest.mark.asyncio
async def test_business_quota_and_managed_helpers_share_refresh(account, monkeypatch):
    from src.panel import creds as panel
    db, manager, adapter = account
    before = await db.get_credential('test.json', 'antigravity')
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def refresh(self):
        calls.append(self.refresh_token)
        entered.set()
        await release.wait()
        self.access_token = 'new'
        self.refresh_token = 'rotated'
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(Credentials, 'refresh', refresh)
    monkeypatch.setattr(panel, 'credential_manager', manager)
    monkeypatch.setattr(panel, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(panel, 'fetch_quota_info', AsyncMock(return_value={'success': False}))
    business = asyncio.create_task(manager._refresh_token(before, 'test.json', 'antigravity'))
    await entered.wait()
    quota = asyncio.create_task(panel._collect_quota_for_filename('test.json', 'antigravity'))
    account_load = asyncio.create_task(manager.get_account_credentials('test.json', 'antigravity'))
    await asyncio.sleep(0)
    release.set()
    _, _, (_, bound) = await asyncio.gather(business, quota, account_load)
    assert calls == ['old-rt']
    assert not await bound.refresh_if_needed()  # nested userinfo uses the coordinator
    assert calls == ['old-rt']
    saved = await db.get_credential('test.json', 'antigravity')
    assert saved['refresh_token'] == 'rotated'
    assert saved['access_token'] == saved['token'] == 'new'
    assert saved['custom_metadata'] == 'keep'


@pytest.mark.asyncio
async def test_managed_credentials_adopt_reauthorization_without_refresh(account, monkeypatch):
    db, manager, _ = account
    await db.store_credential('test.json', credential('first', fresh=True), 'antigravity')
    _, bound = await manager.get_account_credentials('test.json', 'antigravity')
    await db.store_credential('test.json', credential('second', fresh=True), 'antigravity')
    assert await bound.refresh_if_needed()
    assert bound.access_token == 'second'
    assert bound.refresh_token == 'second-rt'


@pytest.mark.asyncio
async def test_project_probe_cannot_write_over_reauthorization(account, monkeypatch):
    from src.panel import creds as panel
    db, manager, adapter = account
    await db.store_credential('test.json', credential('first', fresh=True), 'antigravity')
    entered, release = asyncio.Event(), asyncio.Event()
    async def discover(**kwargs):
        entered.set()
        await release.wait()
        return 'old-authorization-project', 'pro', None
    monkeypatch.setattr(panel, 'credential_manager', manager)
    monkeypatch.setattr(panel, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(panel, 'get_antigravity_api_url', AsyncMock(return_value='https://example.test'))
    monkeypatch.setattr(panel, 'fetch_project_id_and_tier', discover)
    task = asyncio.create_task(panel.verify_credential_project_common('test.json', 'antigravity'))
    await entered.wait()
    await db.store_credential('test.json', credential('new-authorization', fresh=True), 'antigravity')
    await db.update_credential_state('test.json', {'disabled': True}, 'antigravity')
    current = await db.get_credential('test.json', 'antigravity')
    release.set()
    assert (await task).status_code == 409
    assert await db.get_credential('test.json', 'antigravity') == current
    assert (await db.get_credential_state('test.json', 'antigravity'))['disabled']


@pytest.mark.asyncio
async def test_legacy_row_without_revision_upgrades_on_first_conditional_write(account):
    import aiosqlite
    import json
    db, _, _ = account
    legacy = credential()
    async with aiosqlite.connect(db._db_path) as conn:
        await conn.execute('UPDATE antigravity_credentials SET credential_data = ? WHERE filename = ?',
                           (json.dumps(legacy), 'test.json'))
        await conn.commit()
    expected = await db.get_credential('test.json', 'antigravity')
    assert REVISION_KEY not in expected
    assert await db.compare_and_swap_credential('test.json', expected, credential('new', fresh=True), 'antigravity')
    assert (await db.get_credential('test.json', 'antigravity'))[REVISION_KEY]
    assert not await db.compare_and_swap_credential('test.json', expected, legacy, 'antigravity')

@pytest.mark.asyncio
async def test_rejected_token_refresh_is_singleflight_and_reuses_new_authorization(account, monkeypatch):
    db, manager, _ = account
    data = credential('rejected', fresh=True)
    await db.store_credential('test.json', data, 'antigravity')
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def refresh(self):
        calls.append(self.access_token)
        entered.set()
        await release.wait()
        self.access_token = 'recovered'
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(Credentials, 'refresh', refresh)
    first = asyncio.create_task(manager._refresh_token(data, 'test.json', 'antigravity', rejected_access_token='rejected'))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(manager._refresh_token(data, 'test.json', 'antigravity', rejected_access_token='rejected'))
    release.set()
    results = await asyncio.gather(first, second)
    assert calls == ['rejected']
    assert all(r['access_token'] == 'recovered' for r in results)
    newer = await manager._refresh_token(data, 'test.json', 'antigravity', rejected_access_token='rejected')
    assert newer['access_token'] == 'recovered' and calls == ['rejected']
