import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.api_keys import ApiKeyService
from src.credential_manager import CredentialManager
from src.storage_adapter import StorageAdapter
# Import consumers before replacing the factory so lazy imports cannot retain a
# test adapter after its fixture closes.
from src.panel import api_keys as panel_api_keys
import src.antigravity_dynamic_warmup


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    import config
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {'antigravity_quota_guard_enabled': False})
    monkeypatch.setenv('ANTIGRAVITY_QUOTA_GUARD_ENABLED', 'false')
    adapter = StorageAdapter()
    await adapter.initialize()
    for name in ('a.json', 'b.json'):
        await adapter.store_credential(name, {'access_token': 'dummy', 'project_id': 'project'}, mode='antigravity')
    service = ApiKeyService(adapter)
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = adapter
    manager._should_refresh_token = AsyncMock(return_value=False)
    async def ready(name, data):return data
    manager.ensure_antigravity_account_ready = AsyncMock(side_effect=ready)
    import src.storage_adapter
    monkeypatch.setattr(src.storage_adapter, 'get_storage_adapter', AsyncMock(return_value=adapter))
    try:
        yield adapter, service, manager
    finally:
        await adapter.close()


async def test_key_binding_create_update_clear_and_restart(setup):
    adapter, service, _ = setup
    created = await service.create_key(name='user-a', claude_credential_name='a.json')
    key = created['item']['id']
    assert created['item']['claude_credential_name'] == 'a.json'
    assert (await service.update_key(key, claude_credential_name='b.json'))['claude_credential_name'] == 'b.json'
    assert (await service.get_key(key))['claude_credential_name'] == 'b.json'
    assert (await service.update_key(key, claude_credential_name=''))['claude_credential_name'] == ''


@pytest.mark.parametrize('value', ['../a.json', 'folder/a.json', 'folder\\a.json', 'missing.json'])
async def test_invalid_or_unknown_account_cannot_be_saved(setup, value):
    _, service, _ = setup
    with pytest.raises(ValueError):
        await service.create_key(name='invalid', claude_credential_name=value)


async def test_two_keys_are_isolated_for_claude_and_gemini(setup):
    from src.claude_routing import claude_route_scope
    _, service, manager = setup
    first = await service.create_key(name='user-a', claude_credential_name='a.json')
    second = await service.create_key(name='user-b', claude_credential_name='b.json')
    async def select(key, name):
        async with claude_route_scope(key, 'claude-sonnet-4-6'):
            await asyncio.sleep(0)
            results = [await manager.get_valid_credential(mode='antigravity', model_name='claude-sonnet-4-6') for _ in range(3)]
            assert [r[0] for r in results] == [name]*3
    # Verify repeated selection before concurrent tasks: round-robin interleaving alone
    # could accidentally give each task its expected account even without a pin.
    await select(first['item']['id'], 'a.json')
    await select(second['item']['id'], 'b.json')
    await asyncio.gather(select(first['item']['id'], 'a.json'), select(second['item']['id'], 'b.json'))
    async with claude_route_scope(first['item']['id'], 'gemini-2.5-flash'):
        results = [await manager.get_valid_credential(mode='antigravity', model_name='gemini-2.5-flash') for _ in range(4)]
        assert [r[0] for r in results] == ['a.json'] * 4
    unbound = (await service.create_key(name='pool'))['item']['id']
    async with claude_route_scope(unbound, 'gemini-2.5-flash'):
        results = [await manager.get_valid_credential(mode='antigravity', model_name='gemini-2.5-flash') for _ in range(4)]
        assert {r[0] for r in results} == {'a.json', 'b.json'}


@pytest.mark.parametrize('model', ['claude-sonnet-4-6', 'gemini-2.5-flash', 'gemini-3.1-flash-image'])
@pytest.mark.parametrize('reason', ['disabled', 'cooldown', 'excluded', 'deleted', 'health'])
async def test_bound_account_unavailable_never_uses_other_account(setup, reason, model):
    from src.claude_routing import claude_route_scope
    adapter, service, manager = setup
    key = (await service.create_key(name='bound', claude_credential_name='a.json'))['item']['id']
    if reason == 'disabled':await adapter.update_credential_state('a.json', {'disabled': True}, mode='antigravity')
    if reason == 'cooldown':
        import time
        await adapter._backend.set_model_cooldown('a.json', model, time.time()+3600, mode='antigravity')
    if reason == 'deleted':await adapter.delete_credential('a.json', mode='antigravity')
    if reason == 'health':manager.ensure_antigravity_account_ready = AsyncMock(return_value=None)
    async with claude_route_scope(key, model):
        result = await manager.get_valid_credential(mode='antigravity', model_name=model,
                                                  exclude_filenames={'a.json'} if reason == 'excluded' else None)
        assert result is None


