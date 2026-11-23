"""
Antigravity API Client - Handles communication with Google's Antigravity API
处理与 Google Antigravity API 的通信
"""

import asyncio
import hashlib
import inspect
import json
import math
import random
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Callable, Set, Tuple, Mapping

from src.antigravity_400_classifier import upstream_error_log_detail
from src.request_capture import record_local_capture, bind_capture_credential
from src.antigravity_usage import AccountUsageLimitReached, begin_account_usage
from src.rate_limits import CREDITS_EXHAUSTED_MARKER, RateLimit, upstream_rate_limit
from src.claude_routing import bind_claude_request, bind_claude_stream

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
from src.httpx_client import (
    get_decoded_response_headers,
    stream_post_async,
    post_async,
)
from src.stream_guard import aclose_quietly
from src.models import Model, model_to_dict
from src.redis_config import create_redis_client_from_env, redis_socket_timeouts
from src.antigravity_error_classifier import (
    Antigravity403Decision,
    classify_antigravity_403,
)
from src.antigravity_model_catalog import UnknownAntigravityModelError
from src.antigravity_dynamic_warmup import (
    note_dynamic_warmup_outcome,
    release_dynamic_warmup_slot,
)

# 导入共同的基础功能
from src.api.utils import (
    check_should_auto_ban,
    handle_auto_ban,
    hold_transient_account,
    handle_error_with_retry,
    get_retry_config,
    record_api_call_success,
    record_api_call_error,
    parse_and_log_cooldown,
    collect_streaming_response,
)
from src.billing import extract_model_version, extract_usage_metadata, get_billing_recorder
from src.dingtalk_alert import (
    all_accounts_are_unavailable,
    send_all_accounts_unavailable_alert,
)

# ==================== 全局凭证管理器 ====================

# 使用全局单例 credential_manager，自动初始化


# ==================== 会话状态管理 ====================

SESSION_TTL_SECONDS = 6 * 60 * 60
MAX_SESSION_STATES = 1024
# 无显式 sessionId 时的会话复用：同一「账号 × 调用方 × 模型 × 时间桶」共享一条
# trajectory，让 request_step 自然递增（真实客户端不会 100% 停在 step=1）。
# 比例由 ANTIGRAVITY_SESSION_REUSE_RATIO 控制，0 = 完全保持旧行为。
SESSION_REUSE_BUCKET_SECONDS = 900
SESSION_MAX_STEPS = 30
_MAX_SESSION_LOCKS = 4096
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
    allocation_backend: str = "new_request"
    allocation_key_ref: str = ""


@dataclass(frozen=True)
class Antigravity403HandlingResult:
    decision: Antigravity403Decision
    should_retry: bool
    cooldown_until: Optional[float] = None
    # 地区拒绝/绑定异常：换掉出口 IP 后可以用同一账号重试本次请求
    rebind_retry: bool = False


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
_redis_init_lock = asyncio.Lock()


async def _get_redis():
    """懒初始化 Redis 客户端，REDIS_URL 未设置时返回 None。"""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    async with _redis_init_lock:
        if _redis_checked:
            return _redis_client
        try:
            connect_timeout, socket_timeout = redis_socket_timeouts()
            client = create_redis_client_from_env(
                decode_responses=True,
                socket_connect_timeout=connect_timeout,
                socket_timeout=socket_timeout,
            )
            if client is not None:
                try:
                    await client.ping()
                except BaseException:
                    await client.aclose()
                    raise
                _redis_client = client
                log.info("[SESSION] Redis session store enabled")
        except Exception as e:
            log.warning(
                f"[SESSION] Redis unavailable, falling back to in-memory: {type(e).__name__}"
            )
        _redis_checked = True
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


