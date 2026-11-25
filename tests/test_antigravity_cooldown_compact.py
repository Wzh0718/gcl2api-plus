"""冷却表压缩：同组只留最晚、过期即删、总量兜底。"""

import time

import pytest

from src.antigravity_quota_sweep import compact_model_cooldowns


class FakeBackend:
    def __init__(self):
        self.deleted = []

    async def set_model_cooldown(self, filename, model_name, cooldown_until, mode="antigravity"):
        assert cooldown_until is None
        self.deleted.append(model_name)
        return True


@pytest.mark.asyncio
async def test_expired_entries_are_removed():
    now = time.time()
    backend = FakeBackend()
    cooldowns = {
        "gemini-expired": now - 10,
        "gemini-ok": now + 600,
    }

    removed, kept = await compact_model_cooldowns(backend, "a.json", cooldowns, now=now)

    assert removed == 1
    assert backend.deleted == ["gemini-expired"]
    assert kept == {"gemini-ok": now + 600}


@pytest.mark.asyncio
async def test_same_group_keeps_latest_only():
    """同属 gemini 组的一堆模型冷却只保留最晚到期的那条。"""
    now = time.time()
    backend = FakeBackend()
    cooldowns = {
        "gemini-2.5-flash-lite": now + 100,
        "gemini-2.5-flash": now + 300,
        "gemini-3.1-flash-image": now + 900,
        "claude-opus-4-6": now + 200,
    }

    removed, kept = await compact_model_cooldowns(backend, "a.json", cooldowns, now=now)

    assert removed == 2
    assert set(backend.deleted) == {"gemini-2.5-flash-lite", "gemini-2.5-flash"}
    assert set(kept) == {"gemini-3.1-flash-image", "claude-opus-4-6"}


@pytest.mark.asyncio
async def test_empty_table_is_noop():
    backend = FakeBackend()
    removed, kept = await compact_model_cooldowns(backend, "a.json", {}, now=time.time())
    assert removed == 0
    assert kept == {}
    assert backend.deleted == []
