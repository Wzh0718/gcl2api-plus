import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.panel import config_routes as routes
from src.utils import verify_panel_token


@pytest.fixture
def storage(monkeypatch):
    values = {'prompt_fusion_accounts': {'a.json': 'on', 'b.json': 'off'}, 'prompt_fusion_enabled': False}
    async def get(key, default=None):
        return deepcopy(values.get(key, default))
    async def set_value(key, value):
        await asyncio.sleep(0)
        values[key] = deepcopy(value)
        return True
    adapter = AsyncMock()
    adapter.get_config.side_effect = get
    adapter.set_config.side_effect = set_value
    adapter.get_credential_state.return_value = {'disabled': False}
    monkeypatch.setattr(routes, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(routes.config, 'reload_config', AsyncMock())
    return adapter, values


async def test_existing_settings_and_single_account_save(storage):
    adapter, values = storage
    result = await routes.get_prompt_fusion_account('a.json', token='test')
    assert json.loads(result.body) == {'mode': 'on', 'global_enabled': False}
    assert result.headers['cache-control'] == 'no-store'
    await routes.save_prompt_fusion_account('a.json', routes.PromptFusionAccountRequest(mode='inherit'), token='test')
    assert values['prompt_fusion_accounts'] == {'b.json': 'off'}
    assert values['prompt_fusion_enabled'] is False


async def test_concurrent_account_updates_preserve_both(storage):
    _, values = storage
    await asyncio.gather(*(routes.save_prompt_fusion_account(name, routes.PromptFusionAccountRequest(mode='on'), token='test') for name in ['c.json', 'd.json']))
    assert values['prompt_fusion_accounts'] == {'a.json': 'on', 'b.json': 'off', 'c.json': 'on', 'd.json': 'on'}


def test_auth_validation_missing_account_and_failed_write(storage):
    adapter, _ = storage
    app = FastAPI()
    app.include_router(routes.router)
    endpoint = '/config/prompt-fusion/account/a.json'
    with TestClient(app) as client:
        assert client.get(endpoint).status_code in (401, 403)
        assert client.put(endpoint, json={'mode': 'on'}).status_code in (401, 403)
        app.dependency_overrides[verify_panel_token] = lambda: 'test'
        assert client.put(endpoint, json={'mode': 'invalid'}).status_code == 422
        adapter.get_credential_state.return_value = {}
        assert client.get(endpoint).status_code == 404
        assert client.put(endpoint, json={'mode': 'on'}).status_code == 404
        adapter.get_credential_state.return_value = {'disabled': True}
        adapter.set_config.side_effect = None
        adapter.set_config.return_value = False
        assert client.put(endpoint, json={'mode': 'off'}).status_code == 500
