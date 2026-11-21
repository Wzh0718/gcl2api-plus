"""Short-lived negative selection hints. Only the main database grants a claim."""

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass
from urllib.parse import quote

from log import log
from redis.exceptions import RedisError

from src.redis_config import RedisConfigurationError, create_redis_client_from_env


# Publish a complete DB observation atomically. A delayed worker must never
# overwrite a newer observation. The index permits removal after an early reset.
_PUBLISH = """
local previous = redis.call('GET', KEYS[2])
if previous then
    local old = cjson.decode(previous)
    if tonumber(old.started) >= tonumber(ARGV[1]) then return 0 end
    for _, suffix in ipairs(old.markers) do redis.call('DEL', KEYS[1] .. suffix) end
end
local markers = {}
local last = tonumber(ARGV[2])
for i = 4, #ARGV, 3 do
    local suffix, value, expiry = ARGV[i], ARGV[i+1], tonumber(ARGV[i+2])
    redis.call('SET', KEYS[1] .. suffix, value, 'PXAT', expiry)
    table.insert(markers, suffix)
    last = math.max(last, expiry)
end
redis.call('SET', KEYS[2], cjson.encode({started=ARGV[1], markers=markers}), 'PXAT', last)
redis.call('SET', KEYS[1], ARGV[3], 'PXAT', ARGV[2])
return 1
"""

# Check only the requested model and the two account-wide markers. The roster
# expires independently so a new or re-enabled account becomes visible promptly.
_READ = """
local roster = redis.call('GET', KEYS[1])
if not roster then return false end
local data = cjson.decode(roster)
local result = {tostring(data[1])}
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
local function deadline(key)
    local ttl = redis.call('PTTL', key)
    if ttl == -2 then return 0 end
    if ttl == -1 then return false end
    return now + ttl
end
for _, account in ipairs(data[2]) do
    local prefix = KEYS[1] .. ':' .. account[2]
    local model_key = prefix .. ':model:' .. ARGV[1]
    local usage = deadline(prefix .. ':usage')
    local hold = deadline(prefix .. ':hold')
    local model = deadline(model_key)
    if not usage or not hold or not model then return false end
    if model > 0 and redis.call('GET', model_key) == '?' then return false end
    table.insert(result, {account[1], usage, hold, model})
end
return result
"""


@dataclass(frozen=True)
class CachedRejection:
    usage_resume_at: int
    retry_at: float


class AccountCooldowns:
    # Bound stale negatives after account imports, policy changes and manual resets.
    lifetime = 1.0

    def __init__(self, database_identity):
        digest = hashlib.sha256(database_identity.encode()).hexdigest()
        self.key = f"gcli:cooldown:v2:{{{digest[:20]}}}"
        try:
            self.client = create_redis_client_from_env(
                socket_connect_timeout=0.1, socket_timeout=0.1
            )
        except RedisConfigurationError:
            self.client = None
            log.warning("Account cooldown cache has invalid Redis configuration")
        self.snapshot = None
        self.redis_retry = 0
        log.info("Account cooldown cache backend: " + ("redis" if self.client else "memory"))

    async def _redis(self, operation):
        if self.client is None or time.monotonic() < self.redis_retry:
            return None
        try:
            async with asyncio.timeout(0.1):
                return await operation()
        except (RedisError, OSError, TimeoutError, ValueError) as exc:
            if time.monotonic() >= self.redis_retry:
                log.warning(f"Account cooldown cache unavailable: {type(exc).__name__}")
            self.redis_retry = time.monotonic() + 5

    async def read(self, model):
        if self.client and time.monotonic() >= self.redis_retry:
            result = await self._redis(lambda: self.client.eval(
                _READ, 1, self.key, quote(model or "", safe="@")
            ))
            if result:
                expires = int(result[0]) / 1000
                if expires > time.time():
                    rows = [(name.decode(), round(usage / 1000), hold / 1000, {model: until / 1000})
                            for name, usage, hold, until in result[1:]]
                    return expires, rows
            if time.monotonic() >= self.redis_retry:
                return None
        if self.snapshot and self.snapshot[0] > time.time():
            return self.snapshot
        return None

    async def publish(self, started, rows):
        expires = started + self.lifetime
        if expires <= time.time():
            return
        self.snapshot = expires, rows
        if not self.client or time.monotonic() < self.redis_retry:
            return

        roster = []
        markers = []
        now = time.time()
        for name, usage, hold, models in rows:
            account = quote(name, safe="@")
            roster.append((name, account))
            deadlines = [("usage", usage), ("hold", hold)]
            deadlines.extend(("model:" + quote(model, safe="@"), value)
                             for model, value in models.items() if value is not None)
            for kind, value in deadlines:
                if not isinstance(value, (int, float)):
                    value = math.inf  # Mirrors the SQL treatment of nonnumeric JSON.
                if value > now:
                    ttl_end = value if math.isfinite(value) else expires
                    marker = "1" if math.isfinite(value) else "?"
                    markers.extend((f":{account}:{kind}", marker, math.ceil(ttl_end * 1000)))

        expires_ms = int(expires * 1000)
        await self._redis(lambda: self.client.eval(
            _PUBLISH, 2, self.key, self.key + ":index", int(started * 1_000_000),
            expires_ms, json.dumps([expires_ms, roster]), *markers,
        ))

    @staticmethod
    def selection(rows, model, excluded, pinned, now):
        """Calculate blocked accounts and the existing rejection reason for this model."""
        earliest_usage = None
        earliest_recovery = math.inf
        blocked = set()
        has_candidate = False
        for name, usage, hold, cooldowns in rows:
            if name in excluded or (pinned and name != pinned):
                continue
            until = cooldowns.get(model)
            if until is not None and not isinstance(until, (float, int)):
                until = math.inf  # Same treatment as gcli_cooldown for nonnumeric JSON.
            earliest_recovery = min(earliest_recovery, max(until or 0, usage, hold))
            if hold > now or (until is not None and until > now):
                blocked.add(name)
            elif usage > now:
                earliest_usage = min(earliest_usage or usage, usage)
                blocked.add(name)
            else:
                has_candidate = True
        if blocked and not has_candidate:
            retry_at = earliest_recovery if math.isfinite(earliest_recovery) else 0
            return blocked, CachedRejection(earliest_usage or 0, retry_at)
        return blocked, None

    async def close(self):
        if self.client:
            await self.client.aclose()
