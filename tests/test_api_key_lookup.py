from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_lookup_checks_full_secret_for_all_states_without_changing_quota(tmp_path, monkeypatch):
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    from src.api_keys import ApiKeyService
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    try:
        service = ApiKeyService(manager)
        created = await service.create_key(name='lookup', quota_amount='0')
        key, key_id = created['api_key'], created['item']['id']
        before = await manager.get_api_key_record(key_id)
        assert await service.lookup_key_id(key) == key_id  # exhausted
        assert await service.lookup_key_id(key + 'wrong') is None
        assert await service.lookup_key_id(key.upper()) is None
        assert await service.lookup_key_id('not-a-key') is None
        assert await service.lookup_key_id('gcli_00000000_missing') is None
        assert await manager.get_api_key_record(key_id) == before
        await service.update_key(key_id, status='disabled')
        assert await service.lookup_key_id(key) == key_id
        await service.update_key(key_id, status='active', expires_at='2020-01-01T00:00:00+08:00')
        assert await service.lookup_key_id(key) == key_id
        await service.revoke_key(key_id)
        assert await service.lookup_key_id(key) == key_id
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_lookup_route_requires_panel_session_and_returns_only_id(monkeypatch):
    import src.panel.api_keys as routes
    from src.api_keys import hash_api_key

    key = 'gcli_12345678_example-secret'
    storage = AsyncMock()
    storage.get_api_key_by_prefix.return_value = {'id': 'key-id', 'secret_hash': hash_api_key(key)}
    monkeypatch.setattr(routes, 'get_storage_adapter', AsyncMock(return_value=storage))
    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        rejected = await client.post('/api-keys/lookup', json={'api_key': key})
        assert rejected.status_code in (401, 403)
        storage.get_api_key_by_prefix.assert_not_awaited()
        app.dependency_overrides[routes.verify_panel_token] = lambda: 'panel-session'
        response = await client.post('/api-keys/lookup', json={'api_key': f'  {key}  '})
        assert response.status_code == 200
        assert response.json() == {'id': 'key-id'}
        assert response.headers['cache-control'] == 'no-store'
        assert key not in response.text
        assert (await client.post('/api-keys/lookup', json={'api_key': key + 'wrong'})).json() == {'id': None}
