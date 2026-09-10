"""
Antigravity API Client - Handles communication with Google's Antigravity API
处理与 Google Antigravity API 的通信
"""

import asyncio
import hashlib
import inspect
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable, Set, Tuple

from fastapi import Response
from config import (
    get_antigravity_api_url,
    get_antigravity_api_url_candidates,
    get_antigravity_model_fallback_chain,
    get_antigravity_network_check_enabled,
    get_antigravity_stream2nostream,
    get_auto_ban_error_codes,
)
from log import log

from src.credential_manager import credential_manager
from src.httpx_client import stream_post_async, post_async
from src.stream_guard import aclose_quietly
from src.models import Model, model_to_dict
from src.redis_config import create_redis_client_from_env
from src.utils import ANTIGRAVITY_USER_AGENT
from src.antigravity_error_classifier import (
    Antigravity403Decision,
    classify_antigravity_403,
)

# 导入共同的基础功能
from src.api.utils import (
    check_should_auto_ban,
    handle_error_with_retry,
    get_retry_config,
    record_api_call_success,
    record_api_call_error,
    parse_and_log_cooldown,
    collect_streaming_response,
)
from src.billing import extract_usage_metadata, get_billing_recorder
from src.dingtalk_alert import (
    all_accounts_are_unavailable,
    send_all_accounts_unavailable_alert,
)

# ==================== 全局凭证管理器 ====================

# 使用全局单例 credential_manager，自动初始化


# ==================== 会话状态管理 ====================

SESSION_TTL_SECONDS = 6 * 60 * 60
MAX_SESSION_STATES = 1024
IMAGE_MODEL_CACHE_TTL_SECONDS = 5 * 60
MAX_IMAGE_MODEL_CACHE_ENTRIES = 128
IMAGE_MODEL_CAPACITY_MARKER = "MODEL_CAPACITY_EXHAUSTED"
IMAGE_CAPACITY_MAX_RETRIES = 2
IMAGE_CAPACITY_DEFAULT_RETRY_DELAY_SECONDS = 1.0
IMAGE_CAPACITY_MAX_RETRY_DELAY_SECONDS = 60.0
_REDIS_KEY_PREFIX = "antigravity:session:"


@dataclass
class AntigravitySessionState:
    conversation_id: str
    trajectory_id: str
    session_id: str
    step_index: int
    created_at: float
    last_used_at: float


@dataclass(frozen=True)
class Antigravity403HandlingResult:
    decision: Antigravity403Decision
    should_retry: bool
    cooldown_until: Optional[float] = None


@dataclass(frozen=True)
class ImageUpstreamAttempt:
    host: str
    status_code: Optional[int]
    elapsed_seconds: float
    error: Optional[str] = None


@dataclass(frozen=True)
class ImageUpstreamResult:
    response: Any
    host: str
    attempts: Tuple[ImageUpstreamAttempt, ...]


# 内存回退存储
_session_states: Dict[str, AntigravitySessionState] = {}
_image_available_models_cache: Dict[str, Tuple[float, Set[str]]] = {}

# Redis 客户端（懒初始化，REDIS_URL 存在时使用）
_redis_client = None
_redis_checked = False


async def _get_redis():
    """懒初始化 Redis 客户端，REDIS_URL 未设置时返回 None。"""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    try:
        client = create_redis_client_from_env(decode_responses=True)
        if client is None:
            return None
        await client.ping()
        _redis_client = client
        log.info("[SESSION] Redis session store enabled")
    except Exception as e:
        log.warning(
            f"[SESSION] Redis unavailable, falling back to in-memory: {type(e).__name__}"
        )
    return _redis_client


def _extract_first_user_text(request_payload: Dict[str, Any]) -> str:
    contents = request_payload.get("contents", [])
    if not isinstance(contents, list):
        return ""
    for content in contents:
        if not isinstance(content, dict) or content.get("role") != "user":
            continue
        parts = content.get("parts", [])
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                return str(part["text"])
    return ""


def _session_key(request_payload: Dict[str, Any], model: str = "") -> str:
    session_id = request_payload.get("sessionId")
    if session_id:
        return f"session:{session_id}"
    model_prefix = f"model:{model}:" if model else ""
    first_user_text = _extract_first_user_text(request_payload)
    if first_user_text:
        digest = hashlib.sha256(first_user_text.encode("utf-8")).hexdigest()[:32]
        return f"{model_prefix}text:{digest}"
    return f"{model_prefix}default"


def _prune_session_states(now: float) -> None:
    expired = [k for k, s in _session_states.items() if now - s.last_used_at > SESSION_TTL_SECONDS]
    for k in expired:
        _session_states.pop(k, None)
    if len(_session_states) <= MAX_SESSION_STATES:
        return
    overflow = len(_session_states) - MAX_SESSION_STATES
    oldest = sorted(_session_states.items(), key=lambda item: item[1].last_used_at)
    for k, _ in oldest[:overflow]:
        _session_states.pop(k, None)


def _make_new_state(first_user_text: str, now: float) -> AntigravitySessionState:
    if first_user_text:
        digest = hashlib.sha256(first_user_text.encode("utf-8")).digest()
        session_id_val = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
        session_id = f"-{session_id_val}"
    else:
        session_id = f"-{uuid.uuid4().int % 9_000_000_000_000_000_000}"
    return AntigravitySessionState(
        conversation_id=str(uuid.uuid4()),
        trajectory_id=str(uuid.uuid4()),
        session_id=session_id,
        step_index=1,
        created_at=now,
        last_used_at=now,
    )


async def _get_session_state(request_payload: Dict[str, Any], model: str = "") -> AntigravitySessionState:
    now = time.time()
    key = _session_key(request_payload, model)
    first_user_text = _extract_first_user_text(request_payload)

    redis = await _get_redis()
    if redis is not None:
        redis_key = f"{_REDIS_KEY_PREFIX}{key}"
        try:
            raw = await redis.get(redis_key)
            if raw:
                data = json.loads(raw)
                state = AntigravitySessionState(**data)
                state.step_index += 1
                state.last_used_at = now
            else:
                state = _make_new_state(first_user_text, now)
            await redis.set(redis_key, json.dumps(state.__dict__), ex=SESSION_TTL_SECONDS)
            return state
        except Exception as e:
            log.warning(
                f"[SESSION] Redis error, falling back to memory: {type(e).__name__}"
            )

    # 内存回退
    _prune_session_states(now)
    state = _session_states.get(key)
    if state:
        state.step_index += 1
        state.last_used_at = now
        return state
    state = _make_new_state(first_user_text, now)
    _session_states[key] = state
    return state


def _generate_request_id(conversation_id: str, trajectory_id: str, step: int) -> str:
    unix_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"agent/{conversation_id}/{unix_ms}/{trajectory_id}/{step}"


def _build_labels(model: str, trajectory_id: str, step: int) -> Dict[str, str]:
    used_claude = "claude" in model.lower()
    return {
        "last_step_index": str(step),
        "model_enum": model,
        "trajectory_id": trajectory_id,
        "used_claude": str(used_claude).lower(),
        "used_claude_conservative": str(used_claude).lower(),
    }


