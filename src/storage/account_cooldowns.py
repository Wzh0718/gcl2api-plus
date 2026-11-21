"""Short-lived negative selection hints. Only the main database grants a claim."""

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass

from log import log
from redis.exceptions import RedisError

from src.redis_config import RedisConfigurationError, create_redis_client_from_env


@dataclass(frozen=True)
class CachedRejection:
    usage_resume_at: int
    retry_at: float


class AccountCooldowns:
    # Bound stale negatives after account imports, policy changes and manual resets.
    lifetime = 1.0

    def __init__(self, database_identity):
        digest = hashlib.sha256(database_identity.encode()).hexdigest()
        self.key = f"gcli:account-cooldowns:v1:{digest}"
        try:
            self.client = create_redis_client_from_env(
                socket_connect_timeout=0.1, socket_timeout=0.1
            )
        except RedisConfigurationError:
            self.client = None
            log.warning("Account cooldown cache has invalid Redis configuration")
        self.snapshot = None
        self.next_read = 0
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

    async def read(self):
        now = time.time()
        if self.snapshot and self.snapshot[0] > now:
            return self.snapshot[1]
        if self.client and time.monotonic() >= self.next_read:
            self.next_read = time.monotonic() + 0.05
            raw = await self._redis(lambda: self.client.get(self.key))
            if raw:
                try:
                    expires, rows = json.loads(raw)
                    if (time.time() < expires <= time.time() + self.lifetime
                            and isinstance(rows, list)
                            and all(isinstance(name, str) and isinstance(usage, (int, float))
                                    and math.isfinite(usage) and isinstance(hold, (int, float))
                                    and math.isfinite(hold) and isinstance(models, dict)
                                    for name, usage, hold, models in rows)):
                        self.snapshot = expires, rows
                        return rows
                except (TypeError, ValueError):
                    pass
        return None

    async def publish(self, started, rows):
        expires = started + self.lifetime
        if expires <= time.time():
            return
        self.snapshot = expires, rows
        if self.client:
            await self._redis(lambda: self.client.set(
                self.key, json.dumps(self.snapshot), pxat=int(expires * 1000)
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
