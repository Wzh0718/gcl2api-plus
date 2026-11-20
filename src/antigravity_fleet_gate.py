"""全池闸门：为「极端负载下仍能跑完」提供全局准入。

背景（2026-11-20 生产实测）：

- 现有 `antigravity_account_usage` 只作用于**单账号**，全池没有任何总量限制；
  调用方一涌而上就直接透传到上游。
- 11-19 18:03 高峰期一小时 28,066 次进站，其中 18,346 次以本地 429 结束。
  这些请求不是"失败一次"，而是**永远失败**：`Retry-After` 被写死为 1 秒，
  客户端立刻重试，再次撞墙，形成热循环。
- 结果就是极端负载下大批任务跑不完，同时把账号池的冷却/背压进一步拉满。

本模块提供两件事：

1. 全池级准入（在飞并发上限 + 每分钟令牌桶），跨账号生效；
2. 有界等待：突发先在闸门前排队吸收（默认 60s），排不下才快速失败，
   并给出**诚实的 Retry-After**（按令牌补充速度计算），让批量任务退避后
   仍然能跑完，而不是被打成永久失败。

后端（`ANTIGRAVITY_FLEET_BACKEND`）：

- `auto`（默认）：配了 `REDIS_URL` 就用 Redis，否则用进程内计数；
- `memory`：强制进程内，Redis 维护/重启时的降级口；
- `redis`：强制 Redis；连不上时仍降级到进程内，并限频告警。

Redis 侧状态全部自愈，不需要额外的清理任务：

- 在飞许可放在 ZSET（member=许可 id，score=过期时刻）；每次准先前按分数清理
  过期成员，所以进程崩溃、请求被取消、释放投递失败的许可都会在 TTL 后
  自动回收，不会把计数永久顶高；
- 令牌桶放在 HASH（tokens/ts），准入时按时间差补充，没有后台定时器；
- 准入是一个 Lua 脚本（清理 + 判并发 + 补令牌 + 记账）一次完成，多 worker /
  多副本共享同一份额度。

`release()` 保持同步（`finally` / 取消路径都能直接调）：Redis 释放投递到进程内
小队列，由后台协程合并成批执行；没有运行中的事件循环时直接丢弃，许可由
TTL 回收——宁可短暂少一个额度，也不在同步路径里阻塞。
"""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid

from log import log

_STATE_LOCK: asyncio.Lock | None = None
_INFLIGHT = 0
_TOKENS = 0.0
_LAST_REFILL = 0.0
_WAKEUP: asyncio.Event | None = None

# 令牌桶容量按 10 秒的量计，避免"攒一分钟额度再一次性打出去"。
_BURST_SECONDS = 10.0
# Retry-After 上限，避免给出离谱的等待时间。
_MAX_RETRY_AFTER = 30
# Redis 出错后的冷却：这段时间内直接走进程内计数，不再每请求重试。
_REDIS_ERROR_BACKOFF_SECONDS = 30.0
_REDIS_WARN_INTERVAL_SECONDS = 30.0
# 许可 TTL：必须长于最长请求（流式可能几分钟），又要能在进程崩溃后自愈。
_SLOT_TTL_SECONDS_DEFAULT = 900.0
_SLOT_TTL_SECONDS_MIN = 30.0
_SLOT_TTL_SECONDS_MAX = 7200.0
_RELEASE_BATCH_MAX = 64

