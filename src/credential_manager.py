"""
凭证管理器
"""

import asyncio
import hashlib
import inspect
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from log import log
from src.request_timing import database_timing_scope
from src.rate_limits import selection_retry_at, selection_usage_resume_at
from src.operational_audit import emit as audit, reference, refresh_context, expiry_state

from src.google_oauth_api import Credentials
from src.storage_adapter import get_storage_adapter

# Token 刷新窗口抖动：所有账号共用固定 300s 缓冲时，同批导入的账号过期时刻
# 天然对齐，会形成整点刷新簇。按账号稳定散开 300~900s，簇即被打散。
_REFRESH_BUFFER_BASE_SECONDS = 300
_REFRESH_BUFFER_SPREAD_SECONDS = 600


def _refresh_buffer_seconds(credential_data: Dict[str, Any], mode: str) -> int:
    """该账号本次刷新使用的提前量；antigravity 走抖动，其余保持 300s。"""
    if mode != "antigravity":
        return _REFRESH_BUFFER_BASE_SECONDS
    seed = str(
        credential_data.get("refresh_token")
        or credential_data.get("client_id")
        or credential_data.get("_credential_revision")
        or ""
    )
    if not seed:
        return _REFRESH_BUFFER_BASE_SECONDS
    digest = hashlib.sha256(f"agy-refresh-buffer:v1:{seed}".encode("utf-8")).digest()
    return _REFRESH_BUFFER_BASE_SECONDS + int.from_bytes(digest[:4], "big") % _REFRESH_BUFFER_SPREAD_SECONDS


REFRESH_LEASE_TTL = 60
REFRESH_LEASE_RENEW_INTERVAL = 15
REFRESH_OPERATION_TIMEOUT = 45
REFRESH_WAIT_TIMEOUT = 90
REFRESH_FAILURE_COOLDOWN = 5

IMAGE_CAPACITY_DEMOTION_SECONDS = 60

# 代理节点连接失败后短暂冷却，避免账号被反复选中又反复失败。
# 冷却期间该账号会从调度中排除（proxy_group_nodes.cooldown_until）。
PROXY_NODE_FAILURE_COOLDOWN_SECONDS = 300

# 上游明确返回地区资格拒绝（UNSUPPORTED_LOCATION / LOCATION_RESTRICTED）时，
# 结论属于出口 IP 而不是账号：保留账号、换掉节点。被拒绝的节点冷却一段时间，
# 避免组游标立刻把同一个 IP 又发回来。
REGION_REJECTED_NODE_COOLDOWN_SECONDS = 600
# 一次请求内同一账号最多换几次 IP；覆盖「连续几个节点都不在可用地区」的情况。

# 判定为「节点不可达」的错误特征（SOCKS/连接层错误，非 HTTP 状态错误）
_PROXY_CONNECT_ERROR_MARKERS = (
    "socks",
    "proxy server",
    "connect",
    "connection",
    "timeout",
    "timed out",
    "unreachable",
    "refused",
    "reset by peer",
    "network is down",
    "nodename nor servname",
    "name or service not known",
)


def rank_antigravity_image_candidates(
    credential_states: Dict[str, Dict[str, Any]],
    *,
    model_name: str,
    runtime_health: Optional[Dict[str, Dict[str, Any]]] = None,
    excluded_filenames: Optional[Set[str]] = None,
    now: Optional[float] = None,
) -> List[str]:
    """Rank eligible image accounts by live health and their routing queue position."""
    current_time = time.time() if now is None else now
    runtime_health = runtime_health or {}
    excluded = excluded_filenames or set()
    ranked: List[Tuple[Tuple[Any, ...], str]] = []

    for filename, state in credential_states.items():
        if filename in excluded or state.get("disabled"):
            continue
        if state.get("warmup_status") in {"paused", "account_forbidden"}:
            continue
        if max(state.get("transient_hold_until") or 0, state.get("probe_retry_at") or 0) > current_time:
            continue
        cooldowns = state.get("model_cooldowns") or {}
        cooldown_until = cooldowns.get(model_name)
        if isinstance(cooldown_until, (int, float)) and cooldown_until > current_time:
            continue

        live = runtime_health.get(filename) or {}
        live_success = float(live.get("last_success_at") or 0)
        capacity_failure = float(live.get("last_capacity_failure_at") or 0)
        account_failure = float(live.get("last_account_failure_at") or 0)
        latest_failure = max(capacity_failure, account_failure)
        has_recent_failure = (
            latest_failure > live_success
            and current_time - latest_failure < IMAGE_CAPACITY_DEMOTION_SECONDS
        )
        health_priority = 1 if has_recent_failure else 0
        routing_turn = max(int(state.get("routing_turn") or 0), 0)
        rotation_order = max(int(state.get("rotation_order") or 0), 0)
        sort_key = (
            health_priority,
            routing_turn,
            rotation_order,
            filename,
        )
        ranked.append((sort_key, filename))

    ranked.sort(key=lambda item: item[0])
    return [filename for _, filename in ranked]

