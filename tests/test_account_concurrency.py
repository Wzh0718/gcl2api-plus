import asyncio
from unittest.mock import AsyncMock

import pytest

import config
from src import antigravity_pacing as pacing
from src.credential_manager import CredentialManager


@pytest.fixture
def account_limits(monkeypatch):
    monkeypatch.setattr(config, '_config_cache', {
        'antigravity_account_concurrency:a.json': {
            'request_concurrency': 2, 'image_task_concurrency': 2,
        },
    })
    pacing.reset_state()
    yield
    pacing.reset_state()


@pytest.mark.asyncio
async def test_account_request_limits_are_isolated(account_limits, monkeypatch):
    from src import antigravity_dynamic_warmup as warmup
    monkeypatch.setattr(warmup, 'acquire_dynamic_warmup_slot', AsyncMock(return_value={'acquired': True}))
    monkeypatch.setattr(warmup, 'release_dynamic_warmup_slot', AsyncMock())
    monkeypatch.setattr(config, 'get_antigravity_pacing_enabled_sync', lambda: True)
    monkeypatch.setattr(config, 'get_antigravity_pacing_slot_wait_seconds_sync', lambda: 0.01)
    await pacing.acquire_credential_slot('a.json')
    await pacing.acquire_credential_slot('a.json')
    with pytest.raises(pacing.CredentialSlotWaitTimeout):
        await pacing.acquire_credential_slot('a.json')
    await pacing.acquire_credential_slot('b.json')
    pacing.note_transient_429('a.json')
    pacing.note_success('a.json')
    pacing.note_success('a.json')
    assert pacing._SLOTS['a.json'].limit == 2
    pacing.release_credential_slot('a.json')
    await pacing.acquire_credential_slot('a.json')


@pytest.mark.asyncio
async def test_multiple_image_tasks_wait_and_release_independently(account_limits):
    manager = CredentialManager()
    first = await manager._reserve_available_image_credential(['a.json'])
    second = await asyncio.wait_for(manager._reserve_available_image_credential(['a.json']), 0.1)
    assert first[1] != second[1]
    waiting = asyncio.create_task(manager._reserve_available_image_credential(['a.json']))
    await asyncio.sleep(0)
    assert not waiting.done()
    assert await manager.release_image_credential(*first)
    assert not await manager.release_image_credential(*first)
    assert await asyncio.wait_for(waiting, 0.1) is None
    third = await manager._reserve_available_image_credential(['a.json'])
    assert await manager.release_image_credential(*second)
    assert await manager.release_image_credential(*third)


@pytest.mark.asyncio
async def test_image_limit_lowering_preserves_active_leases(account_limits):
    manager = CredentialManager()
    first = await manager._reserve_available_image_credential(['a.json'])
    second = await asyncio.wait_for(manager._reserve_available_image_credential(['a.json']), 0.1)
    config._config_cache['antigravity_account_concurrency:a.json']['image_task_concurrency'] = 1
    waiting = asyncio.create_task(manager._reserve_available_image_credential(['a.json']))
    await asyncio.sleep(0)
    assert not waiting.done()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await manager.release_image_credential(*first)
    assert await manager.release_image_credential(*second)


@pytest.mark.asyncio
async def test_concurrency_api_persists_and_validates_limits(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI
    from src.panel import creds
    from src.storage.sqlite_manager import SQLiteManager
    from src import storage_adapter

    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    backend = SQLiteManager()
    await backend.initialize()
    adapter = SimpleNamespace(
        _backend=backend,
        get_credential=AsyncMock(return_value={'access_token': 'test'}),
        set_config=backend.set_config,
        get_all_config=backend.get_all_config,
    )
    monkeypatch.setattr(creds, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(storage_adapter, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(config, '_config_cache', {})
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(creds, 'credential_manager', SimpleNamespace(notify_image_concurrency_changed=AsyncMock()))
    app = FastAPI()
    app.include_router(creds.router)
    app.dependency_overrides[creds.verify_panel_token] = lambda: 'test'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.patch('/creds/a.json/concurrency', json={
            'request_concurrency': 4, 'image_task_concurrency': 3,
        })
        assert response.status_code == 200
        assert config.get_antigravity_account_concurrency('a.json')['request_concurrency'] == 4
        for value in (0, 65, 1.5, True, '2'):
            invalid = await client.patch('/creds/a.json/concurrency', json={
                'request_concurrency': value, 'image_task_concurrency': 3,
            })
            assert invalid.status_code == 422
        adapter.get_credential.return_value = None
        missing = await client.patch('/creds/missing.json/concurrency', json={
            'request_concurrency': 4, 'image_task_concurrency': 3,
        })
        assert missing.status_code == 404
        adapter.get_credential.return_value = {'access_token': 'test'}
        inherited = await client.patch('/creds/a.json/concurrency', json={
            'request_concurrency': None, 'image_task_concurrency': 3,
        })
        assert inherited.status_code == 200
        assert inherited.json()['request_concurrency'] == config.get_antigravity_pacing_concurrency_sync()
    reopened = SQLiteManager()
    await reopened.initialize()
    assert await reopened.get_config('antigravity_account_concurrency:a.json') == {
        'request_concurrency': None, 'image_task_concurrency': 3,
    }