# 一次完成：清过期许可 → 判在飞上限 → 补令牌 → 记账。返回 {granted, reason, value}：
# reason 0 = 令牌不足（value 是预计等待毫秒），reason 1 = 在飞已满（value 是在飞数）。
_ACQUIRE_LUA = """
local now = tonumber(ARGV[1])
local inflight_limit = tonumber(ARGV[2])
local rate = tonumber(ARGV[3])
local capacity = tonumber(ARGV[4])
local slot_ttl = tonumber(ARGV[5])
local slot_id = ARGV[6]

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local inflight = redis.call('ZCARD', KEYS[1])
if inflight_limit > 0 and inflight >= inflight_limit then
  return {0, 1, inflight}
end

if rate > 0 then
  local state = redis.call('HMGET', KEYS[2], 'tokens', 'ts')
  local tokens = tonumber(state[1])
  local ts = tonumber(state[2])
  if tokens == nil or ts == nil then
    tokens = capacity
  else
    tokens = math.min(capacity, tokens + math.max(now - ts, 0.0) / 1000.0 * rate)
  end
  if tokens < 1.0 then
    redis.call('HSET', KEYS[2], 'tokens', tokens, 'ts', now)
    return {0, 0, math.ceil((1.0 - tokens) / rate * 1000.0)}
  end
  redis.call('HSET', KEYS[2], 'tokens', tokens - 1.0, 'ts', now)
end

redis.call('ZADD', KEYS[1], now + slot_ttl * 1000.0, slot_id)
return {1, 0, 0}
"""

# 重复释放天然安全：ZREM 只删自己那一条，计数永远是集合基数。
_RELEASE_LUA = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""

# 进程内持有的许可：(backend, slot_id)。release 按 LIFO 归还，和 acquire 对称。
_PERMITS: list[tuple[str, str]] = []

_REDIS_CLIENT: object | None = None
_REDIS_BACKEND: "_RedisBackend | None" = None
_REDIS_INIT_LOCK: asyncio.Lock | None = None
_REDIS_DOWN_UNTIL = 0.0
_REDIS_WARNED_AT = 0.0

_RELEASE_QUEUE: asyncio.Queue | None = None
_RELEASE_WORKER: asyncio.Task | None = None
_DROPPED_RELEASES = 0


class FleetTimeout(Exception):
    """全池闸门在等待上限内未放行。"""

    def __init__(self, retry_after: int):
        super().__init__("fleet capacity busy")
        self.retry_after = retry_after


class _RedisBackend:
    """把一个 Redis 客户端包成闸门后端；脚本注册一次，后续走 EVALSHA。"""

    def __init__(self, client: object, prefix: str):
        self.client = client
        self.inflight_key = f"{prefix}:inflight"
        self.tokens_key = f"{prefix}:tokens"
        self._acquire_script = client.register_script(_ACQUIRE_LUA)
        self._release_script = client.register_script(_RELEASE_LUA)

    async def acquire(
        self,
        slot_id: str,
        *,
        now_ms: float,
        inflight_limit: int,
        rate: float,
        capacity: float,
        slot_ttl: float,
    ) -> tuple[bool, int, float]:
        raw = await self._acquire_script(
            keys=[self.inflight_key, self.tokens_key],
            args=[now_ms, inflight_limit, rate, capacity, slot_ttl, slot_id],
        )
        granted = int(raw[0]) == 1
        reason = int(raw[1])
        value = float(raw[2])
        return granted, reason, value

    async def release(self, slot_id: str) -> int:
        return int(await self._release_script(keys=[self.inflight_key], args=[slot_id]))


def _lock() -> asyncio.Lock:
    global _STATE_LOCK, _WAKEUP
    if _STATE_LOCK is None:
        _STATE_LOCK = asyncio.Lock()
    if _WAKEUP is None:
        _WAKEUP = asyncio.Event()
    return _STATE_LOCK


def _config() -> tuple[bool, int, int, float]:
    from config import (
        get_antigravity_fleet_enabled_sync,
        get_antigravity_fleet_inflight_sync,
        get_antigravity_fleet_rpm_sync,
        get_antigravity_fleet_wait_seconds_sync,
    )

    return (
        get_antigravity_fleet_enabled_sync(),
        get_antigravity_fleet_inflight_sync(),
        get_antigravity_fleet_rpm_sync(),
        get_antigravity_fleet_wait_seconds_sync(),
    )


