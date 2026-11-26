"""Refresh account health outside inference, with a single Redis lease holder.

The main database's check timestamps are the durable work list. Restarts and
Redis recovery reconstruct due work; no tasks can silently disappear in a queue.
Without Redis, coordination is local to this process, as logged at startup.
"""
from __future__ import annotations

import asyncio
from contextvars import Context
import hashlib
import os
import time
import uuid
from urllib.parse import urlsplit

from log import log

_TASK = None
_WAKE = None
_LAST_SCAN = 0.0
_POLL_SECONDS = 10
_LEASE_SECONDS = 30
_RENEW = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('EXPIRE', KEYS[1], ARGV[2])
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


async def get_health_policy():
    from config import (get_antigravity_network_check_enabled,
                        get_antigravity_network_check_ttl_seconds,
                        get_antigravity_egress_ip_check_url)
    return (await get_antigravity_network_check_enabled(),
            await get_antigravity_network_check_ttl_seconds(),
            bool(await get_antigravity_egress_ip_check_url()))


def request_health_refresh():
    """Debounced, local notification only: never await Redis or a probe."""
    if _WAKE is not None and time.monotonic() - _LAST_SCAN >= _POLL_SECONDS:
        _WAKE.set()


async def refresh_due_accounts(manager, policy, *, before_probe=None):
    backend = manager._storage_adapter._backend
    names = await backend.health_refresh_candidates(policy)
    for name in names:
        # Re-read eligibility at execution time, including disable/pause changes
        # made while earlier accounts were waiting for the global probe limiter.
        if not await backend.health_refresh_candidates(policy, credential_name=name):
            continue
        if before_probe is not None:
            await before_probe()
        try:
            async with asyncio.timeout(90):
                await manager.check_antigravity_account_health(name)
        except Exception as exc:
            log.warning(f"[HEALTH REFRESH] credential={name} failed: {type(exc).__name__}")


async def run_with_lease(client, key, owner, scan, *, heartbeat_seconds=10):
    async def heartbeat():
        while True:
            await asyncio.sleep(heartbeat_seconds)
            if not await client.eval(_RENEW, 1, key, owner, _LEASE_SECONDS):
                raise RuntimeError("health refresh lease lost")

    work = asyncio.create_task(scan(), context=Context())
    renewal = asyncio.create_task(heartbeat(), context=Context())
    try:
        done, _ = await asyncio.wait((work, renewal), return_when=asyncio.FIRST_COMPLETED)
        if renewal in done:
            await renewal
        await work
    finally:
        work.cancel()
        renewal.cancel()
        await asyncio.gather(work, renewal, return_exceptions=True)


def _lease_key(backend):
    # Distinguish independent stores sharing Redis. Password rotation must not
    # create a second leader for the same database, and secrets never enter keys.
    if getattr(backend, "main_postgres", False):
        dsn = os.getenv("MAIN_POSTGRES_DSN")
    elif getattr(backend, "main_mysql", False):
        dsn = os.getenv("MAIN_MYSQL_DSN")
    else:
        dsn = None
    if dsn:
        url = urlsplit(dsn)
        postgres = getattr(backend, "main_postgres", False)
        identity = ("postgresql" if postgres else "mysql", url.hostname,
                    url.port or (5432 if postgres else 3306), url.path,
                    os.getenv("MAIN_POSTGRES_SCHEMA", "gcli_main") if postgres else "")
    else:
        identity = os.path.realpath(str(backend._db_path))
    digest = hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
    return f"gcli:health-refresh:v1:{digest}"


async def _refresh_once(manager, client, key):
    policy = await get_health_policy()
    if client is None:
        await refresh_due_accounts(manager, policy)
        return
    owner = uuid.uuid4().hex
    if not await client.set(key, owner, nx=True, ex=_LEASE_SECONDS):
        return
    async def fence_probe():
        if not await client.eval(_RENEW, 1, key, owner, _LEASE_SECONDS):
            raise RuntimeError("health refresh lease lost before probe")
    try:
        await run_with_lease(client, key, owner,
            lambda: refresh_due_accounts(manager, policy, before_probe=fence_probe))
    finally:
        await client.eval(_RELEASE, 1, key, owner)


async def _loop(manager, client, key):
    global _LAST_SCAN
    try:
        while True:
            _WAKE.clear()
            _LAST_SCAN = time.monotonic()
            try:
                await _refresh_once(manager, client, key)
            except Exception as exc:
                # Configured Redis unavailable: retry the distributed lease;
                # never silently start uncoordinated process-local consumers.
                log.warning(f"[HEALTH REFRESH] round postponed: {type(exc).__name__}")
            try:
                await asyncio.wait_for(_WAKE.wait(), timeout=_POLL_SECONDS)
            except TimeoutError:
                pass
    finally:
        if client is not None:
            await client.aclose()


async def start_health_refresh():
    global _TASK, _WAKE
    if _TASK is not None and not _TASK.done():
        return _TASK
    from src.credential_manager import credential_manager
    from src.redis_config import create_redis_client_from_env, redis_socket_timeouts
    manager = await credential_manager._get_or_create()
    connect, read = redis_socket_timeouts()
    client = create_redis_client_from_env(decode_responses=True,
        socket_connect_timeout=connect, socket_timeout=read)
    backend = manager._storage_adapter._backend
    _WAKE = asyncio.Event()
    log.info("[HEALTH REFRESH] backend=" + ("redis; durable work=main database" if client is not None
        else "memory (single process); restart reconstructs work from main database"))
    _TASK = asyncio.create_task(_loop(manager, client, _lease_key(backend)),
        name="antigravity_health_refresh", context=Context())
    return _TASK


async def stop_health_refresh():
    global _TASK, _WAKE
    if _TASK is not None:
        _TASK.cancel()
        await asyncio.gather(_TASK, return_exceptions=True)
    _TASK = _WAKE = None
