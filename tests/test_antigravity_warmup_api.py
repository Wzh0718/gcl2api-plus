from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.panel import warmup as warmup_routes
from src.storage.sqlite_manager import SQLiteManager
from tests.test_antigravity_dynamic_warmup import TEST_CONFIG


class Adapter:
    def __init__(self, manager):
        self._backend = manager

    async def get_config(self, key, default=None):
        return await self._backend.get_config(key, default)

    async def set_config(self, key, value):
        return await self._backend.set_config(key, value)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()

    async def initialize():
        await manager.initialize()
        await manager.store_credential("a.json", {"access_token": "a"}, mode="antigravity")
        await manager.store_credential("b.json", {"access_token": "b"}, mode="antigravity")

    import asyncio
    asyncio.run(initialize())
    adapter = Adapter(manager)

    async def get_adapter():
        return adapter

    monkeypatch.setattr(warmup_routes, "get_storage_adapter", get_adapter)
    app = FastAPI()
    app.include_router(warmup_routes.router)
    app.dependency_overrides[warmup_routes.verify_panel_token] = lambda: None
    with TestClient(app) as test_client:
        test_client.manager = manager
        yield test_client


def test_single_batch_cancel_and_resume_are_idempotent(client):
    response = client.post(
        "/antigravity/warmup/batch",
        json={"filenames": ["a.json", "b.json"], "action": "start", "config": TEST_CONFIG},
    )
    assert response.status_code == 200
    assert response.json()["results"]["a.json"]["state"]["status"] == "warming"

    cancelled = client.post(
        "/antigravity/warmup/batch",
        json={"filenames": ["a.json", "b.json"], "action": "cancel"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["results"]["a.json"]["state"]["selectable"] is False

    repeated = client.post(
        "/antigravity/warmup/batch",
        json={"filenames": ["a.json", "b.json"], "action": "cancel"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["results"]["a.json"]["changed"] is False

    resumed = client.post("/antigravity/warmup/a.json", json={"action": "resume"})
    assert resumed.status_code == 200
    assert resumed.json()["state"]["status"] == "warming"


def test_missing_account_makes_batch_atomic(client):
    response = client.post(
        "/antigravity/warmup/batch",
        json={"filenames": ["a.json", "missing.json"], "action": "start"},
    )
    assert response.status_code == 200
    assert response.json()["results"]["missing.json"]["status"] == "missing"
    assert response.json()["results"]["a.json"]["state"]["status"] == "off"


def test_defaults_apply_to_new_warmup_and_list_groups(client):
    saved = client.put(
        "/antigravity/warmup/defaults",
        json={"success_threshold": 5, "window_seconds": 600},
    )
    assert saved.status_code == 200
    assert saved.json()["defaults"]["success_threshold"] == 5

    started = client.post("/antigravity/warmup/a.json", json={"action": "start"})
    assert started.json()["state"]["config"]["success_threshold"] == 5

    group = client.put(
        "/antigravity/warmup/ip-groups/node:1",
        json={"ip_target_concurrency": 3, "ip_initial_concurrency": 1},
    )
    assert group.status_code == 200
    assert group.json()["limits"]["ip_concurrency"] == 1

    listed = client.get("/antigravity/warmup")
    assert listed.status_code == 200
    assert listed.json()["accounts"]["a.json"]["status"] == "warming"
    assert listed.json()["ip_groups"]["node:1"]["config"]["ip_target_concurrency"] == 3


def test_invalid_action_and_config_are_rejected(client):
    invalid_action = client.post(
        "/antigravity/warmup/a.json", json={"action": "unknown"}
    )
    assert invalid_action.status_code == 422

    invalid_config = client.put(
        "/antigravity/warmup/defaults",
        json={"account_initial_concurrency": 3, "account_target_concurrency": 2},
    )
    assert invalid_config.status_code == 400
