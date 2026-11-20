"""Low-frequency IP observations shared by accounts on the same configured route.

State is per event loop/worker, bounded, and contains no raw proxy credentials.
This does not attest the route used by a later OAuth or model request.
"""
import asyncio
import hashlib
import json
import math
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from log import log
from src.operational_audit import emit as audit

SUCCESS_TTL = 1800.0
FAILURE_TTL = 300.0
# A single transient 429 must not freeze every probe for the whole provider:
# batch imports and health checks share this state, and a 5 minute hold turned
# one throttled node into "no free egress IP" for the entire batch.
RATE_LIMIT_TTL = 30.0
COOLDOWN_DEFAULT = 30.0
COOLDOWN_MAX = 60.0
MIN_INTERVAL = 2.0
MAX_ENTRIES = 1024
# Throttling is applied per route first so one unlucky exit cannot stop the
# other exits of the same batch from being probed. Only a sustained streak of
# throttled answers, which means the echo provider is refusing the deployment
# as a whole, escalates to a provider-wide hold.
PROVIDER_TRIP_STREAK = 5


@dataclass(frozen=True)
class Observation:
    status: int
    ip: str | None = None
    country: str | None = None
    observed_at: float | None = None
    cached: bool = False
    error_type: str | None = None


class _State:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.cache = OrderedDict()
        # Per-route holds: a throttled answer says nothing about the other exits.
        self.cooldowns = OrderedDict()
        # Provider-wide hold, armed only after PROVIDER_TRIP_STREAK throttled
        # answers in a row, so a deployment-wide refusal still short-circuits.
        self.provider_cooldowns = OrderedDict()
        self.provider_streak = OrderedDict()
        self.next_start = 0.0


_states = weakref.WeakKeyDictionary()


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _put(mapping, key, value):
    mapping[key] = value
    mapping.move_to_end(key)
    while len(mapping) > MAX_ENTRIES:
        mapping.popitem(last=False)


def _retry_after(headers):
    """Honor Retry-After, but cap the hold so one 429 cannot freeze a whole batch.

    A provider asking for an hour must not stop every other route from being
    probed; the throttle is retried after COOLDOWN_MAX at the latest.
    """
    value = headers.get('Retry-After', '')
    delay = None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = None
    if delay is None or not math.isfinite(delay):
        return COOLDOWN_DEFAULT
    return min(max(delay, 1.0), COOLDOWN_MAX)


async def observe_egress(url, proxy_url, fetch, parse):
    """One outstanding probe per worker; cached failures never trigger retries.

    fetch(proxy) returns a response. parse(response) validates and returns IP/country.
    The inherited proxy is resolved before deriving the cache identity.
    """
    if proxy_url is ...:
        from config import get_proxy_config
        proxy_url = await get_proxy_config()
    route = _digest(str(proxy_url))
    key = _digest(url + '\0' + route)
    provider = _digest((urlsplit(url).hostname or url).lower())
    loop = asyncio.get_running_loop()
    state = _states.setdefault(loop, _State())
    async with state.lock:
        now = time.monotonic()
        existing = state.cache.get(key)
        if existing and existing[0] > now:
            state.cache.move_to_end(key)
            return replace(existing[1], cached=True)
        if state.provider_cooldowns.get(provider, 0) > now:
            # `error_type` stays empty on purpose: a throttled echo is not a
            # probe failure, and callers treat error_type as a hard failure.
            return Observation(status=429, cached=True)
        if state.cooldowns.get(key, 0) > now:
            # This route itself was throttled moments ago; other routes keep
            # their own schedule instead of inheriting this hold.
            return Observation(status=429, cached=True)
        delay = state.next_start - now
        if delay > 0:
            await asyncio.sleep(delay)
        state.next_start = time.monotonic() + MIN_INTERVAL
        # Reserve a negative cache entry before I/O: caller cancellation cannot
        # turn a failing endpoint into a retry storm.
        failure = Observation(status=0, error_type='ProbeInterrupted')
        _put(state.cache, key, (time.monotonic() + FAILURE_TTL, failure))
        try:
            response = await fetch(proxy_url)
            if response.status_code == 200:
                ip, country = parse(response)
                result = Observation(200, ip, country, time.time())
                # A working route proves the provider is not refusing us.
                state.provider_streak.pop(provider, None)
            else:
                result = Observation(response.status_code)
                if response.status_code in (429, 503):
                    hold = _retry_after(getattr(response, 'headers', {}))
                    _put(state.cooldowns, key, time.monotonic() + hold)
                    streak = int(state.provider_streak.get(provider, 0)) + 1
                    _put(state.provider_streak, provider, streak)
                    if streak >= PROVIDER_TRIP_STREAK:
                        # Every recent answer was throttled: stop probing the
                        # provider for a while instead of burning the budget.
                        _put(state.provider_cooldowns, provider, time.monotonic() + hold)
        except Exception as exc:
            result = Observation(status=0, error_type=type(exc).__name__)
        if result.status == 200:
            ttl = SUCCESS_TTL
        elif result.status == 429:
            # The throttle window is short; retry the same route once it passes.
            ttl = RATE_LIMIT_TTL
        else:
            ttl = FAILURE_TTL
        _put(state.cache, key, (time.monotonic() + ttl, result))
        # Log actual samples only, not every cache read. Never log URLs or auth.
        try:
            audit('egress_observation', route_ref=route[:24], probe_ref=key[:24],
                  checked_at=time.time(), observed_at=result.observed_at, status=result.status,
                  ip=result.ip, country=result.country, error_type=result.error_type)
            log.info('[EGRESS_OBSERVATION] ' + json.dumps({
                'route_ref': route[:24], 'probe_ref': key[:24],
                'checked_at': time.time(), 'observed_at': result.observed_at,
                'status': result.status, 'ip': result.ip, 'country': result.country,
                'error_type': result.error_type,
            }))
        except Exception:
            pass  # Logging failure must not turn a valid sample into proxy failure.
        return result
