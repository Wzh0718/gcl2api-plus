"""Expiry rotation preserves sticky, exclusive bindings and avoids dead endpoints."""
import asyncio
import sqlite3
import time

import pytest
import pytest_asyncio

from src.storage.sqlite_manager import SQLiteManager


@pytest_asyncio.fixture
async def pool(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    await manager.initialize()
    group = await manager.create_proxy_group(name="PX6", strategy="failover", exclusive_assignment=True)
    yield manager, group["id"]
    await manager.close()


async def account(manager, group_id, name="one.json"):
    await manager.store_credential(name, {"access_token": "test"}, mode="antigravity")
    await manager.update_credential_network(name, proxy_mode="group", proxy_group_id=group_id)


def node(name, expires_at):
    return {"name": name, "proxy_url": f"http://{name}.example:8080", "expires_at": expires_at}


@pytest.mark.asyncio
async def test_import_saves_expiry_and_renewal_preserves_binding(pool):
    manager, gid = pool
    expiry = time.time() + 7 * 86400
    await manager.import_proxy_group_nodes(gid, [node("old", expiry)])
    await account(manager, gid)
    before = await manager.resolve_credential_network("one.json", "antigravity")
    await manager.import_proxy_group_nodes(gid, [node("old", expiry + 86400)])
    after = await manager.resolve_credential_network("one.json", "antigravity")
    assert after["proxy_node_id"] == before["proxy_node_id"]
    assert (await manager.get_proxy_group(gid))["nodes"][0]["expires_at"] == expiry + 86400
    # A generic re-import without metadata must not erase the known expiry.
    await manager.import_proxy_group_nodes(gid, [{"name": "old", "proxy_url": node("old", expiry)["proxy_url"]}])
    assert (await manager.get_proxy_group(gid))["nodes"][0]["expires_at"] == expiry + 86400


@pytest.mark.asyncio
async def test_rotate_24h_before_expiry_and_clear_old_ip_health(pool):
    manager, gid = pool
    now = time.time()
    await manager.import_proxy_group_nodes(gid, [node("old", now + 2 * 86400)])
    await account(manager, gid)
    before = await manager.resolve_credential_network("one.json", "antigravity")
    await manager.update_antigravity_account_health(
        "one.json", egress_ip="192.0.2.1", binding_status="healthy", eligibility_status="eligible",
    )
    await manager.import_proxy_group_nodes(gid, [node("old", now + 23 * 3600), node("fresh", now + 7 * 86400)])
    after = await manager.resolve_credential_network("one.json", "antigravity")
    assert after["proxy_node_id"] != before["proxy_node_id"]
    assert after["proxy_url"] == "http://fresh.example:8080"
    health = await manager.get_antigravity_account_health("one.json")
    assert health["egress_ip"] is None
    assert health["eligibility_status"] == "unchecked"
    assert (await manager.resolve_credential_network("one.json", "antigravity"))["proxy_node_id"] == after["proxy_node_id"]


@pytest.mark.asyncio
async def test_no_spare_retains_valid_binding_and_reports_shortage(pool):
    manager, gid = pool
    now = time.time()
    await manager.import_proxy_group_nodes(gid, [node("old", now + 20 * 3600)])
    await account(manager, gid)
    before = await manager.resolve_credential_network("one.json", "antigravity")
    await manager.import_proxy_group_nodes(gid, [node("same_expiry", now + 20 * 3600), node("unknown", None)])
    after = await manager.resolve_credential_network("one.json", "antigravity")
    assert after["proxy_node_id"] == before["proxy_node_id"]
    assert after["proxy_expiry_warning"] == "no_replacement"
    listed = (await manager.list_proxy_groups())[0]
    assert listed["expiring_node_count"] == 2
    assert listed["expiry_replacement_shortage"] == 1


@pytest.mark.asyncio
async def test_expired_nodes_are_never_selected_or_sent(pool):
    manager, gid = pool
    now = time.time()
    await manager.import_proxy_group_nodes(gid, [node("old", now + 7 * 86400)])
    await account(manager, gid)
    await manager.resolve_credential_network("one.json", "antigravity")
    await manager.import_proxy_group_nodes(gid, [node("old", now - 1)])
    result = await manager.resolve_credential_network("one.json", "antigravity")
    assert result["proxy_url"] is None
    assert result["bound_proxy_node_id"] is None
    assert await manager.select_proxy_group_node(gid) is None
    assert await manager.get_next_available_credential(mode="antigravity") is None
    assert (await manager.get_proxy_group(gid))["enabled_node_count"] == 0
    assert (await manager.list_proxy_groups())[0]["enabled_node_count"] == 0


@pytest.mark.asyncio
async def test_no_early_rotation_and_no_selection_of_cooling_spare(pool):
    manager, gid = pool
    now = time.time()
    await manager.import_proxy_group_nodes(gid, [node("old", now + 25 * 3600)])
    await account(manager, gid)
    before = await manager.resolve_credential_network("one.json", "antigravity")
    await manager.import_proxy_group_nodes(gid, [node("fresh", now + 7 * 86400)])
    assert (await manager.resolve_credential_network("one.json", "antigravity"))["proxy_node_id"] == before["proxy_node_id"]
    await manager.import_proxy_group_nodes(gid, [node("old", now + 23 * 3600)])
    with sqlite3.connect(manager._db_path) as db:
        db.execute("UPDATE proxy_group_nodes SET cooldown_until=? WHERE name='fresh'", (now + 3600,))
    after = await manager.resolve_credential_network("one.json", "antigravity")
    assert after["proxy_node_id"] == before["proxy_node_id"]
    assert after["proxy_expiry_warning"] == "no_replacement"


@pytest.mark.asyncio
async def test_concurrent_rotations_never_share_one_spare(pool):
    manager, gid = pool
    now = time.time()
    await manager.import_proxy_group_nodes(gid, [node("one", now + 7 * 86400), node("two", now + 7 * 86400)])
    for name in ("one.json", "two.json"):
        await account(manager, gid, name)
        await manager.resolve_credential_network(name, "antigravity")
    await manager.import_proxy_group_nodes(gid, [node("one", now + 3600), node("two", now + 3600), node("spare", now + 10 * 86400)])
    results = await asyncio.gather(*(manager.resolve_credential_network(name, "antigravity") for name in ("one.json", "two.json")))
    assert len({r["proxy_node_id"] for r in results}) == 2
    assert sum(r["proxy_url"] == "http://spare.example:8080" for r in results) == 1
    assert sum(r.get("proxy_expiry_warning") == "no_replacement" for r in results) == 1


@pytest.mark.asyncio
async def test_old_database_gains_nullable_expiry_column(tmp_path, monkeypatch):
    path = tmp_path / "credentials.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE proxy_group_nodes (id INTEGER PRIMARY KEY, group_id INTEGER, name TEXT, proxy_url TEXT, enabled INTEGER, failure_count INTEGER, cooldown_until REAL, last_used_at REAL, created_at REAL, updated_at REAL)")
        db.execute("INSERT INTO proxy_group_nodes(id,name,proxy_url) VALUES(1,'legacy','http://legacy.example:80')")
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    try:
        await manager.initialize()
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT expires_at FROM proxy_group_nodes WHERE id=1").fetchone() == (None,)
    finally:
        await manager.close()