def _backend_requested() -> str:
    """auto → 配了 REDIS_URL 就用 Redis；memory → 强制进程内。"""
    requested = os.getenv("ANTIGRAVITY_FLEET_BACKEND", "auto").strip().lower()
    if requested in {"memory", "local", "in-process"}:
        return "memory"
    if not os.getenv("REDIS_URL", "").strip():
        return "memory"
    return "redis"


def _redis_prefix() -> str:
    return os.getenv("ANTIGRAVITY_FLEET_REDIS_PREFIX", "gcli:fleet:v1").strip().strip(":") or "gcli:fleet:v1"


def _slot_ttl_seconds() -> float:
    raw = os.getenv("ANTIGRAVITY_FLEET_SLOT_TTL_SECONDS", "").strip()
    if not raw:
        return _SLOT_TTL_SECONDS_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return _SLOT_TTL_SECONDS_DEFAULT
    if not math.isfinite(value):
        return _SLOT_TTL_SECONDS_DEFAULT
    return min(max(value, _SLOT_TTL_SECONDS_MIN), _SLOT_TTL_SECONDS_MAX)


def _mark_redis_down(exc: BaseException) -> None:
    """Redis 出错：冷却期内走进程内计数，并限频告警。"""
    global _REDIS_DOWN_UNTIL, _REDIS_WARNED_AT, _REDIS_BACKEND, _REDIS_CLIENT
    _REDIS_DOWN_UNTIL = time.monotonic() + _REDIS_ERROR_BACKOFF_SECONDS
    client, _REDIS_CLIENT = _REDIS_CLIENT, None
    _REDIS_BACKEND = None
    if client is not None:
        aclose = getattr(client, "aclose", None)
        if callable(aclose):
            try:
                asyncio.ensure_future(aclose())
            except Exception:  # 没有运行中的事件循环：交给进程退出时回收
                pass
    now = time.monotonic()
    if now - _REDIS_WARNED_AT >= _REDIS_WARN_INTERVAL_SECONDS:
        _REDIS_WARNED_AT = now
        log.warning(
            f"[FLEET] Redis 后端不可用（{type(exc).__name__}），"
            f"降级进程内计数 {int(_REDIS_ERROR_BACKOFF_SECONDS)}s"
        )


async def _redis_backend() -> _RedisBackend | None:
    global _REDIS_CLIENT, _REDIS_BACKEND, _REDIS_INIT_LOCK
    if time.monotonic() < _REDIS_DOWN_UNTIL:
        return None
    if _REDIS_BACKEND is not None:
        return _REDIS_BACKEND
    if _REDIS_INIT_LOCK is None:
        _REDIS_INIT_LOCK = asyncio.Lock()
    async with _REDIS_INIT_LOCK:
        if time.monotonic() < _REDIS_DOWN_UNTIL:
            return None
        if _REDIS_BACKEND is not None:
            return _REDIS_BACKEND
        try:
            from src.redis_config import create_redis_client_from_env, redis_socket_timeouts

            connect_timeout, socket_timeout = redis_socket_timeouts()
            client = create_redis_client_from_env(
                decode_responses=True,
                socket_connect_timeout=connect_timeout,
                socket_timeout=socket_timeout,
            )
            if client is None:
                return None
            await client.ping()
            _REDIS_CLIENT = client
            _REDIS_BACKEND = _RedisBackend(client, _redis_prefix())
            log.info(f"[FLEET] 全池闸门使用 Redis 后端（{_redis_prefix()}）")
            return _REDIS_BACKEND
        except Exception as exc:
            _mark_redis_down(exc)
            return None


def _refill(rate_per_second: float, capacity: float) -> None:
    """按经过时间补令牌；capacity <= 0 表示不启用令牌桶。"""
    global _TOKENS, _LAST_REFILL
    now = time.monotonic()
    if _LAST_REFILL <= 0:
        _TOKENS = capacity
        _LAST_REFILL = now
        return
    if rate_per_second <= 0:
        return
    _TOKENS = min(capacity, _TOKENS + (now - _LAST_REFILL) * rate_per_second)
    _LAST_REFILL = now


