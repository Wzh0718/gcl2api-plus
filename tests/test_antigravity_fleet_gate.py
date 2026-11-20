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
def _reset(monkeypatch):
    # 默认走进程内后端；Redis 用例自己设置 REDIS_URL。
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_FLEET_BACKEND", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_FLEET_REDIS_PREFIX", raising=False)
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


# ---------------------------------------------------------------------------
# Redis 后端：多 worker/多副本共享同一份额度，同时保留进程内降级
# ---------------------------------------------------------------------------


class _ScriptedRedis:
    """只实现闸门用到的 register_script 的最小替身。"""

    def __init__(self, acquire_results, release_result=1):
        self._acquire_results = list(acquire_results)
        self._release_result = release_result
        self.acquire_calls = []
        self.released = []
        self.scripts = []

    def register_script(self, script):
        self.scripts.append(script)
        if "ZADD" in script:
            async def acquire(keys=None, args=None):
                self.acquire_calls.append((keys, args))
                if len(self._acquire_results) > 1:
                    return self._acquire_results.pop(0)
                return self._acquire_results[0]
            return acquire

        async def release(keys=None, args=None):
            self.released.append(args[0])
            return self._release_result
        return release


def _use_fake_redis(monkeypatch, fake):
    backend = gate._RedisBackend(fake, "gcli:fleet:v1")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")

    async def fake_backend():
        return backend

    monkeypatch.setattr(gate, "_redis_backend", fake_backend)
    return backend


def test_backend_defaults_to_memory_without_redis_url(monkeypatch):
    _configure(monkeypatch, enabled=True, inflight=1, rpm=0)
    assert gate.snapshot()["backend"] == "memory"

    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    assert gate.snapshot()["backend"] == "redis"

    monkeypatch.setenv("ANTIGRAVITY_FLEET_BACKEND", "memory")
    assert gate.snapshot()["backend"] == "memory"


@pytest.mark.asyncio
async def test_redis_backend_grants_permit_and_releases_in_background(monkeypatch):
    _configure(monkeypatch, enabled=True, inflight=5, rpm=0)
    fake = _ScriptedRedis([(1, 0, 0)])
    _use_fake_redis(monkeypatch, fake)

    await gate.acquire()
    assert gate.snapshot()["permits_held"] == 1
    assert len(fake.acquire_calls) == 1
    keys, args = fake.acquire_calls[0]
    assert keys == ["gcli:fleet:v1:inflight", "gcli:fleet:v1:tokens"]
    assert args[1] == 5          # 在飞上限
    assert args[2] == 0          # 未启用令牌桶

    gate.release()               # 同步调用，投递到后台队列
    for _ in range(20):
        if fake.released:
            break
        await asyncio.sleep(0.01)
    assert len(fake.released) == 1
    assert gate.snapshot()["permits_held"] == 0


@pytest.mark.asyncio
async def test_redis_token_shortage_reports_real_retry_after(monkeypatch):
    _configure(monkeypatch, enabled=True, inflight=0, rpm=600)
    fake = _ScriptedRedis([(0, 0, 2500)])   # 还差 2.5s 才有令牌
    _use_fake_redis(monkeypatch, fake)

    with pytest.raises(gate.FleetTimeout) as excinfo:
        await gate.acquire(timeout=0.05)
    assert excinfo.value.retry_after == 3
    assert gate.snapshot()["permits_held"] == 0


@pytest.mark.asyncio
async def test_redis_failure_degrades_to_memory_without_breaking_release(monkeypatch):
    _configure(monkeypatch, enabled=True, inflight=3, rpm=0)

    class Broken:
        def register_script(self, script):
            async def boom(keys=None, args=None):
                raise RuntimeError("redis down")
            return boom

    _use_fake_redis(monkeypatch, Broken())

    await gate.acquire()                     # 降级到进程内
    assert gate.snapshot()["inflight"] == 1
    assert gate.snapshot()["permits_held"] == 1

    gate.release()
    assert gate.snapshot()["inflight"] == 0

    # 冷却期内不再打 Redis
    assert gate._REDIS_DOWN_UNTIL > gate.time.monotonic()


def test_release_without_permit_is_noop():
    gate.release()
    gate.release()
    assert gate.snapshot()["inflight"] == 0
    assert gate.snapshot()["permits_held"] == 0


def test_slot_ttl_is_bounded(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_FLEET_SLOT_TTL_SECONDS", "5")
    assert gate._slot_ttl_seconds() == gate._SLOT_TTL_SECONDS_MIN
    monkeypatch.setenv("ANTIGRAVITY_FLEET_SLOT_TTL_SECONDS", "999999")
    assert gate._slot_ttl_seconds() == gate._SLOT_TTL_SECONDS_MAX
    monkeypatch.setenv("ANTIGRAVITY_FLEET_SLOT_TTL_SECONDS", "abc")
    assert gate._slot_ttl_seconds() == gate._SLOT_TTL_SECONDS_DEFAULT
