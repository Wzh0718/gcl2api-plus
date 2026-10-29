"""Antigravity 每账号请求节奏：并发闸门 + 起始错峰 + 429 自适应收紧。

动机演变：
- 2026-10-21（封控对齐）：真实官方客户端的请求间隔天然不均匀，网关多账号轮转
  发出的请求「恒定间隔 / 瞬时并发」是上游行为指纹识别的明显信号，因此引入
  每账号间隔 + 抖动。
- 2026-10-22（效率优化）：硬间隔把每个账号锁成串行（1500ms → 0.67 rps），
  而单账号实际可承载 15 个并发，业务侧表现为「每个请求先随机等 1~2 秒」。
  节奏控制因此拆成两层：

1. acquire/release_credential_slot()：**每账号并发闸门**（默认 15 个在飞请求），
   这是真实的容量约束。并发占满时等待释放；等待超过配置的排队上限（默认 30 秒）后
   放行（over-admit）并告警，保证业务延迟有界，而不是无上界排队。
2. pace_credential()：同账号两次「请求发起」之间的毫秒级错峰 + 抖动
   （默认 80ms ± 50%），只用于打散瞬时齐发，不再限制吞吐。
3. 429 自适应：同一账号瞬时 429 使并发上限对半收紧（下限 1），成功后每次 +1，
   逐步恢复到配置值——用上游真实信号找容量上限，而不是靠猜固定间隔。
"""

import asyncio
import random
import time
from typing import Dict

from log import log

_LOCKS: Dict[str, asyncio.Lock] = {}
_NEXT_FREE: Dict[str, float] = {}
_429_STRIKES: Dict[str, int] = {}
_SLOTS: Dict[str, "_Slot"] = {}

# over-admit 告警节流（同一账号）
_OVER_ADMIT_LOG_INTERVAL = 30.0
# 瞬时 429 连击冷却：基数 60s，每次连击翻倍，封顶 300s
_TRANSIENT_429_BASE_COOLDOWN = 60.0
_TRANSIENT_429_MAX_COOLDOWN = 300.0
# 状态清理阈值，避免号池频繁上下架导致字典膨胀
_MAX_TRACKED = 2000


class _Slot:
    """单账号的并发闸门状态（纯同步计数 + Event 唤醒）。"""

    __slots__ = ("inflight", "limit", "wakeup", "last_over_admit_log")

    def __init__(self, limit: int) -> None:
        self.inflight = 0
        self.limit = limit
        self.wakeup = asyncio.Event()
        self.last_over_admit_log = 0.0


def _configured_concurrency() -> int:
    from config import get_antigravity_pacing_concurrency_sync

    return max(get_antigravity_pacing_concurrency_sync(), 1)


def _lock_for(credential_name: str) -> asyncio.Lock:
    lock = _LOCKS.get(credential_name)
    if lock is None:
        if len(_LOCKS) >= _MAX_TRACKED:
            _LOCKS.clear()
            _NEXT_FREE.clear()
        lock = asyncio.Lock()
        _LOCKS[credential_name] = lock
    return lock


def _slot_for(credential_name: str) -> _Slot:
    slot = _SLOTS.get(credential_name)
    if slot is None:
        if len(_SLOTS) >= _MAX_TRACKED:
            _SLOTS.clear()
        slot = _Slot(_configured_concurrency())
        _SLOTS[credential_name] = slot
    return slot


async def pace_credential(credential_name: str) -> float:
    """按账号维度的发起错峰：最小间隔 × (1 ± 抖动)。

    在每次发往上游的尝试前调用；返回实际等待秒数（0 表示无需等待）。
    配置（面板 > env > 默认）：
    - antigravity_pacing_enabled：总开关
    - antigravity_pacing_base_ms：每账号发起错峰基准（毫秒，默认 80）
    - antigravity_pacing_jitter_pct：抖动幅度百分比（±pct 均匀随机）
    """
    if not credential_name:
        return 0.0
    from config import (
        get_antigravity_pacing_base_ms_sync,
        get_antigravity_pacing_enabled_sync,
        get_antigravity_pacing_jitter_pct_sync,
    )

    if not get_antigravity_pacing_enabled_sync():
        return 0.0
    base_ms = get_antigravity_pacing_base_ms_sync()
    if base_ms <= 0:
        return 0.0
    jitter_pct = get_antigravity_pacing_jitter_pct_sync()

    async with _lock_for(credential_name):
        now = time.monotonic()
        start = max(now, _NEXT_FREE.get(credential_name, 0.0))
        waited = start - now
        if waited > 0:
            await asyncio.sleep(waited)
        factor = 1.0 + random.uniform(-jitter_pct, jitter_pct) / 100.0
        interval = base_ms * factor / 1000.0
        _NEXT_FREE[credential_name] = start + interval

    if waited >= 1.0:
        log.debug(
            f"[PACING] 账号 {credential_name} 节奏等待 {waited:.2f}s（下间隔 {interval:.2f}s）"
        )
    return waited


