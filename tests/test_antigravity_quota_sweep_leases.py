"""Whole-pool scan budgets and fair coverage across independent workers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from src import antigravity_quota_sweep as sweep


class Clock:
    def __init__(self):
        self.now = 100_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


class SharedRedis:
    """Redis string primitives are atomic; separate workers share this state."""

    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.expires = {}
        self.available = True

    def _check(self, key):
        if not self.available:
            raise ConnectionError("test Redis unavailable")
        if self.expires.get(key, float("inf")) <= self.clock.time():
            self.values.pop(key, None)
            self.expires.pop(key, None)

    async def set(self, key, value, *, nx=False, xx=False, ex=None, px=None):
        self._check(key)
        if (nx and key in self.values) or (xx and key not in self.values):
            return False
        self.values[key] = str(value)
        self.expires.pop(key, None)
        if ex is not None or px is not None:
            self.expires[key] = self.clock.time() + (ex if ex is not None else px / 1000)
        return True

    async def get(self, key):
        self._check(key)
        return self.values.get(key)

    async def delete(self, *keys):
        count = 0
        for key in keys:
            self._check(key)
            count += key in self.values
            self.values.pop(key, None)
            self.expires.pop(key, None)
        return count

    async def eval(self, script, numkeys, *args):
        if numkeys == 1:
            # Atomic compare-owner-and-PEXPIRE for the pool-round lease.
            key, owner, ttl_ms = args
            self._check(key)
            if self.values.get(key) != str(owner):
                return 0
            self.expires[key] = self.clock.time() + float(ttl_ms) / 1000
            return 1
        assert numkeys == 3
        # Owner-fenced account claim and cursor advance are one atomic operation.
        round_key, account_key, cursor_key, owner, ttl, filename = args
        for key in (round_key, account_key, cursor_key):
            self._check(key)
        if self.values.get(round_key) != str(owner):
            return -1
        if account_key in self.values:
            return 0
        self.values[account_key] = "1"
        self.expires[account_key] = self.clock.time() + float(ttl)
        self.values[cursor_key] = str(filename)
        self.expires.pop(cursor_key, None)
        return 1

    async def aclose(self):
        pass


@pytest.fixture
async def harness(monkeypatch, backend):
    clock = Clock()
    redis = SharedRedis(clock) if backend == "redis" else None
    sweep.reset_memory_leases()
    monkeypatch.setattr(sweep, "time", clock)
    monkeypatch.setattr(sweep, "_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(config, "get_antigravity_quota_sweep_batch", AsyncMock(return_value=2))
    monkeypatch.setattr(config, "get_antigravity_quota_sweep_interval_seconds", AsyncMock(return_value=600))
    monkeypatch.setattr(config, "get_antigravity_quota_sweep_account_interval_seconds", AsyncMock(return_value=3600))
    if redis is None:
        monkeypatch.delenv("REDIS_URL", raising=False)
    else:
        monkeypatch.setenv("REDIS_URL", "redis://test.invalid")
    states = {}
    probes = []
    manager = SimpleNamespace(get_creds_status=AsyncMock(return_value=states))
    monkeypatch.setattr("src.credential_manager.credential_manager", manager)

    async def probe(name, state):
        probes.append(name)
        return "quota_unavailable"

    monkeypatch.setattr(sweep, "_probe_account", probe)
    yield SimpleNamespace(clock=clock, redis=redis, states=states, probes=probes)
    sweep.reset_memory_leases()


def add_accounts(harness, count):
    for index in range(count):
        # Cold accounts retain priority forever when upstream quota is unavailable;
        # unknown and ordinary accounts must nevertheless get their first scan.
        harness.states[f"account-{index:02}.json"] = {
            "disabled": False,
            "tier": "pro" if index < 16 or index >= 18 else "unknown",
            "last_success": index,
            "model_cooldowns": {"gemini-2.5-flash": harness.clock.time() + 86400 + index}
            if index < 16 else {},
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_scan_reaches_cooled_unknown_and_ordinary_accounts(harness, backend):
    add_accounts(harness, 20)
    for _ in range(60):
        if backend == "redis":
            # Another worker must continue shared progress rather than starting
            # from its own process-local priority list each round.
            sweep.reset_memory_leases()
        await sweep.sweep_once()
        harness.clock.now += 600
    assert set(harness.probes) == set(harness.states)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_scan_batch_is_shared_until_round_expires(harness, backend):
    add_accounts(harness, 10)
    first = await sweep.sweep_once()
    if backend == "redis":
        sweep.reset_memory_leases()
    second = await sweep.sweep_once()
    assert first["scanned"] == 2
    assert second["scanned"] == 0
    assert len(harness.probes) == 2

    harness.clock.now += 599
    assert (await sweep.sweep_once())["scanned"] == 0
    harness.clock.now += 1
    assert (await sweep.sweep_once())["scanned"] == 2
    assert len(set(harness.probes)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_running_or_cancelled_scan_does_not_admit_another_round(harness, backend, monkeypatch):
    add_accounts(harness, 10)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def probe(name, state):
        harness.probes.append(name)
        entered.set()
        await blocked.wait()
        return "quota_unavailable"

    monkeypatch.setattr(sweep, "_probe_account", probe)
    running = asyncio.create_task(sweep.sweep_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if backend == "redis":
            sweep.reset_memory_leases()
        assert (await asyncio.wait_for(sweep.sweep_once(), 0.2))["scanned"] == 0
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert len(harness.probes) == 1

    # Cancellation consumes this round's budget; another worker cannot immediately
    # retry different accounts, and a later round can recover without manual reset.
    assert (await sweep.sweep_once())["scanned"] == 0
    harness.clock.now += 600
    blocked.set()
    assert (await sweep.sweep_once())["scanned"] == 2
    assert len(set(harness.probes)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["redis"])
async def test_configured_redis_outage_skips_then_recovers(harness):
    add_accounts(harness, 10)
    harness.redis.available = False
    failed = await sweep.sweep_once()
    assert failed["scanned"] == 0
    assert harness.probes == []
    harness.redis.available = True
    assert (await sweep.sweep_once())["scanned"] == 2
    assert len(harness.probes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["redis"])
async def test_old_worker_cancellation_cannot_shorten_new_owner_lease(harness, monkeypatch):
    add_accounts(harness, 10)
    old_entered = asyncio.Event()
    new_entered = asyncio.Event()
    blocked = asyncio.Event()

    async def probe(name, state):
        harness.probes.append(name)
        if len(harness.probes) == 1:
            old_entered.set()
        elif len(harness.probes) == 2:
            new_entered.set()
        await blocked.wait()
        return "quota_unavailable"

    monkeypatch.setattr(sweep, "_probe_account", probe)
    old_worker = asyncio.create_task(sweep.sweep_once())
    new_worker = None
    try:
        await asyncio.wait_for(old_entered.wait(), 1)
        # Model a stalled old process that could not renew its lease in time.
        harness.clock.now += 601
        sweep.reset_memory_leases()
        new_worker = asyncio.create_task(sweep.sweep_once())
        await asyncio.wait_for(new_entered.wait(), 1)
        old_worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old_worker
        harness.clock.now += 1
        assert (await asyncio.wait_for(sweep.sweep_once(), 0.2))["scanned"] == 0
        assert len(harness.probes) == 2
    finally:
        for task in (old_worker, new_worker):
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["redis"])
async def test_redis_outage_during_probe_cancels_work_and_recovers(harness, monkeypatch):
    add_accounts(harness, 10)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    trigger_heartbeat = asyncio.Event()

    class ControlledHeartbeat:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            await trigger_heartbeat.wait()

    async def probe(name, state):
        harness.probes.append(name)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(sweep, "asyncio", ControlledHeartbeat())
    monkeypatch.setattr(sweep, "_probe_account", probe)
    running = asyncio.create_task(sweep.sweep_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        harness.redis.available = False
        trigger_heartbeat.set()
        await asyncio.wait_for(running, 1)
        assert cancelled.is_set()
        assert len(harness.probes) == 1
    finally:
        if not running.done():
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running

    harness.redis.available = True
    harness.clock.now += 601

    async def recovered_probe(name, state):
        harness.probes.append(name)
        return "quota_unavailable"

    # Stop manually triggered heartbeats from spinning in the recovery round.
    monkeypatch.setattr(sweep, "asyncio", asyncio)
    monkeypatch.setattr(sweep, "_probe_account", recovered_probe)
    assert (await sweep.sweep_once())["scanned"] == 2
    assert len(set(harness.probes)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["redis"])
async def test_lost_owner_cannot_advance_cursor_or_start_another_probe(harness, monkeypatch):
    add_accounts(harness, 10)

    async def probe(name, state):
        harness.probes.append(name)
        # The first call suspends long enough to lose its round to another worker.
        # It returns before its own heartbeat notices the lost ownership.
        if len(harness.probes) == 1:
            await harness.redis.set(f"{sweep.SWEEP_LOCK_PREFIX}:round", "another-worker", xx=True, ex=600)
        return "quota_unavailable"

    monkeypatch.setattr(sweep, "_probe_account", probe)
    await sweep.sweep_once()
    assert len(harness.probes) == 1
    assert await harness.redis.get(f"{sweep.SWEEP_LOCK_PREFIX}:cursor") == harness.probes[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory"])
async def test_loop_jitter_does_not_skip_every_other_default_round(harness, monkeypatch):
    add_accounts(harness, 20)
    stop_at = harness.clock.time() + 3600

    class LoopClock:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            if harness.clock.time() + delay >= stop_at:
                raise asyncio.CancelledError
            harness.clock.now += delay

    monkeypatch.setattr(sweep, "asyncio", LoopClock())
    monkeypatch.setattr(sweep, "_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(sweep.random, "uniform", lambda low, high: low)
    monkeypatch.setattr(config, "get_antigravity_quota_sweep_enabled", AsyncMock(return_value=True))
    with pytest.raises(asyncio.CancelledError):
        await sweep.sweep_loop()
    assert len(harness.probes) == 12
