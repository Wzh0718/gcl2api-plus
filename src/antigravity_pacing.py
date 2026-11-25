"""Antigravity 请求准入与发起节奏。

所有请求使用全池许可与养号租约。普通文本直接使用轮转分配的账号。
图片与提示词融合另外占用单账号槽位，瞬时 429 收紧其上限，成功后逐步恢复。
"""

import asyncio
import hashlib
import random
import time
from datetime import datetime
from typing import Dict

from log import log
from src.request_timing import database_timing_scope, measure_phase

_LOCKS: Dict[str, asyncio.Lock] = {}
_NEXT_FREE: Dict[str, float] = {}
_429_STRIKES: Dict[str, int] = {}
_SLOTS: Dict[str, "_Slot"] = {}

# 背压告警节流（同一账号）
_BACKPRESSURE_LOG_INTERVAL = 30.0
# 瞬时 429 连击冷却：基数 60s，每次连击翻倍，封顶 300s
_TRANSIENT_429_BASE_COOLDOWN = 60.0
_TRANSIENT_429_MAX_COOLDOWN = 300.0
# 状态清理阈值，避免号池频繁上下架导致字典膨胀
_MAX_TRACKED = 2000


class CredentialSlotWaitTimeout(Exception):
    """账号并发槽位在等待上限内未释放，需要向调用方施加背压。"""

    def __init__(self, credential_name: str, *, warmup: bool = False):
        from src.rate_limits import RateLimit

        super().__init__(credential_name)
        self.rate_limit = RateLimit('gateway', 'warmup_capacity' if warmup else 'capacity_busy')


class CredentialDormant(CredentialSlotWaitTimeout):
    """账号处于自身每日静默窗口，本轮不参与调度。

    继承 CredentialSlotWaitTimeout，使 API 层现有的「换下一个账号」逻辑
    无需改动即可跳过该账号；rate_limit 用独立 reason 便于面板区分。
    """

    def __init__(self, credential_name: str):
        from src.rate_limits import RateLimit

        super().__init__(credential_name)
        self.rate_limit = RateLimit('gateway', 'dormant_window')


def _stable_digest(credential_name: str, purpose: str) -> int:
    raw = hashlib.sha256(f"{purpose}:{credential_name}".encode("utf-8")).digest()
    return int.from_bytes(raw[:8], "big")


def dormant_window_start_hour(credential_name: str) -> int:
    """账号每日静默窗口的起始小时（0-23），按账号名稳定分配。"""
    return _stable_digest(credential_name, "agy-dormant-hour") % 24


def is_dormant(credential_name: str, *, now: datetime | None = None) -> bool:
    """该账号此刻是否处于静默窗口。总开关关闭时恒为 False。"""
    if not credential_name:
        return False
    from config import (
        get_antigravity_dormant_enabled_sync,
        get_antigravity_dormant_hours_sync,
    )

    if not get_antigravity_dormant_enabled_sync():
        return False
    hours = get_antigravity_dormant_hours_sync()
    if hours <= 0:
        return False
    current = now or datetime.now()
    elapsed = (current.hour - dormant_window_start_hour(credential_name)) % 24
    return elapsed < hours


class _Slot:
    """单账号的并发闸门状态（纯同步计数 + Event 唤醒）。"""

    __slots__ = ("inflight", "limit", "wakeup", "last_backpressure_log")

    def __init__(self, limit: int) -> None:
        self.inflight = 0
        self.limit = limit
        self.wakeup = asyncio.Event()
        self.last_backpressure_log = 0.0


def _configured_concurrency(credential_name: str) -> int:
    from config import get_antigravity_account_concurrency

    return get_antigravity_account_concurrency(credential_name)["request_concurrency"]


def _speed_factor(credential_name: str) -> float:
    """按账号稳定派生「节奏人格」，让整池的间隔成为混合分布而不是同一个区间。

    真人终端之间请求密度差异很大（有人连续追问、有人隔几分钟），
    全池共用一个 uniform(min,max) 会让间隔分布呈现明显的均匀矩形。
    """
    if not credential_name:
        return 1.0
    bucket = _stable_digest(credential_name, "pace") % 100
    if bucket < 20:
        return 0.6
    if bucket < 80:
        return 1.0
    return 1.6


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
        slot = _Slot(_configured_concurrency(credential_name))
        _SLOTS[credential_name] = slot
    return slot


async def pace_credential(credential_name: str, *, require_dynamic: bool = False) -> float:
    """按账号维度的发起错峰。

    两种模式，取"动态区间"优先：

    1. 动态最小间隔（``ANTIGRAVITY_PACING_MIN/MAX_INTERVAL_SECONDS``，默认与 0 表示关闭）：
       每次发往上游的尝试前，在该账号的队列上按 ``uniform(min, max)`` 秒等待。
       用途是压低单账号请求密度（真人终端通常几十秒到几分钟一次），默认例：
       min=2、max=10 → 均值 6s，单账号 ≈0.17 rps。代价是吞吐按比例下降，
       必须按业务量显式开启。
    2. 固定基准错峰（``antigravity_pacing_base_ms``，默认 80ms + ±50% 抖动）：
       只用于打散瞬时齐发，不影响平均吞吐。

    调用约定：``require_dynamic=True``（文本请求）时，动态区间未启用就直接返回 0，
    不会引入固定基准的额外等待；图片请求沿用原行为。

    返回实际等待秒数（0 表示无需等待）。
    """
    if not credential_name:
        return 0.0
    from config import (
        get_antigravity_pacing_base_ms_sync,
        get_antigravity_pacing_enabled_sync,
        get_antigravity_pacing_jitter_pct_sync,
        get_antigravity_pacing_max_interval_seconds_sync,
        get_antigravity_pacing_min_interval_seconds_sync,
    )

    if not get_antigravity_pacing_enabled_sync():
        return 0.0

    interval: float
    low = get_antigravity_pacing_min_interval_seconds_sync()
    high = get_antigravity_pacing_max_interval_seconds_sync()
    if high > 0 and high >= low:
        factor = _speed_factor(credential_name)
        interval = random.uniform(low, high) * factor
    elif require_dynamic:
        return 0.0
    else:
        base_ms = get_antigravity_pacing_base_ms_sync()
        if base_ms <= 0:
            return 0.0
        jitter_pct = get_antigravity_pacing_jitter_pct_sync()
        factor = 1.0 + random.uniform(-jitter_pct, jitter_pct) / 100.0
        interval = base_ms * factor / 1000.0

    async with _lock_for(credential_name):
        now = time.monotonic()
        start = max(now, _NEXT_FREE.get(credential_name, 0.0))
        waited = start - now
        if waited > 0:
            await asyncio.sleep(waited)
        _NEXT_FREE[credential_name] = start + interval

    if waited >= 1.0:
        log.debug(
            f"[PACING] 账号 {credential_name} 节奏等待 {waited:.2f}s（下间隔 {interval:.2f}s）"
        )
    return waited


