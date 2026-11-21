"""上游 5xx 就地冷却 + 计数降权。

背景（2026-11-21 生产事故）：Google CloudCode 开始返回 500 INTERNAL，
而 `auto_ban_error_codes` 里含 500，`handle_error_with_retry` 在重试之前
就把账号 `set_cred_disabled(True)`，一天内误杀 19 个可用账号。

新语义：
- 5xx 不封号，账号进入 ``ANTIGRAVITY_TRANSIENT_HOLD_SECONDS``（默认 600s）窗口；
- 窗口内选择器跳过该账号，请求换号重试；
- 每命中一次累计 ``transient_error_count``，选择排序中每次让出 3 个轮转位；
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
async def test_repeated_5xx_lowers_rotation_priority(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)

    for _ in range(3):
        await manager.hold_antigravity_transient("a.json", seconds=0, status_code=500)

    order = []
    for _ in range(4):
        item = await manager.get_next_available_credential(mode="antigravity")
        assert item is not None
        order.append(item[0])

    # b.json 无计数，应当在 4 次轮转里明显更靠前。
    assert order[0] == "b.json", order
    assert order.count("b.json") >= order.count("a.json"), order


@pytest.mark.asyncio
async def test_success_clears_hold_and_counter(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)
    await manager.hold_antigravity_transient("a.json", seconds=600, status_code=500)

    await manager.record_success("a.json", model_name="gemini-2.5-flash", mode="antigravity")

    stored_until, count, _ = await read_hold(manager, "a.json")
    assert stored_until == 0
    assert count == 0


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
        manager, status_code, "a.json", True, 0, 3, 0, mode="antigravity"
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
async def test_storage_adapter_requires_postgres(monkeypatch):
    """生产主库只跑 PostgreSQL：缺 DSN 时必须显式失败，不能静默落到 SQLite。"""
    from src.storage_adapter import StorageAdapter

    monkeypatch.delenv("MAIN_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("MAIN_STORAGE_BACKEND", raising=False)

    adapter = StorageAdapter()
    with pytest.raises(RuntimeError, match="MAIN_POSTGRES_DSN"):
        await adapter.initialize()