def _session_key(
    request_payload: Dict[str, Any],
    model: str = "",
    credential_key: Optional[str] = None,
    *,
    reuse_ratio: float = 0.0,
    bucket_seconds: int = SESSION_REUSE_BUCKET_SECONDS,
) -> str:
    """Resolve the upstream conversation key.

    ``sessionId`` is the explicit continuation contract and always wins.
    Without it, identical text is still not evidence that two requests belong
    to one conversation — but a caller that never sends ``sessionId`` produces
    ``request_step == 1`` forever, which no real client does. So a fraction of
    uncorrelated traffic (``reuse_ratio``) is bucketed by
    account × caller × model × time window and shares one trajectory.
    """
    from src.claude_routing import current_api_key_id

    session_id = request_payload.get("sessionId")
    if session_id:
        identity = [credential_key or "", current_api_key_id(), model, str(session_id)]
        digest = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
        return f"explicit:v2:{digest}"

    # 未开启复用：保持旧行为（一次请求一条 trajectory）。
    if reuse_ratio <= 0:
        return "request:" + uuid.uuid4().hex

    bucket = int(time.time() // max(int(bucket_seconds), 60))
    identity = [credential_key or "", current_api_key_id(), model, str(bucket)]
    digest = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
    # 按稳定哈希决定本次是否落在复用组，避免同一调用方忽真忽假。
    if int(digest[:8], 16) % 1000 >= int(reuse_ratio * 1000):
        return "request:" + uuid.uuid4().hex
    return f"derived:v1:{digest}"


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


def _derive_session_id(first_user_text: str, credential_key: Optional[str]) -> str:
    """派生 sessionId，对齐真实 hub 客户端：安装级稳定的负 int64 字符串
    （2.23.1/2.25.0 实抓均为同一负值，跨会话、跨请求不变）。

    网关按账号维度稳定：同一 credential 的所有会话共享同一 sessionId；
    无 credential_key 时回退到首条用户文本/随机派生（保持负值格式）。
    """
    if credential_key:
        digest = hashlib.sha256(f"agy-session:{credential_key}".encode("utf-8")).digest()
        val = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
        return str(-val - 1)
    if first_user_text:
        digest = hashlib.sha256(first_user_text.encode("utf-8")).digest()
        session_id_val = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
        return str(-session_id_val - 1)
    return str(-(uuid.uuid4().int % 9_000_000_000_000_000_000) - 1)


def _make_new_state(
    first_user_text: str, now: float, credential_key: Optional[str] = None
) -> AntigravitySessionState:
    return AntigravitySessionState(
        conversation_id=str(uuid.uuid4()),
        trajectory_id=str(uuid.uuid4()),
        session_id=_derive_session_id(first_user_text, credential_key),
        step_index=1,
        created_at=now,
        last_used_at=now,
    )


# One server-side operation covers initialization, allocation, step cap and TTL renewal.
_SESSION_ALLOCATE_LUA = """
local raw = redis.call('GET', KEYS[1])
local state
local max_steps = tonumber(ARGV[4])
if raw then
    state = cjson.decode(raw)
    state.step_index = state.step_index + 1
    if max_steps and state.step_index > max_steps then
        state = cjson.decode(ARGV[1])
    end
    state.last_used_at = tonumber(ARGV[2])
else
    state = cjson.decode(ARGV[1])
end
local result = cjson.encode(state)
redis.call('SET', KEYS[1], result, 'EX', ARGV[3])
return result
"""


# Same-conversation allocations are serialized so concurrent requests cannot
# receive out-of-order step numbers (the upstream compares last_step_index).
_session_locks: Dict[str, asyncio.Lock] = {}


def _session_lock(key: str) -> asyncio.Lock:
    lock = _session_locks.get(key)
    if lock is None:
        if len(_session_locks) > _MAX_SESSION_LOCKS:
            _session_locks.clear()
        lock = asyncio.Lock()
        _session_locks[key] = lock
    return lock


async def _get_session_state(
    request_payload: Dict[str, Any],
    model: str = "",
    credential_key: Optional[str] = None,
) -> AntigravitySessionState:
    from config import (
        get_antigravity_session_bucket_seconds,
        get_antigravity_session_max_steps,
        get_antigravity_session_reuse_ratio,
    )

    now = time.time()
    reuse_ratio = await get_antigravity_session_reuse_ratio()
    max_steps = await get_antigravity_session_max_steps()
    bucket_seconds = await get_antigravity_session_bucket_seconds()
    key = _session_key(
        request_payload,
        model,
        credential_key,
        reuse_ratio=reuse_ratio,
        bucket_seconds=bucket_seconds,
    )
    first_user_text = _extract_first_user_text(request_payload)
    from src.operational_audit import reference
    def allocated(state, backend):
        return replace(state, allocation_backend=backend, allocation_key_ref=reference(key))

    # 未开启复用（或无显式 sessionId）时仍是一条请求一条 trajectory。
    if key.startswith("request:"):
        return allocated(_make_new_state(first_user_text, now, credential_key), "new_request")

    async with _session_lock(key):
        redis = await _get_redis()
        if redis is not None:
            redis_key = f"{_REDIS_KEY_PREFIX}{key}"
            try:
                initial = _make_new_state(first_user_text, now, credential_key)
                raw = await redis.eval(
                    _SESSION_ALLOCATE_LUA, 1, redis_key,
                    json.dumps(initial.__dict__), str(now), SESSION_TTL_SECONDS,
                    str(max_steps),
                )
                return allocated(AntigravitySessionState(**json.loads(raw)), "redis")
            except Exception as e:
                log.warning(
                    f"[SESSION] Redis error, falling back to memory: {type(e).__name__}"
                )

        # 内存回退
        _prune_session_states(now)
        state = _session_states.get(key)
        if state:
            state.step_index += 1
            if state.step_index > max_steps:
                state = _make_new_state(first_user_text, now, credential_key)
            state.last_used_at = now
            _session_states[key] = state
            return allocated(state, "memory")
        state = _make_new_state(first_user_text, now, credential_key)
        _session_states[key] = state
        return allocated(state, "memory")


def _generate_request_id(conversation_id: str, trajectory_id: str, step: int) -> str:
    unix_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"agent/{conversation_id}/{unix_ms}/{trajectory_id}/{step}"


def _build_labels(model: str, state: AntigravitySessionState) -> Dict[str, str]:
    """构建 labels 9 键全集，对齐真实 hub 客户端（2.25.0 实抓）。

    model_enum 必须填服务端 fetchAvailableModels 下发的枚举名
    （如 MODEL_PLACEHOLDER_M318），而非模型 ID——真实客户端如此。
    步进编号从 0 开始（实抓首条 agent 请求 last_step_index=0、
    request_id=<trajectory_id>-0；信封 requestId 末段从 1 开始）。
    """
    from src.antigravity_model_catalog import get_model_enum

    used_claude = "claude" in model.lower()
    label_step = max(state.step_index - 1, 0)
    return {
        "cascade_id": state.conversation_id,
        "root_cascade_id": state.conversation_id,
        "request_id": f"{state.trajectory_id}-{label_step}",
        "trajectory_id": state.trajectory_id,
        "last_step_index": str(label_step),
        "model_enum": get_model_enum(model),
        "used_claude": str(used_claude).lower(),
        "used_claude_conservative": str(used_claude).lower(),
        "used_non_gemini_model": "false" if model.lower().startswith("gemini") else "true",
    }


async def wrap_cli_request(
    gemini_request: Dict[str, Any],
    model: str,
    project_id: str,
    user_email: Optional[str] = None,
    credential_key: Optional[str] = None,
    credential_data: Optional[Dict[str, Any]] = None,
    *,
    skip_prompt_fusion: bool = False,
) -> Tuple[Dict[str, Any], str]:
    """
    将 Gemini 格式请求包装成 Antigravity hub 格式（对齐 2.28.2 实抓信封）。
    返回 (payload, request_id)。

    user_email 为 None 或 gmail/googlemail 邮箱时保持 antigravity 指纹；
    其他邮箱（如企业/教育账号）改用 jetski 指纹（对齐 Antigravity-Manager）。
    credential_key 用于派生账号级稳定 sessionId 和按账号刷新模型目录。
    """
    from src.claude_routing import enforce_key_model
    enforce_key_model(model)
    if (not skip_prompt_fusion and not getattr(gemini_request, 'workflow_contract', None)
            and not getattr(gemini_request, 'preserve_output_format', False)
            and credential_key and credential_data and not is_antigravity_image_request(model, gemini_request)):
        from src.prompt_fusion import fuse_request
        gemini_request = await fuse_request(gemini_request, model, credential_key, credential_data)
    inner = dict(gemini_request)

    # 基线提示词在选号前只埋占位符，这里按账号的 hub 构建替换成对应版本，
    # 保证 system 基线与该账号 UA 声明的版本一致（混淆不覆盖基线部分）。
    if credential_data:
        from src.antigravity_device_profile import get_device_profile
        from src.converter.antigravity_system_prompt import resolve_pending_baseline

        inner = resolve_pending_baseline(
            inner,
            get_device_profile(credential_data, credential_key)["hub_version"],
        )

    # 移除 safetySettings（CLI 不发送）
    inner.pop("safetySettings", None)
    is_image_request = is_antigravity_image_request(model, inner)

    if not is_image_request:
        from src.antigravity_model_catalog import (
            UnknownAntigravityModelError,
            get_model_enum,
        )

        try:
            get_model_enum(model)
        except UnknownAntigravityModelError:
            if credential_key and credential_data:
                await _get_available_models_for_credential(
                    credential_name=credential_key,
                    credential_data=credential_data,
                )
            get_model_enum(model)

    # 获取/更新会话状态
    state = await _get_session_state(inner, model, credential_key)

    if not is_image_request:
        # Agent 请求沿用 hub 会话字段；原生图片请求不携带这些字段。
        # Caller sessionId selects the local conversation; the upstream sessionId
        # identifies the account installation and must not pass through caller input.
        inner["sessionId"] = state.session_id
        inner["labels"] = _build_labels(model, state)

        # generationConfig 默认值按模型目录补齐，对齐真实 agent 请求
        # （真实客户端恒发 maxOutputTokens + thinkingConfig；客户端已指定的保留）。
        from src.antigravity_model_catalog import get_model_info

        info = get_model_info(model) or {}
        gen_cfg = inner.setdefault("generationConfig", {})
        if isinstance(gen_cfg, dict):
            gen_cfg.setdefault("maxOutputTokens", info.get("max_output_tokens") or 65536)
            budget = info.get("thinking_budget")
            if info.get("supports_thinking") and budget is not None:
                gen_cfg.setdefault(
                    "thinkingConfig",
                    {"includeThoughts": True, "thinkingBudget": budget},
                )

    if is_image_request:
        # 对齐 agy hub 原生图片信封：request 内不带 model / responseModalities，
        # requestId 使用 image_gen/<ms>/<uuid>/<step>，而不是 agent/ 轨迹格式。
        inner.pop("model", None)
        gen_cfg = inner.get("generationConfig")
        if isinstance(gen_cfg, dict):
            gen_cfg = dict(gen_cfg)
            gen_cfg.pop("responseModalities", None)
            gen_cfg.pop("response_modalities", None)
            inner["generationConfig"] = gen_cfg
        request_id = (
            f"image_gen/{int(datetime.now(timezone.utc).timestamp() * 1000)}"
            f"/{uuid.uuid4()}/{state.step_index}"
        )
    else:
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
    bind_capture_credential(credential_key, payload, credential_data, session_info={
        "source": "explicit" if gemini_request.get("sessionId") else "independent_request",
        "backend": state.allocation_backend, "key_ref": state.allocation_key_ref,
        "step": state.step_index,
    })
    return payload, request_id


def _model_enum_error_response(exc: UnknownAntigravityModelError) -> Response:
    return Response(
        content=json.dumps({
            "error": {
                "code": 400,
                "message": str(exc),
                "status": "INVALID_ARGUMENT",
            }
        }),
        status_code=400,
        media_type="application/json",
    )


def _upstream_trace_id(headers: Any) -> str:
    """从上游响应头提取 x-cloudaicompanion-trace-id（排障对齐官方抓包字段）。"""
    if not headers:
        return ""
    try:
        return headers.get("x-cloudaicompanion-trace-id", "") or ""
    except AttributeError:
        return ""


# ==================== 辅助函数 ====================


def build_antigravity_headers(
    access_token: str,
    *,
    image_request: bool = False,
    project_id: Optional[str] = None,
    version_override: Optional[str] = None,
    credential_data: Optional[Mapping[str, Any]] = None,
    credential_name: Optional[str] = None,
) -> Dict[str, str]:
    """构建 Antigravity CLI API 请求头。

    version_override 供版本探测等场景指定候选版本；默认使用动态生效版本。
    credential_data / credential_name 用于取该账号自己的设备画像（版本/CL/
    平台），保证同一账号 UA 永久稳定、账号之间互不相同。
    """
    from src.utils import get_antigravity_user_agent_for

    return {
        "User-Agent": get_antigravity_user_agent_for(
            credential_data,
            credential_name,
            version_override=version_override,
        ),
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
        transport="antigravity",
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
        credential_data=credential_data,
        credential_name=credential_name,
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
    from src.antigravity_model_catalog import update_from_catalog
    from src.antigravity_quota_guard import update_quota_snapshot

    update_from_catalog(models)
    update_quota_snapshot(credential_name, models)
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


def _credential_slot_backpressure_response(
    error: Exception | None = None,
) -> Response:
    usage_limited = isinstance(error, AccountUsageLimitReached)
    # 全池闸门会带一个按令牌补充速度算出的真实等待时间；恒为 1 秒会让批量
    # 客户端热重试，在极端负载下把池子打穿。
    retry_after = getattr(error, "retry_after", None)
    response = Response(
        content=json.dumps(
            {
                "error": {
                    "code": 429,
                    "message": (
                        f"{error}; retry later."
                        if usage_limited else "Gateway account capacity is busy; retry later."
                    ),
                    "status": "RESOURCE_EXHAUSTED",
                }
            }
        ),
        status_code=429,
        media_type="application/json",
        headers={"Retry-After": str(retry_after if retry_after else (error.retry_after if usage_limited else 1))},
    )
    response.rate_limit = getattr(error, "rate_limit", RateLimit("gateway", "capacity_busy"))
    return response


def _selection_usage_rejection() -> Response | None:
    from src.rate_limits import selection_usage_resume_at
    resume_at = selection_usage_resume_at.get()
    if resume_at:
        return _credential_slot_backpressure_response(
            AccountUsageLimitReached(max(1, resume_at - int(time.time())))
        )
    return None


def _account_pool_429() -> Response:
    from src.rate_limits import selection_retry_at
    response = _selection_usage_rejection()
    if response is None:
        response = Response(content=json.dumps({"error": "当前无可用凭证"}), status_code=429,
                            media_type="application/json")
        response.rate_limit = RateLimit("gateway", "account_pool_unavailable")
    if retry_at := selection_retry_at.get():
        response.headers["Retry-After"] = str(max(1, math.ceil(retry_at - time.time())))
    return response


def _mark_upstream_429(response, error_body, *, request_id, credential_name, model, upstream_request):
    if response.status_code == 429:
        response.rate_limit = upstream_rate_limit(error_body)
        response.rate_limit_attempt = {
            "request_id": request_id, "credential_name": credential_name,
            "model": model, "upstream_request": upstream_request,
        }


def _billing_response_fields(response, status_code, request_id, credential_name, model, upstream_request):
    fields = {
        "request_id": request_id, "credential_name": credential_name, "model": model,
        "upstream_request": upstream_request,
        "status_code": response.status_code if response is not None else status_code,
    }
    if fields["status_code"] == 429 and (attribution := getattr(response, "rate_limit", None)) is not None:
        fields["rate_limit"] = attribution
        fields.update(getattr(response, "rate_limit_attempt", {}))
    return fields


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


# 一次请求内同一账号因地区拒绝最多换几个出口 IP：覆盖「连续几个组内节点都不可用」
_REGION_REBIND_LIMIT = 2

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
    """Record the 403 category and enforce the configured status-code policy."""
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
    if decision.category == "account_forbidden":
        note_dynamic_warmup_outcome(credential_name, "forbidden")
    elif decision.category in {
        "validation_required",
        "proxy_drift",
        "proxy_failed",
    }:
        note_dynamic_warmup_outcome(credential_name, "paused")
    # 地区拒绝保留绑定并向调用方返回原始拒绝。

    # 绑定故障和地区拒绝本身都不证明账号已被禁用。
    # 只有明确的账号级拒绝（或无法归类的 403 且配置要求封禁）才禁用账号。
    account_level_denial = decision.disable_account or decision.category == "unknown_403"
    if decision.tos_violation or (
        account_level_denial and await check_should_auto_ban(403)
    ):
        await credential_manager.set_cred_disabled(
            credential_name, True, mode="antigravity"
        )

    if decision.category == "validation_required":
        return Antigravity403HandlingResult(
            decision,
            should_retry=False,
            cooldown_until=timestamp + VALIDATION_REQUIRED_COOLDOWN_SECONDS,
        )

    if decision.cooldown_model:
        return Antigravity403HandlingResult(
            decision,
            should_retry=False,
            cooldown_until=timestamp + MODEL_FORBIDDEN_COOLDOWN_SECONDS,
        )

    return Antigravity403HandlingResult(
        decision,
        should_retry=False,
        rebind_retry=bool(decision.retry_after_rebind),
    )


async def _retry_same_account_on_new_egress(
    credential_name: str,
    credential_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """修复代理绑定故障后，返回刷新后的凭证数据。

    只对组绑定生效。换绑失败（组内无空闲节点、自定义代理、固定 IP）返回 None，
    调用方按原有失败路径处理，账号不会被静默丢弃。
    """
    rotate = getattr(credential_manager, "rotate_antigravity_egress", None)
    reload_credential = getattr(
        credential_manager, "reload_antigravity_credential", None
    )
    if rotate is None or reload_credential is None:
        return None
    try:
        rotated = await rotate(credential_name)
    except Exception as exc:  # noqa: BLE001 - rotation must not break the retry path
        log.warning(
            "[ANTIGRAVITY REGION] egress rotation failed: "
            f"credential={credential_name}, error={type(exc).__name__}: {exc}"
        )
        return None
    if rotated is None:
        return None
    try:
        fresh = await reload_credential(credential_name)
    except Exception as exc:  # noqa: BLE001 - keep the original failure path intact
        log.warning(
            "[ANTIGRAVITY REGION] credential reload failed: "
            f"credential={credential_name}, error={type(exc).__name__}: {exc}"
        )
        return None
    if not fresh or not (fresh.get("access_token") or fresh.get("token")):
        return None
    return fresh


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
    apply_cred_result: Callable[..., Any],
    log_prefix: str,
    stop_when_unavailable: bool = False,
) -> Tuple[bool, Optional[asyncio.Task]]:
    """优先使用预热凭证，失败后退回同步刷新。"""
    if next_cred_task is not None:
        try:
            cred_result = await next_cred_task
            next_cred_task = None
            if cred_result and await apply_cred_result(cred_result):
                await asyncio.sleep(retry_interval)
                return True, next_cred_task
            if stop_when_unavailable:
                return False, next_cred_task
        except Exception as e:
            log.warning(f"{log_prefix} 预热凭证任务失败: {e}")
            next_cred_task = None

    if stop_when_unavailable:
        if await refresh_credential_fast():
            await asyncio.sleep(retry_interval)
            return True, next_cred_task
        return False, next_cred_task

    await asyncio.sleep(retry_interval)
    if await refresh_credential_fast():
        return True, next_cred_task

    return False, next_cred_task


async def _text_credential_is_available(credential_name: str, model_name: str) -> bool:
    """复查预选凭证：等待节奏/并发槽位后，可能已被其它请求标记冷却。"""
    from src.storage_adapter import get_storage_adapter

    try:
        storage = await get_storage_adapter()
        state = await storage.get_credential_state(credential_name, mode="antigravity")
        if not state or state.get("disabled"):
            return False
        cooldown = (state.get("model_cooldowns") or {}).get(model_name)
        return cooldown is None or float(cooldown) <= time.time()
    except Exception as exc:
        # 无法确认冷却状态时不向上游盲发，也不走网络异常重试分支。
        log.warning(f"[ANTIGRAVITY] 无法复查凭证冷却: {credential_name}, {type(exc).__name__}")
        return False


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
    from src.claude_routing import key_allowed_model
    if key_allowed_model() or not chain:
        return None
    try:
        index = chain.index(current_model)
    except ValueError:
        return None
    if index + 1 < len(chain):
        return chain[index + 1]
    return None


async def _select_text_credential(model_name):
    """Try the configured suffix in order, retaining the earliest known recovery."""
    from src.rate_limits import selection_retry_at, selection_usage_resume_at
    from src.request_timing import database_timing_scope, measure_phase

    chain = None
    earliest = None
    while True:
        with measure_phase("credential_lookup"), database_timing_scope("credential"):
            result = await credential_manager.get_valid_credential(
                mode="antigravity", model_name=model_name
            )
        if result:
            return model_name, result

        usage = selection_usage_resume_at.get()
        deadline = selection_retry_at.get() or usage
        if deadline and (earliest is None or deadline < earliest[0]):
            earliest = deadline, usage, model_name

        if chain is None:
            chain = await get_antigravity_model_fallback_chain()
        next_model = _next_fallback_model(chain, model_name)
        if next_model is None:
            break
        log.warning(f"[ANTIGRAVITY] 模型 {model_name} 无可用凭证，尝试 {next_model}")
        model_name = next_model

    if earliest is not None:
        deadline, usage, model_name = earliest
        selection_retry_at.set(deadline)
        selection_usage_resume_at.set(usage)
    return model_name, None


# ==================== 新的流式和非流式请求函数 ====================

async def _stream_request_inner(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    api_key_id: str = "env",
    lease_tracker=None,
    request_model: Optional[str] = None,
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
    original_model = request_model or model_name  # Preserve the pre-normalization client model.
    inner_request = body.get("request", body)
    image_request = is_antigravity_image_request(model_name, inner_request)
    from src.request_timing import mark_stage
    mark_stage("api_started")
    gateway_started = time.monotonic()  # 网关处理起点（时效性统计口径 A）

    # 1. 获取有效凭证
    if image_request:
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity", model_name=model_name, image_request=True
        )
    else:
        model_name, cred_result = await _select_text_credential(model_name)

    if not cred_result:
        # 文本号池暂时不可用返回429，图片保留原有语义。
        log.error("[ANTIGRAVITY STREAM] 当前无可用凭证")
        await _alert_all_accounts_unavailable_if_needed(model_name)
        if image_request and _selection_usage_rejection() is None:
            yield Response(content=json.dumps({"error": "当前无可用凭证"}), status_code=500,
                           media_type="application/json")
        else:
            from src.call_monitor import record_gateway_rejection
            response = _account_pool_429()
            await record_gateway_rejection(response.rate_limit, api_key_id=api_key_id, model=model_name)
            yield response
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

    auth_headers = build_antigravity_headers(
        access_token,
        credential_data=credential_data,
        credential_name=current_file,
    )

    # 合并自定义headers
    if headers:
        auth_headers.update(headers)

    # 构建 CLI 格式请求体
    final_payload, request_id = await wrap_cli_request(
        inner_request, model_name, project_id,
        user_email=credential_data.get("user_email"),
        credential_key=current_file,
        credential_data=credential_data,
    )

    # 3. 调用stream_post_async进行请求
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_response = None  # 记录最后一次的错误响应
    next_cred_task = None  # 预热的下一个凭证任务
    attempted_credentials: set[str] = set()
    rate_limited_credentials: set[str] = set()  # 当前模型上429失败或发送前已冷却的凭证
    region_rebinds: Dict[str, int] = {}  # 同一请求内每个账号已为地区拒绝换过的出口 IP 数
    usage_metadata = None
    upstream_model = None  # 上游响应 modelVersion：真实执行模型（归一后的物理模型）
    usage_remainder = ""
    billing_recorded = False

    def consume_usage_chunk(chunk: Any) -> None:
        """Parse only complete SSE lines; retain at most one small partial line."""
        nonlocal usage_metadata, upstream_model, usage_remainder
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
                payload = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            usage_metadata = extract_usage_metadata(payload) or usage_metadata
            upstream_model = extract_model_version(payload) or upstream_model

    async def record_billing_once(success: bool, status_code: Optional[int] = None, *, response=None):
        nonlocal billing_recorded
        if billing_recorded:
            return
        billing_recorded = True
        try:
            mark_stage("recorder_lookup")
            recorder = await get_billing_recorder()
            await recorder.record(
                **_billing_response_fields(response, status_code, request_id, current_file, model_name, final_payload),
                usage_metadata=usage_metadata, success=success, api_key_id=api_key_id,
                task_type="image" if image_request else "chat",
                gateway_seconds=time.monotonic() - gateway_started,
                upstream_model=upstream_model,
                request_model=original_model,
            )
        except Exception as exc:
            log.warning(f"[BILLING] failed to record stream request: {type(exc).__name__}")

    async def refresh_credential_fast():
        nonlocal current_file, credential_data, access_token, auth_headers, project_id, final_payload, proxy_url, request_id
        if lease_tracker is not None:
            await lease_tracker.release()
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity", model_name=model_name, image_request=image_request,
            exclude_filenames=set(rate_limited_credentials),
        )
        if not cred_result:
            return None
        new_file, new_data = cred_result
        if lease_tracker is not None:
            await lease_tracker.activate(
                new_file, new_data.pop("_image_lease_id", None)
            )
        if new_file in rate_limited_credentials:
            if lease_tracker is not None:
                await lease_tracker.release()
            return None
        new_access_token = new_data.get("access_token") or new_data.get("token")
        new_project_id = new_data.get("project_id", "")
        if not new_access_token:
            return None
        new_payload, new_request_id = await wrap_cli_request(
            inner_request,
            model_name,
            new_project_id,
            user_email=new_data.get("user_email"),
            credential_key=new_file,
            credential_data=new_data,
        )
        current_file, credential_data = new_file, new_data
        access_token, project_id = new_access_token, new_project_id
        proxy_url = get_effective_proxy_url(new_data)
        final_payload, request_id = new_payload, new_request_id
        auth_headers["Authorization"] = f"Bearer {new_access_token}"
        return True

    async def apply_cred_result(
        cred_result: Tuple[str, Dict[str, Any]], *, rebuild_payload: bool = True
    ) -> bool:
        nonlocal current_file, credential_data, access_token, project_id, auth_headers, final_payload, proxy_url, request_id
        new_file, new_data = cred_result
        if new_file in rate_limited_credentials and not image_request:
            return False
        new_access_token = new_data.get("access_token") or new_data.get("token")
        new_project_id = new_data.get("project_id", "")
        if not new_access_token or not new_project_id:
            return False
        if rebuild_payload:
            new_payload, new_request_id = await wrap_cli_request(
                inner_request,
                model_name,
                new_project_id,
                user_email=new_data.get("user_email"),
                credential_key=new_file,
                credential_data=new_data,
            )
            final_payload, request_id = new_payload, new_request_id
        current_file, credential_data = new_file, new_data
        if lease_tracker is not None:
            lease_tracker.activate_reserved(
                new_file, new_data.pop("_image_lease_id", None)
            )
        access_token, project_id = new_access_token, new_project_id
        proxy_url = get_effective_proxy_url(new_data)
        auth_headers["Authorization"] = f"Bearer {new_access_token}"
        if not rebuild_payload:
            final_payload["project"] = new_project_id
        return True

    attempt = 0
    while attempt <= max_retries:
        success_recorded = False  # 标记是否已记录成功
        need_retry = False  # 标记是否需要重试
        same_account_retry = False  # 地区拒绝换 IP 后，用同一账号重试
        usage_attempt = None
        usage_metadata = upstream_model = None
        usage_remainder = ""

        # 普通文本按轮转取得账号，只执行全池准入和养号检查。
        # 本地等待单独计时，不计入上游耗时（时效性统计口径 B 只算真实上游时间）。
        from src.antigravity_pacing import (
            CredentialSlotWaitTimeout,
            acquire_credential_slot,
            pace_credential,
            release_credential_slot,
        )

        if image_request:
            from src.antigravity_pacing import (
                acquire_limited_credential_slot as acquire_credential_slot,
                release_limited_credential_slot as release_credential_slot,
            )

        slot_holder = current_file  # 槽位归属固定为本轮尝试的账号，重试换号不影响释放
        pacing_waited = 0.0

        stream = stream_post_async(
            url=target_url,
            body=final_payload,
            native=native,
            headers=auth_headers,
            proxy_url=proxy_url,
            transport="antigravity",
        )
        slot_acquired = False
        try:
            pacing_waited += await acquire_credential_slot(slot_holder)
            slot_acquired = True
            if image_request:
                pacing_waited += await pace_credential(slot_holder)
            if not image_request and (
                current_file in rate_limited_credentials
                or not await _text_credential_is_available(current_file, model_name)
            ):
                log.info(f"[ANTIGRAVITY] 跳过预选后已冷却或429失败的凭证: {current_file} / {model_name}")
                rate_limited_credentials.add(current_file)
                if retry_config["retry_enabled"] and attempt < max_retries and await refresh_credential_fast():
                    attempt += 1
                    continue
                response = _upstream_403_to_503(last_error_response) if last_error_response is not None else _account_pool_429()
                await record_billing_once(False, response=response)
                yield response
                return
            usage_attempt = await begin_account_usage(slot_holder)
            attempted_credentials.add(current_file)
            attempt_started = time.monotonic()  # 本次上游尝试起点（时效性统计口径 B）
            if pacing_waited >= 0.2:
                log.debug(
                    f"[PACING] 本次上游尝试前置等待 {pacing_waited:.2f}s（未计入上游耗时）"
                )

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
                    _mark_upstream_429(chunk, error_body, request_id=request_id, credential_name=current_file,
                                       model=model_name, upstream_request=final_payload)
                    if status_code == 503:
                        # 503 必须在观测到响应时就冷藏账号：不依赖重试开关、
                        # 重试次数或后续分支，避免「503 了但账号还在轮转」。
                        await hold_transient_account(
                            credential_manager, status_code, current_file
                        )

                    # 403 必须先分类；429/503/配置错误码继续走通用重试。
                    if status_code == 403 or _is_retryable_status(status_code, DISABLE_ERROR_CODES):
                        log.warning(f"[ANTIGRAVITY STREAM] 流式请求失败 (status={status_code}), 凭证: {current_file}, traceId: {_upstream_trace_id(getattr(chunk, 'headers', None)) or '无'}")

                        # 解析冷却时间（明确重置时间优先，其余 429 只短冷却）
                        cooldown_until = None
                        if status_code == 429 or status_code == 503:
                            cooldown_until = await _resolve_error_cooldown(
                                status_code, error_body, current_file,
                                headers=getattr(chunk, "headers", None),
                            )

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
                            or status_code in (403, 429)
                        )
                        await record_api_call_error(
                            credential_manager, current_file, status_code,
                            cooldown_until, mode="antigravity", model_name=model_name,
                            error_message=error_body,
                            upstream_seconds=time.monotonic() - attempt_started,
                            gateway_seconds=(time.monotonic() - gateway_started) if stream_final_failure else None
                        )

                        if (
                            classified_403 is not None
                            and classified_403.rebind_retry
                            and region_rebinds.get(current_file, 0) < _REGION_REBIND_LIMIT
                        ):
                            fresh = await _retry_same_account_on_new_egress(
                                current_file, credential_data
                            )
                            if fresh and await apply_cred_result(
                                (current_file, fresh), rebuild_payload=False
                            ):
                                region_rebinds[current_file] = (
                                    region_rebinds.get(current_file, 0) + 1
                                )
                                attempted_credentials.discard(current_file)
                                rate_limited_credentials.discard(current_file)
                                need_retry = True
                                same_account_retry = True
                                break

                        if status_code in (403, 429):
                            if status_code == 429 and await check_should_auto_ban(status_code):
                                await handle_auto_ban(credential_manager, status_code, current_file, mode="antigravity")
                            await record_billing_once(False, response=chunk)
                            await _alert_all_accounts_unavailable_if_needed(model_name, attempted_credentials)
                            yield chunk
                            return

                        should_retry = await handle_error_with_retry(
                            credential_manager, status_code, current_file,
                            retry_config["retry_enabled"], attempt, max_retries, retry_interval,
                            mode="antigravity",
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
                                    mode="antigravity", model_name=model_name,
                                    exclude_filenames=set(rate_limited_credentials),
                                )
                            )

                        if should_retry and attempt < max_retries:
                            need_retry = True
                            break  # 跳出内层循环，准备重试
                        else:
                            # 不重试，直接返回原始错误（上游 403 改写为 503）
                            log.error(f"[ANTIGRAVITY STREAM] 达到最大重试次数或不应重试，返回原始错误")
                            await record_billing_once(False, response=chunk)
                            await _alert_all_accounts_unavailable_if_needed(
                                model_name, attempted_credentials
                            )
                            yield _upstream_403_to_503(chunk)
                            return
                    else:
                        # 错误码不在禁用码当中，直接返回，无需重试
                        log.error(f"[ANTIGRAVITY STREAM] 流式请求失败，非重试错误码 (status={status_code}), 凭证: {current_file}, traceId: {_upstream_trace_id(getattr(chunk, 'headers', None)) or '无'}")
                        await record_api_call_error(
                            credential_manager, current_file, status_code,
                            None, mode="antigravity", model_name=model_name,
                            error_message=error_body,
                            upstream_seconds=time.monotonic() - attempt_started,
                            gateway_seconds=time.monotonic() - gateway_started
                        )
                        await record_billing_once(False, status_code=status_code)
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
                        from src.antigravity_pacing import note_success

                        note_success(current_file)
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
                    consume_usage_chunk("\n")
                if usage_attempt:
                    await usage_attempt.confirm(usage_metadata)
                mark_stage("usage_confirmed")
                log.debug(f"[ANTIGRAVITY STREAM] 流式响应完成，模型: {model_name}")
                mark_stage("billing_enter")
                await record_billing_once(True, status_code=200)
                return
            elif not need_retry:
                # 没有收到任何数据（空回复），需要重试
                log.warning(f"[ANTIGRAVITY STREAM] 收到空回复，无任何内容，凭证: {current_file}")
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
                if same_account_retry:
                    same_account_retry = False
                    log.warning(
                        "[ANTIGRAVITY STREAM] 代理绑定故障已修复，用同一账号重试: "
                        f"{current_file} / {model_name}"
                    )
                    attempt += 1
                    continue

                log.info(f"[ANTIGRAVITY STREAM] 重试请求 (attempt {attempt + 2}/{max_retries + 1})...")

                if lease_tracker is not None:
                    await lease_tracker.release()

                switched, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=(
                        min(retry_interval * (2 ** attempt), 30.0) * random.uniform(0.5, 1.5)
                        if not image_request and last_error_response is not None and last_error_response.status_code == 429
                        else retry_interval
                    ),
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY STREAM]",
                    stop_when_unavailable=not image_request and last_error_response is not None and last_error_response.status_code == 429,
                )
                if not switched:
                    log.error("[ANTIGRAVITY STREAM] 重试时无可用凭证或令牌")
                    return_upstream_error = last_error_response is not None and (
                        last_error_response.status_code == 503
                        or (not image_request and last_error_response.status_code == 429)
                    )
                    await record_billing_once(False, response=last_error_response if return_upstream_error else None)
                    await _alert_all_accounts_unavailable_if_needed(
                        model_name, attempted_credentials
                    )
                    if return_upstream_error:
                        yield last_error_response
                        return
                    yield Response(
                        content=json.dumps({"error": "当前无可用凭证"}),
                        status_code=500,
                        media_type="application/json"
                    )
                    return
                attempt += 1
                continue  # 重试

        except (CredentialSlotWaitTimeout, AccountUsageLimitReached) as exc:
            if slot_acquired:
                slot_acquired = False
                release_credential_slot(slot_holder)
                await release_dynamic_warmup_slot(slot_holder)
            await record_local_capture(slot_holder, model_name, reason=exc.rate_limit.reason)
            rate_limited_credentials.add(slot_holder)
            if exc.rate_limit.reason == "account_usage_limit":
                # A locally rejected candidate consumed no upstream attempt.
                if await refresh_credential_fast():
                    continue
            elif retry_config["retry_enabled"] and attempt < max_retries:
                log.info(
                    f"[PACING] 账号 {slot_holder} 本地槽位繁忙，尝试切换其他账号"
                )
                if await refresh_credential_fast():
                    attempt += 1
                    continue
            response = _credential_slot_backpressure_response(exc)
            await record_billing_once(False, response=response)
            yield response
            return
        except Exception as e:
            if last_error_response is not None and last_error_response.status_code in (403, 429):
                await record_billing_once(False, response=last_error_response)
                yield last_error_response
                return
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
                    await record_billing_once(False, response=last_error_response)
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
            if usage_attempt:
                if native and usage_remainder:
                    consume_usage_chunk("\n")
                await usage_attempt.confirm(usage_metadata)
            # 只有实际获取成功才归还，背压异常不能误释放其他请求的槽位
            if slot_acquired:
                release_credential_slot(slot_holder)
                await release_dynamic_warmup_slot(slot_holder)
            # 无论流被完整消费，还是因重试/返回/异常被中途放弃，都显式关闭上游流
            await aclose_quietly(stream, "[ANTIGRAVITY STREAM]")

    # 所有重试均已耗尽（while 循环正常结束），返回最后记录的错误
    log.error("[ANTIGRAVITY STREAM] 所有重试均失败")
    if last_error_response:
        await record_billing_once(False, response=last_error_response)
        await _alert_all_accounts_unavailable_if_needed(
            model_name, attempted_credentials
        )
        yield _upstream_403_to_503(last_error_response)
    else:
        await record_billing_once(False, status_code=502)
        yield Response(
            content=json.dumps({"error": "请求失败，所有重试均已耗尽"}),
            status_code=502,
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


@bind_claude_stream
async def stream_request(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    api_key_id: str = "env",
    request_model: Optional[str] = None,
):
    lease_tracker = _ImageCredentialLeaseTracker()
    inner = _stream_request_inner(
        body=body,
        native=native,
        headers=headers,
        api_key_id=api_key_id,
        lease_tracker=lease_tracker,
        request_model=request_model,
    )
    try:
        workflow = getattr(body.get('request', body), 'workflow_contract', None)
        if workflow is not None:
            from src.workflow_contracts import validate_workflow_response
            response = validate_workflow_response(await collect_streaming_response(inner), workflow)
            if response.status_code != 200:
                yield response
                return
            yield b'data: ' + response.body + b'\n\n'
            return
        async for item in inner:
            yield item
    except UnknownAntigravityModelError as exc:
        yield _model_enum_error_response(exc)
    finally:
        # 显式关闭内层生成器：消费者提前断开时立即归还并发槽位/上游流/图片 lease，
        # 不依赖 async generator 被 GC 终结的时机（否则槽位可能被短暂计为在飞）。
        await aclose_quietly(inner, "[ANTIGRAVITY STREAM]")
        # Releasing the consumer closes image leases on disconnect/cancellation.
        await lease_tracker.release()


async def _non_stream_image_request_inner(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]],
    api_key_id: str,
    lease_tracker: _ImageCredentialLeaseTracker,
    request_model: Optional[str] = None,
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
    region_rebinds: Dict[str, int] = {}  # 同一请求内该账号已为地区拒绝换过的出口 IP 数
    last_response: Optional[Response] = None
    last_was_capacity = False
    last_was_slot_backpressure = False
    final_payload = None
    last_request_id = f"image-{uuid.uuid4()}"
    billing_recorded = False
    from src.request_timing import mark_stage
    mark_stage("api_started")
    gateway_started = time.monotonic()  # 网关处理起点（时效性统计口径 A）

    async def record_billing_once(
        success: bool,
        credential_name: str,
        response_payload: Any = None,
        status_code: Optional[int] = None,
        *, response=None,
    ) -> None:
        nonlocal billing_recorded
        if billing_recorded:
            return
        billing_recorded = True
        try:
            recorder = await get_billing_recorder()
            await recorder.record(
                **_billing_response_fields(response, status_code, last_request_id, credential_name,
                                           requested_model, final_payload),
                usage_metadata=extract_usage_metadata(response_payload),
                success=success,
                api_key_id=api_key_id,
                task_type="image",
                gateway_seconds=time.monotonic() - gateway_started,
                upstream_model=extract_model_version(response_payload),
                request_model=request_model or requested_model,
            )
        except Exception as exc:
            log.warning(
                f"[BILLING] failed to record image request: {type(exc).__name__}"
            )

    account_attempt = 0
    while account_attempt < max_account_attempts:
        await lease_tracker.release()
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity",
            model_name=requested_model,
            exclude_filenames=attempted_credentials,
            image_request=True,
        )
        if not cred_result:
            if last_response is None:
                last_response = _selection_usage_rejection()
                last_was_slot_backpressure = last_response is not None
            break

        current_file, credential_data = cred_result
        await lease_tracker.activate(
            current_file, credential_data.pop("_image_lease_id", None)
        )
        if current_file in attempted_credentials:
            await lease_tracker.release()
            break
        attempted_credentials.add(current_file)
        account_attempt += 1
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
            credential_key=current_file,
        )
        bind_capture_credential(current_file, final_payload, credential_data)
        auth_headers = build_antigravity_headers(
            access_token,
            image_request=True,
            project_id=project_id,
            credential_data=credential_data,
            credential_name=current_file,
        )
        if headers:
            auth_headers.update(headers)

        # 图片请求同时受单账号在飞上限约束。
        from src.antigravity_pacing import (
            CredentialSlotWaitTimeout,
            acquire_limited_credential_slot,
            pace_credential,
            release_limited_credential_slot,
        )

        pacing_waited = 0.0

        started = time.monotonic()  # 兜底：槽位获取异常时 except 分支仍有耗时基准
        slot_acquired = False
        try:
            pacing_waited += await acquire_limited_credential_slot(current_file)
            slot_acquired = True
            pacing_waited += await pace_credential(current_file)
            started = time.monotonic()

            call_result = await _post_image_generate(
                headers=auth_headers,
                json_body=final_payload,
                proxy_url=get_effective_proxy_url(credential_data),
                timeout=300.0,
                request_id=last_request_id,
                credential_name=current_file,
            )
        except (CredentialSlotWaitTimeout, AccountUsageLimitReached) as exc:
            await record_local_capture(current_file, requested_model, reason=exc.rate_limit.reason)
            last_response = _credential_slot_backpressure_response(exc)
            last_response.rate_limit_attempt = {
                "request_id": last_request_id, "credential_name": current_file,
                "model": requested_model, "upstream_request": final_payload,
            }
            last_was_slot_backpressure = True
            if exc.rate_limit.reason == "account_usage_limit":
                account_attempt -= 1
            await lease_tracker.release()
            continue
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
        finally:
            # 只有实际获取成功才归还，背压异常不能误释放其他请求的槽位
            if slot_acquired:
                release_limited_credential_slot(current_file)
                await release_dynamic_warmup_slot(current_file)

        response = call_result.response
        last_was_slot_backpressure = False
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
            f"upstream_calls={len(call_result.attempts)}, elapsed={elapsed:.3f}s, "
            f"queue={pacing_waited:.3f}s"
        )

        if status_code == 200:
            from src.antigravity_pacing import note_success

            note_success(current_file)
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
            await record_billing_once(True, current_file, response_payload, status_code=200)
            return Response(
                content=response.content,
                status_code=200,
                headers=get_decoded_response_headers(response.headers),
            )

        last_response = Response(
            content=response.content,
            status_code=status_code,
            headers=get_decoded_response_headers(response.headers),
        )
        _mark_upstream_429(last_response, error_text, request_id=last_request_id, credential_name=current_file,
                           model=requested_model, upstream_request=final_payload)
        last_was_capacity = is_image_model_capacity_exhausted(
            status_code, error_text
        )
        if status_code == 503:
            await hold_transient_account(credential_manager, status_code, current_file)
        if last_was_capacity and status_code != 429:
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
            cooldown_until = await _resolve_error_cooldown(
                status_code, error_text, current_file,
                headers=getattr(response, "headers", None),
            )
        if status_code == 403:
            classified_403 = await _handle_antigravity_403(
                credential_name=current_file,
                credential_data=credential_data,
                model_name=requested_model,
                error_text=error_text,
            )
            cooldown_until = classified_403.cooldown_until
        if (
            classified_403 is not None
            and classified_403.rebind_retry
            and region_rebinds.get(current_file, 0) < _REGION_REBIND_LIMIT
        ):
            fresh = await _retry_same_account_on_new_egress(
                current_file, credential_data
            )
            if fresh:
                region_rebinds[current_file] = (
                    region_rebinds.get(current_file, 0) + 1
                )
                attempted_credentials.discard(current_file)
                log.warning(
                    "[ANTIGRAVITY IMAGE] 代理绑定故障已修复，重新进入候选: "
                    f"{current_file} / {requested_model}"
                )
                account_attempt += 1
                continue

        retryable = status_code not in (400, 403, 429) and (
            status_code in (404, 500, 503) or status_code in disable_error_codes
        )
        will_retry = retry_enabled and retryable and account_attempt < max_account_attempts
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

        if will_retry and status_code == 503:
            await asyncio.sleep(retry_config["retry_interval"])

        if not retry_enabled or not retryable:
            break

    billing_credential = next(iter(attempted_credentials), "unknown")
    await record_billing_once(
        False,
        billing_credential,
        response=last_response,
    )
    if last_response is not None:
        if last_was_slot_backpressure:
            return last_response
        if not last_was_capacity:
            await _alert_all_accounts_unavailable_if_needed(
                requested_model, attempted_credentials
            )
        return last_response

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
    request_model: Optional[str] = None,
) -> Response:
    lease_tracker = _ImageCredentialLeaseTracker()
    try:
        return await _non_stream_image_request_inner(
            body, headers, api_key_id, lease_tracker, request_model=request_model
        )
    finally:
        # Covers success, errors, timeouts, task cancellation and client disconnects.
        await lease_tracker.release()


