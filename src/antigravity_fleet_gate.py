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

只在单进程内有意义：生产是 `python web.py` 单进程 + 4 核，故用进程内计数器。
若将来改成多 worker，需要把计数器挪到 Redis 或 DB。
"""

from __future__ import annotations

import asyncio
import math
import time

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


class FleetTimeout(Exception):
    """全池闸门在等待上限内未放行。"""

    def __init__(self, retry_after: int):
        super().__init__("fleet capacity busy")
        self.retry_after = retry_after


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


async def acquire(timeout: float | None = None) -> None:
    """取一个全池许可；超时抛 FleetTimeout。

    未启用或未配置上限时立即返回，不引入任何开销。
    """
    global _INFLIGHT, _TOKENS
    enabled, inflight_limit, rpm_limit, wait_seconds = _config()
    if not enabled or (inflight_limit <= 0 and rpm_limit <= 0):
        return

    deadline = time.monotonic() + (wait_seconds if timeout is None else timeout)
    rate = (rpm_limit / 60.0) if rpm_limit > 0 else 0.0
    capacity = max(rate * _BURST_SECONDS, 1.0) if rate > 0 else 0.0
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


def release() -> None:
    """归还全池许可；可重复调用安全（下限 0）。"""
    global _INFLIGHT
    if _INFLIGHT > 0:
        _INFLIGHT -= 1


def snapshot() -> dict:
    """当前闸门状态，供健康/看板读取。"""
    enabled, inflight_limit, rpm_limit, wait_seconds = _config()
    rate = (rpm_limit / 60.0) if rpm_limit > 0 else 0.0
    capacity = max(rate * _BURST_SECONDS, 1.0) if rate > 0 else 0.0
    _refill(rate, capacity)
    return {
        "enabled": enabled,
        "inflight": _INFLIGHT,
        "inflight_limit": inflight_limit,
        "rpm_limit": rpm_limit,
        "tokens": round(_TOKENS, 2),
        "wait_seconds": wait_seconds,
        "retry_after": _retry_after_for(rate),
    }


def reset_state() -> None:
    """测试用：清空计数与令牌。"""
    global _INFLIGHT, _TOKENS, _LAST_REFILL, _STATE_LOCK, _WAKEUP
    _INFLIGHT = 0
    _TOKENS = 0.0
    _LAST_REFILL = 0.0
    _STATE_LOCK = None
    _WAKEUP = None