def _retry_after_for(rate_per_second: float) -> int:
    if rate_per_second <= 0:
        return 1
    missing = max(1.0 - _TOKENS, 0.0)
    return min(max(int(math.ceil(missing / rate_per_second)), 1), _MAX_RETRY_AFTER)


async def _acquire_local(
    deadline: float, inflight_limit: int, rate: float, capacity: float
) -> None:
    global _INFLIGHT, _TOKENS
    _lock()  # 确保状态对象已创建
    while True:
        async with _lock():
            _refill(rate, capacity)
            inflight_ok = inflight_limit <= 0 or _INFLIGHT < inflight_limit
            token_ok = rate <= 0 or _TOKENS >= 1.0
            if inflight_ok and token_ok:
                _INFLIGHT += 1
                if rate > 0:
                    _TOKENS -= 1.0
                return
            retry_after = _retry_after_for(rate)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FleetTimeout(retry_after)
        await asyncio.sleep(min(remaining, 0.05 if not token_ok else 0.2))


async def _acquire_redis(
    backend: _RedisBackend,
    deadline: float,
    inflight_limit: int,
    rate: float,
    capacity: float,
) -> str:
    slot_id = uuid.uuid4().hex
    slot_ttl = _slot_ttl_seconds()
    while True:
        granted, reason, value = await backend.acquire(
            slot_id,
            now_ms=time.time() * 1000.0,
            inflight_limit=inflight_limit,
            rate=rate,
            capacity=capacity,
            slot_ttl=slot_ttl,
        )
        if granted:
            return slot_id
        if reason == 0:
            # 令牌不足：脚本已给出按补充速度算出的等待毫秒。
            retry_after = min(max(int(math.ceil(value / 1000.0)), 1), _MAX_RETRY_AFTER)
            sleep_for = min(max(value / 1000.0, 0.005), 0.05)
        else:
            retry_after = 1
            sleep_for = 0.2
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FleetTimeout(retry_after)
        await asyncio.sleep(min(sleep_for, remaining))


async def acquire(timeout: float | None = None) -> None:
    """取一个全池许可；超时抛 FleetTimeout。

    未启用或未配置上限时立即返回，不引入任何开销。
    """
    enabled, inflight_limit, rpm_limit, wait_seconds = _config()
    if not enabled or (inflight_limit <= 0 and rpm_limit <= 0):
        return

    deadline = time.monotonic() + (wait_seconds if timeout is None else timeout)
    rate = (rpm_limit / 60.0) if rpm_limit > 0 else 0.0
    capacity = max(rate * _BURST_SECONDS, 1.0) if rate > 0 else 0.0

    if _backend_requested() == "redis":
        backend = await _redis_backend()
        if backend is not None:
            try:
                slot_id = await _acquire_redis(
                    backend, deadline, inflight_limit, rate, capacity
                )
            except FleetTimeout:
                raise
            except Exception as exc:
                # 只降级这一次，不在请求路径上重试 Redis；
                # 冷却期内后续请求直接走进程内计数。
                _mark_redis_down(exc)
            else:
                _PERMITS.append(("redis", slot_id))
                return

    await _acquire_local(deadline, inflight_limit, rate, capacity)
    _PERMITS.append(("memory", ""))


def _release_local() -> None:
    global _INFLIGHT
    if _INFLIGHT > 0:
        _INFLIGHT -= 1


def _queue_release(slot_id: str) -> None:
    """同步路径投递 Redis 释放；没有运行中的事件循环就丢弃，由 TTL 回收。"""
    global _RELEASE_QUEUE, _RELEASE_WORKER, _DROPPED_RELEASES
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _DROPPED_RELEASES += 1
        return
    if _RELEASE_QUEUE is None:
        _RELEASE_QUEUE = asyncio.Queue()
    _RELEASE_QUEUE.put_nowait(slot_id)
    if _RELEASE_WORKER is None or _RELEASE_WORKER.done():
        _RELEASE_WORKER = loop.create_task(_drain_releases())