def _convert_unknown_model_errors(func):
    from functools import wraps

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except UnknownAntigravityModelError as exc:
            return _model_enum_error_response(exc)

    return wrapper


@_convert_unknown_model_errors
@bind_claude_request
async def non_stream_request(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    api_key_id: str = "env",
    request_model: Optional[str] = None,
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
    original_model = request_model or model_name  # Preserve the pre-normalization client model.
    inner_request = body.get("request", body)
    if is_antigravity_image_request(model_name, inner_request):
        return await _non_stream_image_request(body, headers, api_key_id, request_model=original_model)

    # 检查是否启用流式收集模式
    if await get_antigravity_stream2nostream():
        log.debug("[ANTIGRAVITY] 使用流式收集模式实现非流式请求")

        # 调用stream_request获取流
        stream = stream_request(
            body=body, native=False, headers=headers, api_key_id=api_key_id,
            request_model=original_model,
        )

        # 收集流式响应
        # stream_request是一个异步生成器，可能yield Response（错误）或流数据
        # collect_streaming_response会自动处理这两种情况
        return await collect_streaming_response(stream)

    # 否则使用传统非流式模式
    log.debug("[ANTIGRAVITY] 使用传统非流式模式")
    from src.request_timing import database_timing_scope, mark_stage, measure_phase
    mark_stage("api_started")
    gateway_started = time.monotonic()  # 网关处理起点（时效性统计口径 A）

    # 1. 获取有效凭证
    model_name, cred_result = await _select_text_credential(model_name)

    if not cred_result:
        # 文本号池暂时不可用返回429，不作为内部故障。
        log.error("[ANTIGRAVITY] 当前无可用凭证")
        await _alert_all_accounts_unavailable_if_needed(model_name)
        from src.call_monitor import record_gateway_rejection
        response = _account_pool_429()
        await record_gateway_rejection(response.rate_limit, api_key_id=api_key_id, model=model_name)
        return response

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

    auth_headers = build_antigravity_headers(
        access_token,
        credential_data=credential_data,
        credential_name=current_file,
    )

    # 合并自定义headers
    if headers:
        auth_headers.update(headers)

    # 构建 CLI 格式请求体
    inner_request = body.get("request", body)
    final_payload, request_id = await wrap_cli_request(
        inner_request, model_name, project_id,
        user_email=credential_data.get("user_email"),
        credential_key=current_file,
        credential_data=credential_data,
    )

    # 3. 调用post_async进行请求
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_response = None  # 记录最后一次的错误响应
    next_cred_task = None  # 预热的下一个凭证任务
    attempted_credentials: set[str] = set()
    rate_limited_credentials: set[str] = set()  # 当前模型上429失败或发送前已冷却的凭证
    region_rebinds: Dict[str, int] = {}  # 同一请求内每个账号已为地区拒绝换过的出口 IP 数
    usage_metadata = None
    upstream_model = None  # 上游响应 modelVersion：真实执行模型
    billing_recorded = False

    async def record_billing_once(
        success: bool,
        response_payload: Any = None,
        status_code: Optional[int] = None,
        *, response=None,
    ):
        nonlocal billing_recorded, usage_metadata, upstream_model
        if billing_recorded:
            return
        billing_recorded = True
        if response_payload is not None:
            usage_metadata = extract_usage_metadata(response_payload) or usage_metadata
            upstream_model = extract_model_version(response_payload) or upstream_model
        try:
            recorder = await get_billing_recorder()
            await recorder.record(
                **_billing_response_fields(response, status_code, request_id, current_file, model_name, final_payload),
                usage_metadata=usage_metadata,
                success=success,
                api_key_id=api_key_id,
                gateway_seconds=time.monotonic() - gateway_started,
                upstream_model=upstream_model,
                request_model=original_model,
            )
        except Exception as exc:
            log.warning(f"[BILLING] failed to record non-stream request: {type(exc).__name__}")

    async def refresh_credential_fast():
        nonlocal current_file, credential_data, access_token, auth_headers, project_id, final_payload, proxy_url, request_id
        with measure_phase("credential_lookup"), database_timing_scope("credential"):
            cred_result = await credential_manager.get_valid_credential(
                mode="antigravity", model_name=model_name,
                exclude_filenames=set(rate_limited_credentials),
            )
        if not cred_result:
            return None
        new_file, new_data = cred_result
        if new_file in rate_limited_credentials:
            return None
        new_access_token = new_data.get("access_token") or new_data.get("token")
        new_project_id = new_data.get("project_id", "")
        if not new_access_token:
            return None
        new_payload, new_request_id = await wrap_cli_request(
            inner_request,
            model_name,
            new_project_id,
            user_email=new_data.get("user_email"),
            credential_key=new_file,
            credential_data=new_data,
        )
        current_file, credential_data = new_file, new_data
        access_token, project_id = new_access_token, new_project_id
        proxy_url = get_effective_proxy_url(new_data)
        final_payload, request_id = new_payload, new_request_id
        auth_headers["Authorization"] = f"Bearer {new_access_token}"
        return True

    async def apply_cred_result(
        cred_result: Tuple[str, Dict[str, Any]], *, rebuild_payload: bool = True
    ) -> bool:
        nonlocal current_file, credential_data, access_token, project_id, auth_headers, final_payload, proxy_url, request_id
        new_file, new_data = cred_result
        if new_file in rate_limited_credentials:
            return False
        new_access_token = new_data.get("access_token") or new_data.get("token")
        new_project_id = new_data.get("project_id", "")
        if not new_access_token or not new_project_id:
            return False
        if rebuild_payload:
            new_payload, new_request_id = await wrap_cli_request(
                inner_request,
                model_name,
                new_project_id,
                user_email=new_data.get("user_email"),
                credential_key=new_file,
                credential_data=new_data,
            )
            final_payload, request_id = new_payload, new_request_id
        current_file, credential_data = new_file, new_data
        access_token, project_id = new_access_token, new_project_id
        proxy_url = get_effective_proxy_url(new_data)
        auth_headers["Authorization"] = f"Bearer {new_access_token}"
        if not rebuild_payload:
            final_payload["project"] = new_project_id
        return True

    attempt = 0
    while attempt <= max_retries:
        need_retry = False  # 标记是否需要重试

        # 普通文本按轮转取得账号，只执行全池准入和养号检查
        from src.antigravity_pacing import (
            CredentialSlotWaitTimeout,
            acquire_credential_slot,
            release_credential_slot,
        )

        slot_holder = current_file  # 槽位归属固定为本轮尝试的账号，重试换号不影响释放
        pacing_waited = 0.0

        slot_acquired = False
        try:
            with measure_phase("slot_acquire"):
                pacing_waited += await acquire_credential_slot(slot_holder)
            slot_acquired = True
            if (
                current_file in rate_limited_credentials
                or not await _text_credential_is_available(current_file, model_name)
            ):
                log.info(f"[ANTIGRAVITY] 跳过预选后已冷却或429失败的凭证: {current_file} / {model_name}")
                rate_limited_credentials.add(current_file)
                if retry_config["retry_enabled"] and attempt < max_retries and await refresh_credential_fast():
                    attempt += 1
                    continue
                response = _upstream_403_to_503(last_error_response) if last_error_response is not None else _account_pool_429()
                await record_billing_once(False, response=response)
                return response
            with measure_phase("usage_admission"):
                usage_attempt = await begin_account_usage(slot_holder)
            attempted_credentials.add(current_file)
            attempt_started = time.monotonic()  # 本次上游尝试起点（时效性统计口径 B）
            if pacing_waited >= 0.2:
                log.debug(
                    f"[PACING] 本次上游尝试前置等待 {pacing_waited:.2f}s（未计入上游耗时）"
                )

            mark_stage("post_enter")
            response = await post_async(
                url=target_url,
                json=final_payload,
                headers=auth_headers,
                proxy_url=proxy_url,
                transport="antigravity",
                timeout=300.0
            )
            mark_stage("post_exit")
            upstream_elapsed = time.monotonic() - attempt_started
            if usage_attempt and response.status_code not in (403, 429):
                await usage_attempt.confirm_response(response)
            mark_stage("usage_confirmed")

            status_code = response.status_code

            # 成功
            if status_code == 200:
                # 检查是否为空回复
                if not response.content or len(response.content) == 0:
                    log.warning(f"[ANTIGRAVITY] 收到200响应但内容为空，凭证: {current_file}")
                    
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
                    from src.antigravity_pacing import note_success

                    note_success(current_file)
                    await record_api_call_success(
                        credential_manager, current_file, mode="antigravity", model_name=model_name,
                        upstream_seconds=upstream_elapsed,
                        gateway_seconds=time.monotonic() - gateway_started
                    )
                    mark_stage("credential_saved")
                    mark_stage("billing_enter")
                    await record_billing_once(True, _safe_response_json(response) if response.content else None, status_code=200)
                    downstream = Response(
                        content=response.content,
                        status_code=200,
                        headers=get_decoded_response_headers(response.headers)
                    )
                    workflow = getattr(inner_request, 'workflow_contract', None)
                    if workflow is not None:
                        from src.workflow_contracts import validate_workflow_response
                        return validate_workflow_response(downstream, workflow)
                    return downstream

            # 失败 - 记录最后一次错误
            if status_code != 200:
                last_error_response = Response(
                    content=response.content,
                    status_code=status_code,
                    headers=get_decoded_response_headers(response.headers)
                )

                # 判断是否需要重试
                # 缓存错误文本,避免重复解析
                error_text = ""
                try:
                    error_text = response.text
                except Exception:
                    pass
                _mark_upstream_429(last_error_response, error_text, request_id=request_id,
                                   credential_name=current_file, model=model_name, upstream_request=final_payload)
                if status_code == 503:
                    # 与流式路径一致：503 先冷藏账号，再决定是否换号重试。
                    await hold_transient_account(
                        credential_manager, status_code, current_file
                    )

                if status_code == 403 or _is_retryable_status(status_code, DISABLE_ERROR_CODES):
                    log.warning(f"[ANTIGRAVITY] 非流式请求失败 (status={status_code}), 凭证: {current_file}, traceId: {_upstream_trace_id(getattr(response, 'headers', None)) or '无'}")

                    # 解析冷却时间（明确重置时间优先，其余 429 只短冷却）
                    cooldown_until = None
                    if status_code == 429 or status_code == 503:
                        cooldown_until = await _resolve_error_cooldown(
                            status_code, error_text, current_file,
                            headers=getattr(response, "headers", None),
                        )

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
                        or status_code in (403, 429)
                    )
                    await record_api_call_error(
                        credential_manager, current_file, status_code,
                        cooldown_until, mode="antigravity", model_name=model_name,
                        error_message=error_text,
                        upstream_seconds=upstream_elapsed,
                        gateway_seconds=(time.monotonic() - gateway_started) if ns_final_failure else None
                    )

                    if (
                        classified_403 is not None
                        and classified_403.rebind_retry
                        and region_rebinds.get(current_file, 0) < _REGION_REBIND_LIMIT
                    ):
                        fresh = await _retry_same_account_on_new_egress(
                            current_file, credential_data
                        )
                        if fresh and await apply_cred_result(
                            (current_file, fresh), rebuild_payload=False
                        ):
                            region_rebinds[current_file] = (
                                region_rebinds.get(current_file, 0) + 1
                            )
                            attempted_credentials.discard(current_file)
                            rate_limited_credentials.discard(current_file)
                            log.warning(
                                "[ANTIGRAVITY] 代理绑定故障已修复，用同一账号重试: "
                                f"{current_file} / {model_name}"
                            )
                            attempt += 1
                            continue

                    if status_code in (403, 429):
                        if status_code == 429 and await check_should_auto_ban(status_code):
                            await handle_auto_ban(credential_manager, status_code, current_file, mode="antigravity")
                        await record_billing_once(False, response=last_error_response)
                        await _alert_all_accounts_unavailable_if_needed(model_name, attempted_credentials)
                        return last_error_response

                    should_retry = await handle_error_with_retry(
                        credential_manager, status_code, current_file,
                        retry_config["retry_enabled"], attempt, max_retries, retry_interval,
                        mode="antigravity",
                    )

                    if should_retry and next_cred_task is None and attempt < max_retries:
                        next_cred_task = asyncio.create_task(
                            credential_manager.get_valid_credential(
                                mode="antigravity", model_name=model_name,
                                exclude_filenames=set(rate_limited_credentials),
                            )
                        )

                    if should_retry and attempt < max_retries:
                        need_retry = True
                    else:
                        # 不重试，直接返回原始错误（上游 403 改写为 503）
                        log.error(f"[ANTIGRAVITY] 达到最大重试次数或不应重试，返回原始错误")
                        await record_billing_once(False, response=last_error_response)
                        await _alert_all_accounts_unavailable_if_needed(
                            model_name, attempted_credentials
                        )
                        return _upstream_403_to_503(last_error_response)
                else:
                    # 错误码不在禁用码当中，直接返回，无需重试
                    log.error(f"[ANTIGRAVITY] 非流式请求失败，非重试错误码 (status={status_code}), 凭证: {current_file}, traceId: {_upstream_trace_id(getattr(response, 'headers', None)) or '无'}")
                    await record_api_call_error(
                        credential_manager, current_file, status_code,
                        None, mode="antigravity", model_name=model_name,
                        error_message=error_text,
                        upstream_seconds=upstream_elapsed,
                        gateway_seconds=time.monotonic() - gateway_started
                    )
                    await record_billing_once(False, _safe_response_json(response) if response.content else None, status_code=status_code)
                    await _alert_all_accounts_unavailable_if_needed(
                        model_name, attempted_credentials
                    )
                    return last_error_response
            
            # 统一处理重试
            if need_retry:
                log.info(f"[ANTIGRAVITY] 重试请求 (attempt {attempt + 2}/{max_retries + 1})...")

                switched, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=(
                        min(retry_interval * (2 ** attempt), 30.0) * random.uniform(0.5, 1.5)
                        if last_error_response is not None and last_error_response.status_code == 429
                        else retry_interval
                    ),
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY]",
                    stop_when_unavailable=last_error_response is not None and last_error_response.status_code == 429,
                )
                if not switched:
                    log.error("[ANTIGRAVITY] 重试时无可用凭证或令牌")
                    await record_billing_once(False, response=last_error_response if (
                        last_error_response is not None and last_error_response.status_code in (429, 503)
                    ) else None)
                    await _alert_all_accounts_unavailable_if_needed(
                        model_name, attempted_credentials
                    )
                    if last_error_response is not None and last_error_response.status_code in (429, 503):
                        return last_error_response
                    return Response(
                        content=json.dumps({"error": "当前无可用凭证"}),
                        status_code=500,
                        media_type="application/json"
                    )
                attempt += 1
                continue  # 重试

        except (CredentialSlotWaitTimeout, AccountUsageLimitReached) as exc:
            if slot_acquired:
                slot_acquired = False
                release_credential_slot(slot_holder)
                await release_dynamic_warmup_slot(slot_holder)
            await record_local_capture(slot_holder, model_name, reason=exc.rate_limit.reason)
            rate_limited_credentials.add(slot_holder)
            if exc.rate_limit.reason == "account_usage_limit":
                # A locally rejected candidate consumed no upstream attempt.
                if await refresh_credential_fast():
                    continue
            elif retry_config["retry_enabled"] and attempt < max_retries:
                log.info(
                    f"[PACING] 账号 {slot_holder} 本地槽位繁忙，尝试切换其他账号"
                )
                if await refresh_credential_fast():
                    attempt += 1
                    continue
            response = _credential_slot_backpressure_response(exc)
            await record_billing_once(False, response=response)
            return response
        except Exception as e:
            if last_error_response is not None and last_error_response.status_code in (403, 429):
                await record_billing_once(False, response=last_error_response)
                return last_error_response
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
                    await record_billing_once(False, response=last_error_response)
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
        finally:
            # 只有实际获取成功才归还，背压异常不能误释放其他请求的槽位
            if slot_acquired:
                release_credential_slot(slot_holder)
                await release_dynamic_warmup_slot(slot_holder)

    # 所有重试都失败，返回最后一次的原始错误（如果有）或500错误
    log.error("[ANTIGRAVITY] 所有重试均失败")
    if last_error_response:
        await record_billing_once(False, response=last_error_response)
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

