"""Conservative classification for Antigravity 403/503 responses."""

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional


# 上游 503 里的模型级容量标记：cloudcode-pa 返回
#   status=UNAVAILABLE, reason=MODEL_CAPACITY_EXHAUSTED,
#   details=[ErrorInfo{model,...}, RetryInfo{retryDelay:"40s"}]
# 这是「该模型此刻全局没有容量」，与具体凭证无关，几十秒后自愈。
MODEL_CAPACITY_MARKER = "MODEL_CAPACITY_EXHAUSTED"


def is_model_capacity_exhausted(status_code: int, error_text: Optional[str]) -> bool:
    """503 且分类为模型级容量耗尽（不归咎账号）。

    生产实测响应体（2026-11-24/26 gemini-2.5-flash-lite）：
    ``{"error":{"code":503,"status":"UNAVAILABLE","message":"No capacity available
    for model ...","details":[{"reason":"MODEL_CAPACITY_EXHAUSTED","metadata":
    {"error_number":"2010","model":"..."}},{"@type":"...RetryInfo",
    "retryDelay":"39s"}]}}``
    同一分钟内多个不同账号会收到完全相同的 503，说明这是上游窗口而非账号问题。

    判定逻辑收敛到 ``classify_antigravity_503``：reason 权威，error_number 与
    message 兜底，避免只看一个子串就决定要不要重试。
    """
    return classify_antigravity_503(
        status_code, error_text
    ).category == CATEGORY_MODEL_CAPACITY


@dataclass(frozen=True)
class Antigravity403Decision:
    """Actionable result of a 403 classification."""

    category: str
    disable_account: bool = False
    tos_violation: bool = False
    invalidate_binding: bool = False
    cooldown_model: bool = False
    retry_after_rebind: bool = False


_LOCATION_MARKERS = (
    "not currently available in your location",
    "not available in your location",
    "unsupported location",
    "location is not supported",
    "unsupported country",
    "region is not supported",
    "country is not supported",
    "location restriction",
    "geo restriction",
    "geographic restriction",
    # 上游以机器码形式返回地区拒绝（生产实测 403 体含 UNSUPPORTED_LOCATION）。
    # 归一化只做小写和空白折叠，下划线代码必须显式列出，否则会被当成未知 403。
    "unsupported_location",
    "unsupported_region",
    "location_not_supported",
    "location_restricted",
    "location_unsupported",
    "region_not_supported",
    "unsupported_country",
)

_MODEL_MARKERS = (
    "model is not available",
    "model is not permitted",
    "model access",
    "does not have access to model",
    "not allowed to use model",
)

# 账号需要额外验证（VALIDATION_REQUIRED / 验证链接），临时性问题，不应封禁账号
_VALIDATION_MARKERS = (
    "validation_required",
    "verify your account",
    "validation_url",
)

_ACCOUNT_PERMISSION_MARKERS = (
    "authenticated account does not have permission",
    "account does not have permission",
    "account is not permitted",
    "account has been disabled",
    "account has been suspended",
    "access has been revoked",
)

# 明确的条款封禁：账号被 Google 永久停用（TOS_VIOLATION）。
# 这类 403 的响应文本本身就是权威结论，不依赖缓存的健康状态，
# 否则账号会被反复选中又反复 403（曾出现单账号 10 小时吞掉全部流量）。
_TOS_VIOLATION_MARKERS = (
    "violation of terms of service",
    "tos_violation",
    "service has been disabled in this account",
    "submit an appeal",
)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


# ==================== 503 分类 ====================
#
# 503 不是一个东西。线上取到的正文（2026-11-26，17 个账号同一分钟内完全一致）：
#
#   {"error": {"code": 503, "status": "UNAVAILABLE",
#              "message": "No capacity available for model gemini-2.5-flash-lite
#                          on the server",
#              "details": [{"@type": ".../ErrorInfo", "reason":
#                           "MODEL_CAPACITY_EXHAUSTED",
#                           "domain": "cloudcode-pa.googleapis.com",
#                           "metadata": {"error_number": "2010",
#                                        "model": "gemini-2.5-flash-lite"}},
#                          {"@type": ".../RetryInfo", "retryDelay": "39s"}]}}
#
# 只有第一种（模型级容量）值得原号再试一次；配额型要给冷却并换号；服务级
# 不可用与无法识别的正文按账号级故障处理，不做盲目重试。

CATEGORY_MODEL_CAPACITY = "model_capacity"
CATEGORY_QUOTA = "quota"
CATEGORY_MODEL_OVERLOADED = "model_overloaded"
CATEGORY_SERVICE_UNAVAILABLE = "service_unavailable"
CATEGORY_UNKNOWN = "unknown"

