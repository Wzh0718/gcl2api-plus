"""AGY 基础提示默认值迁移：历史 false 一次性纠正，迁移后尊重用户选择。"""
from unittest.mock import AsyncMock

import pytest

import config
import src.storage_adapter
from src.storage_adapter import StorageAdapter


@pytest.fixture(autouse=True)
async def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    monkeypatch.delenv('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', raising=False)
    monkeypatch.setattr(config, '_config_initialized', False)
    monkeypatch.setattr(config, '_config_cache', {})
    instance = StorageAdapter()
    await instance.initialize()
    monkeypatch.setattr(src.storage_adapter, 'get_storage_adapter', AsyncMock(return_value=instance))
    try:
        yield instance
    finally:
        await instance.close()


async def test_legacy_false_is_migrated_once(adapter):
    # 旧版本面板保存任意配置时写下的 false，并非部署方主动关闭。
    await adapter.set_config('antigravity_system_prompt_enabled', False)

    await config.init_config()

    assert await config.get_antigravity_system_prompt_enabled() is True
    assert await adapter.get_config('antigravity_system_prompt_enabled') is True
    assert await adapter.get_config(config.AGY_BASE_PROMPT_DEFAULT_MARKER) is True


async def test_user_choice_after_migration_is_respected(adapter):
    await adapter.set_config('antigravity_system_prompt_enabled', False)
    await config.init_config()
    assert await config.get_antigravity_system_prompt_enabled() is True

    # 迁移完成后，用户在面板取消勾选保存 false。
    await adapter.set_config('antigravity_system_prompt_enabled', False)
    await config.reload_config()
    assert await config.get_antigravity_system_prompt_enabled() is False

    # 再次启动也不会被迁移覆盖（标记已存在）。
    config._config_initialized = False
    await config.init_config()
    assert await config.get_antigravity_system_prompt_enabled() is False
    assert await adapter.get_config('antigravity_system_prompt_enabled') is False


async def test_explicit_environment_value_is_not_migrated(adapter, monkeypatch):
    await adapter.set_config('antigravity_system_prompt_enabled', False)
    monkeypatch.setenv('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'false')

    await config.init_config()

    assert await config.get_antigravity_system_prompt_enabled() is False
    assert await adapter.get_config('antigravity_system_prompt_enabled') is False
    assert await adapter.get_config(config.AGY_BASE_PROMPT_DEFAULT_MARKER) is None


async def test_no_stored_value_marks_migration_without_writing_flag(adapter):
    await config.init_config()

    assert await config.get_antigravity_system_prompt_enabled() is True
    assert await adapter.get_config('antigravity_system_prompt_enabled') is None
    assert await adapter.get_config(config.AGY_BASE_PROMPT_DEFAULT_MARKER) is True


@pytest.mark.parametrize('stored', ['false', 'off', '0'])
async def test_string_false_is_also_migrated(adapter, stored):
    await adapter.set_config('antigravity_system_prompt_enabled', stored)

    await config.init_config()

    assert await config.get_antigravity_system_prompt_enabled() is True
    assert await adapter.get_config('antigravity_system_prompt_enabled') is True
