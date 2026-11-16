"""Clearing the legacy password must preserve explicitly configured credentials."""
import json
from unittest.mock import AsyncMock

import pytest

import config
from src.storage_adapter import StorageAdapter
from src.panel import config_routes
from src.models import ConfigSaveRequest


@pytest.mark.asyncio
async def test_clear_legacy_password_persists_without_changing_separate_passwords(tmp_path, monkeypatch):
    import src.storage_adapter
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    for variable in ('PASSWORD', 'API_PASSWORD', 'PANEL_PASSWORD'):
        monkeypatch.delenv(variable, raising=False)
    adapter = StorageAdapter()
    await adapter.initialize()
    monkeypatch.setattr(config_routes, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(src.storage_adapter, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(config, '_config_cache', {})
    monkeypatch.setattr(config, '_config_initialized', False)
    try:
        await config_routes.save_config(ConfigSaveRequest(config={
            'password': 'legacy-test', 'api_password': 'api-test', 'panel_password': 'panel-test',
        }), token='test')
        await config_routes.save_config(ConfigSaveRequest(config={'password': ''}), token='test')
        response = json.loads((await config_routes.get_config(token='test')).body)
        assert response['config']['password'] == ''
        assert await adapter.get_config('password') == ''
        assert await config.get_api_password() == 'api-test'
        assert await config.get_panel_password() == 'panel-test'
        monkeypatch.setenv('API_PASSWORD', 'env-api-test')
        monkeypatch.setenv('PANEL_PASSWORD', 'env-panel-test')
        assert await config.get_api_password() == 'env-api-test'
        assert await config.get_panel_password() == 'env-panel-test'
    finally:
        await adapter.close()