# 模型级容量：reason 是权威标记，error_number/message 用于 reason 缺失时兜底。
_CAPACITY_REASONS = frozenset({"MODEL_CAPACITY_EXHAUSTED"})
_CAPACITY_ERROR_NUMBERS = frozenset({"2010"})
_CAPACITY_MESSAGE_MARKERS = (
    "no capacity available for model",
    "no capacity available",
)
# 配额型 503：带明确重置信息的，走冷却而不是重试。
_QUOTA_REASONS = frozenset({
    "QUOTA_EXHAUSTED",
    "RESOURCE_EXHAUSTED",
    "RATE_LIMIT_EXCEEDED",
    "INSUFFICIENT_G1_CREDITS_BALANCE",
})
_QUOTA_MESSAGE_MARKERS = ("quota", "rate limit", "resets in")
# 模型过载：同样是全局窗口，但线上暂无样本，先按服务级处理，不盲目原号重试。
_OVERLOADED_REASONS = frozenset({"MODEL_OVERLOADED", "MODEL_BUSY"})
_OVERLOADED_MESSAGE_MARKERS = ("overloaded", "model is busy", "high demand")


@dataclass(frozen=True)
class Antigravity503Decision:
    """一个 503 正文该触发什么动作。"""

    category: str
    marker: str = ""
    reason: str = ""
    error_number: str = ""
    upstream_model: str = ""
    upstream_delay_seconds: Optional[float] = None
    retry_same_account: bool = False
    hold_account: bool = False
    write_cooldown: bool = True

    def describe(self) -> str:
        """一行排障文本，进日志用。"""
        parts = [f"category={self.category}"]
        if self.marker:
            parts.append(f"marker={self.marker}")
        if self.reason:
            parts.append(f"reason={self.reason}")
        if self.error_number:
            parts.append(f"error_number={self.error_number}")
        if self.upstream_model:
            parts.append(f"model={self.upstream_model}")
        if self.upstream_delay_seconds is not None:
            parts.append(f"retryDelay={self.upstream_delay_seconds:g}s")
        return ", ".join(parts)