async def wrap_cli_request(
    gemini_request: Dict[str, Any],
    model: str,
    project_id: str,
    user_email: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """
    将 Gemini 格式请求包装成 Antigravity CLI 格式。
    返回 (payload, request_id)。

    user_email 为 None 或 gmail/googlemail 邮箱时保持 antigravity 指纹；
    其他邮箱（如企业/教育账号）改用 jetski 指纹（对齐 Antigravity-Manager）。
    """
    inner = dict(gemini_request)

    # 移除 safetySettings（CLI 不发送）
    inner.pop("safetySettings", None)
    is_image_request = is_antigravity_image_request(model, inner)

    # 获取/更新会话状态
    state = await _get_session_state(inner, model)

    if not is_image_request:
        # Agent 请求沿用 CLI 会话字段；原生图片请求不携带这些字段。
        if not inner.get("sessionId"):
            inner["sessionId"] = state.session_id
        inner["labels"] = _build_labels(model, state.trajectory_id, state.step_index)

        tool_config = inner.get("toolConfig") or {}
        func_config = tool_config.get("functionCallingConfig") or {}
        if "mode" not in func_config:
            func_config["mode"] = "VALIDATED"
        tool_config["functionCallingConfig"] = func_config
        inner["toolConfig"] = tool_config

    request_id = _generate_request_id(state.conversation_id, state.trajectory_id, state.step_index)

    # 非 gmail 账号使用 jetski 指纹
    email = (user_email or "").strip().lower()
    is_gmail = not email or email.endswith(("@gmail.com", "@googlemail.com"))
    user_agent = "antigravity" if is_gmail else "jetski"
    if not is_gmail:
        inner["metadata"] = {"ideType": "JETSKI"}

    payload = {
        "project": project_id,
        "requestId": request_id,
        "request": inner,
        "model": model,
        "userAgent": user_agent,
        "requestType": "image_gen" if is_image_request else "agent",
    }
    if not is_image_request:
        payload["enabledCreditTypes"] = ["GOOGLE_ONE_AI"]
    return payload, request_id


# ==================== 辅助函数 ====================

def build_antigravity_headers(
    access_token: str,
    *,
    image_request: bool = False,
    project_id: Optional[str] = None,
) -> Dict[str, str]:
    """构建 Antigravity CLI API 请求头。"""
    return {
        "User-Agent": ANTIGRAVITY_USER_AGENT,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }


def is_antigravity_image_request(
    model_name: str, request_payload: Optional[Dict[str, Any]] = None
) -> bool:
    """Detect image generation without relying only on the public model alias."""
    if "image" in (model_name or "").lower():
        return True
    payload = request_payload or {}
    generation_config = payload.get("generationConfig") or {}
    if not isinstance(generation_config, dict):
        return False
    modalities = generation_config.get("responseModalities") or generation_config.get(
        "response_modalities"
    )
    if isinstance(modalities, list) and any(
        str(modality).upper() == "IMAGE" for modality in modalities
    ):
        return True
    response_format = generation_config.get("responseFormat") or {}
    return bool(
        generation_config.get("imageConfig")
        or generation_config.get("image_config")
        or (isinstance(response_format, dict) and response_format.get("image"))
    )


def build_dynamic_image_model_candidates(model_name: str) -> List[str]:
    """Return same-tier image aliases, preserving the requested model first."""
    normalized = (model_name or "").strip().lower()
    families = (
        ("gemini-3.1-pro-image", "gemini-3-pro-image"),
        ("gemini-3.1-flash-image", "gemini-3-flash-image"),
    )
    for family in families:
        if normalized not in family:
            continue
        return [normalized, *(candidate for candidate in family if candidate != normalized)]
    return [normalized] if normalized else []


def _prune_image_model_cache(now: float, *, reserve_entries: int = 0) -> None:
    expired = [
        cache_key
        for cache_key, (cached_at, _) in _image_available_models_cache.items()
        if now - cached_at >= IMAGE_MODEL_CACHE_TTL_SECONDS
    ]
    for cache_key in expired:
        _image_available_models_cache.pop(cache_key, None)

    allowed_entries = max(MAX_IMAGE_MODEL_CACHE_ENTRIES - reserve_entries, 0)
    overflow = len(_image_available_models_cache) - allowed_entries
    if overflow <= 0:
        return
    oldest = sorted(
        _image_available_models_cache.items(), key=lambda item: item[1][0]
    )
    for cache_key, _ in oldest[:overflow]:
        _image_available_models_cache.pop(cache_key, None)


async def _post_image_request(
    path: str,
    *,
    headers: Dict[str, str],
    json_body: Dict[str, Any],
    proxy_url: Any,
    timeout: float,
):
    """Send an image request to the same configured host used by native AGY."""
    host = (await get_antigravity_api_url()).rstrip("/")
    response = await post_async(
        url=f"{host}{path}",
        json=json_body,
        headers=headers,
        proxy_url=proxy_url,
        timeout=timeout,
    )
    return response, host


async def _get_available_models_for_credential(
    *,
    credential_name: str,
    credential_data: Dict[str, Any],
) -> Set[str]:
    """Fetch and briefly cache the model IDs exposed to one account."""
    now = time.monotonic()
    project_id = credential_data.get("project_id") or ""
    cache_key = f"{credential_name}:{project_id}"
    _prune_image_model_cache(now)
    cached = _image_available_models_cache.get(cache_key)
    if cached and now - cached[0] < IMAGE_MODEL_CACHE_TTL_SECONDS:
        return set(cached[1])

    access_token = credential_data.get("access_token") or credential_data.get("token")
    if not access_token:
        return set()

    request_headers = build_antigravity_headers(
        access_token,
        image_request=True,
        project_id=project_id,
    )
    proxy_url = get_effective_proxy_url(credential_data)
    response, _ = await _post_image_request(
        "/v1internal:fetchAvailableModels",
        headers=request_headers,
        json_body={"project": project_id} if project_id else {},
        proxy_url=proxy_url,
        timeout=30.0,
    )
    if response.status_code != 200:
        return set()
    payload = _safe_response_json(response) or {}
    models = payload.get("models")
    if not isinstance(models, dict):
        return set()
    available = {
        str(model_id).strip().lower()
        for model_id in models.keys()
        if str(model_id).strip()
    }
    _prune_image_model_cache(now, reserve_entries=1)
    _image_available_models_cache[cache_key] = (now, available)
    return available


async def resolve_dynamic_image_model_for_credential(
    *,
    credential_name: str,
    credential_data: Dict[str, Any],
    requested_model: str,
) -> str:
    """Resolve version drift for an image model without crossing Pro/Flash tiers."""
    candidates = build_dynamic_image_model_candidates(requested_model)
    if len(candidates) <= 1:
        return requested_model
    try:
        available = await _get_available_models_for_credential(
            credential_name=credential_name,
            credential_data=credential_data,
        )
    except Exception as exc:
        log.warning(
            "[ANTIGRAVITY IMAGE] dynamic model lookup failed: "
            f"credential={credential_name}, error={type(exc).__name__}"
        )
        return requested_model
    for candidate in candidates:
        if candidate in available:
            if candidate != requested_model.lower():
                log.info(
                    "[ANTIGRAVITY IMAGE] dynamic model rewrite: "
                    f"credential={credential_name}, {requested_model} -> {candidate}"
                )
            return candidate
    return requested_model


def is_image_model_capacity_exhausted(status_code: int, error_text: str) -> bool:
    """Identify model-global capacity exhaustion without blaming one account."""
    return status_code == 503 and IMAGE_MODEL_CAPACITY_MARKER in (error_text or "")


def _image_capacity_retry_delay_seconds(response: Any) -> float:
    """Read google.rpc.RetryInfo while bounding one request's wait time."""
    payload = _safe_response_json(response) or {}
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            detail_type = str(detail.get("@type") or "")
            if not detail_type.endswith("google.rpc.RetryInfo"):
                continue
            raw_delay = detail.get("retryDelay")
            if isinstance(raw_delay, (int, float)):
                delay = float(raw_delay)
            elif isinstance(raw_delay, str) and raw_delay.endswith("s"):
                try:
                    delay = float(raw_delay[:-1])
                except ValueError:
                    continue
            else:
                continue
            return min(
                max(delay, 0.0),
                IMAGE_CAPACITY_MAX_RETRY_DELAY_SECONDS,
            )
    return IMAGE_CAPACITY_DEFAULT_RETRY_DELAY_SECONDS


async def _record_image_account_outcome(
    credential_name: str,
    *,
    success: bool = False,
    capacity_failure: bool = False,
    latency_seconds: Optional[float] = None,
) -> None:
    """Support both the async singleton proxy and direct test/manager instances."""
    result = credential_manager.record_image_account_outcome(
        credential_name,
        success=success,
        capacity_failure=capacity_failure,
        latency_seconds=latency_seconds,
    )
    if inspect.isawaitable(result):
        await result


def get_effective_proxy_url(credential_data: Dict[str, Any]):
    """Resolve inherit/custom/direct without exposing proxy credentials in logs."""
    mode = credential_data.get("proxy_mode", "inherit") or "inherit"
    if mode == "direct":
        return None
    if mode in {"custom", "group"}:
        return credential_data.get("proxy_url") or None
    return ...


def _is_retryable_status(status_code: int, disable_error_codes: List[int]) -> bool:
    """统一判断是否属于可重试状态码。"""
    return status_code in (429, 503) or status_code in disable_error_codes


def _safe_response_json(response: Any) -> Optional[Dict[str, Any]]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _upstream_403_to_503(response: Any) -> Any:
    """上游 403 返回给客户端时改写为 503（响应体保留）。

    Claude Code 等客户端收到 403 会触发重新登录流程；上游 Antigravity 的
    403 多为凭证级问题（额度/地区/验证），对客户端而言是临时性服务不可用，
    因此只在最终出口处改写状态码，不触碰响应体。
    """
    if not isinstance(response, Response) or response.status_code != 403:
        return response
    return Response(
        content=response.body,
        status_code=503,
        headers=dict(response.headers),
        media_type=response.media_type,
    )


# 模型冷却默认秒数；VALIDATION_REQUIRED 是账号级临时验证，冷却更长
MODEL_FORBIDDEN_COOLDOWN_SECONDS = 300
VALIDATION_REQUIRED_COOLDOWN_SECONDS = 600


async def _handle_antigravity_403(
    *,
    credential_name: str,
    credential_data: Dict[str, Any],
    model_name: str,
    error_text: str,
) -> Antigravity403HandlingResult:
    """Persist a classified 403 and apply the disposal action for one account.

    只有分类器确认是账号级权限拒绝，并且自动封禁开关允许 403 时，才禁用
    整个账号。地区、模型、验证和未知 403 都保守处置，避免一次端点故障把
    所有仍可发送消息的账号误标成不可用。
    """
    health = await credential_manager.get_antigravity_account_health(
        credential_name
    )
    decision = classify_antigravity_403(
        error_text, account_health=health
    )
    timestamp = time.time()
    updates: Dict[str, Any] = {
        "last_403_category": decision.category,
        "last_403_reason": (error_text or "")[:1000],
        "last_403_at": timestamp,
    }
    if decision.category == "geo_blocked":
        updates["eligibility_status"] = "geo_blocked"
        updates["eligibility_reason"] = (error_text or "")[:1000]
        updates["eligibility_checked_at"] = timestamp
    elif decision.category == "validation_required":
        # 账号待验证：只记录，不标记为封禁
        updates["eligibility_status"] = "validation_required"
        updates["eligibility_reason"] = (error_text or "")[:1000]
        updates["eligibility_checked_at"] = timestamp
    elif decision.category == "proxy_drift":
        updates["binding_status"] = "proxy_drift"
    elif decision.category == "proxy_failed":
        updates["binding_status"] = "proxy_failed"
    elif decision.category == "account_forbidden":
        updates["eligibility_status"] = "account_blocked"
        updates["eligibility_reason"] = (error_text or "")[:1000]
        updates["eligibility_checked_at"] = timestamp
    await credential_manager.update_antigravity_account_health(
        credential_name, **updates
    )

    # VALIDATION_REQUIRED 是临时性账号验证：只冷却模型并轮换凭证，
    # 即使自动封禁开关打开也不禁用账号（验证完成后账号仍可恢复）。
    if decision.category == "validation_required":
        return Antigravity403HandlingResult(
            decision,
            should_retry=True,
            cooldown_until=timestamp + VALIDATION_REQUIRED_COOLDOWN_SECONDS,
        )

    if decision.disable_account:
        if await check_should_auto_ban(403):
            await credential_manager.set_cred_disabled(
                credential_name, True, mode="antigravity"
            )
            return Antigravity403HandlingResult(decision, should_retry=True)
        return Antigravity403HandlingResult(decision, should_retry=False)

    if decision.cooldown_model:
        return Antigravity403HandlingResult(
            decision,
            should_retry=True,
            cooldown_until=timestamp + MODEL_FORBIDDEN_COOLDOWN_SECONDS,
        )

    if decision.retry_after_rebind and await get_antigravity_network_check_enabled():
        rebound = await credential_manager.rebind_antigravity_account(
            credential_name, credential_data
        )
        return Antigravity403HandlingResult(
            decision, should_retry=bool(rebound)
        )

    return Antigravity403HandlingResult(decision, should_retry=False)


async def _alert_all_accounts_unavailable_if_needed(
    model_name: str,
    attempted_credentials: Optional[set[str]] = None,
) -> bool:
    """Send DingTalk alert without allowing notification failures to affect API responses."""
    try:
        credential_states = await credential_manager.get_creds_status(mode="antigravity")
        if not credential_states:
            log.warning("[DINGTALK] skipped all-account alert: credential snapshot is empty")
            return False
        if not all_accounts_are_unavailable(
            credential_states,
            model_name,
            attempted_credentials or set(),
        ):
            return False
        return await send_all_accounts_unavailable_alert(
            model_name=model_name,
            credential_states=credential_states,
        )
    except Exception as exc:
        log.warning(
            f"[DINGTALK] failed to evaluate all-account alert: {type(exc).__name__}"
        )
        return False


async def _switch_credential_for_retry(
    *,
    next_cred_task: Optional[asyncio.Task],
    retry_interval: float,
    refresh_credential_fast: Callable[[], Any],
    apply_cred_result: Callable[[Tuple[str, Dict[str, Any]]], bool],
    log_prefix: str,
) -> Tuple[bool, Optional[asyncio.Task]]:
    """优先使用预热凭证，失败后退回同步刷新。"""
    if next_cred_task is not None:
        try:
            cred_result = await next_cred_task
            next_cred_task = None
            if cred_result and apply_cred_result(cred_result):
                await asyncio.sleep(retry_interval)
                return True, next_cred_task
        except Exception as e:
            log.warning(f"{log_prefix} 预热凭证任务失败: {e}")
            next_cred_task = None

    await asyncio.sleep(retry_interval)
    if await refresh_credential_fast():
        return True, next_cred_task

    return False, next_cred_task


def _is_quota_exhausted_429(status_code: int, error_body: Optional[str]) -> bool:
    """判断是否为「配额耗尽」型 429（区别于瞬时限流）。

    配额耗尽特征：ErrorInfo reason=QUOTA_EXHAUSTED、携带 quotaResetTimeStamp/
    quotaResetDelay、RESOURCE_EXHAUSTED 配额消息，或 credits 余额不足标记。
    瞬时限流（无这些特征的 429）不算，仍走短冷却换号重试。
    """
    if status_code != 429 or not error_body:
        return False
    return any(
        marker in error_body
        for marker in (
            "QUOTA_EXHAUSTED",
            "quotaResetTimeStamp",
            "quotaResetDelay",
            "Resource has been exhausted",
            CREDITS_EXHAUSTED_MARKER,
        )
    )


def _next_fallback_model(chain: List[str], current_model: str) -> Optional[str]:
    """当前模型在降级链中时返回链上下一个模型，否则返回 None。"""
    if not chain:
        return None
    try:
        index = chain.index(current_model)
    except ValueError:
        return None
    if index + 1 < len(chain):
        return chain[index + 1]
    return None


async def _try_model_fallback(
    *,
    model_name: str,
    inner_request: Dict[str, Any],
    credential_data: Dict[str, Any],
    quota_exhausted: bool,
    image_request: bool,
    log_prefix: str,
) -> Optional[Tuple[str, Dict[str, Any], str]]:
    """模型配额耗尽且号池无可用凭证时，按配置的降级链切换到下一个模型。

    返回 (新模型名, 新payload, 新request_id)；不降级时返回 None。
    """
    if image_request or not quota_exhausted:
        return None
    chain = await get_antigravity_model_fallback_chain()
    next_model = _next_fallback_model(chain, model_name)
    if not next_model:
        return None
    new_payload, new_request_id = await wrap_cli_request(
        inner_request, next_model, credential_data.get("project_id", ""),
        user_email=credential_data.get("user_email"),
    )
    log.warning(
        f"{log_prefix} 模型 {model_name} 配额耗尽且号池无可用凭证，降级到 {next_model}"
    )
    return next_model, new_payload, new_request_id


# ==================== 新的流式和非流式请求函数 ====================

async def _stream_request_inner(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    api_key_id: str = "env",
    lease_tracker=None,
):
    """
    流式请求函数

    Args:
        body: 请求体
        native: 是否返回原生bytes流，False则返回str流
        headers: 额外的请求头

    Yields:
        Response对象（错误时）或 bytes流/str流（成功时）
    """
    model_name = body.get("model", "")
    inner_request = body.get("request", body)
    image_request = is_antigravity_image_request(model_name, inner_request)
    gateway_started = time.monotonic()  # 网关处理起点（时效性统计口径 A）

    # 1. 获取有效凭证
    cred_result = await credential_manager.get_valid_credential(
        mode="antigravity", model_name=model_name, image_request=image_request
    )

    # 当前模型号池无可用凭证时，按配置的降级链尝试后续模型（图片请求不降级）
    if not cred_result and not image_request:
        chain = await get_antigravity_model_fallback_chain()
        next_model = _next_fallback_model(chain, model_name)
        while next_model and not cred_result:
            log.warning(
                f"[ANTIGRAVITY STREAM] 模型 {model_name} 号池无可用凭证，降级到 {next_model}"
            )
            model_name = next_model
            cred_result = await credential_manager.get_valid_credential(
                mode="antigravity", model_name=model_name
            )
            next_model = _next_fallback_model(chain, model_name)

    if not cred_result:
        # 如果返回值是None，直接返回错误500
        log.error("[ANTIGRAVITY STREAM] 当前无可用凭证")
        await _alert_all_accounts_unavailable_if_needed(model_name)
        yield Response(
            content=json.dumps({"error": "当前无可用凭证"}),
            status_code=500,
            media_type="application/json"
        )
        return

    current_file, credential_data = cred_result
    if lease_tracker is not None:
        await lease_tracker.activate(
            current_file, credential_data.pop("_image_lease_id", None)
        )
    access_token = credential_data.get("access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id", "")
    proxy_url = get_effective_proxy_url(credential_data)

    if not access_token:
        log.error(f"[ANTIGRAVITY STREAM] No access token in credential: {current_file}")
        await _alert_all_accounts_unavailable_if_needed(
            model_name, {current_file}
        )
        yield Response(
            content=json.dumps({"error": "凭证中没有访问令牌"}),
            status_code=500,
            media_type="application/json"
        )
        return

    # 2. 构建URL和请求头
    antigravity_url = (await get_antigravity_api_url()).rstrip("/")
    target_url = f"{antigravity_url}/v1internal:streamGenerateContent?alt=sse"

    auth_headers = build_antigravity_headers(access_token)

    # 合并自定义headers
    if headers:
        auth_headers.update(headers)

    # 构建 CLI 格式请求体
    final_payload, request_id = await wrap_cli_request(
        inner_request, model_name, project_id,
        user_email=credential_data.get("user_email"),
    )

    # 3. 调用stream_post_async进行请求
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_response = None  # 记录最后一次的错误响应
    last_error_quota_exhausted = False  # 最后一次错误是否为配额耗尽型 429
    next_cred_task = None  # 预热的下一个凭证任务
    attempted_credentials: set[str] = set()
    usage_metadata = None
    usage_remainder = ""
    billing_recorded = False

    def consume_usage_chunk(chunk: Any) -> None:
        """Parse only complete SSE lines; retain at most one small partial line."""
        nonlocal usage_metadata, usage_remainder
        if isinstance(chunk, bytes):
            text = chunk.decode("utf-8", errors="ignore")
        elif isinstance(chunk, str):
            text = chunk
        else:
            return
        if native:
            text = usage_remainder + text
            lines = text.split("\n")
            usage_remainder = lines.pop()[-1_048_576:]
        else:
            lines = [text]
        for line in lines:
            raw = line[5:].strip() if line.startswith("data:") else ""
            if not raw or raw == "[DONE]":
                continue
            try:
                usage_metadata = extract_usage_metadata(json.loads(raw)) or usage_metadata
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

    async def record_billing_once(success: bool):
        nonlocal billing_recorded
        if billing_recorded:
            return
        billing_recorded = True
        try:
            recorder = await get_billing_recorder()
            await recorder.record(
                request_id=request_id, credential_name=current_file, model=model_name,
                usage_metadata=usage_metadata, success=success, api_key_id=api_key_id,
            )
        except Exception as exc:
            log.warning(f"[BILLING] failed to record stream request: {type(exc).__name__}")

    # 内部函数：快速更新凭证(只更新token和project_id,避免重建整个请求)
    async def refresh_credential_fast():
        nonlocal current_file, credential_data, access_token, auth_headers, project_id, final_payload, proxy_url
        if lease_tracker is not None:
            await lease_tracker.release()
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity", model_name=model_name, image_request=image_request
        )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        if lease_tracker is not None:
            await lease_tracker.activate(
                current_file, credential_data.pop("_image_lease_id", None)
            )
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        proxy_url = get_effective_proxy_url(credential_data)
        if not access_token:
            return None
        # 只更新token和project_id,不重建整个headers和payload
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    def apply_cred_result(cred_result: Tuple[str, Dict[str, Any]]) -> bool:
        nonlocal current_file, credential_data, access_token, project_id, auth_headers, final_payload, proxy_url
        current_file, credential_data = cred_result
        if lease_tracker is not None:
            lease_tracker.activate_reserved(
                current_file, credential_data.pop("_image_lease_id", None)
            )
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        proxy_url = get_effective_proxy_url(credential_data)
        if not access_token or not project_id:
            return False
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    attempt = 0
    while attempt <= max_retries:
        success_recorded = False  # 标记是否已记录成功
        need_retry = False  # 标记是否需要重试
        model_fell_back = False  # 标记本轮重试是否由模型降级触发（跳过换号）
        attempted_credentials.add(current_file)
        attempt_started = time.monotonic()  # 本次上游尝试起点（时效性统计口径 B）

        stream = stream_post_async(
            url=target_url,
            body=final_payload,
            native=native,
            headers=auth_headers,
            proxy_url=proxy_url,
        )
        try:
            async for chunk in stream:
                # 判断是否是Response对象
                if isinstance(chunk, Response):
                    status_code = chunk.status_code
                    last_error_response = chunk  # 记录最后一次错误

                    # 缓存错误解析结果,避免重复decode
                    error_body = None
                    try:
                        error_body = chunk.body.decode('utf-8') if isinstance(chunk.body, bytes) else str(chunk.body)
                    except Exception:
                        error_body = ""

                    # 403 必须先分类；429/503/配置错误码继续走通用重试。
                    if status_code == 403 or _is_retryable_status(status_code, DISABLE_ERROR_CODES):
                        log.warning(f"[ANTIGRAVITY STREAM] 流式请求失败 (status={status_code}), 凭证: {current_file}, 响应: {error_body[:500] if error_body else '无'}")
                        last_error_quota_exhausted = _is_quota_exhausted_429(status_code, error_body)

                        # 解析冷却时间（明确重置时间优先，其余 429 只短冷却）
                        cooldown_until = None
                        if status_code == 429 or status_code == 503:
                            cooldown_until = await _resolve_error_cooldown(status_code, error_body)

                        classified_403 = None
                        if status_code == 403:
                            classified_403 = await _handle_antigravity_403(
                                credential_name=current_file,
                                credential_data=credential_data,
                                model_name=model_name,
                                error_text=error_body or "",
                            )
                            cooldown_until = classified_403.cooldown_until

                        # 记录错误并切换凭证
                        # 网关耗时只在最终结果时记一次：重试耗尽/重试关闭/403 判定不再重试
                        stream_final_failure = (
                            attempt >= max_retries
                            or not retry_config["retry_enabled"]
                            or (classified_403 is not None and not classified_403.should_retry)
                        )
                        await record_api_call_error(
                            credential_manager, current_file, status_code,
                            cooldown_until, mode="antigravity", model_name=model_name,
                            error_message=error_body,
                            upstream_seconds=time.monotonic() - attempt_started,
                            gateway_seconds=(time.monotonic() - gateway_started) if stream_final_failure else None
                        )

                        if classified_403 is not None:
                            should_retry = (
                                retry_config["retry_enabled"]
                                and classified_403.should_retry
                                and attempt < max_retries
                            )
                        else:
                            should_retry = await handle_error_with_retry(
                                credential_manager, status_code, current_file,
                                retry_config["retry_enabled"], attempt, max_retries, retry_interval,
                                mode="antigravity"
                            )

                        if should_retry and attempt < max_retries and lease_tracker is not None:
                            await lease_tracker.release()

                        if (
                            should_retry
                            and next_cred_task is None
                            and attempt < max_retries
                            and not image_request
                        ):
                            next_cred_task = asyncio.create_task(
                                credential_manager.get_valid_credential(
                                    mode="antigravity", model_name=model_name
                                )
                            )

                        if should_retry and attempt < max_retries:
                            need_retry = True
                            break  # 跳出内层循环，准备重试
                        else:
                            # 配额耗尽型 429：先沿降级链切换模型，链路全部耗尽才算整体 429
                            if last_error_quota_exhausted:
                                fallback = await _try_model_fallback(
                                    model_name=model_name,
                                    inner_request=inner_request,
                                    credential_data=credential_data,
                                    quota_exhausted=True,
                                    image_request=image_request,
                                    log_prefix="[ANTIGRAVITY STREAM]",
                                )
                                if fallback is not None:
                                    model_name, final_payload, request_id = fallback
                                    attempted_credentials.clear()
                                    need_retry = True
                                    model_fell_back = True
                                    break  # 跳出内层循环，用降级模型重试
                            # 不重试，直接返回原始错误（上游 403 改写为 503）
                            log.error(f"[ANTIGRAVITY STREAM] 达到最大重试次数或不应重试，返回原始错误")
                            await record_billing_once(False)
                            await _alert_all_accounts_unavailable_if_needed(
                                model_name, attempted_credentials
                            )
                            yield _upstream_403_to_503(chunk)
                            return
                    else:
                        # 错误码不在禁用码当中，直接返回，无需重试
                        log.error(f"[ANTIGRAVITY STREAM] 流式请求失败，非重试错误码 (status={status_code}), 凭证: {current_file}, 响应: {error_body[:500] if error_body else '无'}")
                        await record_api_call_error(
                            credential_manager, current_file, status_code,
                            None, mode="antigravity", model_name=model_name,
                            error_message=error_body,
                            upstream_seconds=time.monotonic() - attempt_started,
                            gateway_seconds=time.monotonic() - gateway_started
                        )
                        await record_billing_once(False)
                        await _alert_all_accounts_unavailable_if_needed(
                            model_name, attempted_credentials
                        )
                        yield chunk
                        return
                else:
                    # 不是Response，说明是真流，直接yield返回
                    consume_usage_chunk(chunk)
                    # 只在第一个chunk时记录成功
                    if not success_recorded:
                        await record_api_call_success(
                            credential_manager, current_file, mode="antigravity", model_name=model_name,
                            upstream_seconds=time.monotonic() - attempt_started,
                            gateway_seconds=time.monotonic() - gateway_started
                        )
                        success_recorded = True
                        log.debug(f"[ANTIGRAVITY STREAM] 开始接收流式响应，模型: {model_name}")

                    yield chunk

            # 流式请求完成，检查结果
            if success_recorded:
                if native and usage_remainder:
                    consume_usage_chunk(usage_remainder)
                log.debug(f"[ANTIGRAVITY STREAM] 流式响应完成，模型: {model_name}")
                await record_billing_once(True)
                return
            elif not need_retry:
                # 没有收到任何数据（空回复），需要重试
                log.warning(f"[ANTIGRAVITY STREAM] 收到空回复，无任何内容，凭证: {current_file}")
                last_error_quota_exhausted = False
                await record_api_call_error(
                    credential_manager, current_file, 200,
                    None, mode="antigravity", model_name=model_name,
                    error_message="Empty response from API",
                    upstream_seconds=time.monotonic() - attempt_started,
                    gateway_seconds=(time.monotonic() - gateway_started)
                        if (attempt >= max_retries or not retry_config["retry_enabled"]) else None
                )
                if attempt < max_retries:
                    need_retry = True
                else:
                    log.error(f"[ANTIGRAVITY STREAM] 空回复达到最大重试次数")
                    await record_billing_once(False)
                    yield Response(
                        content=json.dumps({"error": "服务返回空回复"}),
                        status_code=500,
                        media_type="application/json"
                    )
                    return
            
            # 统一处理重试
            if need_retry:
                log.info(f"[ANTIGRAVITY STREAM] 重试请求 (attempt {attempt + 2}/{max_retries + 1})...")

                if lease_tracker is not None:
                    await lease_tracker.release()

                if model_fell_back:
                    # 模型已降级，沿用当前凭证直接重试，重试预算重置
                    attempt = 0
                    continue

                switched, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=retry_interval,
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY STREAM]",
                )
                if not switched:
                    # 配额耗尽且号池无可用凭证时，按降级链切换模型后重试
                    fallback = await _try_model_fallback(
                        model_name=model_name,
                        inner_request=inner_request,
                        credential_data=credential_data,
                        quota_exhausted=last_error_quota_exhausted,
                        image_request=image_request,
                        log_prefix="[ANTIGRAVITY STREAM]",
                    )
                    if fallback is not None:
                        model_name, final_payload, request_id = fallback
                        attempted_credentials.clear()
                        attempt = 0
                        continue
                    log.error("[ANTIGRAVITY STREAM] 重试时无可用凭证或令牌")
                    await record_billing_once(False)
                    await _alert_all_accounts_unavailable_if_needed(
                        model_name, attempted_credentials
                    )
                    yield Response(
                        content=json.dumps({"error": "当前无可用凭证"}),
                        status_code=500,
                        media_type="application/json"
                    )
                    return
                attempt += 1
                continue  # 重试

        except Exception as e:
            log.error(f"[ANTIGRAVITY STREAM] 流式请求异常: {e}, 凭证: {current_file}")
            if attempt < max_retries:
                log.info(f"[ANTIGRAVITY STREAM] 异常后重试 (attempt {attempt + 2}/{max_retries + 1})...")
                await asyncio.sleep(retry_interval)
                attempt += 1
                continue
            else:
                # 所有重试都失败，返回最后一次的错误（如果有）
                log.error(f"[ANTIGRAVITY STREAM] 所有重试均失败，最后异常: {e}")
                if last_error_response:
                    await record_billing_once(False)
                    await _alert_all_accounts_unavailable_if_needed(
                        model_name, attempted_credentials
                    )
                    yield _upstream_403_to_503(last_error_response)
                else:
                    # 如果没有记录到错误响应，返回500错误
                    yield Response(
                        content=json.dumps({"error": f"流式请求异常: {str(e)}"}),
                        status_code=500,
                        media_type="application/json"
                    )
                    await record_billing_once(False)
                return
        finally:
            # 无论流被完整消费，还是因重试/返回/异常被中途放弃，都显式关闭上游流
            await aclose_quietly(stream, "[ANTIGRAVITY STREAM]")

    # 所有重试均已耗尽（while 循环正常结束），返回最后记录的错误
    log.error("[ANTIGRAVITY STREAM] 所有重试均失败")
    if last_error_response:
        await record_billing_once(False)
        await _alert_all_accounts_unavailable_if_needed(
            model_name, attempted_credentials
        )
        yield _upstream_403_to_503(last_error_response)
    else:
        await record_billing_once(False)
        yield Response(
            content=json.dumps({"error": "请求失败，所有重试均已耗尽"}),
            status_code=429,
            media_type="application/json"
        )