async def test_scope_snapshots_binding_for_retries_and_clears_on_exit(setup):
    from src.claude_routing import claude_route_scope
    _, service, manager = setup
    key = (await service.create_key(name='bound', claude_credential_name='a.json'))['item']['id']
    async with claude_route_scope(key, 'claude-sonnet-4-6'):
        await service.update_key(key, claude_credential_name='b.json')
        async with claude_route_scope(key, 'claude-sonnet-4-6'):
            assert (await manager.get_valid_credential(mode='antigravity', model_name='claude-sonnet-4-6'))[0] == 'a.json'
        # Existing model fallback must not escape the original request's account pin.
        assert (await manager.get_valid_credential(mode='antigravity', model_name='gemini-2.5-flash'))[0] == 'a.json'
    async with claude_route_scope(key, 'claude-sonnet-4-6'):
        assert (await manager.get_valid_credential(mode='antigravity', model_name='claude-sonnet-4-6'))[0] == 'b.json'


async def test_stream_binding_survives_task_handoff_and_cancellation(setup):
    from src.claude_routing import bind_claude_stream, bound_claude_credential
    _, service, _ = setup
    key = (await service.create_key(name='stream', claude_credential_name='a.json'))['item']['id']
    seen = []
    @bind_claude_stream
    async def stream_request(body, native=False, headers=None, api_key_id='env'):
        try:
            for _ in range(2):
                seen.append(bound_claude_credential())
                yield 'ok'
        finally:
            seen.append(bound_claude_credential())
    stream = stream_request({'model': 'claude-sonnet-4-6'}, api_key_id=key)
    assert await anext(stream) == 'ok'
    assert bound_claude_credential() is None
    async def finish():
        assert await anext(stream) == 'ok'
        await stream.aclose()
        assert bound_claude_credential() is None
    await asyncio.create_task(finish())
    assert seen == ['a.json']*3


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('model', ['claude-sonnet-4-6', 'claude-haiku-4-5', 'gemini-2.5-flash'])
async def test_openai_route_keeps_bound_account_through_public_api(setup, monkeypatch, stream, model):
    import json
    from types import SimpleNamespace
    from src.api import antigravity as api
    from src.router.antigravity import openai as route
    from src.models import OpenAIChatCompletionRequest
    _, service, manager = setup
    key = (await service.create_key(name='route', claude_credential_name='b.json'))['item']['id']
    selected = []
    async def fake_inner(body, **kwargs):
        account = await manager.get_valid_credential(mode='antigravity', model_name=body['model'])
        selected.append(account[0])
        data = {'response': {'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'ok'}]}, 'finishReason': 'STOP'}]}}
        yield ('data: '+json.dumps(data)+'\n\n').encode()
    monkeypatch.setattr(api, '_stream_request_inner', fake_inner)
    monkeypatch.setattr(api, 'get_antigravity_stream2nostream', AsyncMock(return_value=True))
    result = await route.chat_completions(OpenAIChatCompletionRequest(model=model, stream=stream,
        messages=[{'role': 'user', 'content': 'Task'}]), principal=SimpleNamespace(api_key_id=key))
    if stream:
        async for _ in result.body_iterator:pass
    else:
        assert json.loads(result.body)['choices'][0]['message']['content'] == 'ok'
    assert selected == ['b.json']


async def test_old_database_migrates_binding_column_with_empty_default(setup):
    import aiosqlite
    from src.storage.sqlite_manager import SQLiteManager
    adapter, service, _ = setup
    key = (await service.create_key(name='legacy'))['item']['id']
    async with aiosqlite.connect(adapter._backend._db_path) as db:
        await db.execute('ALTER TABLE api_keys DROP COLUMN claude_credential_name')
        await db.commit()
    fresh = SQLiteManager()
    await fresh.initialize()
    try:
        assert (await fresh.get_api_key_record(key))['claude_credential_name'] == ''
    finally:
        await fresh.close()


async def test_gemini_image_request_keeps_account_binding_and_lease(setup):
    from src.claude_routing import claude_route_scope
    _, service, manager = setup
    key = (await service.create_key(name='image', claude_credential_name='b.json'))['item']['id']
    async with claude_route_scope(key, 'gemini-3.1-flash-image'):
        for _ in range(2):
            result = await manager.get_valid_credential(mode='antigravity', model_name='gemini-3.1-flash-image')
            assert result[0] == 'b.json'
            assert result[1]['_image_lease_id']
            await manager.release_image_credential(result[0], result[1]['_image_lease_id'])
        assert await manager.get_valid_credential(mode='antigravity', model_name='gemini-3.1-flash-image', exclude_filenames={'b.json'}) is None


async def test_account_choices_require_panel_auth_and_only_return_metadata(setup, monkeypatch):
    import httpx
    from fastapi import FastAPI
    panel = panel_api_keys
    adapter, _, _ = setup
    await adapter.update_credential_state('a.json', {'user_email': 'a@example.test', 'disabled': True}, mode='antigravity')
    monkeypatch.setattr(panel, 'get_storage_adapter', AsyncMock(return_value=adapter))
    app = FastAPI()
    app.include_router(panel.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api-keys/accounts')).status_code in (401, 403, 422)
        app.dependency_overrides[panel.verify_panel_token] = lambda: 'ok'
        response = await client.get('/api-keys/accounts')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['items'] == [
            {'filename': 'a.json', 'email': 'a@example.test', 'disabled': True},
            {'filename': 'b.json', 'email': '', 'disabled': False},
        ]
