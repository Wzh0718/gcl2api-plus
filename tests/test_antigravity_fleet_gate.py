"""全池闸门：极端负载下的准入与退出。

目的不是限速本身，而是让批量任务在极端负载下**能跑完**：
先在闸前有界排队吸收突发，排不下才快速失败，并给出诚实的 Retry-After，
避免客户端按固定 1 秒热重试把池子打穿。
"""

import asyncio

import pytest

import config
import src.antigravity_fleet_gate as gate


def _configure(monkeypatch, *, enabled=True, inflight=0, rpm=0, wait=60.0):
    monkeypatch.setattr(config, "get_antigravity_fleet_enabled_sync", lambda: enabled)
    monkeypatch.setattr(config, "get_antigravity_fleet_inflight_sync", lambda: inflight)
    monkeypatch.setattr(config, "get_antigravity_fleet_rpm_sync", lambda: rpm)
    monkeypatch.setattr(config, "get_antigravity_fleet_wait_seconds_sync", lambda: wait)


@pytest.fixture(autouse=True)
def _reset():
    gate.reset_state()
    yield
    gate.reset_state()


@pytest.mark.asyncio
async def test_disabled_gate_is_free(monkeypatch):
    _configure(monkeypatch, enabled=False, inflight=1, rpm=1)
    for _ in range(5):
        await gate.acquire()
    assert gate.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_unconfigured_limits_do_not_block(monkeypatch):
    _configure(monkeypatch, enabled=True, inflight=0, rpm=0)
    for _ in range(50):
        await gate.acquire()
    assert gate.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_inflight_limit_waits_then_fails_with_retry_after(monkeypatch):
    _configure(monkeypatch, enabled=True, inflight=1, rpm=0)
    await gate.acquire()
    assert gate.snapshot()["inflight"] == 1

    with pytest.raises(gate.FleetTimeout) as excinfo:
        await gate.acquire(timeout=0.05)
    assert 1 <= excinfo.value.retry_after <= 30

    gate.release()
    await gate.acquire(timeout=0.5)


@pytest.mark.asyncio
async def test_inflight_is_released_and_never_negative(monkeypatch):
    _configure(monkeypatch, enabled=True, inflight=2, rpm=0)
    await gate.acquire()
    await gate.acquire()
    assert gate.snapshot()["inflight"] == 2
    gate.release()
    gate.release()
    gate.release()
    gate.release()
    assert gate.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_rpm_bucket_blocks_after_burst_and_reports_wait(monkeypatch):
    # 60 rpm → 1 token/s，突发容量 10 秒的量
    _configure(monkeypatch, enabled=True, inflight=0, rpm=60)
    for _ in range(10):
        await gate.acquire()
    with pytest.raises(gate.FleetTimeout) as excinfo:
        await gate.acquire(timeout=0.05)
    assert excinfo.value.retry_after >= 1


@pytest.mark.asyncio
async def test_bounded_wait_absorbs_short_burst(monkeypatch):
    """突发应先被等待吸收，而不是立即失败——这是「跑得完」的关键。"""
    _configure(monkeypatch, enabled=True, inflight=1, rpm=0, wait=5.0)
    await gate.acquire()

    async def release_soon():
        await asyncio.sleep(0.05)
        gate.release()

    task = asyncio.create_task(release_soon())
    await gate.acquire(timeout=2.0)   # 应当等到释放后成功
    await task
    gate.release()
    assert gate.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_pacing_translates_fleet_timeout_into_routable_backpressure(monkeypatch):
    """闸门超时必须变成 API 层认识的背压异常，并保留真实 retry_after。"""
    import src.antigravity_pacing as pacing

    async def boom():
        raise gate.FleetTimeout(7)

    monkeypatch.setattr(gate, "acquire", boom)
    monkeypatch.setattr(config, "get_antigravity_dormant_enabled_sync", lambda: False)

    with pytest.raises(pacing.CredentialSlotWaitTimeout) as excinfo:
        await pacing.acquire_credential_slot("acct.json")
    error = excinfo.value
    assert error.rate_limit.reason == "fleet_busy"
    assert error.retry_after == 7


def test_backpressure_response_uses_real_retry_after():
    from src.antigravity_pacing import CredentialSlotWaitTimeout
    from src.rate_limits import RateLimit
    from src.api.antigravity import _credential_slot_backpressure_response

    error = CredentialSlotWaitTimeout("acct.json")
    error.rate_limit = RateLimit("gateway", "fleet_busy")
    error.retry_after = 9
    response = _credential_slot_backpressure_response(error)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "9"