class _ImageCredentialLeaseTracker:
    """Keep one request's image-account lease release idempotent and cancellation-safe."""

    def __init__(self) -> None:
        self._credential_name: Optional[str] = None
        self._lease_id: Optional[str] = None

    async def activate(
        self, credential_name: str, lease_id: Optional[str]
    ) -> None:
        await self.release()
        self.activate_reserved(credential_name, lease_id)

    def activate_reserved(
        self, credential_name: str, lease_id: Optional[str]
    ) -> None:
        if lease_id:
            if self._lease_id is not None:
                raise RuntimeError("image credential lease was replaced before release")
            self._credential_name = credential_name
            self._lease_id = lease_id

    async def release(self) -> None:
        credential_name = self._credential_name
        lease_id = self._lease_id
        self._credential_name = None
        self._lease_id = None
        if credential_name and lease_id:
            await credential_manager.release_image_credential(
                credential_name, lease_id
            )


async def stream_request(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    api_key_id: str = "env",
):
    lease_tracker = _ImageCredentialLeaseTracker()
    try:
        async for item in _stream_request_inner(
            body=body,
            native=native,
            headers=headers,
            api_key_id=api_key_id,
            lease_tracker=lease_tracker,
        ):
            yield item
    finally:
        # Releasing the consumer closes image leases on disconnect/cancellation.
        await lease_tracker.release()


