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
MIN_INTERVAL = 2.0
MAX_ENTRIES = 1024


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
        self.cooldowns = OrderedDict()
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
    value = headers.get('Retry-After', '')
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = FAILURE_TTL
    return max(FAILURE_TTL, delay) if math.isfinite(delay) else FAILURE_TTL


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
        if state.cooldowns.get(provider, 0) > now:
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
            else:
                result = Observation(response.status_code)
                if response.status_code in (429, 503):
                    _put(state.cooldowns, provider, time.monotonic() + _retry_after(getattr(response, 'headers', {})))
        except Exception as exc:
            result = Observation(status=0, error_type=type(exc).__name__)
        ttl = SUCCESS_TTL if result.status == 200 else FAILURE_TTL
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
