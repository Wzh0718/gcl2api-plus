"""Antigravity 额度/档位低频对账。

为什么需要它：

- 429 写下的模型冷却按上游当时给的 `quotaResetTimeStamp` 截止，官方提前重置
  额度后，我方仍会把账号排除在候选之外；
- 凭证导入时持久化的 tier 是快照，账号被 Google 降档（Starter Quota）后
  面板和筛选仍在用旧档位。

为什么必须低频：

- `loadCodeAssist` / `fetchAvailableModels` 虽然不消耗额度，但账号级调用频率
  本身就是风控特征。整池频繁扫描等于告诉上游"这是一批脚本号"。
- 整池默认 10 分钟一轮、每轮 2 个号，Redis 轮次租约协调多个 worker，
  每账号另有 1 小时租约；游标保证未探测账号不会被固定优先级饿死。
- 未配置 Redis 时使用进程内租约；已配置但不可用时跳过本轮，恢复后继续。

只做三件事：校准 tier、清掉与上游额度矛盾的冷却、记录 403/刷新失败的分类。
不自动禁用账号——封禁判定仍走既有的 403 复核链路。
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
import json
import math
import random
import time
import uuid
from typing import Any, Dict, Iterable, Optional, Tuple

from log import log

SWEEP_LOCK_PREFIX = "gcli:quota-sweep:v1"
_STARTUP_DELAY_SECONDS = 90.0
_MEMORY_LEASES: Dict[str, float] = {}
_MEMORY_ROUND_UNTIL = 0.0
_MEMORY_ROUND_ACTIVE = False
_MEMORY_CURSOR = ""
_LOOP_TASK: Optional[asyncio.Task] = None
_BACKEND = ""

# Both heartbeat and final cooldown updates must still belong to this worker.
_RENEW_ROUND = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

# Fence each new probe as well as the heartbeat. A stalled worker resuming after
# its round expires must not move the new owner's cursor or start another probe.
_CLAIM_PROBE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
if not redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[2]) then return 0 end
redis.call('SET', KEYS[3], ARGV[3])
return 1
"""


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
    from src.redis_config import create_redis_client_from_env, redis_socket_timeouts

    connect_timeout, socket_timeout = redis_socket_timeouts()
    return create_redis_client_from_env(
        decode_responses=True,
        socket_connect_timeout=connect_timeout,
        socket_timeout=socket_timeout,
    )


async def _claim_probe_lease(client, filename: str, ttl: int, owner: str) -> bool:
    """跨进程抢占「本账号本周期内已探测」租约。"""
    key = f"{SWEEP_LOCK_PREFIX}:{filename}"
    if client is not None:
        claimed = await client.eval(
            _CLAIM_PROBE, 3, f"{SWEEP_LOCK_PREFIX}:round", key,
            f"{SWEEP_LOCK_PREFIX}:cursor", owner, ttl, filename,
        )
        if claimed == -1:
            raise RuntimeError("quota sweep round lease lost")
        return bool(claimed)
    now = time.time()
    expires = _MEMORY_LEASES.get(filename, 0.0)
    if expires > now:
        return False
    if len(_MEMORY_LEASES) > 2000:
        for name, deadline in list(_MEMORY_LEASES.items()):
            if deadline <= now:
                del _MEMORY_LEASES[name]
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


async def _scan_candidates(client, owner: str, limit: int, account_interval: int, stats: dict) -> None:
    global _MEMORY_CURSOR
    from src.credential_manager import credential_manager

    counters: Dict[str, int] = {}
    states = await credential_manager.get_creds_status(mode="antigravity")
    now = time.time()
    candidates: list[Tuple[Tuple[int, float, str], str, Dict[str, Any]]] = []
    for filename, state in (states or {}).items():
        if not isinstance(state, dict) or state.get("disabled"):
            continue
        if state.get("warmup_status") in {"paused", "account_forbidden"}:
            continue
        candidates.append((_candidate_priority(state, now), filename, state))
    candidates.sort(key=lambda item: (item[0], item[1]))
    cursor_key = f"{SWEEP_LOCK_PREFIX}:cursor"
    cursor = await client.get(cursor_key) if client is not None else _MEMORY_CURSOR
    if cursor:
        # Only the initial round uses urgency. Subsequent rounds traverse a stable
        # ring, so expired leases at its head cannot displace unseen accounts.
        candidates.sort(key=lambda item: item[1])
        offset = bisect_right([item[1] for item in candidates], cursor)
        candidates = candidates[offset:] + candidates[:offset]

    for _, filename, state in candidates:
        if stats["scanned"] >= limit:
            break
        if not await _claim_probe_lease(client, filename, account_interval, owner):
            continue
        if client is None:
            _MEMORY_CURSOR = filename
        stats["scanned"] += 1
        try:
            outcome = await _probe_account(filename, state)
            if outcome:
                label = outcome.split(":")[0]
                counters[label] = counters.get(label, 0) + 1
            stats.setdefault("results", {})[filename] = outcome
        except Exception as exc:  # noqa: BLE001 - 单账号失败不影响整轮
            counters["error"] = counters.get("error", 0) + 1
            log.warning(f"[QUOTA SWEEP] {filename} 探测失败: {type(exc).__name__}: {exc}")

    stats["ok"] = counters.get("ok", 0)
    stats.update(counters)
    stats["skipped"] = len(candidates) - stats["scanned"]
    if stats["scanned"]:
        log.info(
            f"[QUOTA SWEEP] 本轮探测 {stats['scanned']} 个账号，"
            f"未探测 {stats['skipped']} 个，结果 {json.dumps(counters, ensure_ascii=False)}"
        )