async def _non_stream_image_request_inner(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]],
    api_key_id: str,
    lease_tracker: _ImageCredentialLeaseTracker,
) -> Response:
    """Image-specific account scheduling and dynamic model resolution."""
    requested_model = body.get("model", "")
    inner_request = body.get("request", body)
    retry_config = await get_retry_config()
    retry_enabled = bool(retry_config.get("retry_enabled", True))
    configured_attempts = int(retry_config.get("max_retries", 2)) + 1
    max_account_attempts = min(max(configured_attempts, 1), 3) if retry_enabled else 1
    disable_error_codes = await get_auto_ban_error_codes()
    attempted_credentials: Set[str] = set()
    last_response: Optional[Response] = None
    last_was_capacity = False
    last_request_id = f"image-{uuid.uuid4()}"
    billing_recorded = False
    gateway_started = time.monotonic()  # 网关处理起点（时效性统计口径 A）

    async def record_billing_once(
        success: bool, credential_name: str, response_payload: Any = None
    ) -> None:
        nonlocal billing_recorded
        if billing_recorded:
            return
        billing_recorded = True
        try:
            recorder = await get_billing_recorder()
            await recorder.record(
                request_id=last_request_id,
                credential_name=credential_name,
                model=requested_model,
                usage_metadata=extract_usage_metadata(response_payload),
                success=success,
                api_key_id=api_key_id,
            )
        except Exception as exc:
            log.warning(
                f"[BILLING] failed to record image request: {type(exc).__name__}"
            )

    for account_attempt in range(max_account_attempts):
        await lease_tracker.release()
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity",
            model_name=requested_model,
            exclude_filenames=attempted_credentials,
            image_request=True,
        )
        if not cred_result:
            break

        current_file, credential_data = cred_result
        await lease_tracker.activate(
            current_file, credential_data.pop("_image_lease_id", None)
        )
        attempted_credentials.add(current_file)
        access_token = credential_data.get("access_token") or credential_data.get(
            "token"
        )
        project_id = credential_data.get("project_id", "")
        if not access_token or not project_id:
            await lease_tracker.release()
            await record_api_call_error(
                credential_manager,
                current_file,
                500,
                None,
                mode="antigravity",
                model_name=requested_model,
                error_message="Image credential missing access token or project_id",
            )
            continue

        resolved_model = await resolve_dynamic_image_model_for_credential(
            credential_name=current_file,
            credential_data=credential_data,
            requested_model=requested_model,
        )
        final_payload, last_request_id = await wrap_cli_request(
            inner_request,
            resolved_model,
            project_id,
            user_email=credential_data.get("user_email"),
        )
        auth_headers = build_antigravity_headers(
            access_token,
            image_request=True,
            project_id=project_id,
        )
        if headers:
            auth_headers.update(headers)

        started = time.monotonic()
        try:
            call_result = await _post_image_generate(
                headers=auth_headers,
                json_body=final_payload,
                proxy_url=get_effective_proxy_url(credential_data),
                timeout=300.0,
                request_id=last_request_id,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            await _record_image_account_outcome(
                current_file, success=False, latency_seconds=elapsed
            )
            await lease_tracker.release()
            log.warning(
                "[ANTIGRAVITY IMAGE] account transport failed: "
                f"credential={current_file}, tier={credential_data.get('tier')}, "
                f"elapsed={elapsed:.3f}s, error={type(exc).__name__}"
            )
            continue

        response = call_result.response
        elapsed = time.monotonic() - started
        status_code = response.status_code
        response_payload = _safe_response_json(response) if response.content else None
        error_text = ""
        if status_code != 200:
            try:
                error_text = response.text
            except Exception:
                error_text = ""
        log.info(
            "[ANTIGRAVITY IMAGE] upstream result: "
            f"request_id={last_request_id}, credential={current_file}, "
            f"tier={credential_data.get('tier')}, "
            f"model={resolved_model}, host={call_result.host}, status={status_code}, "
            f"upstream_calls={len(call_result.attempts)}, elapsed={elapsed:.3f}s"
        )

        if status_code == 200:
            await _record_image_account_outcome(
                current_file, success=True, latency_seconds=elapsed
            )
            await lease_tracker.release()
            await record_api_call_success(
                credential_manager,
                current_file,
                mode="antigravity",
                model_name=requested_model,
                upstream_seconds=elapsed,
                gateway_seconds=time.monotonic() - gateway_started,
            )
            await record_billing_once(True, current_file, response_payload)
            return Response(
                content=response.content,
                status_code=200,
                headers=dict(response.headers),
            )

        last_response = Response(
            content=response.content,
            status_code=status_code,
            headers=dict(response.headers),
        )
        last_was_capacity = is_image_model_capacity_exhausted(
            status_code, error_text
        )
        if last_was_capacity:
            await _record_image_account_outcome(
                current_file,
                success=False,
                capacity_failure=True,
                latency_seconds=elapsed,
            )
            await lease_tracker.release()
            continue

        await _record_image_account_outcome(
            current_file, success=False, latency_seconds=elapsed
        )
        await lease_tracker.release()
        cooldown_until = None
        classified_403 = None
        if status_code in (429, 503):
            cooldown_until = await _resolve_error_cooldown(status_code, error_text)
        if status_code == 403:
            classified_403 = await _handle_antigravity_403(
                credential_name=current_file,
                credential_data=credential_data,
                model_name=requested_model,
                error_text=error_text,
            )
            cooldown_until = classified_403.cooldown_until
        retryable = status_code in (403, 404, 429, 500, 503) or status_code in disable_error_codes
        if classified_403 is not None:
            retryable = classified_403.should_retry
        will_retry = retry_enabled and retryable and account_attempt < max_account_attempts - 1
        await record_api_call_error(
            credential_manager,
            current_file,
            status_code,
            cooldown_until,
            mode="antigravity",
            model_name=requested_model,
            error_message=error_text,
            upstream_seconds=elapsed,
            gateway_seconds=None if will_retry else time.monotonic() - gateway_started,
        )

        if not retry_enabled or not retryable:
            break

    billing_credential = next(iter(attempted_credentials), "unknown")
    await record_billing_once(False, billing_credential)
    if last_response is not None:
        if not last_was_capacity:
            await _alert_all_accounts_unavailable_if_needed(
                requested_model, attempted_credentials
            )
        return _upstream_403_to_503(last_response)

    await _alert_all_accounts_unavailable_if_needed(
        requested_model, attempted_credentials or None
    )
    return Response(
        content=json.dumps({"error": "当前无可用图片凭证"}),
        status_code=500,
        media_type="application/json",
    )


async def _non_stream_image_request(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]],
    api_key_id: str,
) -> Response:
    lease_tracker = _ImageCredentialLeaseTracker()
    try:
        return await _non_stream_image_request_inner(
            body, headers, api_key_id, lease_tracker
        )
    finally:
        # Covers success, errors, timeouts, task cancellation and client disconnects.
        await lease_tracker.release()


