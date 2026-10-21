"""Antigravity quotaInfo 主动避让（quota guard）。

fetchAvailableModels 响应中每个模型都带 quotaInfo{remainingFraction, resetTime}
（2026-10-21 hub/2.25.0 实抓确认）。本模块缓存各账号最近一次抓取到的额度快照，
在凭证选择时提前避开额度将尽的账号——在 429 QUOTA_EXHAUSTED 发生之前换号，
而不是被打之后冷却。

缓存为进程内内存态，带 TTL；过期视为未知（不阻拦）。
"""

import time
from typing import Any, Dict, Optional, Set

from log import log

# credential -> {model_id: (remaining_fraction, fetched_at)}
_SNAPSHOTS: Dict[str, Dict[str, tuple]] = {}
_FETCHED_AT: Dict[str, float] = {}

_TTL_SECONDS = 15 * 60
_MAX_TRACKED = 2000


def update_quota_snapshot(credential_name: str, models_payload: Any) -> None:
    """从 fetchAvailableModels 的 models 字典提取 quotaInfo 并缓存。"""
    if not credential_name or not isinstance(models_payload, dict):
        return
    snapshot: Dict[str, tuple] = {}
    now = time.time()
    for model_id, info in models_payload.items():
        if not isinstance(info, dict):
            continue
        quota = info.get("quotaInfo")
        if not isinstance(quota, dict):
            continue
        fraction = quota.get("remainingFraction")
        if isinstance(fraction, (int, float)):
            snapshot[str(model_id)] = (float(fraction), now)
    if not snapshot:
        return
    if len(_SNAPSHOTS) >= _MAX_TRACKED:
        _SNAPSHOTS.clear()
        _FETCHED_AT.clear()
    _SNAPSHOTS[credential_name] = snapshot
    _FETCHED_AT[credential_name] = now
    log.debug(
        f"[QUOTA GUARD] 更新账号 {credential_name} 额度快照：{len(snapshot)} 个模型"
    )


def is_model_quota_low(
    credential_name: str, model_name: str, threshold: float
) -> Optional[bool]:
    """该账号该模型额度是否低于阈值。

    返回 True/False；无快照、快照过期或该模型无数据时返回 None（不阻拦）。
    """
    if not credential_name or not model_name:
        return None
    fetched_at = _FETCHED_AT.get(credential_name)
    if not fetched_at or time.time() - fetched_at > _TTL_SECONDS:
        return None
    entry = _SNAPSHOTS.get(credential_name, {}).get(model_name)
    if entry is None:
        return None
    fraction, _ = entry
    return fraction < threshold


def low_quota_accounts(model_name: str, threshold: float) -> Set[str]:
    """返回当前快照中该模型额度低于阈值的账号集合（供选择时排除）。"""
    if not model_name:
        return set()
    low: Set[str] = set()
    now = time.time()
    for credential_name, snapshot in _SNAPSHOTS.items():
        fetched_at = _FETCHED_AT.get(credential_name, 0.0)
        if now - fetched_at > _TTL_SECONDS:
            continue
        entry = snapshot.get(model_name)
        if entry is not None and entry[0] < threshold:
            low.add(credential_name)
    return low


def snapshot_age_seconds(credential_name: str) -> Optional[float]:
    """该账号快照距现在的秒数；无快照返回 None。"""
    fetched_at = _FETCHED_AT.get(credential_name)
    return (time.time() - fetched_at) if fetched_at else None