async def _renew_round(client, owner: str, ttl: int) -> None:
    while True:
        await asyncio.sleep(min(20.0, ttl / 3))
        if not await client.eval(_RENEW_ROUND, 1, f"{SWEEP_LOCK_PREFIX}:round", owner, ttl * 1000):
            raise RuntimeError("quota sweep round lease lost")


async def _scan_with_renewal(client, owner, ttl, limit, account_interval, stats):
    scan = asyncio.create_task(_scan_candidates(client, owner, limit, account_interval, stats))
    heartbeat = asyncio.create_task(_renew_round(client, owner, ttl))
    try:
        done, _ = await asyncio.wait((scan, heartbeat), return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done:
            await heartbeat  # A lost/unreachable lease cancels in-progress probing.
        await scan
    finally:
        scan.cancel()
        heartbeat.cancel()
        await asyncio.gather(scan, heartbeat, return_exceptions=True)


async def sweep_once(*, batch: Optional[int] = None) -> Dict[str, Any]:
    """执行一个整池租约保护的批次；Redis 故障时不启动本地副本。"""
    global _MEMORY_ROUND_UNTIL, _MEMORY_ROUND_ACTIVE, _BACKEND
    from config import (
        get_antigravity_quota_sweep_account_interval_seconds,
        get_antigravity_quota_sweep_batch,
        get_antigravity_quota_sweep_interval_seconds,
    )

    limit = batch if batch is not None else await get_antigravity_quota_sweep_batch()
    account_interval = int(await get_antigravity_quota_sweep_account_interval_seconds())
    interval = await get_antigravity_quota_sweep_interval_seconds()
    ttl = max(60, math.ceil(interval))
    stats: Dict[str, Any] = {"scanned": 0, "skipped": 0}
    client = None
    claimed = False
    owner = uuid.uuid4().hex
    started = time.monotonic()
    try:
        client = await _redis()
        backend = "redis" if client is not None else "memory (single process)"
        if backend != _BACKEND:
            log.info(f"[QUOTA SWEEP] lease backend: {backend}")
            _BACKEND = backend
        if client is not None:
            claimed = bool(await client.set(f"{SWEEP_LOCK_PREFIX}:round", owner, nx=True, ex=ttl))
        elif not _MEMORY_ROUND_ACTIVE and time.monotonic() >= _MEMORY_ROUND_UNTIL:
            _MEMORY_ROUND_ACTIVE = claimed = True
        if claimed:
            started = time.monotonic()
            if client is not None:
                await _scan_with_renewal(client, owner, ttl, limit, account_interval, stats)
            else:
                _MEMORY_ROUND_UNTIL = started + interval
                await _scan_candidates(None, owner, limit, account_interval, stats)
    except Exception as exc:
        log.warning(f"[QUOTA SWEEP] 本轮停止，等待下轮重试: {type(exc).__name__}: {exc}")
    finally:
        if claimed and client is None:
            _MEMORY_ROUND_ACTIVE = False
        if client is not None:
            try:
                if claimed:
                    remaining_ms = max(1, math.ceil((started + interval - time.monotonic()) * 1000))
                    await client.eval(_RENEW_ROUND, 1, f"{SWEEP_LOCK_PREFIX}:round", owner, remaining_ms)
            except Exception as exc:
                log.warning(f"[QUOTA SWEEP] 轮次租约收尾失败，等待 TTL: {type(exc).__name__}")
            finally:
                try:
                    await client.aclose()
                except Exception as exc:
                    log.warning(f"[QUOTA SWEEP] Redis 连接关闭失败: {type(exc).__name__}")
    return stats


def _jitter(interval: float) -> float:
    """仅正向抖动，避免提前撞上尚未到期的轮次租约后空等一整轮。"""
    return interval * random.uniform(1.0, 1.15)


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
    global _MEMORY_ROUND_UNTIL, _MEMORY_ROUND_ACTIVE, _MEMORY_CURSOR, _BACKEND
    _MEMORY_LEASES.clear()
    _MEMORY_ROUND_UNTIL = 0.0
    _MEMORY_ROUND_ACTIVE = False
    _MEMORY_CURSOR = ""
    _BACKEND = ""