async def non_stream_request(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    api_key_id: str = "env",
) -> Response:
    """
    非流式请求函数

    Args:
        body: 请求体
        headers: 额外的请求头

    Returns:
        Response对象
    """
    model_name = body.get("model", "")
    inner_request = body.get("request", body)
    if is_antigravity_image_request(model_name, inner_request):
        return await _non_stream_image_request(body, headers, api_key_id)

    # 检查是否启用流式收集模式
    if await get_antigravity_stream2nostream():
        log.debug("[ANTIGRAVITY] 使用流式收集模式实现非流式请求")

        # 调用stream_request获取流
        stream = stream_request(
            body=body, native=False, headers=headers, api_key_id=api_key_id
        )

        # 收集流式响应
        # stream_request是一个异步生成器，可能yield Response（错误）或流数据
        # collect_streaming_response会自动处理这两种情况
        return await collect_streaming_response(stream)

    # 否则使用传统非流式模式
    log.debug("[ANTIGRAVITY] 使用传统非流式模式")
    gateway_started = time.monotonic()  # 网关处理起点（时效性统计口径 A）

    # 1. 获取有效凭证
    cred_result = await credential_manager.get_valid_credential(
        mode="antigravity", model_name=model_name
    )

    # 当前模型号池无可用凭证时，按配置的降级链尝试后续模型
    if not cred_result:
        chain = await get_antigravity_model_fallback_chain()
        next_model = _next_fallback_model(chain, model_name)
        while next_model and not cred_result:
            log.warning(
                f"[ANTIGRAVITY] 模型 {model_name} 号池无可用凭证，降级到 {next_model}"
            )
            model_name = next_model
            cred_result = await credential_manager.get_valid_credential(
                mode="antigravity", model_name=model_name
            )
            next_model = _next_fallback_model(chain, model_name)

    if not cred_result:
        # 如果返回值是None，直接返回错误500
        log.error("[ANTIGRAVITY] 当前无可用凭证")
        await _alert_all_accounts_unavailable_if_needed(model_name)
        return Response(
            content=json.dumps({"error": "当前无可用凭证"}),
            status_code=500,
            media_type="application/json"
        )

    current_file, credential_data = cred_result
    access_token = credential_data.get("access_token") or credential_data.get("token")
    proxy_url = get_effective_proxy_url(credential_data)
    project_id = credential_data.get("project_id", "")

    if not access_token:
        log.error(f"[ANTIGRAVITY] No access token in credential: {current_file}")
        await _alert_all_accounts_unavailable_if_needed(
            model_name, {current_file}
        )
        return Response(
            content=json.dumps({"error": "凭证中没有访问令牌"}),
            status_code=500,
            media_type="application/json"
        )

    # 2. 构建URL和请求头
    antigravity_url = await get_antigravity_api_url()
    target_url = f"{antigravity_url}/v1internal:generateContent"

    auth_headers = build_antigravity_headers(access_token)

    # 合并自定义headers
    if headers:
        auth_headers.update(headers)

    # 构建 CLI 格式请求体
    inner_request = body.get("request", body)
    final_payload, request_id = await wrap_cli_request(
        inner_request, model_name, project_id,
        user_email=credential_data.get("user_email"),
    )

    # 3. 调用post_async进行请求
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_response = None  # 记录最后一次的错误响应
    last_error_quota_exhausted = False  # 最后一次错误是否为配额耗尽型 429
    next_cred_task = None  # 预热的下一个凭证任务
    attempted_credentials: set[str] = set()
    usage_metadata = None
    billing_recorded = False

    async def record_billing_once(success: bool, response_payload: Any = None):
        nonlocal billing_recorded, usage_metadata
        if billing_recorded:
            return
        billing_recorded = True
        if response_payload is not None:
            usage_metadata = extract_usage_metadata(response_payload) or usage_metadata
        try:
            recorder = await get_billing_recorder()
            await recorder.record(
                request_id=request_id,
                credential_name=current_file,
                model=model_name,
                usage_metadata=usage_metadata,
                success=success,
                api_key_id=api_key_id,
            )
        except Exception as exc:
            log.warning(f"[BILLING] failed to record non-stream request: {type(exc).__name__}")

    # 内部函数：快速更新凭证(只更新token和project_id,避免重建整个请求)
    async def refresh_credential_fast():
        nonlocal current_file, credential_data, access_token, auth_headers, project_id, final_payload, proxy_url
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity", model_name=model_name
        )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        proxy_url = get_effective_proxy_url(credential_data)
        if not access_token:
            return None
        # 只更新token和project_id,不重建整个headers和payload
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    def apply_cred_result(cred_result: Tuple[str, Dict[str, Any]]) -> bool:
        nonlocal current_file, credential_data, access_token, project_id, auth_headers, final_payload, proxy_url
        current_file, credential_data = cred_result
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        proxy_url = get_effective_proxy_url(credential_data)
        if not access_token or not project_id:
            return False
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    attempt = 0
    while attempt <= max_retries:
        need_retry = False  # 标记是否需要重试
        attempted_credentials.add(current_file)
        
        try:
            attempt_started = time.monotonic()  # 本次上游尝试起点（时效性统计口径 B）
            response = await post_async(
                url=target_url,
                json=final_payload,
                headers=auth_headers,
                proxy_url=proxy_url,
                timeout=300.0
            )
            upstream_elapsed = time.monotonic() - attempt_started

            status_code = response.status_code

            # 成功
            if status_code == 200:
                # 检查是否为空回复
                if not response.content or len(response.content) == 0:
                    log.warning(f"[ANTIGRAVITY] 收到200响应但内容为空，凭证: {current_file}")
                    last_error_quota_exhausted = False
                    
                    # 记录错误
                    await record_api_call_error(
                        credential_manager, current_file, 200,
                        None, mode="antigravity", model_name=model_name,
                        error_message="Empty response from API",
                        upstream_seconds=upstream_elapsed,
                        gateway_seconds=(time.monotonic() - gateway_started)
                            if (attempt >= max_retries or not retry_config["retry_enabled"]) else None
                    )
                    
                    if attempt < max_retries:
                        need_retry = True
                    else:
                        log.error(f"[ANTIGRAVITY] 空回复达到最大重试次数")
                        await record_billing_once(False)
                        return Response(
                            content=json.dumps({"error": "服务返回空回复"}),
                            status_code=500,
                            media_type="application/json"
                        )
                else:
                    # 正常响应
                    await record_api_call_success(
                        credential_manager, current_file, mode="antigravity", model_name=model_name,
                        upstream_seconds=upstream_elapsed,
                        gateway_seconds=time.monotonic() - gateway_started
                    )
                    await record_billing_once(True, _safe_response_json(response) if response.content else None)
                    return Response(
                        content=response.content,
                        status_code=200,
                        headers=dict(response.headers)
                    )

            # 失败 - 记录最后一次错误
            if status_code != 200:
                last_error_response = Response(
                    content=response.content,
                    status_code=status_code,
                    headers=dict(response.headers)
                )

                # 判断是否需要重试
                # 缓存错误文本,避免重复解析
                error_text = ""
                try:
                    error_text = response.text
                except Exception:
                    pass

                if status_code == 403 or _is_retryable_status(status_code, DISABLE_ERROR_CODES):
                    log.warning(f"[ANTIGRAVITY] 非流式请求失败 (status={status_code}), 凭证: {current_file}, 响应: {error_text[:500] if error_text else '无'}")
                    last_error_quota_exhausted = _is_quota_exhausted_429(status_code, error_text)

                    # 解析冷却时间（明确重置时间优先，其余 429 只短冷却）
                    cooldown_until = None
                    if status_code == 429 or status_code == 503:
                        cooldown_until = await _resolve_error_cooldown(status_code, error_text)

                    classified_403 = None
                    if status_code == 403:
                        classified_403 = await _handle_antigravity_403(
                            credential_name=current_file,
                            credential_data=credential_data,
                            model_name=model_name,
                            error_text=error_text,
                        )
                        cooldown_until = classified_403.cooldown_until

                    # 记录错误并切换凭证
                    # 网关耗时只在最终结果时记一次：重试耗尽/重试关闭/403 判定不再重试
                    ns_final_failure = (
                        attempt >= max_retries
                        or not retry_config["retry_enabled"]
                        or (classified_403 is not None and not classified_403.should_retry)
                    )
                    await record_api_call_error(
                        credential_manager, current_file, status_code,
                        cooldown_until, mode="antigravity", model_name=model_name,
                        error_message=error_text,
                        upstream_seconds=upstream_elapsed,
                        gateway_seconds=(time.monotonic() - gateway_started) if ns_final_failure else None
                    )

                    if classified_403 is not None:
                        should_retry = (
                            retry_config["retry_enabled"]
                            and classified_403.should_retry
                            and attempt < max_retries
                        )
                    else:
                        should_retry = await handle_error_with_retry(
                            credential_manager, status_code, current_file,
                            retry_config["retry_enabled"], attempt, max_retries, retry_interval,
                            mode="antigravity"
                        )

                    if should_retry and next_cred_task is None and attempt < max_retries:
                        next_cred_task = asyncio.create_task(
                            credential_manager.get_valid_credential(
                                mode="antigravity", model_name=model_name
                            )
                        )

                    if should_retry and attempt < max_retries:
                        need_retry = True
                    else:
                        # 配额耗尽型 429：先沿降级链切换模型，链路全部耗尽才算整体 429
                        if last_error_quota_exhausted:
                            fallback = await _try_model_fallback(
                                model_name=model_name,
                                inner_request=inner_request,
                                credential_data=credential_data,
                                quota_exhausted=True,
                                image_request=False,
                                log_prefix="[ANTIGRAVITY]",
                            )
                            if fallback is not None:
                                model_name, final_payload, request_id = fallback
                                attempted_credentials.clear()
                                attempt = 0
                                continue
                        # 不重试，直接返回原始错误（上游 403 改写为 503）
                        log.error(f"[ANTIGRAVITY] 达到最大重试次数或不应重试，返回原始错误")
                        await record_billing_once(False)
                        await _alert_all_accounts_unavailable_if_needed(
                            model_name, attempted_credentials
                        )
                        return _upstream_403_to_503(last_error_response)
                else:
                    # 错误码不在禁用码当中，直接返回，无需重试
                    log.error(f"[ANTIGRAVITY] 非流式请求失败，非重试错误码 (status={status_code}), 凭证: {current_file}, 响应: {error_text[:500] if error_text else '无'}")
                    await record_api_call_error(
                        credential_manager, current_file, status_code,
                        None, mode="antigravity", model_name=model_name,
                        error_message=error_text,
                        upstream_seconds=upstream_elapsed,
                        gateway_seconds=time.monotonic() - gateway_started
                    )
                    await record_billing_once(False, _safe_response_json(response) if response.content else None)
                    await _alert_all_accounts_unavailable_if_needed(
                        model_name, attempted_credentials
                    )
                    return last_error_response
            
            # 统一处理重试
            if need_retry:
                log.info(f"[ANTIGRAVITY] 重试请求 (attempt {attempt + 2}/{max_retries + 1})...")

                switched, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=retry_interval,
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY]",
                )
                if not switched:
                    # 配额耗尽且号池无可用凭证时，按降级链切换模型后重试
                    fallback = await _try_model_fallback(
                        model_name=model_name,
                        inner_request=inner_request,
                        credential_data=credential_data,
                        quota_exhausted=last_error_quota_exhausted,
                        image_request=False,
                        log_prefix="[ANTIGRAVITY]",
                    )
                    if fallback is not None:
                        model_name, final_payload, request_id = fallback
                        attempted_credentials.clear()
                        attempt = 0
                        continue
                    log.error("[ANTIGRAVITY] 重试时无可用凭证或令牌")
                    await record_billing_once(False)
                    await _alert_all_accounts_unavailable_if_needed(
                        model_name, attempted_credentials
                    )
                    return Response(
                        content=json.dumps({"error": "当前无可用凭证"}),
                        status_code=500,
                        media_type="application/json"
                    )
                attempt += 1
                continue  # 重试

        except Exception as e:
            log.error(f"[ANTIGRAVITY] 非流式请求异常: {e}, 凭证: {current_file}")
            if attempt < max_retries:
                log.info(f"[ANTIGRAVITY] 异常后重试 (attempt {attempt + 2}/{max_retries + 1})...")
                await asyncio.sleep(retry_interval)
                attempt += 1
                continue
            else:
                # 所有重试都失败，返回最后一次的错误（如果有）或500错误
                log.error(f"[ANTIGRAVITY] 所有重试均失败，最后异常: {e}")
                if last_error_response:
                    await record_billing_once(False)
                    await _alert_all_accounts_unavailable_if_needed(
                        model_name, attempted_credentials
                    )
                    return _upstream_403_to_503(last_error_response)
                else:
                    await record_billing_once(False)
                    return Response(
                        content=json.dumps({"error": f"非流式请求异常: {str(e)}"}),
                        status_code=500,
                        media_type="application/json"
                    )

    # 所有重试都失败，返回最后一次的原始错误（如果有）或500错误
    log.error("[ANTIGRAVITY] 所有重试均失败")
    if last_error_response:
        await record_billing_once(False)
        await _alert_all_accounts_unavailable_if_needed(
            model_name, attempted_credentials
        )
        return _upstream_403_to_503(last_error_response)
    else:
        await record_billing_once(False)
        return Response(
            content=json.dumps({"error": "所有重试均失败"}),
            status_code=500,
            media_type="application/json"
        )