class CredentialManager:
    """
    统一凭证管理器
    所有存储操作通过storage_adapter进行
    """

    def __init__(self):
        # 核心状态
        self._refresh_tasks: Dict[Tuple[str, str], asyncio.Task] = {}
        self._initialized = False
        self._storage_adapter = None
        self._image_runtime_health: Dict[str, Dict[str, Any]] = {}
        self._image_lease_condition = asyncio.Condition()
        self._image_account_leases: Dict[str, Set[str]] = {}

    async def _ensure_initialized(self):
        """确保管理器已初始化（内部使用）"""
        if not self._initialized or self._storage_adapter is None:
            await self.initialize()

    async def initialize(self):
        """初始化凭证管理器"""
        if self._initialized and self._storage_adapter is not None:
            return

        # 初始化统一存储适配器
        self._storage_adapter = await get_storage_adapter()
        self._initialized = True

    async def close(self):
        """清理资源"""
        log.debug("Closing credential manager...")
        # Let shielded refreshes persist their results before shutting storage down.
        if self._refresh_tasks:
            await asyncio.gather(*list(self._refresh_tasks.values()), return_exceptions=True)
        await self.reset_image_account_leases()
        self._initialized = False
        log.debug("Credential manager closed")

    async def _reserve_available_image_credential(
        self, ranked_filenames: List[str]
    ) -> Optional[Tuple[str, str]]:
        """Atomically reserve the highest-ranked image account with capacity."""
        from config import get_antigravity_account_concurrency

        async with self._image_lease_condition:
            for filename in ranked_filenames:
                limit = get_antigravity_account_concurrency(filename)["image_task_concurrency"]
                leases = self._image_account_leases.get(filename, set())
                if len(leases) >= limit:
                    continue
                lease_id = uuid.uuid4().hex
                self._image_account_leases.setdefault(filename, set()).add(lease_id)
                return filename, lease_id

            # All eligible accounts are busy. Waiting under the condition avoids
            # a lost wake-up between checking the pool and sleeping.
            await self._image_lease_condition.wait()
            return None

    async def release_image_credential(
        self, credential_name: str, lease_id: Optional[str]
    ) -> bool:
        """Release one image lease; stale or duplicate releases are ignored."""
        if not lease_id:
            return False
        async with self._image_lease_condition:
            leases = self._image_account_leases.get(credential_name)
            if not leases or lease_id not in leases:
                return False
            leases.remove(lease_id)
            if not leases:
                del self._image_account_leases[credential_name]
            self._image_lease_condition.notify_all()
            return True

    async def notify_image_concurrency_changed(self) -> None:
        """Wake queued image tasks without discarding active leases."""
        async with self._image_lease_condition:
            self._image_lease_condition.notify_all()

    async def reset_image_account_leases(self) -> None:
        """Clear process-local leases during graceful shutdown or reinitialize."""
        async with self._image_lease_condition:
            self._image_account_leases.clear()
            self._image_lease_condition.notify_all()

    async def get_valid_credential(
        self,
        mode: str = "geminicli",
        model_name: Optional[str] = None,
        exclude_filenames: Optional[Set[str]] = None,
        image_request: bool = False,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        获取有效的凭证 - 随机负载均衡版
        每次随机选择一个可用的凭证（未禁用、未冷却）
        如果刷新失败会自动禁用失效凭证并重试获取下一个可用凭证

        Args:
            mode: 凭证模式 ("geminicli" 或 "antigravity")
            model_name: 完整模型名，用于模型级冷却检查
        """
        await self._ensure_initialized()

        from src.claude_routing import bound_claude_credential
        selection_retry_at.set(0)
        selection_usage_resume_at.set(0)
        pinned = bound_claude_credential() if mode == "antigravity" else None
        if mode == "antigravity" and (
            image_request or "image" in (model_name or "").lower()
        ):
            return await self._get_valid_antigravity_image_credential(
                model_name=model_name or "",
                exclude_filenames=exclude_filenames,
                credential_name=pinned,
            )

        selection_options = {"credential_name": pinned} if pinned else {}

        # 最多重试3次
        max_retries = 3
        attempted_filenames: set[str] = set(exclude_filenames or set())
        preferred_result = None

        # quotaInfo 主动避让：额度快照低于阈值的账号本轮排除（在 429 之前换号）；
        # 若排除后无可用账号则回退为不避让（快照可能滞后，不宁可信其有）
        if mode == "antigravity" and model_name:
            try:
                from config import (
                    get_antigravity_quota_guard_enabled,
                    get_antigravity_quota_guard_threshold,
                )

                if await get_antigravity_quota_guard_enabled():
                    from src.antigravity_quota_guard import low_quota_accounts

                    low = low_quota_accounts(
                        model_name, await get_antigravity_quota_guard_threshold()
                    ) - attempted_filenames
                    if low:
                        probe = await self._storage_adapter._backend.get_next_available_credential(
                            mode=mode,
                            model_name=model_name,
                            exclude_filenames=attempted_filenames | low,
                            **selection_options,
                        )
                        if probe is not None:
                            preferred_result = probe
                            log.info(
                                f"[QUOTA GUARD] 模型 {model_name} 额度将尽，"
                                f"本轮避让 {len(low)} 个账号"
                            )
                            attempted_filenames |= low
                        else:
                            log.warning(
                                f"[QUOTA GUARD] 模型 {model_name} 全部账号额度快照均低，"
                                f"回退为不避让"
                            )
            except Exception as e:
                log.debug(f"[QUOTA GUARD] 避让检查失败（忽略，按不避让处理）: {e}")

        for attempt in range(max_retries):
            if attempt == 0 and preferred_result is not None:
                result = preferred_result
            else:
                result = await self._storage_adapter._backend.get_next_available_credential(
                    mode=mode,
                    model_name=model_name,
                    exclude_filenames=attempted_filenames,
                    **selection_options,
                )

            # 如果没有可用凭证，直接返回None
            if not result:
                if attempt == 0:
                    log.warning(f"没有可用凭证 (mode={mode}, model_name={model_name})")
                return None

            filename, credential_data = result
            attempted_filenames.add(filename)

            # Token 刷新检查
            if await self._should_refresh_token(credential_data, mode=mode):
                log.debug(f"Token需要刷新 - 文件: {filename} (mode={mode})")
                refreshed_data = await self._refresh_token(credential_data, filename, mode=mode)
                if refreshed_data:
                    credential_data = refreshed_data
                    log.debug(f"Token刷新成功: {filename} (mode={mode})")
                else:
                    # 刷新失败（_refresh_token内部已自动禁用失效凭证）
                    log.warning(f"Token刷新失败，尝试获取下一个凭证: {filename} (mode={mode}, attempt={attempt+1}/{max_retries})")
                    # 继续循环，尝试获取下一个可用凭证
                    continue

            if mode == "antigravity":
                ready_data = await self.ensure_antigravity_account_ready(
                    filename, credential_data
                )
                if ready_data is None:
                    continue
                credential_data = ready_data

            return filename, credential_data

        # 重试次数用尽
        log.error(f"重试{max_retries}次后仍无可用凭证 (mode={mode}, model_name={model_name})")
        return None

    async def _get_valid_antigravity_image_credential(
        self,
        *,
        model_name: str,
        exclude_filenames: Optional[Set[str]] = None,
        credential_name: Optional[str] = None,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Reserve one image task slot, waiting when every candidate is busy."""
        attempted_filenames = set(exclude_filenames or set())
        readiness_failures = 0

        while readiness_failures < 3:
            states = await self._storage_adapter.get_all_credential_states(
                mode="antigravity"
            )
            active_filenames = set(states)
            self._image_runtime_health = {
                filename: health
                for filename, health in self._image_runtime_health.items()
                if filename in active_filenames
            }
            ranked = rank_antigravity_image_candidates(
                {name: state for name, state in states.items() if name == credential_name}
                if credential_name else states,
                model_name=model_name,
                runtime_health=self._image_runtime_health,
                excluded_filenames=attempted_filenames,
            )
            now = time.time()
            available = [name for name in ranked if (states[name].get("usage_resume_at") or 0) <= now]
            if ranked and not available:
                selection_usage_resume_at.set(min(states[name]["usage_resume_at"] for name in ranked))
            ranked = available
            if not ranked:
                log.warning(
                    f"没有可用图片凭证 (mode=antigravity, model_name={model_name})"
                )
                return None

            reservation = await self._reserve_available_image_credential(ranked)
            if reservation is None:
                # A lease was released or the pool was reset. Re-read live
                # account states before attempting another reservation.
                continue

            filename, lease_id = reservation
            state = states.get(filename) or {}
            prepared = False
            try:
                mark_selected = getattr(
                    self._storage_adapter, "mark_credential_selected", None
                )
                if callable(mark_selected):
                    claimed = await mark_selected(
                        filename,
                        mode="antigravity",
                        expected_call_count=max(
                            int(state.get("call_count") or 0), 0
                        ),
                    )
                    if not claimed:
                        # Another worker advanced this account after our snapshot.
                        # Release the local lease and rank the refreshed routing queue.
                        await asyncio.sleep(0)
                        continue

                credential_data = await self._storage_adapter.get_credential(
                    filename, mode="antigravity"
                )
                if not credential_data:
                    attempted_filenames.add(filename)
                    readiness_failures += 1
                    continue
                credential_data = dict(credential_data)
                credential_data["tier"] = state.get("tier") or credential_data.get(
                    "tier"
                ) or "pro"
                credential_data["user_email"] = state.get(
                    "user_email"
                ) or credential_data.get("user_email")

                if await self._should_refresh_token(
                    credential_data, mode="antigravity"
                ):
                    refreshed = await self._refresh_token(
                        credential_data, filename, mode="antigravity"
                    )
                    if not refreshed:
                        attempted_filenames.add(filename)
                        readiness_failures += 1
                        continue
                    credential_data = dict(refreshed)
                    credential_data["tier"] = state.get("tier") or "pro"
                    credential_data["user_email"] = state.get("user_email")

                ready_data = await self.ensure_antigravity_account_ready(
                    filename, credential_data
                )
                if ready_data is None:
                    attempted_filenames.add(filename)
                    readiness_failures += 1
                    continue
                ready_data = dict(ready_data)
                ready_data["_image_lease_id"] = lease_id
                prepared = True
                return filename, ready_data
            finally:
                if not prepared:
                    await self.release_image_credential(filename, lease_id)

        log.warning(
            f"图片凭证候选均未就绪 (mode=antigravity, model_name={model_name})"
        )
        return None

    def record_image_account_outcome(
        self,
        credential_name: str,
        *,
        success: bool = False,
        capacity_failure: bool = False,
        latency_seconds: Optional[float] = None,
    ) -> None:
        """Update process-local image health without marking capacity as account quota."""
        now = time.time()
        state = self._image_runtime_health.setdefault(credential_name, {})
        if latency_seconds is not None:
            state["last_latency_seconds"] = max(float(latency_seconds), 0.0)
        if success:
            state["last_success_at"] = now
            state["consecutive_capacity_failures"] = 0
        elif capacity_failure:
            state["last_capacity_failure_at"] = now
            state["consecutive_capacity_failures"] = int(
                state.get("consecutive_capacity_failures") or 0
            ) + 1
        else:
            state["last_account_failure_at"] = now

    async def add_credential(self, credential_name: str, credential_data: Dict[str, Any]):
        """
        新增或更新一个凭证
        存储层会自动处理轮换顺序
        """
        await self._ensure_initialized()
        await self._storage_adapter.store_credential(credential_name, credential_data)
        log.info(f"Credential added/updated: {credential_name}")

    async def add_antigravity_credential(self, credential_name: str, credential_data: Dict[str, Any]):
        """
        新增或更新一个Antigravity凭证
        存储层会自动处理轮换顺序
        """
        await self._ensure_initialized()
        await self._storage_adapter.store_credential(credential_name, credential_data, mode="antigravity")
        log.info(f"Antigravity credential added/updated: {credential_name}")

    async def ensure_antigravity_account_ready(
        self,
        credential_name: str,
        credential_data: Dict[str, Any],
        *,
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Gate account health, with fail-closed semantics for exclusive proxy groups."""
        await self._ensure_initialized()
        from config import (
            get_antigravity_egress_ip_check_url,
            get_antigravity_network_check_enabled,
            get_antigravity_network_check_ttl_seconds,
        )
        from .antigravity_account_health import account_health_check_is_fresh
        from .antigravity_account_health import account_health_has_negative_evidence
        from .antigravity_account_health import account_health_is_fresh_eligible

        from src.storage.sqlite_manager import EXCLUSIVE_PROBE_RETRY_SECONDS, SelectedAntigravityCredential

        # PostgreSQL claims carry a resolved network snapshot only when the
        # binding is still reusable. An unbound group account yields ``None``;
        # fall through so this call resolves (and persists) a node instead of
        # dereferencing it here.
        selected = (
            isinstance(credential_data, SelectedAntigravityCredential)
            and credential_data.filename == credential_name
            and bool(credential_data.network)
        )
        if selected:
            expiry = credential_data.network.get("bound_proxy_expires_at")
            selected = not force and (expiry is None or expiry > time.time())
        if selected:
            network = credential_data.network
            health = credential_data.health
        else:
            network = await self._storage_adapter.resolve_credential_network(credential_name, mode="antigravity")
            health = None
        if network.get("fixed_proxy_ip") and not network.get("proxy_url"):
            return None
        exclusive = bool(network.get("exclusive_assignment"))
        credential_data = dict(credential_data)
        credential_data.update(network)
        if not exclusive and not await get_antigravity_network_check_enabled():
            return credential_data
        if exclusive and not await get_antigravity_egress_ip_check_url():
            log.warning(
                "[ANTIGRAVITY HEALTH] exclusive account has no IP echo URL: "
                f"credential={credential_name}"
            )
            return None

        if health is None:
            health = await self._storage_adapter.get_antigravity_account_health(credential_name)
        ttl_seconds = await get_antigravity_network_check_ttl_seconds()
        if exclusive and health.get("eligibility_status") in {"geo_blocked", "error"}:
            ttl_seconds = min(ttl_seconds, EXCLUSIVE_PROBE_RETRY_SECONDS)
        def usable_exclusive_route(check):
            if account_health_is_fresh_eligible(check, ttl_seconds=ttl_seconds):
                return True
            return (
                bool(network.get("proxy_url") and network.get("proxy_node_id"))
                and check.get("eligibility_status") == "error"
                and check.get("binding_status") in {"unchecked", "proxy_failed"}
                and check.get("eligibility_reason") in {
                    "IP echo read failed: ReadError", "IP echo read failed: ReadTimeout",
                    "IP check failed: ValueError: ReadError", "IP check failed: ValueError: ReadTimeout",
                }
                and not check.get("last_403_category")
            )

        if not force and account_health_check_is_fresh(
            health, ttl_seconds=ttl_seconds
        ):
            if exclusive and not usable_exclusive_route(health):
                log.warning(
                    "[ANTIGRAVITY HEALTH] exclusive account failed strict cached gate: "
                    f"credential={credential_name}, binding={health.get('binding_status')}, "
                    f"eligibility={health.get('eligibility_status')}"
                )
                return None
            if account_health_has_negative_evidence(health):
                log.warning(
                    "[ANTIGRAVITY HEALTH] cached ineligible account skipped: "
                    f"credential={credential_name}, binding={health.get('binding_status')}, "
                    f"eligibility={health.get('eligibility_status')}"
                )
                return None
            if health.get("binding_status") != "healthy" or health.get(
                "eligibility_status"
            ) != "eligible":
                message = (
                    "[ANTIGRAVITY HEALTH] cached check inconclusive, allowing request: "
                    f"credential={credential_name}, binding={health.get('binding_status')}, "
                    f"eligibility={health.get('eligibility_status')}"
                )
                if health.get("eligibility_status") == "geo_blocked":
                    log.info(message)
                else:
                    log.warning(message)
            return credential_data

        access_token = credential_data.get("access_token") or credential_data.get("token")
        if not access_token:
            return None
        result = await self.check_antigravity_account_health(
            credential_name,
            credential_data,
            network=network, current_health=health,
        )
        if exclusive and not usable_exclusive_route(result):
            log.warning(
                "[ANTIGRAVITY HEALTH] exclusive account failed strict fresh gate: "
                f"credential={credential_name}, binding={result.get('binding_status')}, "
                f"eligibility={result.get('eligibility_status')}"
            )
            return None
        if account_health_has_negative_evidence(result):
            log.warning(
                "[ANTIGRAVITY HEALTH] account rejected before request: "
                f"credential={credential_name}, binding={result.get('binding_status')}, "
                f"eligibility={result.get('eligibility_status')}"
            )
            return None

        if (
            result.get("binding_status") != "healthy"
            or result.get("eligibility_status") != "eligible"
        ):
            log.warning(
                "[ANTIGRAVITY HEALTH] fresh check inconclusive, allowing request: "
                f"credential={credential_name}, binding={result.get('binding_status')}, "
                f"eligibility={result.get('eligibility_status')}"
            )
        return credential_data

    async def check_antigravity_account_health(
        self,
        credential_name: str,
        credential_data: Optional[Dict[str, Any]] = None,
        *, network=None, current_health=None,
    ) -> Dict[str, Any]:
        """Run one explicit IP and eligibility check, independent of gating mode."""
        await self._ensure_initialized()
        from config import (
            get_antigravity_api_url,
            get_antigravity_egress_ip_check_url,
        )
        from .antigravity_account_health import check_antigravity_account_health

        if credential_data is None:
            credential_data = await self._storage_adapter.get_credential(
                credential_name, mode="antigravity"
            )
        if not credential_data:
            raise ValueError(f"Antigravity credential not found: {credential_name}")
        if await self._should_refresh_token(credential_data, mode="antigravity"):
            refreshed = await self._refresh_token(
                credential_data, credential_name, mode="antigravity"
            )
            if not refreshed:
                raise ValueError("Antigravity access token refresh failed")
            credential_data = refreshed
            network = current_health = None

        if network is None:
            network = await self._storage_adapter.resolve_credential_network(credential_name, mode="antigravity")
        credential_data = dict(credential_data)
        credential_data.update(network)
        access_token = credential_data.get("access_token") or credential_data.get("token")
        if not access_token:
            raise ValueError("Antigravity credential has no access token")
        return dict(
            await check_antigravity_account_health(
                storage=self._storage_adapter,
                credential_name=credential_name,
                access_token=access_token,
                network=network,
                ip_check_url=await get_antigravity_egress_ip_check_url(),
                api_base_url=await get_antigravity_api_url(),
                credential_data=credential_data,
                current_health=current_health,
            )
        )

    async def rebind_antigravity_account(
        self,
        credential_name: str,
        credential_data: Dict[str, Any],
    ) -> bool:
        """Move a group account off its current node and validate the new binding."""
        await self._ensure_initialized()
        from config import get_antigravity_egress_ip_check_url

        if not await get_antigravity_egress_ip_check_url():
            return False
        invalidated = await self._storage_adapter.invalidate_antigravity_proxy_binding(
            credential_name, require_replacement=True
        )
        if not invalidated:
            return False
        result = await self.check_antigravity_account_health(
            credential_name, credential_data
        )
        return (
            result.get("binding_status") == "healthy"
            and result.get("eligibility_status") == "eligible"
        )

    async def rotate_antigravity_egress(
        self,
        credential_name: str,
        *,
        cooldown_seconds: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Repair a failed group proxy binding using a free replacement node."""
        await self._ensure_initialized()
        network = await self._storage_adapter.get_credential_network(
            credential_name, mode="antigravity"
        )
        if (
            network.get("proxy_mode") != "group"
            or not network.get("proxy_group_id")
            or network.get("fixed_proxy_ip")
        ):
            return None
        cooldown = (
            REGION_REJECTED_NODE_COOLDOWN_SECONDS
            if cooldown_seconds is None
            else max(int(cooldown_seconds), 0)
        )
        try:
            rotated = await self._storage_adapter.invalidate_antigravity_proxy_binding(
                credential_name,
                cooldown_seconds=cooldown,
                require_replacement=True,
            )
        except Exception as exc:  # noqa: BLE001 - a failed rebind keeps the caller's retry path
            log.warning(
                "[ANTIGRAVITY REGION] rebind failed: "
                f"credential={credential_name}, error={type(exc).__name__}: {exc}"
            )
            return None
        if not rotated:
            log.warning(
                "[ANTIGRAVITY REGION] no free node in group: "
                f"credential={credential_name}"
            )
            return None
        log.warning(
            "[ANTIGRAVITY REGION] repaired failed proxy binding: "
            f"credential={credential_name}, group={network.get('proxy_group_id')}"
        )
        return await self._storage_adapter.get_antigravity_account_health(credential_name)

    async def reload_antigravity_credential(
        self, credential_name: str
    ) -> Optional[Dict[str, Any]]:
        """重新读取账号凭证与当前生效出口，供同一账号换 IP 后重试请求。"""
        await self._ensure_initialized()
        credential_data = await self._storage_adapter.get_credential(
            credential_name, mode="antigravity"
        )
        if not credential_data:
            return None
        network = await self._storage_adapter.resolve_credential_network(
            credential_name, mode="antigravity"
        )
        merged = dict(credential_data)
        merged.update(network or {})
        return merged

    async def get_antigravity_account_health(
        self, credential_name: str
    ) -> Dict[str, Any]:
        await self._ensure_initialized()
        return await self._storage_adapter.get_antigravity_account_health(
            credential_name
        )

    async def update_antigravity_account_health(
        self, credential_name: str, **updates: Any
    ) -> Dict[str, Any]:
        await self._ensure_initialized()
        return await self._storage_adapter.update_antigravity_account_health(
            credential_name, **updates
        )

    async def remove_credential(self, credential_name: str, mode: str = "geminicli") -> bool:
        """删除一个凭证"""
        await self._ensure_initialized()
        try:
            await self._storage_adapter.delete_credential(credential_name, mode=mode)
            log.info(f"Credential removed: {credential_name} (mode={mode})")
            return True
        except Exception as e:
            log.error(f"Error removing credential {credential_name}: {e}")
            return False

    async def update_credential_state(self, credential_name: str, state_updates: Dict[str, Any], mode: str = "geminicli"):
        """更新凭证状态"""
        log.debug(f"[CredMgr] update_credential_state 开始: credential_name={credential_name}, state_updates={state_updates}, mode={mode}")
        log.debug(f"[CredMgr] 调用 _ensure_initialized...")
        await self._ensure_initialized()
        log.debug(f"[CredMgr] _ensure_initialized 完成")
        try:
            log.debug(f"[CredMgr] 调用 storage_adapter.update_credential_state...")
            success = await self._storage_adapter.update_credential_state(
                credential_name, state_updates, mode=mode
            )
            log.debug(f"[CredMgr] storage_adapter.update_credential_state 返回: {success}")
            if success:
                log.debug(f"Updated credential state: {credential_name} (mode={mode})")
            else:
                log.warning(f"Failed to update credential state: {credential_name} (mode={mode})")
            return success
        except Exception as e:
            log.error(f"Error updating credential state {credential_name}: {e}")
            return False

    async def set_cred_disabled(self, credential_name: str, disabled: bool, mode: str = "geminicli"):
        """设置凭证的启用/禁用状态"""
        try:
            log.info(f"[CredMgr] set_cred_disabled 开始: credential_name={credential_name}, disabled={disabled}, mode={mode}")
            success = await self.update_credential_state(
                credential_name, {"disabled": disabled}, mode=mode
            )
            log.info(f"[CredMgr] update_credential_state 返回: success={success}")
            if success:
                action = "disabled" if disabled else "enabled"
                log.info(f"Credential {action}: {credential_name} (mode={mode})")
            else:
                log.warning(f"[CredMgr] 设置禁用状态失败: credential_name={credential_name}, disabled={disabled}")
            return success
        except Exception as e:
            log.error(f"Error setting credential disabled state {credential_name}: {e}")
            return False

    async def get_creds_status(
        self, mode: str = "geminicli"
    ) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证的状态"""
        await self._ensure_initialized()
        try:
            return await self._storage_adapter.get_all_credential_states(mode=mode)
        except Exception as e:
            log.error(f"Error getting credential statuses: {e}")
            return {}

    async def get_creds_summary(self) -> List[Dict[str, Any]]:
        """
        获取所有凭证的摘要信息（轻量级，不包含完整凭证数据）
        使用后端的高性能查询
        """
        await self._ensure_initialized()
        try:
            return await self._storage_adapter._backend.get_credentials_summary()
        except Exception as e:
            log.error(f"Error getting credentials summary: {e}")
            return []

    async def get_or_fetch_user_email(self, credential_name: str, mode: str = "geminicli") -> Optional[str]:
        """获取或获取用户邮箱地址"""
        try:
            # 确保已初始化
            await self._ensure_initialized()
            
            # 从状态中获取缓存的邮箱
            state = await self._storage_adapter.get_credential_state(credential_name, mode=mode)
            cached_email = state.get("user_email") if state else None

            if cached_email:
                return cached_email

            # 如果没有缓存，从凭证数据获取
            credential_data = await self._storage_adapter.get_credential(credential_name, mode=mode)
            if not credential_data:
                return None

            from .google_oauth_api import get_user_email
            _, credentials = await self.get_account_credentials(credential_name, mode)

            # 获取邮箱
            email = await get_user_email(
                credentials, antigravity=(mode == "antigravity")
            )

            if email:
                # 缓存邮箱地址
                await self._storage_adapter.update_credential_state(
                    credential_name, {"user_email": email}, mode=mode
                )
                return email

            return None

        except Exception as e:
            log.error(f"Error fetching user email for {credential_name}: {e}")
            return None

    async def record_api_call_result(
        self,
        credential_name: str,
        success: bool,
        error_code: Optional[int] = None,
        cooldown_until: Optional[float] = None,
        mode: str = "geminicli",
        model_name: Optional[str] = None,
        error_message: Optional[str] = None,
        upstream_seconds: Optional[float] = None,
        gateway_seconds: Optional[float] = None,
        error_source: str = "generation"
    ):
        """
        记录API调用结果

        Args:
            credential_name: 凭证名称
            success: 是否成功
            error_code: 错误码（如果失败）
            cooldown_until: 冷却截止时间戳（Unix时间戳，针对429 QUOTA_EXHAUSTED）
            mode: 凭证模式 ("geminicli" 或 "antigravity")
            model_name: 模型名（用于设置模型级冷却）
            error_message: 错误信息（如果失败）
            upstream_seconds: 本次上游调用耗时（秒，可选，用于时效性统计）
            gateway_seconds: 本次请求网关处理耗时（秒，可选，仅最终结果时传）
        """
        await self._ensure_initialized()
        try:
            if success:
                # 必须等待持久化完成。SQLite 后端会串行化这类轻量条件写入，
                # 避免高并发成功请求创建无界后台任务和大量数据库文件句柄。
                with database_timing_scope("credential_save"):
                    await self._storage_adapter._backend.record_success(
                        credential_name, model_name=model_name, mode=mode
                    )

            elif error_code:
                # 记录错误码和错误信息
                error_messages = {}
                if mode == "antigravity" and error_code == 400:
                    from .antigravity_400_classifier import classify_antigravity_400

                    decision = classify_antigravity_400(error_message, source=error_source)
                    error_messages["400"] = decision.to_json()
                    log.warning(
                        f"[ANTIGRAVITY 400] credential={credential_name}, "
                        f"source={decision.source}, category={decision.category}, "
                        f"reason={decision.reason}"
                    )
                elif error_message:
                    error_messages[str(error_code)] = error_message

                state_updates = {
                    "error_codes": [error_code],
                    "error_messages": error_messages,
                }

                await self.update_credential_state(credential_name, state_updates, mode=mode)

                # 设置模型级冷却
                if cooldown_until is not None and model_name:
                    if hasattr(self._storage_adapter._backend, 'set_model_cooldown'):
                        await self._storage_adapter._backend.set_model_cooldown(
                            credential_name, model_name, cooldown_until, mode=mode
                        )
                        log.info(
                            f"设置模型级冷却: {credential_name}, model_name={model_name}, "
                            f"冷却至: {datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat()}"
                        )

            # 累计「凭证 × 模型」调用统计（后端支持时；统计内部已容错，不影响主流程）
            if model_name:
                increment = getattr(
                    self._storage_adapter._backend, "increment_model_stats", None
                )
                if increment:
                    with database_timing_scope("model_stats"):
                        await increment(
                            credential_name, model_name, success,
                            status_code=error_code, mode=mode,
                            upstream_seconds=upstream_seconds,
                            gateway_seconds=gateway_seconds,
                        )

        except Exception as e:
            log.error(f"Error recording API call result for {credential_name}: {e}")

    async def hold_transient(
        self,
        credential_name: str,
        status_code: Optional[int] = None,
        mode: str = "antigravity",
    ) -> float:
        """上游 5xx：把账号放回冷却窗口，等窗口过去再参与轮转。

        与 ``record_api_call_result`` 失败路径的区别：这里不写 error_codes、
        不改 ``disabled``，账号只是暂时不被选中。
        """
        await self._ensure_initialized()
        from config import get_antigravity_transient_hold_seconds

        seconds = await get_antigravity_transient_hold_seconds()
        if seconds <= 0:
            return 0.0
        return await self._storage_adapter.hold_antigravity_transient(
            credential_name, seconds=seconds, status_code=status_code
        )

    async def _should_refresh_token(self, credential_data: Dict[str, Any], mode: str = "geminicli") -> bool:
        """检查token是否需要刷新"""
        try:
            # 如果没有access_token或过期时间，需要刷新
            if not credential_data.get("access_token") and not credential_data.get("token"):
                log.debug("没有access_token，需要刷新")
                return True

            expiry_str = credential_data.get("expiry")
            if not expiry_str:
                log.debug("没有过期时间，需要刷新")
                return True

            # 解析过期时间
            try:
                if isinstance(expiry_str, str):
                    if "+" in expiry_str:
                        file_expiry = datetime.fromisoformat(expiry_str)
                    elif expiry_str.endswith("Z"):
                        file_expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                    else:
                        file_expiry = datetime.fromisoformat(expiry_str)
                else:
                    log.debug("过期时间格式无效，需要刷新")
                    return True

                # 确保时区信息
                if file_expiry.tzinfo is None:
                    file_expiry = file_expiry.replace(tzinfo=timezone.utc)

                # 检查是否还有足够有效期
                # 官方 Linux agy 使用 300 秒提前过期窗口；geminicli 同样保留 5 分钟缓冲。
                now = datetime.now(timezone.utc)
                time_left = (file_expiry - now).total_seconds()
                refresh_buffer = _refresh_buffer_seconds(credential_data, mode)

                log.debug(
                    f"Token时间检查: "
                    f"当前UTC时间={now.isoformat()}, "
                    f"过期时间={file_expiry.isoformat()}, "
                    f"剩余时间={int(time_left/60)}分{int(time_left%60)}秒"
                )

                if time_left > refresh_buffer:
                    return False
                else:
                    log.debug(f"Token即将过期（剩余{int(time_left/60)}分钟），需要刷新")
                    return True

            except Exception as e:
                log.warning(f"解析过期时间失败: {e}，需要刷新")
                return True

        except Exception as e:
            log.error(f"检查token过期时出错: {e}")
            return True

    async def get_account_credentials(self, filename: str, mode: str = "geminicli"):
        """Load an existing account through the single refresh/persistence path.

        The returned Credentials also delegates later refreshes to this manager,
        including nested userinfo/project helper calls.
        """
        await self._ensure_initialized()
        filename = os.path.basename(filename)
        data = await self._storage_adapter.get_credential(filename, mode=mode)
        if data is None:
            raise ValueError("Credential no longer exists")
        if await self._should_refresh_token(data, mode=mode):
            data = await self._refresh_token(data, filename, mode=mode)
            if data is None:
                raise ValueError("Credential refresh failed or authorization changed")
        credentials = Credentials.from_dict(data)
        if mode == "antigravity":
            from .proxy_groups import proxy_argument_from_network
            network = await self._storage_adapter.resolve_credential_network(filename, mode=mode)
            credentials.proxy_url = proxy_argument_from_network(network)

        async def refresh_bound():
            _, latest = await self.get_account_credentials(filename, mode)
            changed = credentials.access_token != latest.access_token
            for field in ("access_token", "refresh_token", "client_id", "client_secret",
                          "expires_at", "project_id", "oauth_client_key", "proxy_url"):
                setattr(credentials, field, getattr(latest, field))
            return changed

        credentials._refresh_callback = refresh_bound
        return dict(data), credentials

    async def _current_valid_credential(self, filename, mode):
        current = await self._storage_adapter.get_credential(filename, mode=mode)
        if current is not None and not await self._should_refresh_token(current, mode=mode):
            return current
        return None

    async def _refresh_token(
        self, credential_data: Dict[str, Any], filename: str, mode: str = "geminicli", *,
        rejected_access_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Share one refresh per account within this manager/event loop.

        Shield the task so cancellation of one request does not cancel other
        waiters or interrupt saving a rotated token. The worker also acquires a database lease.
        """
        filename = os.path.basename(filename)
        key = (mode, filename)
        task = self._refresh_tasks.get(key)
        joined = task is not None
        if task is None:
            options = {"rejected_access_token": rejected_access_token} if rejected_access_token else {}
            task = asyncio.create_task(self._refresh_token_once(dict(credential_data), filename, mode, **options),
                                       name="oauth-refresh:" + uuid.uuid4().hex)
            self._refresh_tasks[key] = task

            def finished(done):
                if self._refresh_tasks.get(key) is done:
                    self._refresh_tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()  # retrieve errors even if all waiters cancelled

            task.add_done_callback(finished)
        audit("refresh_requested", account_ref=reference(filename), mode=mode,
              refresh_event_id=task.get_name().removeprefix("oauth-refresh:"),
              source=inspect.currentframe().f_back.f_code.co_name,
              outcome="joined" if joined else "created",
              revision_ref=reference(credential_data.get("_credential_revision")), **expiry_state(credential_data))
        result = await asyncio.shield(task)
        return dict(result) if result is not None else None

    async def _refresh_token_once(self, credential_data, filename, mode="geminicli", *, rejected_access_token=None):
        name = asyncio.current_task().get_name()
        event_id = name.removeprefix("oauth-refresh:") if name.startswith("oauth-refresh:") else uuid.uuid4().hex
        token = refresh_context.set({"refresh_event_id": event_id,
                                     "account_ref": reference(filename), "mode": mode})
        started = time.monotonic()
        outcome = "cancelled"
        audit("refresh_started", revision_ref=reference(credential_data.get("_credential_revision")),
              **expiry_state(credential_data))
        try:
            options = {"rejected_access_token": rejected_access_token} if rejected_access_token else {}
            result = await self._refresh_token_coordinated(credential_data, filename, mode, **options)
            outcome = "credential_available" if result is not None else "no_result"
            return result
        finally:
            audit("refresh_finished", outcome=outcome, duration_ms=round((time.monotonic()-started)*1000))
            refresh_context.reset(token)

    async def _refresh_token_coordinated(self, credential_data, filename, mode="geminicli", *, rejected_access_token=None):
        """Cross-process single flight. Coordination failure never falls back to OAuth."""
        await self._ensure_initialized()
        owner = uuid.uuid4().hex
        acquired = False
        work = heartbeat = None
        result = None
        try:
            deadline = time.monotonic() + REFRESH_WAIT_TIMEOUT
            delay = 0.1
            wait_started = time.monotonic()
            attempts = 0
            while time.monotonic() < deadline:
                current = await self._storage_adapter.get_credential(filename, mode=mode)
                if current is None:
                    audit("refresh_skipped", reason="credential_deleted")
                    return None
                if ((current.get("access_token") or current.get("token")) != rejected_access_token
                        and not await self._should_refresh_token(current, mode=mode)):
                    audit("refresh_reused", revision_ref=reference(current.get("_credential_revision")))
                    return current
                status = await self._storage_adapter.acquire_refresh_lease(filename, owner, mode, ttl=REFRESH_LEASE_TTL)
                attempts += 1
                if attempts == 1 or status != "busy":
                    audit("refresh_lease", outcome=status, lease_owner_ref=reference(owner),
                          attempts=attempts, waited_ms=round((time.monotonic()-wait_started)*1000))
                if status == "acquired":
                    acquired = True
                    break
                if status == "cooldown":
                    return None
                if status != "busy":
                    raise RuntimeError("Unexpected refresh lease status")
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))
                delay = min(delay * 1.5, 1.0)
            if not acquired:
                audit("refresh_lease", outcome="wait_timeout", attempts=attempts)
                log.warning(f"等待刷新租约超时: {filename} (mode={mode})")
                return None

            async def renew():
                while True:
                    await asyncio.sleep(REFRESH_LEASE_RENEW_INTERVAL)
                    if not await self._storage_adapter.renew_refresh_lease(filename, owner, mode, ttl=REFRESH_LEASE_TTL):
                        audit("refresh_lease", outcome="lost")
                        raise RuntimeError("Refresh lease ownership lost")

            # Bound each attempt. A blocked/lost heartbeat stops the operation;
            # the atomic lease check in CAS remains the final write fence.
            heartbeat = asyncio.create_task(renew())
            options = {"rejected_access_token": rejected_access_token} if rejected_access_token else {}
            work = asyncio.create_task(self._refresh_token_owned(
                current, filename, mode, lease_owner=owner, **options
            ))
            done, _ = await asyncio.wait((work, heartbeat), timeout=REFRESH_OPERATION_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat
            if work not in done:
                raise TimeoutError("Refresh exceeded lease operation deadline")
            result = await work
            return result
        except Exception as exc:
            audit("refresh_coordination_failed", error_type=type(exc).__name__)
            log.error(f"刷新租约协调失败: {filename} (mode={mode}, type={type(exc).__name__})")
            return None
        finally:
            tasks = [task for task in (work, heartbeat) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if acquired:
                try:
                    # Share failures briefly too, avoiding queued sequential retries.
                    await self._storage_adapter.release_refresh_lease(
                        filename, owner, mode, cooldown=REFRESH_FAILURE_COOLDOWN if result is None else 0
                    )
                    audit("refresh_lease", outcome="released", lease_owner_ref=reference(owner))
                except Exception:
                    audit("refresh_lease", outcome="release_failed")
                    log.warning(f"刷新租约释放失败，将自动过期: {filename} (mode={mode})")

    async def _refresh_token_owned(
        self, credential_data: Dict[str, Any], filename: str, mode: str = "geminicli", *, lease_owner: str,
        rejected_access_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """刷新token并更新存储"""
        await self._ensure_initialized()
        expected = None
        try:
            # A caller may have queued a stale snapshot before another refresh
            # completed. Re-read persisted state before making an OAuth request.
            current = await self._storage_adapter.get_credential(filename, mode=mode)
            if current is None:
                return None  # do not recreate an account deleted during selection
            expected = dict(current)
            credential_data = dict(current)
            if ((credential_data.get("access_token") or credential_data.get("token")) != rejected_access_token
                    and not await self._should_refresh_token(credential_data, mode=mode)):
                return credential_data
            creds = Credentials.from_dict(credential_data)
            if mode == "antigravity":
                from .proxy_groups import proxy_argument_from_network

                network = credential_data
                if not credential_data.get("proxy_mode"):
                    network = await self._storage_adapter.resolve_credential_network(filename, mode=mode)
                creds.proxy_url = proxy_argument_from_network(network)

            # 检查是否可以刷新
            if not creds.refresh_token:
                log.error(f"没有refresh_token，无法刷新: {filename} (mode={mode})")
                # 自动禁用没有refresh_token的凭证
                try:
                    await self._storage_adapter.compare_and_swap_credential(
                        filename, expected, expected, mode=mode, lease_owner=lease_owner, state_updates={"disabled": True}
                    )
                except Exception as e:
                    log.error(f"禁用凭证失败 {filename}: {e}")
                return None

            # Revalidate immediately before outbound I/O after potentially slow
            # credential/network resolution. Never start with an expired lease.
            if not await self._storage_adapter.renew_refresh_lease(
                filename, lease_owner, mode, ttl=REFRESH_LEASE_TTL
            ):
                return None
            # 刷新token
            log.debug(f"正在刷新token: {filename} (mode={mode})")
            audit("oauth_operation_started", revision_ref=reference(expected.get("_credential_revision")),
                  proxy_config_ref=reference(creds.proxy_url) if isinstance(creds.proxy_url, str) else None,
                  proxy_mode="default_unresolved" if creds.proxy_url is ... else "direct" if creds.proxy_url is None else "explicit")
            oauth_started = time.monotonic()
            await creds.refresh()
            audit("oauth_operation_succeeded", duration_ms=round((time.monotonic()-oauth_started)*1000),
                  refresh_token_changed=creds.refresh_token != expected.get("refresh_token"))

            # 更新凭证数据
            if creds.access_token:
                credential_data["access_token"] = creds.access_token
                # 保持兼容性
                credential_data["token"] = creds.access_token

            if creds.expires_at:
                credential_data["expiry"] = creds.expires_at.isoformat()

            # Preserve OAuth rotation and the client used for this authorization.
            if creds.refresh_token:
                credential_data["refresh_token"] = creds.refresh_token
            if creds.oauth_client_key:
                credential_data["oauth_client_key"] = creds.oauth_client_key

            # 保存到存储
            saved = await self._storage_adapter.compare_and_swap_credential(
                filename, expected, credential_data, mode=mode, lease_owner=lease_owner
            )
            audit("refresh_write_result", committed=bool(saved))
            if saved:
                log.info(f"Token刷新成功并已保存: {filename} (mode={mode})")
            else:
                log.info(f"丢弃过期的刷新结果（凭证已变更或删除）: {filename} (mode={mode})")
            return await self._current_valid_credential(filename, mode)

        except Exception as e:
            audit("refresh_failed", error_type=type(e).__name__, http_status=getattr(e, "status_code", None),
                  oauth_event_id=getattr(e, "evidence_id", None))
            error_msg = str(e)
            log.error(f"Token刷新失败 {filename} (mode={mode}): {error_msg}")

            # 尝试提取HTTP状态码（TokenError可能携带status_code属性）
            status_code = None
            if hasattr(e, 'status_code'):
                status_code = e.status_code

            # A multi-client failure may mention earlier errors in its text.
            # The final structured OAuth code takes precedence over that history.
            oauth_error_code = getattr(e, "error_code", None)
            diagnostic_text = (
                json.dumps({"error": oauth_error_code}) if oauth_error_code else error_msg
            )

            # 状态码不足以证明授权失效；需要明确的 OAuth 撤销/过期证据。
            is_permanent_failure = self._is_permanent_refresh_failure(diagnostic_text, status_code)

            # An old OAuth failure must not disable or annotate a replacement
            # authorization. Persist diagnosis and disable together under the CAS.
            if expected is not None and (status_code or is_permanent_failure):
                from .antigravity_400_classifier import classify_antigravity_400
                code = status_code or 400
                detail = classify_antigravity_400(diagnostic_text, source="oauth").to_json()
                updates = {"error_codes": [code], "error_messages": {str(code): detail}}
                if is_permanent_failure:
                    updates["disabled"] = True
                applied = await self._storage_adapter.compare_and_swap_credential(
                    filename, expected, expected, mode=mode, lease_owner=lease_owner, state_updates=updates
                )
                if not applied:
                    return await self._current_valid_credential(filename, mode)
            if not is_permanent_failure and mode == "antigravity":
                # Only the still-current authorization may influence its proxy binding.
                current = await self._storage_adapter.get_credential(filename, mode=mode)
                if current == expected:
                    await self._cool_dead_proxy_node(filename, error_msg)

            return None

    async def _cool_dead_proxy_node(self, filename: str, error_msg: str) -> bool:
        """代理节点连不上时冷却该节点并解绑账号，避免账号被反复选中反复失败。

        只对「组模式 + 连接层错误」生效：HTTP 状态类错误（429/5xx）由各自的
        冷却逻辑处理，不应把节点标记为不可用。
        """
        text = (error_msg or "").lower()
        if not any(marker in text for marker in _PROXY_CONNECT_ERROR_MARKERS):
            return False
        try:
            network = await self._storage_adapter.get_credential_network(
                filename, mode="antigravity"
            )
        except Exception as exc:
            log.debug(f"读取账号网络配置失败 {filename}: {exc}")
            return False
        if network.get("proxy_mode") != "group":
            return False
        try:
            cooled = await self._storage_adapter.invalidate_antigravity_proxy_binding(
                filename, cooldown_seconds=PROXY_NODE_FAILURE_COOLDOWN_SECONDS
            )
        except Exception as exc:
            log.warning(f"冷却代理节点失败 {filename}: {exc}")
            return False
        if cooled:
            log.warning(
                f"代理节点不可达，已冷却 {PROXY_NODE_FAILURE_COOLDOWN_SECONDS}s 并解绑账号: {filename}"
            )
        return bool(cooled)

    def _is_permanent_refresh_failure(self, error_msg: str, status_code: Optional[int] = None) -> bool:
        """
        判断是否是凭证永久失效的错误

        Args:
            error_msg: 错误信息
            status_code: HTTP状态码（如果有）

        Returns:
            True表示凭证永久失效应封禁，False表示临时错误不应封禁
        """
        # Only explicit authorization revocation is account-level evidence.
        # Client configuration and a bare 400/401/403 do not prove a dead account.
        from .antigravity_400_classifier import classify_antigravity_400

        if status_code in (429, 500, 502, 503, 504):
            return False
        return classify_antigravity_400(error_msg, source="oauth").disable_account


class _CredentialManagerSingleton:
    """单例包装器，支持懒加载和自动初始化"""

    _instance: Optional[CredentialManager] = None
    _lock = None

    def __init__(self):
        self._manager = None

    async def _get_or_create(self) -> CredentialManager:
        """获取或创建单例实例（线程安全）"""
        if self._instance is None:
            # 简单的实例创建（异步环境下一般不需要复杂的锁）
            if self._instance is None:
                self._instance = CredentialManager()
                await self._instance.initialize()
                log.debug("CredentialManager singleton initialized")

        return self._instance

    def __getattr__(self, name):
        """代理所有方法调用到真实的 CredentialManager 实例"""
        async def _async_wrapper(*args, **kwargs):
            manager = await self._get_or_create()
            method = getattr(manager, name)
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        return _async_wrapper


# 全局单例实例 - 直接导入即可使用
credential_manager = _CredentialManagerSingleton()
