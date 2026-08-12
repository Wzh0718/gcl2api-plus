"""
凭证管理器
"""

import asyncio
import inspect
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from log import log

from src.google_oauth_api import Credentials
from src.storage_adapter import get_storage_adapter


IMAGE_CAPACITY_DEMOTION_SECONDS = 60


def _antigravity_tier_priority(tier: Any) -> int:
    normalized = str(tier or "").strip().lower()
    if "ultra" in normalized or "utrl" in normalized:
        return 0
    if "pro" in normalized:
        return 1
    if "free" in normalized:
        return 2
    return 3


def rank_antigravity_image_candidates(
    credential_states: Dict[str, Dict[str, Any]],
    *,
    model_name: str,
    runtime_health: Optional[Dict[str, Dict[str, Any]]] = None,
    excluded_filenames: Optional[Set[str]] = None,
    now: Optional[float] = None,
) -> List[str]:
    """Rank image accounts by live health, least use, then account tier."""
    current_time = time.time() if now is None else now
    runtime_health = runtime_health or {}
    excluded = excluded_filenames or set()
    ranked: List[Tuple[Tuple[Any, ...], str]] = []

    for filename, state in credential_states.items():
        if filename in excluded or state.get("disabled"):
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
        call_count = max(int(state.get("call_count") or 0), 0)
        rotation_order = max(int(state.get("rotation_order") or 0), 0)
        sort_key = (
            health_priority,
            call_count,
            _antigravity_tier_priority(state.get("tier")),
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
        self._initialized = False
        self._storage_adapter = None
        self._image_runtime_health: Dict[str, Dict[str, Any]] = {}
        self._image_lease_condition = asyncio.Condition()
        self._image_account_leases: Dict[str, str] = {}

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
        await self.reset_image_account_leases()
        self._initialized = False
        log.debug("Credential manager closed")

    async def _reserve_available_image_credential(
        self, ranked_filenames: List[str]
    ) -> Optional[Tuple[str, str]]:
        """Atomically reserve the highest-ranked idle image account."""
        async with self._image_lease_condition:
            for filename in ranked_filenames:
                if filename in self._image_account_leases:
                    continue
                lease_id = uuid.uuid4().hex
                self._image_account_leases[filename] = lease_id
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
            if self._image_account_leases.get(credential_name) != lease_id:
                return False
            del self._image_account_leases[credential_name]
            self._image_lease_condition.notify_all()
            return True

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
        每次随机选择一个可用的凭证（未禁用、未冷却、符合preview要求）
        如果刷新失败会自动禁用失效凭证并重试获取下一个可用凭证

        Args:
            mode: 凭证模式 ("geminicli" 或 "antigravity")
            model_name: 完整模型名，用于模型级冷却检查和preview筛选
                       - geminicli: 完整模型名
                                   - 包含 "preview" 的模型只能使用 preview=True 的凭证
                                   - 不包含 "preview" 的模型优先使用 preview=False 的凭证
                       - antigravity: 完整模型名（如 "gemini-2.0-flash-exp"）
        """
        await self._ensure_initialized()

        if mode == "antigravity" and (
            image_request or "image" in (model_name or "").lower()
        ):
            return await self._get_valid_antigravity_image_credential(
                model_name=model_name or "",
                exclude_filenames=exclude_filenames,
            )

        # 最多重试3次
        max_retries = 3
        attempted_filenames: set[str] = set(exclude_filenames or set())
        for attempt in range(max_retries):
            result = await self._storage_adapter._backend.get_next_available_credential(
                mode=mode,
                model_name=model_name,
                exclude_filenames=attempted_filenames,
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
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Reserve one idle image account, waiting when every candidate is busy."""
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
                states,
                model_name=model_name,
                runtime_health=self._image_runtime_health,
                excluded_filenames=attempted_filenames,
            )
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
                        # Release the local lease and rank the refreshed counters.
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

                network = await self._storage_adapter.resolve_credential_network(
                    filename, mode="antigravity"
                )
                credential_data.update(network)
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
        """Gate one account on a fresh, stable IP and eligible region when enabled."""
        await self._ensure_initialized()
        from config import (
            get_antigravity_network_check_enabled,
            get_antigravity_network_check_ttl_seconds,
        )
        from .antigravity_account_health import account_health_check_is_fresh
        from .antigravity_account_health import account_health_has_negative_evidence

        if not await get_antigravity_network_check_enabled():
            return credential_data

        network = await self._storage_adapter.resolve_credential_network(
            credential_name, mode="antigravity"
        )
        credential_data = dict(credential_data)
        credential_data.update(network)
        health = await self._storage_adapter.get_antigravity_account_health(
            credential_name
        )
        ttl_seconds = await get_antigravity_network_check_ttl_seconds()
        if not force and account_health_check_is_fresh(
            health, ttl_seconds=ttl_seconds
        ):
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
                # 检测设施故障留下的状态不含账号证据，放行；代理真挂了
                # 请求会自己失败并走正常重试。geo_blocked 是明确的放行
                # 状态（见 account_health_has_negative_evidence），只记 info。
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
        )
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

        network = await self._storage_adapter.resolve_credential_network(
            credential_name, mode="antigravity"
        )
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
            credential_name
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

            # 创建凭证对象并自动刷新 token
            from .google_oauth_api import Credentials, get_user_email
            from .proxy_groups import proxy_argument_from_network

            credentials = Credentials.from_dict(credential_data)
            if mode == "antigravity":
                network = await self._storage_adapter.resolve_credential_network(credential_name, mode=mode)
                credentials.proxy_url = proxy_argument_from_network(network)
            if not credentials:
                return None

            # 自动刷新 token（如果需要）
            token_refreshed = await credentials.refresh_if_needed()

            # 如果 token 被刷新了，更新存储
            if token_refreshed:
                log.info(f"Token已自动刷新: {credential_name} (mode={mode})")
                updated_data = credentials.to_dict()
                await self._storage_adapter.store_credential(credential_name, updated_data, mode=mode)

            # 获取邮箱
            email = await get_user_email(credentials)

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
        gateway_seconds: Optional[float] = None
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
                await self._storage_adapter._backend.record_success(
                    credential_name, model_name=model_name, mode=mode
                )

            elif error_code:
                # 记录错误码和错误信息
                error_messages = {}
                if error_message:
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
                    await increment(
                        credential_name, model_name, success,
                        status_code=error_code, mode=mode,
                        upstream_seconds=upstream_seconds,
                        gateway_seconds=gateway_seconds,
                    )

        except Exception as e:
            log.error(f"Error recording API call result for {credential_name}: {e}")

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
                # antigravity 提前 900 秒刷新（对齐 Antigravity-Manager 的
                # TOKEN_REFRESH_SKEW_SECONDS），geminicli 保持 5 分钟缓冲
                now = datetime.now(timezone.utc)
                time_left = (file_expiry - now).total_seconds()
                refresh_buffer = 900 if mode == "antigravity" else 300

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

    async def _refresh_token(
        self, credential_data: Dict[str, Any], filename: str, mode: str = "geminicli"
    ) -> Optional[Dict[str, Any]]:
        """刷新token并更新存储"""
        await self._ensure_initialized()
        try:
            # 创建Credentials对象
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
                    await self.update_credential_state(filename, {"disabled": True}, mode=mode)
                    log.warning(f"凭证已自动禁用（缺少refresh_token）: {filename}")
                except Exception as e:
                    log.error(f"禁用凭证失败 {filename}: {e}")
                return None

            # 刷新token
            log.debug(f"正在刷新token: {filename} (mode={mode})")
            await creds.refresh()

            # 更新凭证数据
            if creds.access_token:
                credential_data["access_token"] = creds.access_token
                # 保持兼容性
                credential_data["token"] = creds.access_token

            if creds.expires_at:
                credential_data["expiry"] = creds.expires_at.isoformat()

            # 保存到存储
            await self._storage_adapter.store_credential(filename, credential_data, mode=mode)
            log.info(f"Token刷新成功并已保存: {filename} (mode={mode})")

            return credential_data

        except Exception as e:
            error_msg = str(e)
            log.error(f"Token刷新失败 {filename} (mode={mode}): {error_msg}")

            # 尝试提取HTTP状态码（TokenError可能携带status_code属性）
            status_code = None
            if hasattr(e, 'status_code'):
                status_code = e.status_code

            # 检查是否是凭证永久失效的错误（只有明确的400/403等才判定为永久失效）
            is_permanent_failure = self._is_permanent_refresh_failure(error_msg, status_code)

            if is_permanent_failure:
                log.warning(f"检测到凭证永久失效 (HTTP {status_code}): {filename}")
                # 记录失效状态
                if status_code:
                    await self.record_api_call_result(filename, False, status_code, mode=mode)
                else:
                    await self.record_api_call_result(filename, False, 400, mode=mode)

                # 禁用失效凭证
                try:
                    # 直接禁用该凭证（随机选择机制会自动跳过它）
                    disabled_ok = await self.update_credential_state(filename, {"disabled": True}, mode=mode)
                    if disabled_ok:
                        log.warning(f"永久失效凭证已禁用: {filename}")
                    else:
                        log.warning("永久失效凭证禁用失败，将由上层逻辑继续处理")
                except Exception as e2:
                    log.error(f"禁用永久失效凭证时出错 {filename}: {e2}")
            else:
                # 网络错误或其他临时性错误，不封禁凭证
                log.warning(f"Token刷新失败但非永久性错误 (HTTP {status_code})，不封禁凭证: {filename}")

            return None

    def _is_permanent_refresh_failure(self, error_msg: str, status_code: Optional[int] = None) -> bool:
        """
        判断是否是凭证永久失效的错误

        Args:
            error_msg: 错误信息
            status_code: HTTP状态码（如果有）

        Returns:
            True表示凭证永久失效应封禁，False表示临时错误不应封禁
        """
        # 优先使用HTTP状态码判断
        if status_code is not None:
            # 400/401/403 明确表示凭证有问题，应该封禁
            if status_code in [400, 401, 403]:
                log.debug(f"检测到客户端错误状态码 {status_code}，判定为永久失效")
                return True
            # 500/502/503/504 是服务器错误，不应封禁凭证
            elif status_code in [500, 502, 503, 504]:
                log.debug(f"检测到服务器错误状态码 {status_code}，不应封禁凭证")
                return False
            # 429 (限流) 不应封禁凭证
            elif status_code == 429:
                log.debug("检测到限流错误 429，不应封禁凭证")
                return False

        # 如果没有状态码，回退到错误信息匹配（谨慎判断）
        # 只有明确的凭证失效错误才判定为永久失效
        permanent_error_patterns = [
            "invalid_grant",
            "refresh_token_expired",
            "invalid_refresh_token",
            "unauthorized_client",
            "access_denied",
        ]

        error_msg_lower = error_msg.lower()
        for pattern in permanent_error_patterns:
            if pattern.lower() in error_msg_lower:
                log.debug(f"错误信息匹配到永久失效模式: {pattern}")
                return True

        # 默认认为是临时错误（如网络问题），不应封禁凭证
        log.debug("未匹配到明确的永久失效模式，判定为临时错误")
        return False

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
