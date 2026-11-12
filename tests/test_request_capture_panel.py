import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import config
from src.capture_config import capture_policies
from src.models import ConfigSaveRequest
from src.panel import request_capture as panel
from src.panel import config_routes
from src.request_capture import get_capture_store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUEST_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("REQUEST_CAPTURE_DIR", str(tmp_path / "capture"))
    monkeypatch.setenv("REQUEST_CAPTURE_MIN_FREE_MB", "0")
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {})
    app = FastAPI()
    app.include_router(panel.router)
    app.include_router(config_routes.router)
    return app, get_capture_store()


@pytest.mark.asyncio
async def test_panel_auth_filters_and_bounded_export(setup, monkeypatch):
    import time
    app, store = setup
    event = {"request_id": "r1", "credential_name": "account-a", "created_at": time.time(), "details": {"client": {"messages": ["hello"]}}}
    store.write("normal", event, capture_policies()["normal"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for route in ("status", "records", "export"):
            assert (await client.get(f"/call-monitor/capture/{route}")).status_code in (401, 403, 422)
        app.dependency_overrides[panel.verify_panel_token] = lambda: "ok"
        response = await client.get("/call-monitor/capture/records?policy=normal&request_id=r1")
        assert response.status_code == 200 and response.json()["records"][0]["request_id"] == "r1"
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/call-monitor/capture/records?policy=normal&request_id=missing")).json()["records"] == []
        assert (await client.get("/call-monitor/capture/export")).status_code == 400
        assert (await client.get("/call-monitor/capture/records?limit=201")).status_code == 422
        assert (await client.get("/call-monitor/capture/records?policy=other")).status_code == 400
        exported = await client.get("/call-monitor/capture/export?request_id=r1")
        assert exported.status_code == 200
        assert exported.json()["policies"]["normal"][0]["details"] == event["details"]


@pytest.mark.asyncio
async def test_configuration_validation_locks_and_immediate_capacity_shrink(setup, monkeypatch):
    import os
    import time
    app, store = setup
    values = {}
    class Storage:
        async def set_config(self, key, value):
            values[key] = value
            return True
        async def get_all_config(self):
            return values
    async def reload_config():
        config._config_cache.update(values)
    monkeypatch.setattr(config_routes, "get_storage_adapter", AsyncMock(return_value=Storage()))
    monkeypatch.setattr(config, "reload_config", reload_config)
    for i in range(20):
        store.write("normal", {"request_id": str(i), "created_at": time.time(), "data": os.urandom(80000).hex()},
                    capture_policies()["normal"], max_entry_bytes=256 * 1024)
    assert store.usage("normal") > 1024**2
    app.dependency_overrides[panel.verify_panel_token] = lambda: "ok"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for values_bad in ({"request_capture_normal_max_mb": 0}, {"request_capture_bogus": 1}, {"request_capture_context_count": 1.5}):
            assert (await client.post("/config/save", json={"config": values_bad})).status_code == 400
        response = await client.post("/config/save", json={"config": {"request_capture_enabled": False, "request_capture_normal_max_mb": 1}})
        assert response.status_code == 200
        assert "request_capture_enabled" not in values  # environment is locked
        assert values["request_capture_normal_max_mb"] == 1
        assert store.usage("normal") <= 1024**2


@pytest.mark.asyncio
async def test_large_record_reports_export_incompleteness(setup, monkeypatch):
    import time
    app, store = setup
    monkeypatch.setenv("REQUEST_CAPTURE_EXPORT_MAX_MB", "1")
    store.write("normal", {"request_id": "large", "created_at": time.time(), "details": {"text": "a" * 800000}},
                capture_policies()["normal"], max_entry_bytes=2 * 1024**2)
    app.dependency_overrides[panel.verify_panel_token] = lambda: "ok"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/call-monitor/capture/export?request_id=large")
        assert response.status_code == 200 and len(response.content) <= 1024**2
        assert response.json()["truncated"] is True
