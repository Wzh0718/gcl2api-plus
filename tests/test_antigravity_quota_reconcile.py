"""Tests for reconcile_model_cooldowns_with_quota long-cooldown handling."""

import time

import pytest

from src.api.antigravity import (
    LONG_COOLDOWN_WEEKLY_THRESHOLD_SECONDS,
    reconcile_model_cooldowns_with_quota,
)

MODEL = "gemini-3.1-flash-image"


class FakeBackend:
    def __init__(self):
        self.cleared = []

    async def set_model_cooldown(self, filename, model_name, cooldown_until, mode="antigravity"):
        assert cooldown_until is None
        self.cleared.append((filename, model_name))


def _groups(gemini_5h=None, gemini_weekly=None):
    buckets = []
    if gemini_5h is not None:
        buckets.append(
            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": gemini_5h}
        )
    if gemini_weekly is not None:
        buckets.append(
            {
                "bucketId": "gemini-weekly",
                "window": "weekly",
                "remainingFraction": gemini_weekly,
            }
        )
    return [{"displayName": "Gemini Models", "buckets": buckets}]


@pytest.mark.asyncio
async def test_short_cooldown_cleared_by_5h_group_remaining():
    """短冷却（瞬时限流）保持原有行为：5h 窗口有剩余即清除。"""
    backend = FakeBackend()
    cooldowns = {MODEL: time.time() + 3600}
    cleared = await reconcile_model_cooldowns_with_quota(
        backend, "cred.json", cooldowns, models={}, groups=_groups(gemini_5h=0.5)
    )
    assert cleared == 1
    assert backend.cleared == [("cred.json", MODEL)]


@pytest.mark.asyncio
async def test_short_cooldown_falls_back_to_quota_info():
    """短冷却没有组数据时，仍回落到模型级 quotaInfo。"""
    backend = FakeBackend()
    cooldowns = {MODEL: time.time() + 3600}
    cleared = await reconcile_model_cooldowns_with_quota(
        backend, "cred.json", cooldowns, models={MODEL: {"remaining": 0.9}}, groups=None
    )
    assert cleared == 1
    assert backend.cleared == [("cred.json", MODEL)]


@pytest.mark.asyncio
async def test_long_cooldown_not_cleared_by_full_5h_window():
    """周配额耗尽（长冷却）时，5h 窗口满血不能作为清冷却依据。"""
    backend = FakeBackend()
    cooldowns = {MODEL: time.time() + 128 * 3600}
    cleared = await reconcile_model_cooldowns_with_quota(
        backend,
        "cred.json",
        cooldowns,
        models={},
        groups=_groups(gemini_5h=1.0, gemini_weekly=0.0),
    )
    assert cleared == 0
    assert backend.cleared == []


@pytest.mark.asyncio
async def test_long_cooldown_not_cleared_by_quota_info_only():
    """周配额耗尽（长冷却）时，quotaInfo 显示满血也不能清冷却（concise-shelter 案例）。"""
    backend = FakeBackend()
    cooldowns = {MODEL: time.time() + 128 * 3600}
    cleared = await reconcile_model_cooldowns_with_quota(
        backend,
        "cred.json",
        cooldowns,
        models={MODEL: {"remaining": 1.0}},
        groups=None,
    )
    assert cleared == 0
    assert backend.cleared == []


@pytest.mark.asyncio
async def test_long_cooldown_cleared_when_weekly_bucket_has_remaining():
    """长冷却在 weekly bucket 确有剩余时才清除。"""
    backend = FakeBackend()
    cooldowns = {MODEL: time.time() + 128 * 3600}
    cleared = await reconcile_model_cooldowns_with_quota(
        backend,
        "cred.json",
        cooldowns,
        models={},
        groups=_groups(gemini_5h=0.0, gemini_weekly=0.4),
    )
    assert cleared == 1
    assert backend.cleared == [("cred.json", MODEL)]


@pytest.mark.asyncio
async def test_threshold_boundary_uses_short_cooldown_path():
    """冷却剩余时长刚好低于阈值时按短冷却路径处理。"""
    backend = FakeBackend()
    cooldowns = {MODEL: time.time() + LONG_COOLDOWN_WEEKLY_THRESHOLD_SECONDS - 60}
    cleared = await reconcile_model_cooldowns_with_quota(
        backend, "cred.json", cooldowns, models={MODEL: {"remaining": 0.5}}, groups=None
    )
    assert cleared == 1
    assert backend.cleared == [("cred.json", MODEL)]
