"""antigravity_pacing / antigravity_quota_guard 专项测试（2026-10-21，2026-10-22 扩展并发闸门）。"""

import asyncio
import time

import pytest

from src import antigravity_pacing as pacing
from src import antigravity_quota_guard as quota_guard


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    pacing.reset_state()
    quota_guard._SNAPSHOTS.clear()
    quota_guard._FETCHED_AT.clear()
    # 固定配置：启用、错峰基准 100ms、抖动 0%、并发上限 15（便于确定性断言）
    monkeypatch.setenv("ANTIGRAVITY_PACING_ENABLED", "true")
    monkeypatch.setenv("ANTIGRAVITY_PACING_BASE_MS", "100")
    monkeypatch.setenv("ANTIGRAVITY_PACING_JITTER_PCT", "0")
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "15")
    # 背压等待上限缩短，避免测试变慢
    monkeypatch.setenv("ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS", "0.1")
    yield


@pytest.mark.asyncio
async def test_pacing_enforces_min_interval_per_credential():
    await pacing.pace_credential("acc-a")
    t0 = time.monotonic()
    await pacing.pace_credential("acc-a")
    waited = time.monotonic() - t0
    assert waited >= 0.09  # 100ms 基准（留 10% 定时器余量）


@pytest.mark.asyncio
async def test_pacing_is_per_credential():
    await pacing.pace_credential("acc-a")
    t0 = time.monotonic()
    await pacing.pace_credential("acc-b")  # 不同账号不受 acc-a 排队影响
    waited = time.monotonic() - t0
    assert waited < 0.05


@pytest.mark.asyncio
async def test_pacing_disabled_returns_zero(monkeypatch):
    import config

    monkeypatch.setattr(
        config, "get_antigravity_pacing_enabled_sync", lambda: False
    )
    assert await pacing.pace_credential("acc-a") == 0.0


@pytest.mark.asyncio
async def test_pacing_jitter_varies_interval(monkeypatch):
    import config

    monkeypatch.setattr(
        config, "get_antigravity_pacing_jitter_pct_sync", lambda: 50
    )
    intervals = []
    for _ in range(30):
        await pacing.pace_credential("acc-a")
        intervals.append(pacing._NEXT_FREE["acc-a"])
    deltas = [b - a for a, b in zip(intervals, intervals[1:])]
    assert min(deltas) >= 0.049  # 0.5x
    assert max(deltas) <= 0.151  # 1.5x
    assert len({round(d, 4) for d in deltas}) > 1  # 确实有抖动


def test_transient_429_cooldown_escalates_and_resets():
    first = pacing.transient_429_cooldown("acc-a")
    assert 59 <= first - time.time() <= 61
    second = pacing.transient_429_cooldown("acc-a")
    assert 119 <= second - time.time() <= 121
    third = pacing.transient_429_cooldown("acc-a")
    assert 239 <= third - time.time() <= 241
    pacing.note_success("acc-a")
    after_reset = pacing.transient_429_cooldown("acc-a")
    assert 59 <= after_reset - time.time() <= 61


def test_transient_429_cooldown_caps_at_300s():
    for _ in range(10):
        cooldown = pacing.transient_429_cooldown("acc-a")
    assert cooldown - time.time() <= 301


# ==================== 并发闸门（2026-10-22） ====================


@pytest.mark.asyncio
async def test_slot_wait_seconds_defaults_to_30(monkeypatch):
    import config

    monkeypatch.delenv("ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS")
    monkeypatch.setattr(config, "_config_cache", {})
    monkeypatch.setattr(config, "_config_initialized", True)
    assert config.get_antigravity_pacing_slot_wait_seconds_sync() == 30.0
    assert await config.get_antigravity_pacing_slot_wait_seconds() == 30.0


@pytest.mark.asyncio
async def test_slot_allows_configured_concurrency_without_wait(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "3")
    for _ in range(3):
        assert await pacing.acquire_credential_slot("acc-a") < 0.01
    assert pacing._SLOTS["acc-a"].inflight == 3


@pytest.mark.asyncio
async def test_slot_wait_timeout_applies_backpressure(monkeypatch):
    """并发占满且无人释放：等待上限后抛出背压异常，不超额占用槽位。"""
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "1")
    await pacing.acquire_credential_slot("acc-a")
    t0 = time.monotonic()
    with pytest.raises(pacing.CredentialSlotWaitTimeout):
        await pacing.acquire_credential_slot("acc-a")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.09  # 等到配置的 0.1 秒等待上限
    assert elapsed < 0.5
    assert pacing._SLOTS["acc-a"].inflight == 1


@pytest.mark.asyncio
async def test_slot_release_wakes_waiter(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS", "5")
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "1")
    await pacing.acquire_credential_slot("acc-a")

    async def _release_soon():
        await asyncio.sleep(0.05)
        pacing.release_credential_slot("acc-a")

    releaser = asyncio.create_task(_release_soon())
    t0 = time.monotonic()
    await pacing.acquire_credential_slot("acc-a")
    elapsed = time.monotonic() - t0
    await releaser
    assert elapsed < 1.0  # 由 release 唤醒，而不是等满上限
    assert pacing._SLOTS["acc-a"].inflight == 1


