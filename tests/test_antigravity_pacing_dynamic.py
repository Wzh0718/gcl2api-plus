"""账号级动态间隔：区间生效、文本不额外引入固定基准等待。"""

import time

import pytest

import config
from src import antigravity_pacing as pacing


@pytest.fixture(autouse=True)
def _reset():
    pacing.reset_state()
    yield
    pacing.reset_state()


def _patch(monkeypatch, *, enabled=True, low=0.0, high=0.0, base_ms=80, jitter=50):
    monkeypatch.setattr(config, "get_antigravity_pacing_enabled_sync", lambda: enabled)
    monkeypatch.setattr(config, "get_antigravity_pacing_min_interval_seconds_sync", lambda: low)
    monkeypatch.setattr(config, "get_antigravity_pacing_max_interval_seconds_sync", lambda: high)
    monkeypatch.setattr(config, "get_antigravity_pacing_base_ms_sync", lambda: base_ms)
    monkeypatch.setattr(config, "get_antigravity_pacing_jitter_pct_sync", lambda: jitter)


@pytest.mark.asyncio
async def test_dynamic_interval_waits_within_range(monkeypatch):
    """启用 2-10s 时，第二次调用要等一个落在区间内的间隔。"""
    _patch(monkeypatch, low=0.05, high=0.06)

    first = await pacing.pace_credential("a.json")
    assert first == 0.0

    started = time.monotonic()
    second = await pacing.pace_credential("a.json")
    elapsed = time.monotonic() - started

    assert 0.04 <= second <= 0.08
    assert 0.04 <= elapsed <= 0.12


@pytest.mark.asyncio
async def test_text_path_has_no_extra_wait_when_dynamic_disabled(monkeypatch):
    """动态区间关闭时，文本路径不应引入固定基准的等待。"""
    _patch(monkeypatch, low=0.0, high=0.0, base_ms=80)

    assert await pacing.pace_credential("b.json", require_dynamic=True) == 0.0
    assert await pacing.pace_credential("b.json", require_dynamic=True) == 0.0


@pytest.mark.asyncio
async def test_image_path_falls_back_to_base_interval(monkeypatch):
    """图片路径（require_dynamic=False）在动态区间关闭时仍走固定基准。"""
    _patch(monkeypatch, low=0.0, high=0.0, base_ms=50, jitter=0)

    await pacing.pace_credential("c.json")
    waited = await pacing.pace_credential("c.json")
    assert 0.04 <= waited <= 0.06


@pytest.mark.asyncio
async def test_pacing_disabled_returns_zero(monkeypatch):
    _patch(monkeypatch, enabled=False, low=0.01, high=0.02)

    assert await pacing.pace_credential("d.json") == 0.0
    assert await pacing.pace_credential("d.json") == 0.0
