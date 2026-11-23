"""上游 5xx 就地冷却 + 计数降权。

背景（2026-11-21 生产事故）：Google CloudCode 开始返回 500 INTERNAL，
而 `auto_ban_error_codes` 里含 500，`handle_error_with_retry` 在重试之前
就把账号 `set_cred_disabled(True)`，一天内误杀 19 个可用账号。

新语义：
- 5xx 不封号，账号进入 ``ANTIGRAVITY_TRANSIENT_HOLD_SECONDS``（默认 600s）窗口；
- 窗口内选择器跳过该账号，请求换号重试；
- 每命中一次累计 ``transient_error_count``，冷却到期后平等参与轮转；
- 一次真实成功把窗口与计数清零。
"""

import time

import pytest

from src.api.utils import handle_error_with_retry
from src.storage.sqlite_manager import SQLiteManager


async def make_manager(tmp_path, monkeypatch, names=("a.json", "b.json")):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    await manager.initialize()
    for name in names:
        await manager.store_credential(name, {"access_token": name}, mode="antigravity")
    return manager


async def read_hold(manager, name):
    async with __import__("src.storage.sqlite_pragmas", fromlist=["connect"]).connect(
        manager._db_path
    ) as db:
        async with db.execute(
            "SELECT transient_hold_until, transient_error_count, disabled "
            "FROM antigravity_credentials WHERE filename = ?",
            (name,),
        ) as cursor:
            row = await cursor.fetchone()
    return row


@pytest.mark.asyncio
async def test_transient_hold_does_not_disable_account(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)

    before = time.time()
    hold_until = await manager.hold_antigravity_transient(
        "a.json", seconds=600, status_code=500
    )

    assert hold_until == pytest.approx(before + 600, abs=5)
    stored_until, count, disabled = await read_hold(manager, "a.json")
    assert stored_until == pytest.approx(hold_until)
    assert count == 1
    assert disabled == 0, "5xx 不得把账号标记为禁用"


@pytest.mark.asyncio
async def test_hold_window_excludes_account_then_releases_it(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)

    await manager.hold_antigravity_transient("a.json", seconds=600, status_code=500)

    picked = await manager.get_next_available_credential(mode="antigravity")
    assert picked is not None and picked[0] == "b.json", "窗口内必须跳过被冷却的账号"

    # 窗口到期后重新参与轮转。
    async with __import__("src.storage.sqlite_pragmas", fromlist=["connect"]).connect(
        manager._db_path
    ) as db:
        await db.execute(
            "UPDATE antigravity_credentials SET transient_hold_until = ? WHERE filename = ?",
            (time.time() - 1, "a.json"),
        )
        await db.commit()

    seen = set()
    for _ in range(4):
        item = await manager.get_next_available_credential(mode="antigravity")
        assert item is not None
        seen.add(item[0])
    assert "a.json" in seen, "冷却到期后账号必须回到池子"


@pytest.mark.asyncio
async def test_expired_5xx_holds_rejoin_even_rotation(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)

    for _ in range(3):
        await manager.hold_antigravity_transient("a.json", seconds=0, status_code=500)

    order = []
    for _ in range(4):
        item = await manager.get_next_available_credential(mode="antigravity")
        assert item is not None
        order.append(item[0])

    assert set(order[:2]) == {"a.json", "b.json"}
    assert order[:2] == order[2:]



@pytest.mark.asyncio
async def test_success_clears_counter_but_keeps_hold_window(tmp_path, monkeypatch):
    """成功的请求只清降权计数。

    5xx 之后紧跟的 200 通常来自冷却前发出的在飞请求，不能证明上游已恢复；
    窗口必须走完，否则 503 这类高频错误上的降温会瞬间失效。
    """
    manager = await make_manager(tmp_path, monkeypatch)
    hold_until = await manager.hold_antigravity_transient(
        "a.json", seconds=600, status_code=500
    )

    await manager.record_success("a.json", model_name="gemini-2.5-flash", mode="antigravity")

    stored_until, count, disabled = await read_hold(manager, "a.json")
    assert stored_until == pytest.approx(hold_until), "成功不得提前解除冷却窗口"
    assert count == 0, "降权计数应清零"
    assert disabled == 0


class _StubManager:
    """只记录调用，不触达存储。"""

    def __init__(self):
        self.disabled = []
        self.holds = []

    async def hold_transient(self, credential_name, status_code=None, mode="antigravity"):
        self.holds.append((credential_name, status_code))
        return time.time() + 600

    async def set_cred_disabled(self, credential_name, disabled, mode="geminicli"):
        self.disabled.append((credential_name, disabled))
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_retry_path_cools_down_instead_of_banning(status_code):
    manager = _StubManager()

    should_retry = await handle_error_with_retry(
        manager, status_code, "a.json", True, 1, 3, 0, mode="antigravity"
    )

    assert should_retry is True, "5xx 仍应换号重试"
    assert manager.holds == [("a.json", status_code)]
    assert manager.disabled == [], "5xx 绝不能走 set_cred_disabled"


