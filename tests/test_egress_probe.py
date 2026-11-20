import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from src import egress_probe as probe


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch):
    clock = [1000.0]
    async def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(probe.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(probe.asyncio, 'sleep', sleep)
    probe._states.clear()
    return clock


def parse(response):
    return response.json()['ip'], 'US'


def fetcher():
    return AsyncMock(return_value=httpx.Response(200, json={'ip': '203.0.113.1'}))


@pytest.mark.asyncio
async def test_concurrent_accounts_share_one_probe_and_original_sample_time(fake_clock):
    fetch = fetcher()
    results = await asyncio.gather(*(probe.observe_egress('https://echo.test/ip', 'http://proxy', fetch, parse) for _ in range(50)))
    assert fetch.await_count == 1
    assert sum(not r.cached for r in results) == 1
    assert len({r.observed_at for r in results}) == 1
    fake_clock[0] += probe.SUCCESS_TTL + 1
    assert not (await probe.observe_egress('https://echo.test/ip', 'http://proxy', fetch, parse)).cached
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_distinct_routes_and_endpoint_changes_have_separate_cache_and_spacing(fake_clock):
    starts = []
    async def fetch(proxy):
        starts.append(fake_clock[0])
        return httpx.Response(200, json={'ip': '203.0.113.1'})
    for url, route in [('https://echo.test/ip', 'one'), ('https://echo.test/ip', 'two'), ('https://other.test/ip', 'two')]:
        await probe.observe_egress(url, route, fetch, parse)
    assert starts == [1000, 1002, 1004]


@pytest.mark.asyncio
async def test_inherited_proxy_is_resolved_before_caching(monkeypatch):
    config = AsyncMock(side_effect=['one', 'two', 'two'])
    monkeypatch.setattr('config.get_proxy_config', config)
    fetch = fetcher()
    for _ in range(3):
        await probe.observe_egress('https://echo.test/ip', ..., fetch, parse)
    assert [c.args[0] for c in fetch.await_args_list] == ['one', 'two']


@pytest.mark.asyncio
async def test_429_cooldown_is_capped_to_keep_other_routes_probeable(fake_clock):
    fetch = AsyncMock(return_value=httpx.Response(429, headers={'Retry-After': '1200'}))
    first = await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)
    assert first.status == 429 and first.error_type is None
    # A different route keeps its own schedule: one throttled exit must not stop
    # the rest of the pool from being probed.
    other = await probe.observe_egress('https://echo.test/ip', 'two', fetch, parse)
    assert other.status == 429 and not other.cached
    assert fetch.await_count == 2
    # The throttled route itself is held, and a provider asking for 20 minutes
    # must not freeze it that long.
    held = await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)
    assert held.cached and held.status == 429
    assert fetch.await_count == 2
    fake_clock[0] += probe.COOLDOWN_MAX + 1
    await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)
    assert fetch.await_count == 3


@pytest.mark.asyncio
async def test_throttled_route_is_retried_after_short_rate_limit_ttl(fake_clock):
    fetch = AsyncMock(side_effect=[httpx.Response(429), httpx.Response(200, json={'ip': '203.0.113.1'})])
    assert (await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)).status == 429
    # The 429 is cached for RATE_LIMIT_TTL, not the generic failure TTL, so the
    # same route is re-probed shortly after the throttle window instead of
    # staying poisoned for five minutes.
    fake_clock[0] += probe.RATE_LIMIT_TTL + 1
    assert (await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)).status == 200
    assert fetch.await_count == 2
    assert probe.RATE_LIMIT_TTL < probe.FAILURE_TTL


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [TimeoutError('private proxy password'), httpx.Response(200, json={}), httpx.Response(500)])
async def test_failures_are_cached(outcome, fake_clock):
    fetch = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    first = await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)
    second = await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)
    assert first.status != 200
    assert second.cached
    assert fetch.await_count == 1
    assert 'private' not in str(second)
    fake_clock[0] += probe.FAILURE_TTL + 1
    await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_probe_leaves_cooldown():
    fetch = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)
    result = await probe.observe_egress('https://echo.test/ip', 'one', fetch, parse)
    assert result.cached
    assert fetch.await_count == 1


def test_retry_after_is_bounded_and_defaults_to_short_cooldown(monkeypatch):
    monkeypatch.setattr(probe.time, 'time', lambda: 0)
    # Honored within the cap, never raised to the failure TTL, never above the cap.
    assert probe._retry_after({'Retry-After': '5'}) == 5
    assert probe._retry_after({'Retry-After': 'Thu, 01 Jan 1970 00:00:05 GMT'}) == 5
    assert probe._retry_after({'Retry-After': 'Thu, 01 Jan 1970 00:20:00 GMT'}) == probe.COOLDOWN_MAX
    assert probe._retry_after({'Retry-After': 'invalid'}) == probe.COOLDOWN_DEFAULT
    assert probe._retry_after({}) == probe.COOLDOWN_DEFAULT
    assert probe.COOLDOWN_MAX < probe.FAILURE_TTL


def test_state_cache_is_bounded():
    from collections import OrderedDict
    cache = OrderedDict()
    for i in range(probe.MAX_ENTRIES + 10):
        probe._put(cache, str(i), i)
    assert len(cache) == probe.MAX_ENTRIES
    assert '0' not in cache
