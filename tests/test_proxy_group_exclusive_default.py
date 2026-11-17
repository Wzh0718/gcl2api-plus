"""独占出口（一账号一节点）作为默认策略的回归测试。

覆盖两件事：
1. 新建代理组默认独占，不再允许「共享出口」成为新建时的隐式默认；
2. 启动时为历史代理组做一次性收敛，但绝不动已有共享绑定的组。
"""

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from src.storage.sqlite_manager import SQLiteManager


def credential(name):
    return {
        "access_token": name + "-token",
        "refresh_token": name + "-secret",
        "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "oauth_client_key": "consumer",
    }


@pytest_asyncio.fixture
async def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    db = SQLiteManager()
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_new_group_defaults_to_exclusive(storage):
    group = await storage.create_proxy_group(name="default-exclusive")
    assert group["exclusive_assignment"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_exclusive_column", [False, True])
async def test_initialize_converges_eligible_groups_only(storage, missing_exclusive_column):
    eligible = await storage.create_proxy_group(name="eligible")
    conflicting = await storage.create_proxy_group(name="conflicting")
    await storage.import_proxy_group_nodes(eligible["id"], [
        {"name": "n1", "proxy_url": "http://n1:8080"},
        {"name": "n2", "proxy_url": "http://n2:8080"},
    ])
    await storage.import_proxy_group_nodes(conflicting["id"], [
        {"name": "m1", "proxy_url": "http://m1:8080"},
    ])
    for name in ("a", "b", "c", "d"):
        await storage.store_credential(name + ".json", credential(name), "antigravity")
    async with aiosqlite.connect(storage._db_path) as db:
        nodes = {}
        async with db.execute(
            "SELECT group_id, id FROM proxy_group_nodes ORDER BY id"
        ) as cursor:
            for group_id, node_id in await cursor.fetchall():
                nodes.setdefault(group_id, []).append(node_id)
        # eligible：两个账号各占一个节点
        await db.execute(
            "UPDATE antigravity_credentials SET proxy_mode='group', proxy_group_id=?, "
            "bound_proxy_node_id=? WHERE filename='a.json'",
            (eligible["id"], nodes[eligible["id"]][0]),
        )
        await db.execute(
            "UPDATE antigravity_credentials SET proxy_mode='group', proxy_group_id=?, "
            "bound_proxy_node_id=? WHERE filename='b.json'",
            (eligible["id"], nodes[eligible["id"]][1]),
        )
        # conflicting：两个账号挤在同一个节点
        await db.execute(
            "UPDATE antigravity_credentials SET proxy_mode='group', proxy_group_id=?, "
            "bound_proxy_node_id=? WHERE filename IN ('c.json','d.json')",
            (conflicting["id"], nodes[conflicting["id"]][0]),
        )
        # 回到「历史状态」：两个组都是共享默认
        await db.execute("UPDATE proxy_groups SET exclusive_assignment=0")
        await db.execute(
            "DELETE FROM config WHERE key = ?",
            ("_migration_proxy_groups_exclusive_default_v1",),
        )
        if missing_exclusive_column:
            await db.execute("ALTER TABLE proxy_groups DROP COLUMN exclusive_assignment")
        await db.commit()

    restarted = SQLiteManager()
    await restarted.initialize()
    try:
        assert (await restarted.get_proxy_group(eligible["id"]))["exclusive_assignment"] is True
        assert (await restarted.get_proxy_group(conflicting["id"]))["exclusive_assignment"] is False
    finally:
        await restarted.close()

    # 迁移后人工关闭独占，后续重启必须保留选择。
    await storage.update_proxy_group(eligible["id"], exclusive_assignment=False)
    restarted = SQLiteManager()
    await restarted.initialize()
    try:
        assert (await restarted.get_proxy_group(eligible["id"]))["exclusive_assignment"] is False
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_restart_preserves_new_explicit_shared_group(storage):
    group = await storage.create_proxy_group(name="explicit-shared", exclusive_assignment=False)
    restarted = SQLiteManager()
    await restarted.initialize()
    try:
        assert (await restarted.get_proxy_group(group["id"]))["exclusive_assignment"] is False
    finally:
        await restarted.close()
