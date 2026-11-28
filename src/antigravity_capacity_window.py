"""模型级容量 503 的短窗口熔断（按模型记，不按账号）。

上游 cloudcode-pa 对单个模型会返回 503 ``MODEL_CAPACITY_EXHAUSTED``，并在
``RetryInfo.retryDelay`` 里给出建议等待（线上样本 38–40 秒）。这个窗口与账号
无关：同一分钟内换号、换请求都会连续撞上，网关每次都要多烧一次上游往返才把
503 交给客户端。

这里只做一件事：把「这个模型刚才容量耗尽、建议等 N 秒」记在进程内，供入口
决定是否直接走降级链的下一个模型。窗口内的请求不会被拒绝——容量窗口是
「大概率被拒」而不是「一定被拒」，实测同一窗口里仍有 200；没有降级链时照常
尝试，由上游决定。

后端固定在进程内存：窗口只有 5–300 秒，属于单进程内的即时提示；为它每次请求
加一次 Redis 往返，成本高于收益。多副本各自学习、各自生效，窗口内只是少一次
无谓的上游尝试。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional

# 上游没给 RetryInfo 时的默认窗口。容量抖动一般持续几十秒，取 30 秒。
DEFAULT_WINDOW_SECONDS = 30.0
# 下限防止上游给 0/负数时窗口立刻失效，上限防止离谱的 retryDelay 把模型锁死。
MIN_WINDOW_SECONDS = 5.0
MAX_WINDOW_SECONDS = 300.0

# 模型名 → 窗口截止时刻（time.monotonic）。过期项在读取时顺手清掉。
_expiry: Dict[str, float] = {}
# 模型名 → 上次「入口改投」日志时刻。窗口内每个请求都会跳过，日志按模型限频，
# 否则一次 30 秒的容量窗口能把日志刷掉几万行。
_skip_logged: Dict[str, float] = {}
_LOG_INTERVAL_SECONDS = 10.0
_lock = threading.Lock()


def _configured_default_seconds() -> float:
    """默认窗口秒数，可用 ANTIGRAVITY_CAPACITY_WINDOW_SECONDS 覆盖。"""
    raw = os.getenv("ANTIGRAVITY_CAPACITY_WINDOW_SECONDS", "").strip()
    if not raw:
        return DEFAULT_WINDOW_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WINDOW_SECONDS
    return value if value > 0 else DEFAULT_WINDOW_SECONDS


def window_seconds(delay_seconds: Optional[float]) -> float:
    """把上游给的 retryDelay 收敛到 [MIN, MAX]；缺失时用默认值。"""
    value = delay_seconds if delay_seconds is not None else _configured_default_seconds()
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_WINDOW_SECONDS
    if not seconds > 0:
        seconds = DEFAULT_WINDOW_SECONDS
    return min(max(seconds, MIN_WINDOW_SECONDS), MAX_WINDOW_SECONDS)


def record_capacity_window(model: str, delay_seconds: Optional[float] = None) -> float:
    """登记一次模型级容量耗尽，返回窗口截止的单调时刻。

    同一模型重复登记取较晚的截止时刻：上游每次都会重新给 RetryInfo，新的窗口
    不应被旧窗口更短的尾巴覆盖。

    Args:
        model: 请求模型名（上游 metadata.model 不同时由调用方另记一条）。
        delay_seconds: 上游 RetryInfo.retryDelay，缺失时用默认窗口。

    Returns:
        窗口截止的 monotonic 时刻；模型名为空时不登记，返回 0。
    """
    name = (model or "").strip()
    if not name:
        return 0.0
    deadline = time.monotonic() + window_seconds(delay_seconds)
    with _lock:
        current = _expiry.get(name, 0.0)
        if deadline > current:
            _expiry[name] = deadline
        return _expiry[name]


def capacity_window_remaining(model: str) -> float:
    """该模型容量窗口的剩余秒数；不在窗口内返回 0。"""
    name = (model or "").strip()
    if not name:
        return 0.0
    now = time.monotonic()
    with _lock:
        deadline = _expiry.get(name)
        if deadline is None:
            return 0.0
        if deadline <= now:
            del _expiry[name]
            return 0.0
        return deadline - now


def should_log_skip(model: str) -> bool:
    """入口改投日志按模型限频：同一模型 10 秒内只记一条。"""
    name = (model or "").strip()
    if not name:
        return False
    now = time.monotonic()
    with _lock:
        if now - _skip_logged.get(name, 0.0) < _LOG_INTERVAL_SECONDS:
            return False
        _skip_logged[name] = now
        return True


def reset_capacity_windows() -> None:
    """清空全部窗口（测试与运维解封用）。"""
    with _lock:
        _expiry.clear()
        _skip_logged.clear()


def capacity_window_snapshot() -> Dict[str, float]:
    """当前仍在窗口内的模型及剩余秒数，供排障与测试断言。"""
    now = time.monotonic()
    with _lock:
        return {
            name: deadline - now
            for name, deadline in _expiry.items()
            if deadline > now
        }