async def _drain_releases() -> None:
    """把同步释放合并成批写 Redis：每批最多 64 个，一次循环发完。"""
    global _RELEASE_WORKER, _DROPPED_RELEASES
    try:
        while True:
            queue = _RELEASE_QUEUE
            if queue is None or queue.empty():
                return
            batch: list[str] = []
            while len(batch) < _RELEASE_BATCH_MAX and not queue.empty():
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                return
            backend = await _redis_backend()
            if backend is None:
                # 冷却期内不刷 Redis：这些许可由 TTL 自动回收。
                _DROPPED_RELEASES += len(batch)
                return
            try:
                for slot_id in batch:
                    await backend.release(slot_id)
            except Exception as exc:
                _mark_redis_down(exc)
                _DROPPED_RELEASES += len(batch)
                return
            await asyncio.sleep(0)
    finally:
        # 先摘掉 worker 标记再看一次队列：释放和收尾之间有竞态，
        # 漏掉的那一个许可会一直占着额度直到 TTL。
        _RELEASE_WORKER = None
        queue = _RELEASE_QUEUE
        if queue is not None and not queue.empty():
            try:
                _RELEASE_WORKER = asyncio.get_running_loop().create_task(_drain_releases())
            except RuntimeError:
                pass


def release() -> None:
    """归还全池许可；可重复调用安全（多调不会把计数打穿）。"""
    if not _PERMITS:
        return
    backend, slot_id = _PERMITS.pop()
    if backend == "memory":
        _release_local()
        return
    _queue_release(slot_id)


def snapshot() -> dict:
    """当前闸门状态，供健康/看板读取；Redis 存量在 Redis 侧，见 inflight_key。"""
    enabled, inflight_limit, rpm_limit, wait_seconds = _config()
    rate = (rpm_limit / 60.0) if rpm_limit > 0 else 0.0
    capacity = max(rate * _BURST_SECONDS, 1.0) if rate > 0 else 0.0
    _refill(rate, capacity)
    redis_ready = _REDIS_BACKEND is not None and time.monotonic() >= _REDIS_DOWN_UNTIL
    return {
        "enabled": enabled,
        "backend": _backend_requested(),
        "backend_ready": redis_ready if _backend_requested() == "redis" else True,
        "inflight": _INFLIGHT,
        "inflight_limit": inflight_limit,
        "rpm_limit": rpm_limit,
        "tokens": round(_TOKENS, 2),
        "wait_seconds": wait_seconds,
        "retry_after": _retry_after_for(rate),
        "permits_held": len(_PERMITS),
        "dropped_releases": _DROPPED_RELEASES,
        "redis_inflight_key": _redis_prefix() + ":inflight",
    }


def reset_state() -> None:
    """测试用：清空计数、令牌、许可与 Redis 连接状态。"""
    global _INFLIGHT, _TOKENS, _LAST_REFILL, _STATE_LOCK, _WAKEUP
    global _REDIS_CLIENT, _REDIS_BACKEND, _REDIS_INIT_LOCK, _REDIS_DOWN_UNTIL, _REDIS_WARNED_AT
    global _RELEASE_QUEUE, _RELEASE_WORKER, _DROPPED_RELEASES
    _INFLIGHT = 0
    _TOKENS = 0.0
    _LAST_REFILL = 0.0
    _STATE_LOCK = None
    _WAKEUP = None
    _REDIS_CLIENT = None
    _REDIS_BACKEND = None
    _REDIS_INIT_LOCK = None
    _REDIS_DOWN_UNTIL = 0.0
    _REDIS_WARNED_AT = 0.0
    _RELEASE_QUEUE = None
    _RELEASE_WORKER = None
    _DROPPED_RELEASES = 0
    _PERMITS.clear()
