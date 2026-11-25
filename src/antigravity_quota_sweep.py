"""Antigravity 额度/档位低频对账。

为什么需要它：

- 429 写下的模型冷却按上游当时给的 `quotaResetTimeStamp` 截止，官方提前重置
  额度后，我方仍会把账号排除在候选之外；
- 凭证导入时持久化的 tier 是快照，账号被 Google 降档（Starter Quota）后
  面板和筛选仍在用旧档位。

为什么必须低频：

- `loadCodeAssist` / `fetchAvailableModels` 虽然不消耗额度，但账号级调用频率
  本身就是风控特征。整池频繁扫描等于告诉上游"这是一批脚本号"。
- 因此扫描按"整池 10 分钟一轮、每轮 2 个号"，并用 Redis 做**跨进程的每账号
  租约**（默认 6 小时）。71 个号时约 12 次/小时，落在正常客户端行为区间；
  增加副本也不会让同一账号被重复探测。

只做三件事：校准 tier、清掉与上游额度矛盾的冷却、记录 403/刷新失败的分类。
不自动禁用账号——封禁判定仍走既有的 403 复核链路。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Dict, Iterable, Optional, Tuple

from log import log

SWEEP_LOCK_PREFIX = "gcli:quota-sweep:v1"
_STARTUP_DELAY_SECONDS = 90.0
_MEMORY_LEASES: Dict[str, float] = {}
_LOOP_TASK: Optional[asyncio.Task] = None


def _tier_from_paid_tier(paid_tier: Dict[str, Any]) -> str:
    """把 loadCodeAssist 的 paidTier 映射成库里 tier 的取值。"""
    identifier = str(paid_tier.get("id") or "").lower()
    name = str(paid_tier.get("name") or "")
    if "ultra" in identifier or "Ultra" in name:
        return "ultra"
    if "starter" in name.lower() or identifier == "free-tier":
        return "free"
    return "pro" if identifier else "free"


def _candidate_priority(state: Dict[str, Any], now: float) -> Tuple[int, float, str]:
    """越小的键越先被扫描：冷却中的号最先，其次是档位未知的号。"""
    cooldowns = state.get("model_cooldowns") or {}
    active = [
        float(until)
        for until in cooldowns.values()
        if isinstance(until, (int, float)) and until > now
    ]
    if active:
        return 0, min(active), ""
    tier = state.get("tier")
    if tier not in {"free", "pro", "ultra"}:
        return 1, 0.0, ""
    return 2, float(state.get("last_success") or 0.0), ""


async def _redis():
    try:
        from src.redis_config import create_redis_client_from_env

        return create_redis_client_from_env(decode_responses=True)
    except Exception:  # noqa: BLE001 - 缓存不可用时退回进程内租约
        return None


async def _claim_probe_lease(client, filename: str, ttl: int) -> bool:
    """跨进程抢占「本账号本周期内已探测」租约。"""
    key = f"{SWEEP_LOCK_PREFIX}:{filename}"
    if client is not None:
        try:
            claimed = await client.set(key, "1", nx=True, ex=ttl)
            return bool(claimed)
        except Exception as exc:  # noqa: BLE001 - Redis 抖动时退回内存
            log.debug(f"[QUOTA SWEEP] Redis 租约失败，退回内存: {type(exc).__name__}")
    now = time.time()
    expires = _MEMORY_LEASES.get(filename, 0.0)
    if expires > now:
        return False
    if len(_MEMORY_LEASES) > 2000:
        _MEMORY_LEASES.clear()
    _MEMORY_LEASES[filename] = now + ttl
    return True


# 冷却表按「配额组」压缩后每组只保留最晚的一条；整表再兜一个上限。
# 免费档号被反复 429 时，单账号冷却能堆到 100+ 条，既拖慢选号读取，
# 也让"哪条还在生效"完全不可读。
COMPACT_COOLDOWN_LIMIT = 32


async def compact_model_cooldowns(
    backend, filename: str, cooldowns: Dict[str, Any], *, now: Optional[float] = None
) -> Tuple[int, Dict[str, float]]:
    """压缩一个账号的模型冷却表，返回 (删除条目数, 保留的冷却表)。

    - 已过期的条目直接删；
    - 同一配额组（gemini / 3p）共享额度，只保留最晚到期的那条；
    - 压缩后仍超过 ``COMPACT_COOLDOWN_LIMIT`` 时，保留最晚到期的 N 条。
    """
    from src.api.antigravity import model_quota_group

    current = time.time() if now is None else now
    active = {
        str(model): float(until)
        for model, until in (cooldowns or {}).items()
        if isinstance(until, (int, float)) and until > current
    }
    drop = {str(model) for model in (cooldowns or {})} - set(active)

    latest: Dict[str, Tuple[str, float]] = {}
    for model, until in active.items():
        group = model_quota_group(model)
        if group not in latest or until > latest[group][1]:
            latest[group] = (model, until)
    keep = {model for model, _ in latest.values()}
    drop |= set(active) - keep

    if len(keep) > COMPACT_COOLDOWN_LIMIT:
        ordered = sorted(latest.values(), key=lambda item: item[1], reverse=True)
        keep = {model for model, _ in ordered[:COMPACT_COOLDOWN_LIMIT]}
        drop |= set(active) - keep

    for model in sorted(drop):
        try:
            await backend.set_model_cooldown(filename, model, None, mode="antigravity")
        except Exception as exc:  # noqa: BLE001 - 压缩失败不影响探测
            log.warning(f"[QUOTA SWEEP] 压缩冷却失败 {filename}/{model}: {type(exc).__name__}")
    return len(drop), {model: active[model] for model in keep}


async def _probe_account(filename: str, state: Dict[str, Any]) -> Optional[str]:
    """探测单个账号，返回结果标记；异常向上抛给调用方统计。"""
    from src.api.antigravity import (
        fetch_paid_tier_info,
        fetch_quota_info,
        reconcile_model_cooldowns_with_quota,
    )
    from src.credential_manager import credential_manager

    from src.storage_adapter import get_storage_adapter

    cooldowns = state.get("model_cooldowns") or {}
    now = time.time()
    active = {
        model: float(until)
        for model, until in cooldowns.items()
        if isinstance(until, (int, float)) and until > now
    }
    if cooldowns:
        removed, active = await compact_model_cooldowns(
            (await get_storage_adapter())._backend, filename, cooldowns, now=now
        )
        if removed:
            log.info(
                f"[QUOTA SWEEP] {filename} 冷却表压缩：删除 {removed} 条，保留 {len(active)} 条"
            )

    try:
        credential_data, creds = await credential_manager.get_account_credentials(
            filename, "antigravity"
        )
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "refresh failed" in message or "authorization changed" in message:
            return "refresh_failed"
        raise

    access_token = credential_data.get("access_token") or credential_data.get("token")
    if not access_token:
        return "no_token"

    covered = "tier_unchanged"
    paid = await fetch_paid_tier_info(
        access_token,
        proxy_url=getattr(creds, "proxy_url", ...),
        credential_data=credential_data,
        credential_name=filename,
    )
    if paid:
        tier = _tier_from_paid_tier(paid)
        if tier != state.get("tier"):
            await credential_manager.update_credential_state(
                filename, {"tier": tier}, mode="antigravity"
            )
            log.info(
                f"[QUOTA SWEEP] {filename} 档位快照 {state.get('tier')} → {tier}"
                f"（paidTier={paid.get('name')}）"
            )
            covered = "tier_changed"

    if not active:
        return covered

    info = await fetch_quota_info(
        access_token,
        project_id=credential_data.get("project_id"),
        proxy_url=getattr(creds, "proxy_url", ...),
        credential_data=credential_data,
        credential_name=filename,
    )
    if not info.get("success"):
        return "quota_unavailable"

    from src.storage_adapter import get_storage_adapter

    adapter = await get_storage_adapter()
    cleared = await reconcile_model_cooldowns_with_quota(
        adapter._backend,
        filename,
        active,
        info.get("models") or {},
        info.get("groups"),
        mode="antigravity",
    )
    if cleared:
        return f"cooldown_cleared:{cleared}"
    return "cooldown_kept"


async def sweep_once(*, batch: Optional[int] = None) -> Dict[str, Any]:
    """执行一轮扫描，最多探测 batch 个账号。"""
    from config import (
        get_antigravity_quota_sweep_account_interval_seconds,
        get_antigravity_quota_sweep_batch,
    )
    from src.credential_manager import credential_manager

    limit = batch if batch is not None else await get_antigravity_quota_sweep_batch()
    account_interval = int(await get_antigravity_quota_sweep_account_interval_seconds())
    stats: Dict[str, Any] = {"scanned": 0, "skipped": 0}
    counters: Dict[str, int] = {}

    try:
        states = await credential_manager.get_creds_status(mode="antigravity")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[QUOTA SWEEP] 读取账号状态失败: {type(exc).__name__}")
        return stats

    now = time.time()
    candidates: list[Tuple[Tuple[int, float, str], str, Dict[str, Any]]] = []
    for filename, state in (states or {}).items():
        if not isinstance(state, dict) or state.get("disabled"):
            continue
        if state.get("warmup_status") in {"paused", "account_forbidden"}:
            continue
        candidates.append((_candidate_priority(state, now), filename, state))
    candidates.sort(key=lambda item: (item[0], item[1]))

    client = await _redis()
    try:
        for _, filename, state in candidates:
            if stats["scanned"] >= limit:
                break
            if not await _claim_probe_lease(client, filename, account_interval):
                continue
            try:
                outcome = await _probe_account(filename, state)
                stats["scanned"] += 1
                if outcome:
                    counters[outcome.split(":")[0]] = counters.get(outcome.split(":")[0], 0) + 1
                stats.setdefault("results", {})[filename] = outcome
            except Exception as exc:  # noqa: BLE001 - 单账号失败不影响整轮
                stats["scanned"] += 1
                counters["error"] = counters.get("error", 0) + 1
                log.warning(
                    f"[QUOTA SWEEP] {filename} 探测失败: {type(exc).__name__}: {exc}"
                )
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    stats["ok"] = counters.get("ok", 0)
    stats.update(counters)
    stats["skipped"] = len(candidates) - stats["scanned"]
    if stats["scanned"]:
        log.info(
            f"[QUOTA SWEEP] 本轮探测 {stats['scanned']} 个账号，"
            f"跳过 {stats['skipped']} 个（租约未到期），结果 {json.dumps(counters, ensure_ascii=False)}"
        )
    return stats


def _jitter(interval: float) -> float:
    """±15% 抖动，避免多副本在同一秒对齐成突发。"""
    return interval * random.uniform(0.85, 1.15)


async def sweep_loop() -> None:
    """后台循环：启动等待 → 周期扫描。"""
    from config import (
        get_antigravity_quota_sweep_enabled,
        get_antigravity_quota_sweep_interval_seconds,
    )

    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    while True:
        try:
            if await get_antigravity_quota_sweep_enabled():
                await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 循环不能因单轮失败退出
            log.warning(f"[QUOTA SWEEP] 扫描循环异常: {type(exc).__name__}: {exc}")
        interval = await get_antigravity_quota_sweep_interval_seconds()
        try:
            await asyncio.sleep(_jitter(interval))
        except asyncio.CancelledError:
            raise


async def start_sweep() -> Optional[asyncio.Task]:
    """启动后台扫描任务（幂等）。未启用时返回 None。"""
    global _LOOP_TASK
    from config import get_antigravity_quota_sweep_enabled_sync

    if not get_antigravity_quota_sweep_enabled_sync():
        log.info("[QUOTA SWEEP] 未启用，跳过后台对账")
        return None
    if _LOOP_TASK and not _LOOP_TASK.done():
        return _LOOP_TASK
    _LOOP_TASK = asyncio.create_task(sweep_loop(), name="antigravity_quota_sweep")
    log.info("[QUOTA SWEEP] 后台额度/档位对账已启动")
    return _LOOP_TASK


async def stop_sweep() -> None:
    global _LOOP_TASK
    if _LOOP_TASK and not _LOOP_TASK.done():
        _LOOP_TASK.cancel()
        try:
            await _LOOP_TASK
        except asyncio.CancelledError:
            pass
    _LOOP_TASK = None


def reset_memory_leases() -> None:
    """测试与运维排查用：清空进程内租约。"""
    _MEMORY_LEASES.clear()
