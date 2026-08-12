"""Conservative classification for Antigravity HTTP 403 responses."""

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class Antigravity403Decision:
    """Actionable result of a 403 classification."""

    category: str
    disable_account: bool = False
    invalidate_binding: bool = False
    cooldown_model: bool = False
    retry_after_rebind: bool = False


_LOCATION_MARKERS = (
    "not currently available in your location",
    "not available in your location",
    "unsupported location",
    "unsupported country",
    "region is not supported",
    "country is not supported",
    "location restriction",
    "geo restriction",
    "geographic restriction",
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


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


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

    if _contains_any(normalized, _LOCATION_MARKERS):
        return Antigravity403Decision(
            category="geo_blocked",
            invalidate_binding=True,
            retry_after_rebind=True,
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
        return Antigravity403Decision(
            category="geo_blocked",
            invalidate_binding=True,
            retry_after_rebind=True,
        )

    health_confirmed = (
        binding_status == "healthy" and eligibility_status == "eligible"
    )
    if health_confirmed and _contains_any(normalized, _ACCOUNT_PERMISSION_MARKERS):
        return Antigravity403Decision(
            category="account_forbidden",
            disable_account=True,
        )

    return Antigravity403Decision(category="unknown_403")
