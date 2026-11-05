"""Classify bad requests without persisting arbitrary upstream request content."""

from dataclasses import asdict, dataclass
import json
import re
from typing import Optional


@dataclass(frozen=True)
class Antigravity400Decision:
    category: str
    reason: str
    message: str
    source: str
    disable_account: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def upstream_error_log_detail(status_code: int, error_text: Optional[str]) -> str:
    if status_code == 400:
        return classify_antigravity_400(error_text).to_json()
    return str(error_text or "无")[:500]


def classify_antigravity_400(
    error_text: Optional[str], *, source: str = "generation"
) -> Antigravity400Decision:
    """Keep only a known category and fixed description; never retain raw bodies."""
    text = str(error_text or "")
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    detail = error if isinstance(error, dict) else {}
    message = str(detail.get("message") or "").lower()
    status = str(detail.get("status") or "").upper()
    reasons = {
        str(item.get("reason") or "").upper()
        for item in detail.get("details", []) if isinstance(item, dict)
    } if isinstance(detail.get("details"), list) else set()

    def result(category, reason, description, disable=False):
        return Antigravity400Decision(category, reason, description, source, disable)

    if source == "oauth":
        # Legacy TokenError strings may prefix the JSON body. Match whole error
        # identifiers, never a generic HTTP 400/401/403 as evidence of revocation.
        code = error.lower() if isinstance(error, str) else ""
        def has(*codes):
            if code:
                return code in codes
            return any(
                re.search(r"(?<![a-z_])" + re.escape(v) + r"(?![a-z_])", text.lower())
                for v in codes
            )
        if has("invalid_grant", "invalid_refresh_token", "refresh_token_expired"):
            return result("oauth_invalid_grant", "INVALID_GRANT", "Refresh authorization is invalid or expired.", True)
        if has("invalid_client", "unauthorized_client"):
            return result("oauth_client_error", "INVALID_CLIENT", "OAuth client configuration or authorization mismatch.")
        if has("invalid_request", "unsupported_grant_type", "invalid_scope"):
            return result("oauth_request_error", "INVALID_REQUEST", "OAuth request parameters are invalid.")
        return result("unknown_400", "UNKNOWN_400", "OAuth failure has no recognized revocation evidence.")

    if "TOS_VIOLATION" in reasons or "violation of terms of service" in message:
        return result("account_forbidden", "TOS_VIOLATION", "The service reports an account restriction.")
    if "VALIDATION_REQUIRED" in reasons or any(v in message for v in ("requires verification", "verify your account", "validation_required")):
        return result("validation_required", "VALIDATION_REQUIRED", "The account requires verification.")
    if any(v in message for v in ("location is not supported", "not available in your location", "unsupported location", "unsupported country")):
        return result("geo_blocked", "LOCATION_RESTRICTED", "The request is unavailable in this location.")
    if "model" in message and any(v in message for v in ("not supported", "not found", "not available", "unknown model", "not permitted")):
        return result("model_error", "MODEL_ERROR", "The requested model is unavailable or unsupported.")
    if status == "INVALID_ARGUMENT" or "INVALID_ARGUMENT" in reasons:
        return result("invalid_argument", "INVALID_ARGUMENT", "Request arguments failed upstream validation.")
    return result("unknown_400", "UNKNOWN_400", "Bad request has no recognized account restriction evidence.")
