"""Antigravity 每账号请求节奏（随机抖动）与 429 连击冷却升级。

动机（2026-10-21 封控对齐）：真实官方客户端的请求间隔天然不均匀，
而网关多账号轮转发出的请求间隔过于机械（恒定间隔/瞬时并发），
是上游行为指纹识别的明显信号。本模块提供：

1. pace_credential()：每账号最小请求间隔 + ±抖动，在每次上游尝试前调用；
   多账号并发时聚合吞吐不受单账号间隔限制（每个账号各自排队）。
2. 429 连击升级：同一账号连续瞬时 429（无明确重置时间）时，短冷却按
   2 的幂次升级（60s → 120s → …，封顶 300s），成功后重置。
"""

import asyncio
import random
import time
from typing import Dict

from log import log

_LOCKS: Dict[str, asyncio.Lock] = {}
_NEXT_FREE: Dict[str, float] = {}
_429_STRIKES: Dict[str, int] = {}

# 瞬时 429 连击冷却：基数 60s，每次连击翻倍，封顶 300s
_TRANSIENT_429_BASE_COOLDOWN = 60.0
_TRANSIENT_429_MAX_COOLDOWN = 300.0
# 状态清理阈值，避免号池频繁上下架导致字典膨胀
_MAX_TRACKED = 2000


def _lock_for(credential_name: str) -> asyncio.Lock:
    lock = _LOCKS.get(credential_name)
    if lock is None:
        if len(_LOCKS) >= _MAX_TRACKED:
            _LOCKS.clear()
            _NEXT_FREE.clear()
        lock = asyncio.Lock()
        _LOCKS[credential_name] = lock
    return lock


async def pace_credential(credential_name: str) -> float:
    """按账号维度的请求节奏控制：最小间隔 × (1 ± 抖动)。

    在每次发往上游的尝试前调用；返回实际等待秒数（0 表示无需等待）。
    配置（面板 > env > 默认）：
    - antigravity_pacing_enabled：总开关
    - antigravity_pacing_base_ms：每账号最小请求间隔基准（毫秒）
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


def note_transient_429(credential_name: str) -> int:
    """记录一次瞬时 429（无明确重置时间），返回当前连击数。"""
    if not credential_name:
        return 1
    strikes = _429_STRIKES.get(credential_name, 0) + 1
    if len(_429_STRIKES) >= _MAX_TRACKED:
        _429_STRIKES.clear()
    _429_STRIKES[credential_name] = strikes
    return strikes


def note_success(credential_name: str) -> None:
    """上游成功后重置该账号的 429 连击计数。"""
    if credential_name:
        _429_STRIKES.pop(credential_name, None)


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