# ==================== 模型和配额查询 ====================

# 轻量端点可降级的状态码：408/429/5xx 或连接异常时尝试下一个 host；非 429 的 4xx 不降级
_HOST_FALLBACK_STATUSES = (408, 429)


async def _post_with_host_fallback(
    path: str,
    headers: Dict[str, str],
    json_body: Optional[Dict[str, Any]] = None,
    proxy_url: Any = ...,
    timeout: Optional[float] = None,
):
    """
    轻量端点三 host 降级 POST（sandbox → daily → prod，仅默认配置时启用）。

    遇到 408/429/5xx 或连接异常时按顺序尝试下一个 host；非 429 的 4xx
    立即返回（不降级）。全部 host 连接异常时抛出最后一个异常；
    全部返回可降级状态码时返回最后一个响应，交给调用方按失败处理。
    """
    candidates = await get_antigravity_api_url_candidates()
    last_response = None
    last_exc: Optional[Exception] = None

    for base_url in candidates:
        url = f"{base_url}{path}"
        try:
            kwargs: Dict[str, Any] = {
                "url": url,
                "json": json_body if json_body is not None else {},
                "headers": headers,
                "proxy_url": proxy_url,
            }
            if timeout is not None:
                kwargs["timeout"] = timeout
            response = await post_async(**kwargs)
        except Exception as e:
            last_exc = e
            log.warning(
                f"[ANTIGRAVITY] {path} 请求 {base_url} 连接异常，尝试下一个 host: {type(e).__name__}: {e}"
            )
            continue

        if response.status_code in _HOST_FALLBACK_STATUSES or response.status_code >= 500:
            log.warning(
                f"[ANTIGRAVITY] {path} 请求 {base_url} 返回 {response.status_code}，尝试下一个 host"
            )
            last_response = response
            continue

        return response

    if last_response is not None:
        return last_response
    raise last_exc  # type: ignore[misc]


