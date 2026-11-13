import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import config
from src.api_keys import ApiKeyService, ApiKeyPrincipal
from src.storage_adapter import StorageAdapter
from src.claude_routing import claude_route_scope, enforce_key_model, key_prompt_fusion_mode
from src.prompt_fusion import enabled
from src.panel import api_keys as panel
from src.router.antigravity import openai, anthropic, gemini
from src.api import antigravity
from src.utils import authenticate_antigravity_key, verify_panel_token


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {})
    adapter = StorageAdapter()
    await adapter.initialize()
    import src.storage_adapter
    monkeypatch.setattr(src.storage_adapter, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(panel, 'get_storage_adapter', AsyncMock(return_value=adapter))
    try:
        yield adapter, ApiKeyService(adapter)
    finally:
        await adapter.close()


async def test_create_update_partial_patch_and_restart(setup):
    adapter, service = setup
    item = (await service.create_key(name='restricted', prompt_fusion_mode='on', allowed_model='claude-sonnet-4-6'))['item']
    assert item['prompt_fusion_mode'] == 'on'
    assert item['allowed_model'] == 'claude-sonnet-4-6'
    assert 'secret_hash' not in item and 'secret_ciphertext' not in item
    item = await service.update_key(item['id'], name='renamed')
    assert item['prompt_fusion_mode'] == 'on' and item['allowed_model'] == 'claude-sonnet-4-6'
    await adapter.close()
    await adapter.initialize()
    item = await service.get_key(item['id'])
    assert item['prompt_fusion_mode'] == 'on'
    item = await service.update_key(item['id'], prompt_fusion_mode='inherit', allowed_model='')
    assert item['allowed_model'] == '' and item['prompt_fusion_mode'] == 'inherit'


async def test_existing_database_migration_is_idempotent(setup):
    adapter, service = setup
    item = (await service.create_key(name='legacy'))['item']
    path = adapter._backend._db_path
    await adapter.close()
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE api_keys DROP COLUMN prompt_fusion_mode')
        db.execute('ALTER TABLE api_keys DROP COLUMN allowed_model')
    for _ in range(2):
        await adapter.initialize()
        record = await adapter.get_api_key_record(item['id'])
        assert record['prompt_fusion_mode'] == 'inherit' and record['allowed_model'] == ''
        assert record['name'] == 'legacy'
        await adapter.close()


@pytest.mark.parametrize('key_mode,account_mode,global_on,expected', [
    ('on', 'off', False, True), ('off', 'on', True, False),
    ('inherit', 'on', False, True), ('inherit', 'off', True, False),
    ('inherit', 'inherit', True, True), ('inherit', 'inherit', False, False)])
async def test_key_account_global_priority(setup, key_mode, account_mode, global_on, expected):
    _, service = setup
    config._config_cache.update(prompt_fusion_enabled=global_on, prompt_fusion_accounts={'a.json': account_mode})
    item = (await service.create_key(name='priority', prompt_fusion_mode=key_mode))['item']
    async with claude_route_scope(item['id'], 'gemini-2.5-flash'):
        assert await enabled('a.json') is expected
    assert key_prompt_fusion_mode() == 'inherit'


async def test_two_keys_share_account_without_sharing_policy(setup):
    _, service = setup
    a = (await service.create_key(name='on', prompt_fusion_mode='on'))['item']
    b = (await service.create_key(name='off', prompt_fusion_mode='off'))['item']
    async def check(item, expected):
        async with claude_route_scope(item['id'], 'gemini-2.5-flash'):
            await asyncio.sleep(0)
            assert await enabled('shared.json') is expected
    await asyncio.gather(check(a, True), check(b, False))


async def test_model_limit_blocks_fallback_and_optimizer_bypass(setup):
    _, service = setup
    item = (await service.create_key(name='restricted', allowed_model='gemini-2.5-flash'))['item']
    async with claude_route_scope(item['id'], 'gemini-2.5-flash'):
        enforce_key_model('gemini-2.5-flash')
        assert antigravity._next_fallback_model(['gemini-2.5-flash', 'other'], 'gemini-2.5-flash') is None
        with pytest.raises(HTTPException) as exc:
            await antigravity.wrap_cli_request({}, 'claude-sonnet-4-6', 'project', skip_prompt_fusion=True)
        assert exc.value.status_code == 403
    assert antigravity._next_fallback_model(['gemini-2.5-flash', 'other'], 'gemini-2.5-flash') == 'other'


@pytest.mark.parametrize('protocol,stream', [(p,s) for p in ('openai','anthropic','gemini') for s in (False,True)])
async def test_protocols_reject_model_before_upstream(setup, monkeypatch, protocol, stream):
    _, service = setup
    item = (await service.create_key(name='restricted', allowed_model='gemini-2.5-flash'))['item']
    app = FastAPI()
    app.include_router({'openai':openai, 'anthropic':anthropic, 'gemini':gemini}[protocol].router)
    app.dependency_overrides[authenticate_antigravity_key] = lambda: ApiKeyPrincipal(item['id'],'test','managed','test')
    selection = AsyncMock(side_effect=AssertionError('Must not select an account'))
    monkeypatch.setattr(antigravity.credential_manager, 'get_valid_credential', selection)
    if protocol == 'openai':
        path='/antigravity/v1/chat/completions'
        payload={'model':'claude-sonnet-4-6','messages':[{'role':'user','content':'write an example'}],'stream':stream}
    elif protocol == 'anthropic':
        path='/antigravity/v1/messages'
        payload={'model':'claude-sonnet-4-6','messages':[{'role':'user','content':'write an example'}],'max_tokens':64,'stream':stream}
    else:
        path='/antigravity/v1beta/models/claude-sonnet-4-6:'+('streamGenerateContent' if stream else 'generateContent')
        payload={'contents':[{'role':'user','parts':[{'text':'write an example'}]}]}
    with TestClient(app) as client:
        response = client.post(path, json=payload)
    assert response.status_code == 403, response.text
    selection.assert_not_called()


async def test_panel_fields_and_catalog_require_auth(setup):
    app = FastAPI()
    app.include_router(panel.router)
    with TestClient(app) as client:
        assert client.get('/api-keys/models').status_code in (401,403)
        app.dependency_overrides[verify_panel_token] = lambda:'test'
        models=client.get('/api-keys/models').json()['items']
        assert 'claude-sonnet-4-6' in models
        response=client.post('/api-keys',json={'name':'panel','prompt_fusion_mode':'on','allowed_model':'claude-sonnet-4-6'})
        assert response.status_code == 201,response.text
        item=response.json()['item']
        assert item['prompt_fusion_mode']=='on' and item['allowed_model']=='claude-sonnet-4-6'
        response=client.patch('/api-keys/'+item['id'],json={'name':'renamed'})
        assert response.json()['allowed_model']=='claude-sonnet-4-6'
        for payload in ({'prompt_fusion_mode':'invalid'},{'prompt_fusion_mode':None},{'allowed_model':'../bad'},{'allowed_model':None}):
            assert client.patch('/api-keys/'+item['id'],json=payload).status_code in (400,422)
