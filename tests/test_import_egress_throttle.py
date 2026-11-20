"""Import must not turn a throttled egress probe into a disabled account."""
import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.panel import auth as panel
from src.storage.sqlite_manager import SQLiteManager


@pytest.fixture
async def storage(tmp_path, monkeypatch):
    from src import auth as core_auth

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    await manager.initialize()
    monkeypatch.setattr(panel, "get_storage_adapter", AsyncMock(return_value=manager))
    monkeypatch.setattr(core_auth, "get_storage_adapter", AsyncMock(return_value=manager))
    monkeypatch.setattr(
        panel, "get_antigravity_egress_ip_check_url", AsyncMock(return_value="https://echo.example"),
    )
    yield manager
    await manager.close()


async def _batch_with_node(storage, proxy_url="http://node.example:8080"):
    group = await storage.create_proxy_group(name="PX6")
    await storage.import_proxy_group_nodes(group["id"], [{"name": "node", "proxy_url": proxy_url}])
    batch_id = await storage.refresh_import_queue.create_batch(["mock-token"], group["id"], None)
    return group["id"], batch_id


@pytest.mark.asyncio
async def test_rate_limited_probe_requeues_instead_of_disabling(storage, monkeypatch):
    _, batch_id = await _batch_with_node(storage)
    queue = storage.refresh_import_queue
    monkeypatch.setattr(
        panel, "observe_egress",
        AsyncMock(return_value=SimpleNamespace(status=429, ip=None, cached=True, error_type="RateLimited")),
    )

    assert await panel.process_next_refresh_import(queue)
    item = (await queue.list_items(batch_id))[0]
    assert item["status"] == "queued"
    assert "限流" in item["message"]
    # The throttle is retried after a short delay, not immediately.
    assert await queue.claim_next() is None


@pytest.mark.asyncio
async def test_probe_timeout_requeues_item(storage, monkeypatch):
    _, batch_id = await _batch_with_node(storage)
    queue = storage.refresh_import_queue
    monkeypatch.setattr(panel, "EGRESS_BUDGET_MIN_SECONDS", 0.05)
    monkeypatch.setattr(panel, "EGRESS_BUDGET_PER_NODE_SECONDS", 0.01)

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(5)
        return SimpleNamespace(status=200, ip="192.0.2.1")

    monkeypatch.setattr(panel, "observe_egress", slow)

    assert await panel.process_next_refresh_import(queue)
    item = (await queue.list_items(batch_id))[0]
    assert item["status"] == "queued"
    assert "超时" in item["message"]


@pytest.mark.asyncio
async def test_exhausted_pool_still_disables_item(storage, monkeypatch):
    group = await storage.create_proxy_group(name="PX6")
    batch_id = await storage.refresh_import_queue.create_batch(["mock-token"], group["id"], None)
    queue = storage.refresh_import_queue
    observe = AsyncMock()
    monkeypatch.setattr(panel, "observe_egress", observe)

    assert await panel.process_next_refresh_import(queue)
    item = (await queue.list_items(batch_id))[0]
    assert item["status"] == "disabled"
    assert "出口 IP" in item["message"]
    # No node means no probe at all.
    observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_free_import_nodes_skip_exits_held_by_deleted_accounts(storage):
    group = await storage.create_proxy_group(name="PX6")
    await storage.import_proxy_group_nodes(group["id"], [
        {"name": "held", "proxy_url": "http://192.0.2.10:8080"},
        {"name": "free", "proxy_url": "http://192.0.2.11:8080"},
    ])
    await storage.store_credential("gone.json", {}, "antigravity")
    await storage.update_antigravity_account_health("gone.json", egress_ip="192.0.2.10")
    assert await storage.delete_credential("gone.json", "antigravity")

    free = await storage.list_free_proxy_import_nodes(group["id"])
    assert [url for _, url in free] == ["http://192.0.2.11:8080"]

    with sqlite3.connect(storage._db_path) as conn:
        conn.execute("UPDATE antigravity_egress_reservations SET expires_at = ?", (int(time.time()) - 1,))
        conn.commit()
    assert len(await storage.list_free_proxy_import_nodes(group["id"])) == 2


@pytest.mark.asyncio
async def test_retry_budget_exhaustion_disables_with_reason(storage):
    _, batch_id = await _batch_with_node(storage)
    queue = storage.refresh_import_queue
    claim = await queue.claim_next()
    assert await queue.defer(claim, "出口探测被限流，稍后自动重试", delay_seconds=0, max_retries=2)
    with sqlite3.connect(storage._db_path) as conn:
        conn.execute("UPDATE refresh_import_items SET retry_after = ?", (int(time.time()) - 1,))
        conn.commit()

    claim = await queue.claim_next()
    assert claim["retry_count"] == 1
    assert await queue.defer(claim, "出口探测被限流，稍后自动重试", delay_seconds=0, max_retries=2)
    item = (await queue.list_items(batch_id))[0]
    assert item["status"] == "disabled"
    assert "限流" in item["message"] and "已重试" in item["message"]


@pytest.mark.asyncio
async def test_reserved_exits_report_their_own_reason(storage, monkeypatch):
    group = await storage.create_proxy_group(name="PX6")
    await storage.import_proxy_group_nodes(group["id"], [
        {"name": "held", "proxy_url": "http://192.0.2.20:8080"},
    ])
    await storage.store_credential("gone.json", {}, "antigravity")
    await storage.update_antigravity_account_health("gone.json", egress_ip="192.0.2.20")
    assert await storage.delete_credential("gone.json", "antigravity")
    batch_id = await storage.refresh_import_queue.create_batch(["mock-token"], group["id"], None)
    observe = AsyncMock()
    monkeypatch.setattr(panel, "observe_egress", observe)

    assert await panel.process_next_refresh_import(storage.refresh_import_queue)
    item = (await storage.refresh_import_queue.list_items(batch_id))[0]
    assert item["status"] == "disabled"
    assert "7 天保留期" in item["message"]
    observe.assert_not_awaited()