async def _post_image_generate(
    *,
    headers: Dict[str, str],
    json_body: Dict[str, Any],
    proxy_url: Any = ...,
    timeout: float = 300.0,
    request_id: Optional[str] = None,
) -> ImageUpstreamResult:
    """Run image generation on the configured AGY host with capacity backoff."""
    host = (await get_antigravity_api_url()).rstrip("/")
    attempts: List[ImageUpstreamAttempt] = []
    for capacity_attempt in range(IMAGE_CAPACITY_MAX_RETRIES + 1):
        started = time.monotonic()
        try:
            response = await post_async(
                url=f"{host}/v1internal:generateContent",
                json=json_body,
                headers=headers,
                proxy_url=proxy_url,
                timeout=timeout,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            attempts.append(
                ImageUpstreamAttempt(
                    host=host,
                    status_code=None,
                    elapsed_seconds=elapsed,
                    error=type(exc).__name__,
                )
            )
            log.warning(
                "[ANTIGRAVITY IMAGE] connection failed: "
                f"request_id={request_id or '-'}, host={host}, "
                f"elapsed={elapsed:.3f}s, error={type(exc).__name__}"
            )
            raise

        elapsed = time.monotonic() - started
        attempts.append(
            ImageUpstreamAttempt(
                host=host,
                status_code=response.status_code,
                elapsed_seconds=elapsed,
            )
        )
        error_text = response.text if response.status_code != 200 else ""
        if not is_image_model_capacity_exhausted(response.status_code, error_text):
            return ImageUpstreamResult(response, host, tuple(attempts))
        if capacity_attempt >= IMAGE_CAPACITY_MAX_RETRIES:
            return ImageUpstreamResult(response, host, tuple(attempts))

        retry_delay = _image_capacity_retry_delay_seconds(response)
        log.warning(
            "[ANTIGRAVITY IMAGE] model capacity exhausted; retrying same host: "
            f"request_id={request_id or '-'}, host={host}, "
            f"retry_in={retry_delay:.3f}s, attempt={capacity_attempt + 1}/"
            f"{IMAGE_CAPACITY_MAX_RETRIES}"
        )
        await asyncio.sleep(retry_delay)

    raise RuntimeError("unreachable image generation retry state")


async def fetch_available_models() -> List[Dict[str, Any]]:
    """
    获取可用模型列表，返回符合 OpenAI API 规范的格式
    
    Returns:
        模型列表，格式为字典列表（用于兼容现有代码）
        
    Raises:
        返回空列表如果获取失败
    """
    # 获取凭证管理器和可用凭证
    cred_result = await credential_manager.get_valid_credential(mode="antigravity")
    if not cred_result:
        log.error("[ANTIGRAVITY] No valid credentials available for fetching models")
        return []

    current_file, credential_data = cred_result
    access_token = credential_data.get("access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id")
    proxy_url = get_effective_proxy_url(credential_data)

    if not access_token:
        log.error(f"[ANTIGRAVITY] No access token in credential: {current_file}")
        return []

    # 构建请求头
    headers = build_antigravity_headers(access_token)

    try:
        # 使用 POST 请求获取模型列表（默认配置时按 sandbox → daily → prod 降级）
        response = await _post_with_host_fallback(
            "/v1internal:fetchAvailableModels",
            headers,
            json_body={"project": project_id} if project_id else {},
            proxy_url=proxy_url,
        )

        if response.status_code == 200:
            data = response.json()
            log.debug(f"[ANTIGRAVITY] Raw models response: {json.dumps(data, ensure_ascii=False)[:500]}")

            # 转换为 OpenAI 格式的模型列表，使用 Model 类
            model_list = []
            current_timestamp = int(datetime.now(timezone.utc).timestamp())

            if 'models' in data and isinstance(data['models'], dict):
                # 遍历模型字典
                for model_id in data['models'].keys():
                    model = Model(
                        id=model_id,
                        object='model',
                        created=current_timestamp,
                        owned_by='google'
                    )
                    model_list.append(model_to_dict(model))
            # 添加额外的 claude-sonnet-4-6-thinking 模型
            if "claude-sonnet-4-6" in data.get('models', {}):
                model = Model(
                    id='claude-sonnet-4-6-thinking',
                    object='model',
                    created=current_timestamp,
                    owned_by='google'
                )
                model_list.append(model_to_dict(model))
            # 添加额外的 claude-opus-4-6 模型
            if "claude-opus-4-6-thinking" in data.get('models', {}):
                claude_opus_model = Model(
                    id='claude-opus-4-6',
                    object='model',
                    created=current_timestamp,
                    owned_by='google'
                )
                model_list.append(model_to_dict(claude_opus_model))

            log.info(f"[ANTIGRAVITY] Fetched {len(model_list)} available models")
            return model_list
        else:
            log.error(f"[ANTIGRAVITY] Failed to fetch models ({response.status_code}): {response.text[:500]}")
            return []

    except Exception as e:
        import traceback
        log.error(f"[ANTIGRAVITY] Failed to fetch models: {e}")
        log.error(f"[ANTIGRAVITY] Traceback: {traceback.format_exc()}")
        return []


async def fetch_quota_info(
    access_token: str,
    proxy_url: Any = ...,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    获取指定凭证的额度信息
    
    Args:
        access_token: Antigravity 访问令牌
        project_id: loadCodeAssist 返回并随凭证持久化的项目 ID
        
    Returns:
        包含额度信息的字典，格式为：
        {
            "success": True/False,
            "models": {
                "model_name": {
                    "remaining": 0.95,
                    "resetTime": "12-20 10:30",
                    "resetTimeRaw": "2025-12-20T02:30:00Z"
                }
            },
            "groups": [  # retrieveUserQuotaSummary 的分组额度（weekly/5h 窗口），失败时为 None
                {
                    "displayName": "Gemini Models",
                    "description": "...",
                    "buckets": [
                        {"bucketId": "gemini-weekly", "window": "weekly",
                         "remainingFraction": 0.99, "resetTime": "...", "displayName": "Weekly Limit"}
                    ]
                }
            ],
            "error": "错误信息" (仅在失败时)
        }
    """

    project_id = (project_id or "").strip()
    if not project_id:
        return {
            "success": False,
            "error": "凭证缺少 Project ID，请先点击“检验”更新 Project ID",
        }

    headers = build_antigravity_headers(access_token)

    try:
        # 默认配置时按 sandbox → daily → prod 降级
        response = await _post_with_host_fallback(
            "/v1internal:fetchAvailableModels",
            headers,
            json_body={"project": project_id},
            proxy_url=proxy_url,
            timeout=30.0,
        )

        if response.status_code == 200:
            data = response.json()
            log.debug(f"[ANTIGRAVITY QUOTA] Raw response: {json.dumps(data, ensure_ascii=False)[:500]}")

            quota_info = {}

            if 'models' in data and isinstance(data['models'], dict):
                for model_id, model_data in data['models'].items():
                    if isinstance(model_data, dict) and 'quotaInfo' in model_data:
                        quota = model_data['quotaInfo']
                        remaining = quota.get('remainingFraction', 0)
                        reset_time_raw = quota.get('resetTime', '')

                        # 转换为北京时间
                        reset_time_beijing = 'N/A'
                        if reset_time_raw:
                            try:
                                utc_date = datetime.fromisoformat(reset_time_raw.replace('Z', '+00:00'))
                                # 转换为北京时间 (UTC+8)
                                from datetime import timedelta
                                beijing_date = utc_date + timedelta(hours=8)
                                reset_time_beijing = beijing_date.strftime('%m-%d %H:%M')
                            except Exception as e:
                                log.warning(f"[ANTIGRAVITY QUOTA] Failed to parse reset time: {e}")

                        quota_info[model_id] = {
                            "remaining": remaining,
                            "resetTime": reset_time_beijing,
                            "resetTimeRaw": reset_time_raw
                        }

            # 分组额度（weekly/5h 窗口）——best-effort，失败不影响主结果
            quota_groups = await _fetch_quota_summary_groups(
                headers,
                proxy_url,
                project_id=project_id,
            )

            return {
                "success": True,
                "models": quota_info,
                "groups": quota_groups
            }
        else:
            log.error(f"[ANTIGRAVITY QUOTA] Failed to fetch quota ({response.status_code}): {response.text[:500]}")
            return {
                "success": False,
                "error": f"API返回错误: {response.status_code}"
            }

    except Exception as e:
        import traceback
        log.error(f"[ANTIGRAVITY QUOTA] Failed to fetch quota: {e}")
        log.error(f"[ANTIGRAVITY QUOTA] Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "error": str(e)
        }


async def _fetch_quota_summary_groups(
    headers: Dict[str, str],
    proxy_url: Any = ...,
    project_id: Optional[str] = None,
) -> Optional[list]:
    """
    调用 v1internal:retrieveUserQuotaSummary 获取分组额度（Gemini 组 / Claude+GPT 组，
    每组含 weekly 和 5h 两个窗口 bucket）。

    fetchAvailableModels 的 quotaInfo 只反映 5h 窗口，weekly 限额只能从这里拿到。
    best-effort：任何失败都返回 None，不影响主额度查询。
    默认配置时按 sandbox → daily → prod 降级。
    """
    try:
        response = await _post_with_host_fallback(
            "/v1internal:retrieveUserQuotaSummary",
            headers,
            json_body={"project": project_id} if project_id else {},
            proxy_url=proxy_url,
            timeout=30.0,
        )
        if response.status_code == 200:
            data = response.json()
            groups = data.get("groups")
            if isinstance(groups, list):
                return groups
            log.warning(f"[ANTIGRAVITY QUOTA] retrieveUserQuotaSummary: unexpected response shape: {str(data)[:200]}")
        else:
            log.warning(
                f"[ANTIGRAVITY QUOTA] retrieveUserQuotaSummary failed ({response.status_code}): {response.text[:300]}"
            )
    except Exception as e:
        log.warning(f"[ANTIGRAVITY QUOTA] retrieveUserQuotaSummary error: {e}")
    return None


# ==================== 冷却对账（配额恢复自动清冷却） ====================

# 瞬时限流（429 但未携带配额重置时间）的默认短冷却秒数
RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS = 60

# 上游 429 可能携带的 GOOGLE_ONE_AI credit reason。这个 reason 本身不能证明
# 账号会长期不可用；只有明确的 quotaResetTimeStamp/quotaResetDelay 才能延长冷却。
CREDITS_EXHAUSTED_MARKER = "INSUFFICIENT_G1_CREDITS_BALANCE"


async def _resolve_error_cooldown(status_code: int, error_body: Optional[str]) -> Optional[float]:
    """
    解析 429/503 的冷却时间：

    - 错误体带 quotaResetTimeStamp/quotaResetDelay 时，按上游明确重置时间冷却；
    - 其余 429（包括 INSUFFICIENT_G1_CREDITS_BALANCE）都按瞬时限流处理，
      只短冷却 RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS 秒，之后允许再次发送。
    """
    cooldown_until = None
    if error_body:
        try:
            cooldown_until = await parse_and_log_cooldown(error_body, mode="antigravity")
        except Exception:
            cooldown_until = None

    if cooldown_until is None and status_code == 429:
        cooldown_until = time.time() + RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS
        marker = (
            f"（包含 {CREDITS_EXHAUSTED_MARKER}）"
            if error_body and CREDITS_EXHAUSTED_MARKER in error_body
            else ""
        )
        log.info(
            f"[ANTIGRAVITY] 429{marker} 未携带明确配额重置时间，按瞬时限流处理，"
            f"冷却 {RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS} 秒"
        )

    return cooldown_until


def model_quota_group(model_name: str) -> str:
    """
    模型所属的配额组。Antigravity 的额度按组共享（retrieveUserQuotaSummary）：
    - "gemini" 组：gemini 全系（含 tab_/chat_ 等内部模型）
    - "3p" 组：Claude 和 GPT 模型
    """
    m = (model_name or "").lower()
    if "claude" in m or "gpt" in m:
        return "3p"
    return "gemini"


def _group_window_remaining(groups: Optional[list], window: str) -> Dict[str, float]:
    """从 retrieveUserQuotaSummary 的 groups 中提取各组指定窗口的剩余比例。"""
    remaining: Dict[str, float] = {}
    if not groups:
        return remaining
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("window") != window:
                continue
            bucket_id = str(bucket.get("bucketId", "")).lower()
            group_key = "3p" if bucket_id.startswith("3p") else "gemini"
            fraction = bucket.get("remainingFraction")
            if isinstance(fraction, (int, float)):
                remaining[group_key] = max(remaining.get(group_key, 0.0), float(fraction))
    return remaining


# 冷却剩余时长超过该阈值时，视为上游明确给出的周配额重置
# （429 携带 quotaResetDelay/quotaResetTimeStamp，如 image 模型的 128h）。
LONG_COOLDOWN_WEEKLY_THRESHOLD_SECONDS = 24 * 3600


async def reconcile_model_cooldowns_with_quota(
    backend: Any,
    filename: str,
    model_cooldowns: Dict[str, float],
    models: Dict[str, Any],
    groups: Optional[list],
    mode: str = "antigravity",
) -> int:
    """
    配额恢复后自动清理残留的模型冷却。

    背景：额度是按组共享的，但冷却是按「凭证 × 模型」记录的。高并发下某个
    模型吃到 429（可能只是瞬时限流）后会被长期标记冷却，即使同组额度早已恢复。
    当配额查询显示该模型所属组的 5h 窗口还有剩余（或该模型自身 quotaInfo 有剩余）时，
    认为冷却是过期的，直接清除。

    长冷却（剩余时长 > LONG_COOLDOWN_WEEKLY_THRESHOLD_SECONDS，即上游 429 明确给出
    周配额重置时间的情形）只认 retrieveUserQuotaSummary 的 weekly bucket：
    fetchAvailableModels 的 quotaInfo 和 groups 的 5h bucket 在周额度仍耗尽时
    会显示满血（窗口已重置但周限额未重置），用它们清冷却会造成
    「清冷却 → 再请求 → 又 429 → 再长冷却」的抖动。

    Returns:
        清除的冷却数量
    """
    if not model_cooldowns or not hasattr(backend, "set_model_cooldown"):
        return 0

    group_5h_remaining = _group_window_remaining(groups, "5h")
    group_weekly_remaining = _group_window_remaining(groups, "weekly")
    now = time.time()
    cleared = 0

    for model_name in list(model_cooldowns.keys()):
        group_key = model_quota_group(model_name)

        cooldown_until = model_cooldowns.get(model_name)
        is_long_cooldown = (
            isinstance(cooldown_until, (int, float))
            and cooldown_until - now > LONG_COOLDOWN_WEEKLY_THRESHOLD_SECONDS
        )

        if is_long_cooldown:
            # 长冷却只信 weekly 窗口；weekly 数据缺失时保持冷却，不用 5h/quotaInfo 猜
            remaining = group_weekly_remaining.get(group_key)
        else:
            # 组额度（retrieveUserQuotaSummary，权威数据源）
            remaining = group_5h_remaining.get(group_key)

            # 组数据缺失时回落到该模型自身的 quotaInfo（fetchAvailableModels）
            if remaining is None:
                model_quota = models.get(model_name)
                if isinstance(model_quota, dict):
                    value = model_quota.get("remaining")
                    if isinstance(value, (int, float)):
                        remaining = float(value)

        if remaining is not None and remaining > 0:
            window_label = "weekly" if is_long_cooldown else "5h"
            try:
                await backend.set_model_cooldown(filename, model_name, None, mode=mode)
                cleared += 1
                log.info(
                    f"[ANTIGRAVITY] 配额已恢复（{group_key} 组 {window_label} 窗口剩余 "
                    f"{remaining:.2%}），清除过期冷却: {filename} / {model_name}"
                )
            except Exception as e:
                log.warning(f"[ANTIGRAVITY] 清除模型冷却失败 {filename}/{model_name}: {e}")

    return cleared
