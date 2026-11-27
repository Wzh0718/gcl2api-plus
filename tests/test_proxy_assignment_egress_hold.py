"""Group assignment must not hand a deleted account's held exit to a new account.

A deleted account keeps its last observed public exit for seven days
(`antigravity_egress_reservations`). The health gate treats that exit as an
IP-drift signal and refuses to route the account, so assigning it leaves the
account enabled but never selected. Assignment and rebinding must skip held
exits, exactly like the import path already does.
"""
import sqlite3
import time

import pytest

from src.storage.sqlite_manager import SQLiteManager


@pytest.fixture
async def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    await manager.initialize()
    yield manager
    await manager.close()


async def _group_with_nodes(storage, urls):
    group = await storage.create_proxy_group(name="PX6", exclusive_assignment=True)
    await storage.import_proxy_group_nodes(
        group["id"], [{"name": f"node-{i}", "proxy_url": url} for i, url in enumerate(urls)]
    )
    return group


async def _hold_exit(storage, filename, egress_ip):
    """Create a seven-day deletion hold for one exit the way delete does."""
    await storage.store_credential(filename, {}, "antigravity")
    await storage.update_antigravity_account_health(filename, egress_ip=egress_ip)
    assert await storage.delete_credential(filename, "antigravity")


@pytest.mark.asyncio
async def test_group_assignment_skips_exit_held_by_deleted_account(storage):
    group = await _group_with_nodes(storage, [
        "http://192.0.2.10:8080",
        "http://192.0.2.11:8080",
    ])
    await _hold_exit(storage, "gone.json", "192.0.2.10")

    await storage.store_credential("live.json", {}, "antigravity")
    await storage.update_credential_network(
        "live.json", proxy_mode="group", proxy_group_id=group["id"]
    )

    network = await storage.resolve_credential_network("live.json", "antigravity")
    assert network["proxy_url"] == "http://192.0.2.11:8080"
    assert network["bound_proxy_node_id"]


@pytest.mark.asyncio
async def test_group_assignment_returns_nothing_when_every_exit_is_held(storage):
    group = await _group_with_nodes(storage, ["http://192.0.2.10:8080"])
    await _hold_exit(storage, "gone.json", "192.0.2.10")

    await storage.store_credential("live.json", {}, "antigravity")
    await storage.update_credential_network(
        "live.json", proxy_mode="group", proxy_group_id=group["id"]
    )

    network = await storage.resolve_credential_network("live.json", "antigravity")
    assert network["proxy_url"] is None
    assert not network["bound_proxy_node_id"]


@pytest.mark.asyncio
async def test_held_exit_becomes_assignable_after_the_hold_expires(storage):
    group = await _group_with_nodes(storage, [
        "http://192.0.2.10:8080",
        "http://192.0.2.11:8080",
    ])
    await _hold_exit(storage, "gone.json", "192.0.2.10")

    await storage.store_credential("live.json", {}, "antigravity")
    await storage.update_credential_network(
        "live.json", proxy_mode="group", proxy_group_id=group["id"]
    )
    first = await storage.resolve_credential_network("live.json", "antigravity")
    assert first["proxy_url"] == "http://192.0.2.11:8080"

    with sqlite3.connect(storage._db_path) as conn:
        conn.execute("UPDATE antigravity_egress_reservations SET expires_at = ?", (int(time.time()) - 1,))
        conn.commit()
    # The held exit is importable again; the other node stays bound to live.json.
    assert await storage.list_free_proxy_import_nodes(group["id"]) == [
        (1, "http://192.0.2.10:8080")
    ]

    assert await storage.invalidate_antigravity_proxy_binding(
        "live.json", require_replacement=True
    )
    rebound = await storage.resolve_credential_network("live.json", "antigravity")
    assert rebound["proxy_url"] == "http://192.0.2.10:8080"


@pytest.mark.asyncio
async def test_rebind_skips_exit_that_became_held(storage):
    group = await _group_with_nodes(storage, [
        "http://192.0.2.10:8080",
        "http://192.0.2.11:8080",
        "http://192.0.2.12:8080",
    ])
    await _hold_exit(storage, "gone-a.json", "192.0.2.10")

    await storage.store_credential("live.json", {}, "antigravity")
    await storage.update_credential_network(
        "live.json", proxy_mode="group", proxy_group_id=group["id"]
    )
    first = await storage.resolve_credential_network("live.json", "antigravity")
    assert first["proxy_url"] == "http://192.0.2.11:8080"

    # The exit the account is bound to enters a deletion hold afterwards.
    await _hold_exit(storage, "gone-b.json", "192.0.2.11")

    assert await storage.invalidate_antigravity_proxy_binding(
        "live.json", require_replacement=True
    )
    rebound = await storage.resolve_credential_network("live.json", "antigravity")
    assert rebound["proxy_url"] == "http://192.0.2.12:8080"