def _503_details(error_response: Any) -> tuple[dict, dict, Optional[float]]:
    """拆出 ErrorInfo 元数据、RetryInfo 建议等待。"""
    error = error_response.get("error") if isinstance(error_response, dict) else None
    if not isinstance(error, dict):
        return {}, {}, None
    metadata: dict = {}
    delay: Optional[float] = None
    details = error.get("details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            detail_type = str(detail.get("@type") or "")
            if detail_type.endswith("google.rpc.RetryInfo"):
                raw_delay = detail.get("retryDelay")
                if isinstance(raw_delay, (int, float)):
                    delay = float(raw_delay)
                elif isinstance(raw_delay, str) and raw_delay.endswith("s"):
                    try:
                        delay = float(raw_delay[:-1])
                    except ValueError:
                        delay = None
                continue
            # reason/metadata 不要求 @type：真实正文与历史样本里都有省略类型的写法，
            # 只认 @type 会把 MODEL_CAPACITY_EXHAUSTED 漏判成服务级 503。
            info_metadata = detail.get("metadata")
            if isinstance(info_metadata, dict):
                metadata.update(info_metadata)
            detail_reason = str(detail.get("reason") or "")
            if detail_reason and not metadata.get("reason"):
                metadata["reason"] = detail_reason
    return metadata, error, delay


def classify_antigravity_503(
    status_code: Any,
    error_text: Optional[str],
) -> Antigravity503Decision:
    """按正文把 503 分成四类，只有模型级容量允许原号再试一次。

    判定顺序：容量 → 配额 → 模型过载 → 服务级/未知。结构化字段（ErrorInfo.reason、
    metadata.error_number）优先于纯文本，正文无法解析时退化为关键字匹配。
    """
    try:
        code = int(status_code or 0)
    except (TypeError, ValueError):
        code = 0
    if code != 503:
        return Antigravity503Decision(
            category=CATEGORY_UNKNOWN,
            retry_same_account=False,
            hold_account=False,
            write_cooldown=True,
        )

    text = str(error_text or "")
    lowered = " ".join(text.lower().split())
    reason = ""
    error_number = ""
    upstream_model = ""
    delay: Optional[float] = None

    parsed = None
    unparsed_body = False
    if text.strip().startswith("{"):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            # 看起来是 JSON 但拆不开：绝不当成容量型去重试，标记出来便于排障。
            unparsed_body = True
            parsed = None
    if isinstance(parsed, dict):
        metadata, error, delay = _503_details(parsed)
        reason = str(metadata.get("reason") or "").upper()
        error_number = str(metadata.get("error_number") or "")
        upstream_model = str(metadata.get("model") or "")
        if not reason:
            reason = str(error.get("reason") or "").upper()

    # Known structured reasons win over numeric hints and message wording.
    # Use legacy text matching only when no recognized reason is available.
    known_reason = reason in (_CAPACITY_REASONS | _QUOTA_REASONS | _OVERLOADED_REASONS)
    text_upper = text.upper()
    capacity_hit = reason in _CAPACITY_REASONS
    quota_hit = reason in _QUOTA_REASONS
    overloaded_hit = reason in _OVERLOADED_REASONS
    if not known_reason:
        capacity_hit = (
            error_number in _CAPACITY_ERROR_NUMBERS
            or any(marker in text_upper for marker in _CAPACITY_REASONS)
            or _contains_any(lowered, _CAPACITY_MESSAGE_MARKERS)
        )
        quota_hit = (any(marker in text_upper for marker in _QUOTA_REASONS)
                     or _contains_any(lowered, _QUOTA_MESSAGE_MARKERS))
        overloaded_hit = (any(marker in text_upper for marker in _OVERLOADED_REASONS)
                          or _contains_any(lowered, _OVERLOADED_MESSAGE_MARKERS))

    if capacity_hit:
        if reason in _CAPACITY_REASONS:
            marker = "reason"
        elif error_number in _CAPACITY_ERROR_NUMBERS:
            marker = "error_number"
        elif _contains_any(lowered, _CAPACITY_MESSAGE_MARKERS):
            marker = "message"
        else:
            marker = "text"
        return Antigravity503Decision(
            category=CATEGORY_MODEL_CAPACITY,
            marker=marker,
            reason=reason,
            error_number=error_number,
            upstream_model=upstream_model,
            upstream_delay_seconds=delay,
            retry_same_account=True,
            hold_account=False,
            # 不写冷却、不锁账号：只由调用方把这个号在轮转队列里往后挪，
            # 软降权到「仍可能被选中，但不频繁」，而不是整段跳过。
            write_cooldown=False,
        )

    # 2) 配额型：有明确重置信息，冷却换号，不做同号重试。
    if quota_hit:
        return Antigravity503Decision(
            category=CATEGORY_QUOTA,
            marker="reason" if reason in _QUOTA_REASONS else "text",
            reason=reason,
            error_number=error_number,
            upstream_model=upstream_model,
            upstream_delay_seconds=delay,
            retry_same_account=False,
            hold_account=False,
            write_cooldown=True,
        )

    # 3) 模型过载：全局窗口，但暂无线上样本，先按服务级处理，不盲目重试。
    if overloaded_hit:
        return Antigravity503Decision(
            category=CATEGORY_MODEL_OVERLOADED,
            marker="reason" if reason in _OVERLOADED_REASONS else "text",
            reason=reason,
            error_number=error_number,
            upstream_model=upstream_model,
            upstream_delay_seconds=delay,
            retry_same_account=False,
            hold_account=True,
            write_cooldown=True,
        )

    # 4) 服务级不可用 / 无法识别的正文：冷藏账号后换号，不盲目重试。
    category = CATEGORY_SERVICE_UNAVAILABLE if lowered else CATEGORY_UNKNOWN
    marker = "unparsed" if unparsed_body else ("fallback" if lowered else "empty")
    return Antigravity503Decision(
        category=category,
        marker=marker,
        reason=reason,
        error_number=error_number,
        upstream_model=upstream_model,
        upstream_delay_seconds=delay,
        retry_same_account=False,
        hold_account=True,
        write_cooldown=True,
    )


def classify_antigravity_403(
    error_text: Optional[str],
    *,
    account_health: Optional[Mapping[str, Any]] = None,
) -> Antigravity403Decision:
    """Classify a 403 without treating every forbidden response as a dead account.

    Proxy-binding evidence has priority. Explicit response markers beat cached
    eligibility, while vague responses may still use that cache as context.
    Account disabling is intentionally restricted to an explicit account-level
    denial after both the proxy binding and regional eligibility were confirmed.
    """
    health = account_health or {}
    binding_status = str(health.get("binding_status") or "unchecked").lower()
    eligibility_status = str(
        health.get("eligibility_status") or "unchecked"
    ).lower()
    normalized = " ".join(str(error_text or "").lower().split())

    # 条款封禁（TOS_VIOLATION）是明确的账号级永久停用：
    # 响应文本已足够权威，不再要求缓存健康状态为 eligible。
    if _contains_any(normalized, _TOS_VIOLATION_MARKERS):
        return Antigravity403Decision(
            category="account_forbidden",
            disable_account=True,
            tos_violation=True,
        )

    if _contains_any(normalized, _LOCATION_MARKERS):
        return Antigravity403Decision(category="geo_blocked")

    if binding_status in {"proxy_drift", "proxy_failed"}:
        return Antigravity403Decision(
            category=binding_status,
            invalidate_binding=True,
            retry_after_rebind=True,
        )

    # 账号需要额外验证：只冷却模型并轮换凭证，绝不封禁账号
    if _contains_any(normalized, _VALIDATION_MARKERS):
        return Antigravity403Decision(
            category="validation_required",
            cooldown_model=True,
        )

    if _contains_any(normalized, _MODEL_MARKERS) or (
        "model " in normalized
        and any(word in normalized for word in ("not available", "not permitted"))
    ):
        return Antigravity403Decision(
            category="model_forbidden",
            cooldown_model=True,
        )

    # 缓存的地区结论只能补充模糊 403，不能覆盖本次响应里更具体的模型证据。
    if eligibility_status == "geo_blocked":
        return Antigravity403Decision(category="geo_blocked")

    health_confirmed = (
        binding_status == "healthy" and eligibility_status == "eligible"
    )
    if health_confirmed and _contains_any(normalized, _ACCOUNT_PERMISSION_MARKERS):
        return Antigravity403Decision(
            category="account_forbidden",
            disable_account=True,
        )

    return Antigravity403Decision(category="unknown_403")
