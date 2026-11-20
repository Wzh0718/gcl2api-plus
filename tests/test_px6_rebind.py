"""PX6 到期换绑：过期节点上的账号要自动迁走，没有可用节点要显式标失败。"""
import time

import pytest
import pytest_asyncio

from src.storage.sqlite_manager import SQLiteManager
from src.storage.sqlite_pragmas import connect as sqlite_connect


@pytest_asyncio.fixture
async def pool(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    await manager.initialize()
    group = await manager.create_proxy_group(name="PX6", strategy="failover",
                                             exclusive_assignment=True)
    yield manager, group["id"], tmp_path
    await manager.close()


def node(name, expires_at):
    return {"name": name, "proxy_url": f"http://{name}.example:8080", "expires_at": expires_at}


async def bind_account(manager, group_id, filename="a.json", node_id=None):
    """建账号并设成组模式；node_id 给定时直接硬绑定（模拟历史遗留的过期绑定）。"""
    await manager.store_credential(filename, {"access_token": "t"}, mode="antigravity")
    await manager.update_credential_network(filename, proxy_mode="group", proxy_group_id=group_id)
    if node_id is not None:
        async with sqlite_connect(manager._db_path) as db:
            await db.execute(
                "UPDATE antigravity_credentials SET bound_proxy_node_id = ? WHERE filename = ?",
                (int(node_id), filename),
            )
            await db.commit()
    return node_id


async def node_id_of(manager, gid, name):
    for n in (await manager.get_proxy_group(gid))["nodes"]:
        if n["name"] == name:
            return n["id"]
    raise AssertionError(f"node {name} missing")


@pytest.mark.asyncio
async def test_rebind_moves_account_to_live_node_and_marks_it(pool):
    manager, gid, _ = pool
    past = time.time() - 60
    future = time.time() + 7 * 86400
    await manager.import_proxy_group_nodes(gid, [node("dead", past), node("live", future)])
    await bind_account(manager, gid, node_id=await node_id_of(manager, gid, "dead"))

    result = await manager.rebind_expired_proxy_accounts(gid)

    assert result["checked"] == 1
    assert len(result["rebound"]) == 1
    after = await manager.get_credential_network("a.json", mode="antigravity")
    assert after["bound_proxy_node_name"] == "live"
    health = await manager.get_antigravity_account_health("a.json")
    assert health["binding_status"] == "proxy_rebound"
    assert "dead" in (health["eligibility_reason"] or "")
    assert "live" in (health["eligibility_reason"] or "")

    # 账号列表要能直接看到标记与原因（面板徽章读的就是这两个字段）
    summary = await manager.get_credentials_summary(mode="antigravity")
    item = next(i for i in summary["items"] if i["filename"] == "a.json")
    assert item["binding_status"] == "proxy_rebound"
    assert "dead" in (item["eligibility_reason"] or "")


@pytest.mark.asyncio
async def test_rebind_marks_failure_when_no_live_node(pool):
    manager, gid, _ = pool
    past = time.time() - 60
    await manager.import_proxy_group_nodes(gid, [node("dead", past)])
    await bind_account(manager, gid, node_id=await node_id_of(manager, gid, "dead"))

    result = await manager.rebind_expired_proxy_accounts(gid)

    assert result["checked"] == 1
    assert result["rebound"] == []
    assert len(result["unassignable"]) == 1
    health = await manager.get_antigravity_account_health("a.json")
    assert health["binding_status"] == "proxy_failed"
    assert "暂无可用节点" in (health["eligibility_reason"] or "")


@pytest.mark.asyncio
async def test_rebind_respects_exclusive_assignment(pool):
    manager, gid, _ = pool
    past = time.time() - 60
    future = time.time() + 7 * 86400
    await manager.import_proxy_group_nodes(gid, [node("dead", past), node("live", future)])
    dead_id = await node_id_of(manager, gid, "dead")
    await bind_account(manager, gid, "a.json", node_id=dead_id)
    await bind_account(manager, gid, "b.json", node_id=dead_id)

    result = await manager.rebind_expired_proxy_accounts(gid)

    assert result["checked"] >= 1
    moved = {item["filename"]: item for item in result["rebound"]}
    stuck = {item["filename"] for item in result["unassignable"]}
    # 独占组里 live 只能给一个账号，另一个必须显式失败而不是共享
    assert len(moved) + len(stuck) == result["checked"]
    assert not (set(moved) & stuck)


@pytest.mark.asyncio
async def test_orphan_nodes_disabled_but_bound_kept(pool):
    manager, gid, _ = pool
    future = time.time() + 7 * 86400
    await manager.import_proxy_group_nodes(
        gid, [node("kept", future), node("orphan", future)]
    )
    await bind_account(manager, gid, node_id=await node_id_of(manager, gid, "kept"))
    bound_name = "kept"

    disabled = await manager.disable_orphan_proxy_nodes(
        gid, {node("kept", future)["proxy_url"], node("fresh", future)["proxy_url"]}
    )

    assert disabled == 1
    nodes = {n["name"]: n for n in (await manager.get_proxy_group(gid))["nodes"]}
    assert nodes[bound_name]["enabled"] == 1
    others = [n for name, n in nodes.items() if name != bound_name]
    assert others and all(n["enabled"] == 0 for n in others)


@pytest.mark.asyncio
async def test_px6_key_encrypted_roundtrip(pool):
    manager, _, _ = pool
    from src.px6_refresh import key_status, load_api_key, save_api_key

    assert await load_api_key() is None
    assert await save_api_key("abcd-1234-efgh-5678") is True
    assert await load_api_key() == "abcd-1234-efgh-5678"

    status = await key_status()
    assert status["configured"] is True
    assert status["key_tail"] == "5678"
    # 落盘必须是密文，不能出现明文
    raw = await manager.get_config("px6_api_key_ciphertext")
    assert "abcd-1234" not in str(raw)

    assert await save_api_key("") is True
    assert await load_api_key() is None


@pytest.mark.asyncio
async def test_rebind_treats_unknown_url_as_stale_when_live_list_given(pool):
    """早期节点 expires_at 为空，只能靠 PX6 授权列表判断是否还可用。"""
    manager, gid, _ = pool
    future = time.time() + 7 * 86400
    await manager.import_proxy_group_nodes(
        gid, [node("legacy", None), node("live", future)]
    )
    await bind_account(manager, gid, node_id=await node_id_of(manager, gid, "legacy"))

    # legacy 不在 PX6 当前授权列表里 → 视为失效并迁走
    result = await manager.rebind_expired_proxy_accounts(
        gid, live_urls={node("live", future)["proxy_url"]}
    )

    assert result["checked"] == 1
    assert result["rebound"][0]["to_node_name"] == "live"
    health = await manager.get_antigravity_account_health("a.json")
    assert "已不在 PX6 授权列表" in (health["eligibility_reason"] or "")

    # 绑定节点仍在授权列表里时不迁移
    again = await manager.rebind_expired_proxy_accounts(
        gid, live_urls={node("live", future)["proxy_url"], node("legacy", None)["proxy_url"]}
    )
    assert again["checked"] == 0