@pytest.mark.asyncio
async def test_slot_release_is_idempotent_at_zero(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "2")
    await pacing.acquire_credential_slot("acc-a")
    pacing.release_credential_slot("acc-a")
    pacing.release_credential_slot("acc-a")  # 多余释放不应变负
    assert pacing._SLOTS["acc-a"].inflight == 0


@pytest.mark.asyncio
async def test_slot_disabled_bypasses_gate(monkeypatch):
    import config

    monkeypatch.setattr(config, "get_antigravity_pacing_enabled_sync", lambda: False)
    for _ in range(20):
        assert await pacing.acquire_credential_slot("acc-a") == 0.0
    assert "acc-a" not in pacing._SLOTS


@pytest.mark.asyncio
async def test_429_tightens_concurrency_and_success_restores(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "4")
    await pacing.acquire_credential_slot("acc-a")
    slot = pacing._SLOTS["acc-a"]
    slot.limit = 4

    pacing.note_transient_429("acc-a")
    assert slot.limit == 2  # 对半收紧
    pacing.note_transient_429("acc-a")
    assert slot.limit == 1  # 下限 1

    pacing.note_success("acc-a")
    assert slot.limit == 2  # 成功后 +1 恢复

    pacing.release_credential_slot("acc-a")
    assert slot.inflight == 0


@pytest.mark.asyncio
async def test_concurrency_config_lower_applies_immediately(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "8")
    await pacing.acquire_credential_slot("acc-a")
    pacing.release_credential_slot("acc-a")
    slot = pacing._SLOTS["acc-a"]
    assert slot.limit == 8

    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "2")
    await pacing.acquire_credential_slot("acc-a")
    assert slot.limit == 2  # 面板下调立即生效
    pacing.release_credential_slot("acc-a")


def test_quota_guard_snapshot_and_threshold():
    models = {
        "gemini-3.8-flash-high": {"quotaInfo": {"remainingFraction": 0.5}},
        "gemini-3.5-flash-low": {"quotaInfo": {"remainingFraction": 0.01}},
        "no-quota-model": {},
    }
    quota_guard.update_quota_snapshot("acc-a", models)
    assert quota_guard.is_model_quota_low("acc-a", "gemini-3.8-flash-high", 0.05) is False
    assert quota_guard.is_model_quota_low("acc-a", "gemini-3.5-flash-low", 0.05) is True
    assert quota_guard.is_model_quota_low("acc-a", "no-quota-model", 0.05) is None
    assert quota_guard.is_model_quota_low("acc-b", "gemini-3.5-flash-low", 0.05) is None

    low = quota_guard.low_quota_accounts("gemini-3.5-flash-low", 0.05)
    assert low == {"acc-a"}


def test_quota_guard_stale_snapshot_ignored():
    quota_guard.update_quota_snapshot(
        "acc-a", {"m": {"quotaInfo": {"remainingFraction": 0.0}}}
    )
    quota_guard._FETCHED_AT["acc-a"] = time.time() - 16 * 60  # 超过 15 分钟 TTL
    assert quota_guard.is_model_quota_low("acc-a", "m", 0.05) is None
    assert quota_guard.low_quota_accounts("m", 0.05) == set()


@pytest.mark.asyncio
async def test_quota_guard_filters_credential_selection(monkeypatch):
    """get_valid_credential 应避开低额度账号，全部低时回退不避让。"""
    import config
    from src.credential_manager import CredentialManager

    manager = CredentialManager()

    calls = []

    class _FakeBackend:
        async def get_next_available_credential(
            self, mode, model_name, exclude_filenames
        ):
            calls.append(set(exclude_filenames))
            if "low-acc.json" in exclude_filenames:
                return ("good-acc.json", {"access_token": "t", "project_id": "p"})
            return ("low-acc.json", {"access_token": "t", "project_id": "p"})

    class _FakeAdapter:
        _backend = _FakeBackend()

    monkeypatch.setattr(manager, "_storage_adapter", _FakeAdapter())

    async def _noop_initialized():
        return None

    monkeypatch.setattr(manager, "_ensure_initialized", _noop_initialized)
    async def _no_refresh(*a, **k):
        return False

    async def _ready(self, f, d):
        return d

    monkeypatch.setattr(CredentialManager, "_should_refresh_token", _no_refresh)
    monkeypatch.setattr(
        CredentialManager,
        "ensure_antigravity_account_ready",
        _ready,
    )

    async def _enabled():
        return True

    async def _threshold():
        return 0.05

    monkeypatch.setattr(config, "get_antigravity_quota_guard_enabled", _enabled)
    monkeypatch.setattr(config, "get_antigravity_quota_guard_threshold", _threshold)

    quota_guard.update_quota_snapshot(
        "low-acc.json", {"m-1": {"quotaInfo": {"remainingFraction": 0.0}}}
    )

    result = await manager.get_valid_credential(mode="antigravity", model_name="m-1")
    assert result is not None
    assert result[0] == "good-acc.json"
    # 首次 probe 应带着 low-acc 排除
    assert any("low-acc.json" in c for c in calls)