async def acquire_credential_slot(credential_name: str) -> float:
    """获取全池许可和养号租约，普通文本轮转无需等待账号槽位。"""
    if not credential_name:
        return 0.0
    from src.antigravity_dynamic_warmup import acquire_dynamic_warmup_slot

    if is_dormant(credential_name):
        log.debug(f"[PACING] 账号 {credential_name} 处于静默窗口，本轮跳过")
        raise CredentialDormant(credential_name)

    from src.antigravity_fleet_gate import FleetTimeout
    from src.antigravity_fleet_gate import acquire as fleet_acquire
    from src.antigravity_fleet_gate import release as fleet_release

    # 全池闸门先于账号闸门：极端负载下先在闸前排队吸收，排不下才快速失败，
    # 并带上按令牌补充速度算出的 Retry-After（而不是恒为 1 秒的热重试）。
    try:
        with measure_phase("slot_fleet"):
            await fleet_acquire()
    except FleetTimeout as exc:
        error = CredentialSlotWaitTimeout(credential_name)
        from src.rate_limits import RateLimit

        error.rate_limit = RateLimit('gateway', 'fleet_busy')
        error.retry_after = exc.retry_after
        raise error from None

    try:
        with measure_phase("slot_warmup"), database_timing_scope("slot_warmup"):
            warmup = await acquire_dynamic_warmup_slot(credential_name)
    except BaseException:
        fleet_release()
        raise
    if not warmup.get("acquired"):
        fleet_release()
        raise CredentialSlotWaitTimeout(credential_name, warmup=True)
    return 0.0


async def acquire_limited_credential_slot(credential_name: str) -> float:
    """图片及账号绑定的辅助请求另行占用单账号并发槽位。"""
    if not credential_name:
        return 0.0
    from src.antigravity_fleet_gate import release as fleet_release
    from config import (
        get_antigravity_pacing_enabled_sync,
        get_antigravity_pacing_slot_wait_seconds_sync,
    )
    from src.antigravity_dynamic_warmup import release_dynamic_warmup_slot

    await acquire_credential_slot(credential_name)
    try:
        if not get_antigravity_pacing_enabled_sync():
            return 0.0

        target = _configured_concurrency(credential_name)
        slot = _slot_for(credential_name)
        # 面板下调立即生效；上调只通过 note_success() 逐步恢复（AIMD）
        if slot.limit > target:
            slot.limit = target

        started = time.monotonic()
        deadline = started + get_antigravity_pacing_slot_wait_seconds_sync()
        while True:
            slot.limit = min(slot.limit, _configured_concurrency(credential_name))
            if slot.inflight < slot.limit:
                slot.inflight += 1
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                waited = time.monotonic() - started
                now = time.monotonic()
                if now - slot.last_backpressure_log > _BACKPRESSURE_LOG_INTERVAL:
                    slot.last_backpressure_log = now
                    log.warning(
                        f"[PACING] 账号 {credential_name} 并发已满（上限 {slot.limit}，"
                        f"在飞 {slot.inflight}），等待 {waited:.2f}s，触发本地背压"
                    )
                raise CredentialSlotWaitTimeout(credential_name)
            slot.wakeup.clear()
            # clear 之后再确认一次，避免与同步 release（set）竞态丢唤醒
            if slot.inflight < slot.limit:
                continue
            try:
                with measure_phase("slot_account_wait"):
                    await asyncio.wait_for(slot.wakeup.wait(), remaining)
            except asyncio.TimeoutError:
                continue

        waited = time.monotonic() - started
        if waited >= 1.0:
            log.debug(
                f"[PACING] 账号 {credential_name} 并发排队 {waited:.2f}s"
                f"（在飞 {slot.inflight}/{slot.limit}）"
            )
        return waited
    except BaseException:
        await release_dynamic_warmup_slot(credential_name)
        fleet_release()
        raise


def apply_account_concurrency(credential_name: str) -> None:
    """Apply an explicit panel edit immediately and wake queued requests."""
    slot = _SLOTS.get(credential_name)
    if slot is not None:
        slot.limit = _configured_concurrency(credential_name)
        slot.wakeup.set()


def release_credential_slot(credential_name: str) -> None:
    """归还本次请求的全池许可。"""
    if not credential_name:
        return
    from src.antigravity_fleet_gate import release as fleet_release

    fleet_release()


def release_limited_credential_slot(credential_name: str) -> None:
    release_credential_slot(credential_name)
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
    target = _configured_concurrency(credential_name)
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