async def acquire_credential_slot(credential_name: str) -> float:
    """获取该账号的一个并发槽位；返回等待秒数。

    - 有空位立即返回 0；
    - 占满时等待释放，最多等 antigravity_pacing_slot_wait_seconds（默认 30 秒）；
    - 超时后放行（over-admit）并告警，保证业务延迟有界。
    """
    if not credential_name:
        return 0.0
    from config import (
        get_antigravity_pacing_concurrency_sync,
        get_antigravity_pacing_enabled_sync,
        get_antigravity_pacing_slot_wait_seconds_sync,
    )

    if not get_antigravity_pacing_enabled_sync():
        return 0.0

    target = max(get_antigravity_pacing_concurrency_sync(), 1)
    slot = _slot_for(credential_name)
    # 面板下调立即生效；上调只通过 note_success() 逐步恢复（AIMD）
    if slot.limit > target:
        slot.limit = target

    started = time.monotonic()
    deadline = started + get_antigravity_pacing_slot_wait_seconds_sync()
    over_admitted = False
    while True:
        if slot.inflight < slot.limit:
            slot.inflight += 1
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            slot.inflight += 1
            over_admitted = True
            break
        slot.wakeup.clear()
        # clear 之后再确认一次，避免与同步 release（set）竞态丢唤醒
        if slot.inflight < slot.limit:
            continue
        try:
            await asyncio.wait_for(slot.wakeup.wait(), remaining)
        except asyncio.TimeoutError:
            continue

    waited = time.monotonic() - started
    if over_admitted and time.monotonic() - slot.last_over_admit_log > _OVER_ADMIT_LOG_INTERVAL:
        slot.last_over_admit_log = time.monotonic()
        log.warning(
            f"[PACING] 账号 {credential_name} 并发已满（上限 {slot.limit}，"
            f"在飞 {slot.inflight}），等待 {waited:.2f}s 后放行（over-admit）"
        )
    elif waited >= 1.0:
        log.debug(
            f"[PACING] 账号 {credential_name} 并发排队 {waited:.2f}s"
            f"（在飞 {slot.inflight}/{slot.limit}）"
        )
    return waited


def release_credential_slot(credential_name: str) -> None:
    """释放并发槽位。纯同步，可在 finally / 取消路径安全调用。"""
    if not credential_name:
        return
    slot = _SLOTS.get(credential_name)
    if slot is None:
        return
    if slot.inflight > 0:
        slot.inflight -= 1
    slot.wakeup.set()


def note_transient_429(credential_name: str) -> int:
    """记录一次瞬时 429（无明确重置时间），返回当前连击数。"""
    if not credential_name:
        return 1
    strikes = _429_STRIKES.get(credential_name, 0) + 1
    if len(_429_STRIKES) >= _MAX_TRACKED:
        _429_STRIKES.clear()
    _429_STRIKES[credential_name] = strikes
    slot = _SLOTS.get(credential_name)
    if slot is not None:
        tightened = max(slot.limit // 2, 1)
        if tightened != slot.limit:
            log.info(
                f"[PACING] 账号 {credential_name} 429 收紧并发上限 "
                f"{slot.limit} → {tightened}"
            )
            slot.limit = tightened
            slot.wakeup.set()
    return strikes


def note_success(credential_name: str) -> None:
    """上游成功后重置 429 连击计数，并把并发上限恢复一档（AIMD）。"""
    if not credential_name:
        return
    _429_STRIKES.pop(credential_name, None)
    slot = _SLOTS.get(credential_name)
    if slot is None:
        return
    target = _configured_concurrency()
    if slot.limit < target:
        slot.limit += 1


def transient_429_cooldown(credential_name: str) -> float:
    """瞬时 429 的冷却截止时间：连击升级（60s 翻倍，封顶 300s）。"""
    strikes = note_transient_429(credential_name)
    seconds = min(
        _TRANSIENT_429_BASE_COOLDOWN * (2 ** (strikes - 1)),
        _TRANSIENT_429_MAX_COOLDOWN,
    )
    if strikes > 1:
        log.info(
            f"[PACING] 账号 {credential_name} 连续第 {strikes} 次瞬时 429，"
            f"冷却升级为 {seconds:.0f}s"
        )
    return time.time() + seconds


def reset_state() -> None:
    """清空全部账号状态（测试用）。"""
    _LOCKS.clear()
    _NEXT_FREE.clear()
    _429_STRIKES.clear()
    _SLOTS.clear()