@pytest.mark.asyncio
async def test_403_still_uses_auto_ban_when_enabled(monkeypatch):
    """5xx 分支不能吞掉真正的 403 处理。"""
    manager = _StubManager()

    async def fake_should_ban(code):
        return code == 403

    monkeypatch.setattr("src.api.utils.check_should_auto_ban", fake_should_ban)

    should_retry = await handle_error_with_retry(
        manager, 403, "a.json", True, 0, 3, 0, mode="antigravity"
    )

    assert should_retry is True
    assert manager.disabled == [("a.json", True)]
    assert manager.holds == []


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_sqlite_main_store_survives_restart(monkeypatch, tmp_path, explicit):
    from src.storage_adapter import StorageAdapter

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    if explicit:
        monkeypatch.setenv("MAIN_STORAGE_BACKEND", "SQLite")
        monkeypatch.setenv("MAIN_POSTGRES_DSN", "postgresql://unused.invalid/main")

    adapter = StorageAdapter()
    await adapter.initialize()
    try:
        assert await adapter.store_credential("existing.json", {"project_id": "existing"})
    finally:
        await adapter.close()

    from src.px6_refresh import sync_once

    reopened = StorageAdapter()
    monkeypatch.setattr("src.storage_adapter._storage_adapter", reopened)
    assert await sync_once() == {"skipped": "no_key"}
    try:
        credential = await reopened.get_credential("existing.json")
        assert credential["project_id"] == "existing"
        assert (tmp_path / "credentials.db").is_file()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_explicit_postgres_requires_dsn(monkeypatch):
    from src.storage_adapter import StorageAdapter

    monkeypatch.delenv("MAIN_POSTGRES_DSN", raising=False)
    monkeypatch.setenv("MAIN_STORAGE_BACKEND", "postgresql")

    adapter = StorageAdapter()
    with pytest.raises(RuntimeError, match="MAIN_POSTGRES_DSN"):
        await adapter.initialize()


@pytest.mark.asyncio
async def test_first_503_cools_the_account_immediately(tmp_path, monkeypatch):
    """一次 503 就必须冷藏账号：首档 60 秒，不依赖重试次数或重试开关。"""
    manager = await make_manager(tmp_path, monkeypatch)

    before = time.time()
    hold_until = await manager.hold_antigravity_transient(
        "a.json", seconds=600, status_code=503
    )

    assert hold_until == pytest.approx(before + 60, abs=5)
    _, count, disabled = await read_hold(manager, "a.json")
    assert count == 1
    assert disabled == 0
    picked = await manager.get_next_available_credential(mode="antigravity")
    assert picked is not None and picked[0] == "b.json"


@pytest.mark.asyncio
async def test_repeated_503_backs_off_then_caps_and_success_resets(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)

    async def expire(name):
        async with __import__("src.storage.sqlite_pragmas", fromlist=["connect"]).connect(
            manager._db_path
        ) as db:
            await db.execute(
                "UPDATE antigravity_credentials SET transient_hold_until = ? WHERE filename = ?",
                (time.time() - 1, name),
            )
            await db.commit()

    for expected in (60, 120, 240, 480, 600, 600):
        hold_until = await manager.hold_antigravity_transient(
            "a.json", seconds=600, status_code=503
        )
        assert hold_until - time.time() == pytest.approx(expected, abs=5)
        await expire("a.json")

    await manager.record_success("a.json", model_name="gemini-2.5-flash", mode="antigravity")
    await expire("a.json")
    hold_until = await manager.hold_antigravity_transient(
        "a.json", seconds=600, status_code=503
    )
    assert hold_until - time.time() == pytest.approx(60, abs=5)


@pytest.mark.asyncio
async def test_parallel_503_failures_share_one_window(tmp_path, monkeypatch):
    import asyncio

    manager = await make_manager(tmp_path, monkeypatch)

    deadlines = await asyncio.gather(*(
        manager.hold_antigravity_transient("a.json", seconds=600, status_code=503)
        for _ in range(4)
    ))

    assert max(deadlines) == min(deadlines)
    assert deadlines[0] - time.time() == pytest.approx(60, abs=5)
    _, count, _ = await read_hold(manager, "a.json")
    assert count == 1, "同一窗口内的并发 503 不得叠加阶梯"
