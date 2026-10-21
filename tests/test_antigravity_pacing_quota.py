"""antigravity_pacing / antigravity_quota_guard 专项测试（2026-10-21）。"""

import time

import pytest

from src import antigravity_pacing as pacing
from src import antigravity_quota_guard as quota_guard


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    pacing._LOCKS.clear()
    pacing._NEXT_FREE.clear()
    pacing._429_STRIKES.clear()
    quota_guard._SNAPSHOTS.clear()
    quota_guard._FETCHED_AT.clear()
    # 固定配置：启用、基准 100ms、抖动 0%（便于确定性断言）
    monkeypatch.setenv("ANTIGRAVITY_PACING_ENABLED", "true")
    monkeypatch.setenv("ANTIGRAVITY_PACING_BASE_MS", "100")
    monkeypatch.setenv("ANTIGRAVITY_PACING_JITTER_PCT", "0")
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