# 轻量端点可降级的状态码：408/5xx 或连接异常时尝试下一个 host；其他 4xx 不降级
_HOST_FALLBACK_STATUSES = (408,)


async def _post_with_host_fallback(
    path: str,
    headers: Dict[str, str],
    json_body: Optional[Dict[str, Any]] = None,
    proxy_url: Any = ...,
    timeout: Optional[float] = None,
):
    """
    轻量端点三 host 降级 POST（sandbox → daily → prod，仅默认配置时启用）。

    遇到 408/5xx 或连接异常时按顺序尝试下一个 host；其他 4xx
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
                "transport": "antigravity",
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
    credential_name: str = "",
) -> ImageUpstreamResult:
    """Run image generation on the configured AGY host with capacity backoff."""
    host = (await get_antigravity_api_url()).rstrip("/")
    attempts: List[ImageUpstreamAttempt] = []
    for capacity_attempt in range(IMAGE_CAPACITY_MAX_RETRIES + 1):
        usage_attempt = await begin_account_usage(credential_name)
        started = time.monotonic()
        try:
            response = await post_async(
                url=f"{host}/v1internal:generateContent",
                json=json_body,
                headers=headers,
                proxy_url=proxy_url,
                transport="antigravity",
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
        if usage_attempt and response.status_code not in (403, 429):
            await usage_attempt.confirm_response(response)
        attempts.append(
            ImageUpstreamAttempt(
                host=host,
                status_code=response.status_code,
                elapsed_seconds=elapsed,
            )
        )
        error_text = response.text if response.status_code != 200 else ""
        if response.status_code == 429 or not is_image_model_capacity_exhausted(response.status_code, error_text):
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

    # 构建请求头（沿用该账号画像，保持与生成请求同一份 UA）
    headers = build_antigravity_headers(
        access_token,
        credential_data=credential_data,
        credential_name=current_file,
    )

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

            # 热更新模型目录（labels.model_enum 枚举名 / generationConfig 默认值）
            from src.antigravity_model_catalog import update_from_catalog
            from src.antigravity_quota_guard import update_quota_snapshot

            update_from_catalog(data.get("models"))
            update_quota_snapshot(current_file, data.get("models"))

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
            log.error(f"[ANTIGRAVITY] Failed to fetch models: HTTP {response.status_code}")
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
    *,
    credential_data: Optional[Mapping[str, Any]] = None,
    credential_name: Optional[str] = None,
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

    headers = build_antigravity_headers(
        access_token,
        credential_data=credential_data,
        credential_name=credential_name,
    )

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
            log.error(f"[ANTIGRAVITY QUOTA] Failed to fetch quota: HTTP {response.status_code}")
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


# loadCodeAssist paidTier 的「降档」档位名：付费资格被 Google 回收的账号，
# paidTier 会显示为该名称（实测表现：仅剩周限额、没有 5h 窗口、Argon 被禁用）。
ANTIGRAVITY_STARTER_QUOTA_NAME = "Antigravity Starter Quota"


async def fetch_paid_tier_info(
    access_token: str,
    proxy_url: Any = ...,
    *,
    credential_data: Optional[Mapping[str, Any]] = None,
    credential_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    调用 v1internal:loadCodeAssist 获取账号实时付费档位（paidTier）。

    背景：凭证导入时持久化的 tier 是快照；账号被 Google 降档后（paidTier 变成
    "Antigravity Starter Quota"）面板仍显示旧档位，会误导排查。额度查询时
    顺带用本函数校准。

    Returns:
        {"id": paidTier.id, "name": paidTier.name, "degraded": bool}；
        请求失败或响应中没有 paidTier 时返回 None（best-effort，不影响额度查询）。
    """
    try:
        headers = build_antigravity_headers(
            access_token,
            credential_data=credential_data,
            credential_name=credential_name,
        )
        response = await _post_with_host_fallback(
            "/v1internal:loadCodeAssist",
            headers,
            json_body={"metadata": {"ideType": "ANTIGRAVITY"}},
            proxy_url=proxy_url,
            timeout=30.0,
        )
        if response.status_code != 200:
            log.warning(
                f"[ANTIGRAVITY TIER] loadCodeAssist failed ({response.status_code}): {response.text[:200]}"
            )
            return None

        data = response.json()
        paid_tier = data.get("paidTier")
        if not isinstance(paid_tier, dict):
            return None
        tier_id = paid_tier.get("id")
        name = paid_tier.get("name")
        if not tier_id and not name:
            return None

        degraded = name == ANTIGRAVITY_STARTER_QUOTA_NAME
        if degraded:
            log.info(f"[ANTIGRAVITY TIER] 账号已被降档为 {ANTIGRAVITY_STARTER_QUOTA_NAME}")
        # 客户端 bootstrap 序列（fetchAdminControls / aicode:fetchConfig）。
        # 真实客户端启动时会先打这两类端点；按账号每天一次去重，best-effort。
        try:
            from src.antigravity_client_bootstrap import bootstrap_account_session

            await bootstrap_account_session(
                credential_name=credential_name or "",
                access_token=access_token,
                project_id=str((credential_data or {}).get("project_id") or ""),
                proxy_url=proxy_url,
                credential_data=dict(credential_data or {}),
            )
        except Exception as exc:  # noqa: BLE001 - bootstrap 不得影响档位查询
            log.debug(f"[ANTIGRAVITY TIER] bootstrap 跳过: {type(exc).__name__}: {exc}")
        return {"id": tier_id, "name": name, "degraded": degraded}
    except Exception as e:
        log.warning(f"[ANTIGRAVITY TIER] loadCodeAssist error: {e}")
        return None


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

