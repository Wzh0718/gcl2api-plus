"""Throttling must be scoped to the route that hit it, not to the whole provider.

A batch import probes one candidate exit per node. When the echo service answers
one route with 429, the other routes are usually fine and must still be probed.
"""
from types import SimpleNamespace

import pytest

import src.egress_probe as ep


def _ok(payload):
    return SimpleNamespace(status_code=200, headers={}, json=lambda: payload)


def _throttled(retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return SimpleNamespace(status_code=429, headers=headers)


def _parse(response):
    payload = response.json()
    country = str(payload.get("country") or "").upper() or None
    return payload["ip"], country


def _never(*_args, **_kwargs):
    raise AssertionError("parse must not run for a non-200 answer")


@pytest.mark.asyncio
async def test_throttled_route_does_not_silence_other_routes(monkeypatch):
    monkeypatch.setattr(ep, "MIN_INTERVAL", 0.0)
    url = "https://echo.example/ip"

    async def fetch(proxy_url):
        if "bad" in proxy_url:
            return _throttled(retry_after=30)
        return _ok({"ip": "198.51.100.9", "country": "us"})

    first = await ep.observe_egress(url, "http://bad:1", fetch, _parse)
    second = await ep.observe_egress(url, "http://good:1", fetch, _parse)

    assert first.status == 429
    # The second route still reached the provider instead of inheriting the hold.
    assert second.status == 200
    assert second.ip == "198.51.100.9"
    assert second.country == "US"
    assert second.cached is False


@pytest.mark.asyncio
async def test_throttled_route_is_held_locally(monkeypatch):
    monkeypatch.setattr(ep, "MIN_INTERVAL", 0.0)
    url = "https://echo.example/ip"
    calls = []

    async def fetch(proxy_url):
        calls.append(proxy_url)
        return _throttled(retry_after=30)

    await ep.observe_egress(url, "http://bad:1", fetch, _parse)
    repeat = await ep.observe_egress(url, "http://bad:1", fetch, _parse)

    assert repeat.status == 429
    assert repeat.cached is True
    # Same route is served from the hold instead of re-hitting the provider.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_provider_hold_only_after_a_sustained_streak(monkeypatch):
    monkeypatch.setattr(ep, "MIN_INTERVAL", 0.0)
    url = "https://echo.example/ip"
    calls = []

    async def fetch(proxy_url):
        calls.append(proxy_url)
        return _throttled()

    for index in range(ep.PROVIDER_TRIP_STREAK):
        await ep.observe_egress(url, f"http://n{index}:1", fetch, _parse)
    assert len(calls) == ep.PROVIDER_TRIP_STREAK

    # A brand new route is now short-circuited: the whole provider looks refused.
    extra = await ep.observe_egress(url, "http://n99:1", fetch, _parse)
    assert extra.status == 429
    assert extra.cached is True
    assert len(calls) == ep.PROVIDER_TRIP_STREAK


@pytest.mark.asyncio
async def test_success_resets_the_provider_streak(monkeypatch):
    monkeypatch.setattr(ep, "MIN_INTERVAL", 0.0)
    url = "https://echo.example/ip"
    calls = []

    async def fetch(proxy_url):
        calls.append(proxy_url)
        if len(calls) == 3:
            return _ok({"ip": "198.51.100.10"})
        return _throttled()

    for index in range(2):
        await ep.observe_egress(url, f"http://n{index}:1", fetch, _parse)
    good = await ep.observe_egress(url, "http://good:1", fetch, _parse)
    assert good.status == 200

    # The counter restarted, so the next throttled answers do not trip the
    # provider hold yet.
    after = await ep.observe_egress(url, "http://n9:1", fetch, _parse)
    assert after.status == 429
    fresh = await ep.observe_egress(url, "http://n10:1", fetch, _parse)
    assert fresh.status == 429
    assert fresh.cached is False
