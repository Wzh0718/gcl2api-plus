"""Dynamic pacing must remain visible and editable through panel configuration."""
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import config
from src.models import ConfigSaveRequest
from src.panel import config_routes
from src.storage_adapter import StorageAdapter


LOW = "antigravity_pacing_min_interval_seconds"
HIGH = "antigravity_pacing_max_interval_seconds"


@pytest.fixture
async def storage(tmp_path, monkeypatch):
    import src.storage_adapter
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.delenv("ANTIGRAVITY_PACING_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_PACING_MAX_INTERVAL_SECONDS", raising=False)
    monkeypatch.setattr(config, "_config_cache", {})
    monkeypatch.setattr(config, "_config_initialized", True)
    adapter = StorageAdapter()
    await adapter.initialize()
    monkeypatch.setattr(config_routes, "get_storage_adapter", AsyncMock(return_value=adapter))
    monkeypatch.setattr(src.storage_adapter, "get_storage_adapter", AsyncMock(return_value=adapter))
    yield adapter
    await adapter.close()


async def test_panel_reads_effective_intervals_and_saves_zero(storage, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_PACING_MIN_INTERVAL_SECONDS", "2")
    monkeypatch.setenv("ANTIGRAVITY_PACING_MAX_INTERVAL_SECONDS", "10")
    data = json.loads((await config_routes.get_config(token="test")).body)
    assert (data["config"][LOW], data["config"][HIGH]) == (2, 10)
    for values in ({LOW: "0.25", HIGH: "1.5"}, {LOW: "0", HIGH: "0"}):
        await config_routes.save_config(ConfigSaveRequest(config=values), token="test")
        data = json.loads((await config_routes.get_config(token="test")).body)
        expected = tuple(float(values[key]) for key in (LOW, HIGH))
        assert (data["config"][LOW], data["config"][HIGH]) == expected
        assert (config.get_antigravity_pacing_min_interval_seconds_sync(),
                config.get_antigravity_pacing_max_interval_seconds_sync()) == expected
        assert await storage.get_config(LOW) == expected[0]
        assert await storage.get_config(HIGH) == expected[1]


@pytest.mark.parametrize("key,value", [
    (key, value) for key in (LOW, HIGH)
    for value in (-1, 601, "NaN", "Infinity", True, None, "")
])
async def test_invalid_interval_never_persists_any_configuration(storage, key, value):
    with pytest.raises(HTTPException) as error:
        await config_routes.save_config(
            ConfigSaveRequest(config={key: value, "antigravity_pacing_base_ms": 123}), token="test")
    assert error.value.status_code == 400
    assert await storage.get_config(key) is None
    assert await storage.get_config("antigravity_pacing_base_ms") is None


async def test_partial_updates_validate_against_retained_bound(storage):
    await config_routes.save_config(ConfigSaveRequest(config={LOW: 2, HIGH: 10}), token="test")
    for values in ({LOW: 11}, {HIGH: 1}, {LOW: 10, HIGH: 2}):
        with pytest.raises(HTTPException) as error:
            await config_routes.save_config(ConfigSaveRequest(config=values), token="test")
        assert error.value.status_code == 400
    assert await storage.get_config(LOW) == 2
    assert await storage.get_config(HIGH) == 10
    # A zero upper bound disables dynamic pacing, even if the lower bound is retained.
    await config_routes.save_config(ConfigSaveRequest(config={HIGH: 0}), token="test")
    assert config.get_antigravity_pacing_max_interval_seconds_sync() == 0