async def _resolve_error_cooldown(
    status_code: int,
    error_body: Optional[str],
    credential_name: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> Optional[float]:
    """取正文配额重置与 Retry-After 中较晚的有效未来时间。

    Retry-After 支持非负整数秒和 HTTP-date；无有效上游时间的 429
    （包括 INSUFFICIENT_G1_CREDITS_BALANCE）沿用 60s→300s 连击冷却。
    503 没有有效上游时间时仍不设置冷却。
    """
    now = time.time()
    cooldown_until = None
    if error_body:
        try:
            deadline = await parse_and_log_cooldown(error_body, mode="antigravity")
            if (
                isinstance(deadline, (int, float))
                and not isinstance(deadline, bool)
                and math.isfinite(deadline)
                and deadline > now
            ):
                cooldown_until = deadline
        except Exception:
            pass

    if headers:
        try:
            value = next(
                (value for key, value in headers.items() if key.lower() == "retry-after"),
                "",
            ).strip()
            if value and value.isascii() and value.isdigit():
                deadline = now + float(value)
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                deadline = date.timestamp()
            if math.isfinite(deadline) and deadline > now:
                cooldown_until = max(cooldown_until or deadline, deadline)
        except (AttributeError, TypeError, ValueError, OverflowError):
            # 无效/过期上游时间不能抑制原有退避，也不能覆盖正文中的有效时间。
            pass

    if cooldown_until is None and status_code == 429:
        if credential_name:
            from src.antigravity_pacing import transient_429_cooldown

            cooldown_until = transient_429_cooldown(credential_name)
        else:
            cooldown_until = time.time() + RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS
        marker = (
            f"（包含 {CREDITS_EXHAUSTED_MARKER}）"
            if error_body and CREDITS_EXHAUSTED_MARKER in error_body
            else ""
        )
        log.info(
            f"[ANTIGRAVITY] 429{marker} 未携带明确配额重置时间，按瞬时限流处理，"
            f"冷却至 {datetime.fromtimestamp(cooldown_until).strftime('%H:%M:%S')}"
            + (f"（凭证 {credential_name}）" if credential_name else "")
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
