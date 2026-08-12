"""Account-bound egress IP and regional eligibility checks for Antigravity."""

import ipaddress
import json
import time
from typing import Any, Mapping, Optional, Tuple

from src.antigravity_error_classifier import classify_antigravity_403
from src.httpx_client import get_async, post_async
from src.proxy_groups import proxy_argument_from_network
from src.utils import ANTIGRAVITY_USER_AGENT


def _first_value(payload: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_egress_identity(payload: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
    """Parse common operator IP-echo JSON formats without guessing an address."""
    raw_ip = _first_value(payload, ("ip", "query", "origin", "address"))
    if not raw_ip:
        raise ValueError("IP echo response does not contain an IP address")
    candidate = str(raw_ip).split(",", 1)[0].strip()
    try:
        normalized_ip = str(ipaddress.ip_address(candidate))
    except ValueError as exc:
        raise ValueError("IP echo response contains an invalid IP address") from exc

    raw_country = _first_value(
        payload,
        ("country_code", "countryCode", "country", "country_code2"),
    )
    country = str(raw_country).strip().upper() if raw_country else None
    return normalized_ip, country or None


def account_health_check_is_fresh(
    health: Mapping[str, Any],
    *,
    ttl_seconds: int,
    now: Optional[float] = None,
) -> bool:
    """Return whether any persisted health result is still within its TTL."""
    checked_at = health.get("eligibility_checked_at")
    if checked_at is None:
        return False
    current_time = time.time() if now is None else float(now)
    try:
        age = current_time - float(checked_at)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= max(int(ttl_seconds), 0)


def account_health_is_fresh_eligible(
    health: Mapping[str, Any],
    *,
    ttl_seconds: int,
    now: Optional[float] = None,
) -> bool:
    """Return whether a persisted check is both fresh and request-eligible."""
    return (
        account_health_check_is_fresh(
            health, ttl_seconds=ttl_seconds, now=now
        )
        and health.get("binding_status") == "healthy"
        and health.get("eligibility_status") == "eligible"
    )


# 明确的不利证据：出口 IP 漂移、账号封禁。
# geo_blocked 不再作为拦截依据：地区资格判定误伤面大（出口 IP 波动、
# 检测路径与实际请求路径不一致等），缓存一条 geo_blocked 就会把
# 有额度的账号整体跳过导致无可用凭证；地区问题让请求自己失败走重试。
# 除此之外的状态（proxy_failed/error/unchecked）都是检测设施故障，
# 不包含任何账号证据，门控不应据此拦截账号。
_NEGATIVE_BINDING_STATES = frozenset({"proxy_drift"})
_NEGATIVE_ELIGIBILITY_STATES = frozenset({"account_blocked"})


def account_health_has_negative_evidence(health: Mapping[str, Any]) -> bool:
    """Return whether a persisted check holds positive evidence against the account.

    Only explicit negative evidence (IP drift, account block) may
    gate a request; detection-infrastructure failures must not.
    """
    return (
        health.get("binding_status") in _NEGATIVE_BINDING_STATES
        or health.get("eligibility_status") in _NEGATIVE_ELIGIBILITY_STATES
    )


def _response_payload(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _safe_reason(response: Any, payload: Any) -> str:
    if payload is not None:
        try:
            value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            value = str(payload)
    else:
        value = str(getattr(response, "text", "") or "")
    return value[:1000]


async def check_antigravity_account_health(
    *,
    storage: Any,
    credential_name: str,
    access_token: str,
    network: Mapping[str, Any],
    ip_check_url: str,
    api_base_url: str,
    checked_at: Optional[float] = None,
) -> Mapping[str, Any]:
    """Check IP stability and eligibility through one account's bound proxy."""
    timestamp = time.time() if checked_at is None else float(checked_at)
    current = await storage.get_antigravity_account_health(credential_name)

    if not ip_check_url:
        return await storage.update_antigravity_account_health(
            credential_name,
            binding_status="unchecked",
            eligibility_status="error",
            eligibility_reason=(
                "ANTIGRAVITY_EGRESS_IP_CHECK_URL is not configured"
            ),
            eligibility_checked_at=timestamp,
        )

    try:
        proxy_url = proxy_argument_from_network(network)
        ip_response = await get_async(
            ip_check_url,
            headers={"User-Agent": ANTIGRAVITY_USER_AGENT},
            timeout=15.0,
            proxy_url=proxy_url,
        )
        if ip_response.status_code != 200:
            # 回显服务临时不可用（如 429 限流）：响应已经穿过代理，说明代理链路
            # 本身正常，这次失败不包含任何账号或地区证据。保留既有健康状态，
            # 只记录原因并刷新检查时间，避免每个请求都重试打满回显服务。
            return await storage.update_antigravity_account_health(
                credential_name,
                eligibility_reason=(
                    f"IP echo temporarily unavailable: HTTP {ip_response.status_code}"
                ),
                eligibility_checked_at=timestamp,
            )
        ip_payload = _response_payload(ip_response)
        if not isinstance(ip_payload, Mapping):
            raise ValueError("IP echo response is not a JSON object")
        egress_ip, egress_country = parse_egress_identity(ip_payload)
    except Exception as exc:
        return await storage.update_antigravity_account_health(
            credential_name,
            binding_status="proxy_failed",
            eligibility_status="error",
            eligibility_reason=f"IP check failed: {type(exc).__name__}: {exc}"[:1000],
            eligibility_checked_at=timestamp,
        )

    previous_ip = current.get("egress_ip")
    if previous_ip and previous_ip != egress_ip:
        return await storage.update_antigravity_account_health(
            credential_name,
            egress_ip=egress_ip,
            egress_country=egress_country,
            binding_status="proxy_drift",
            eligibility_status="unchecked",
            eligibility_reason=f"Egress IP changed from {previous_ip} to {egress_ip}",
            eligibility_checked_at=timestamp,
        )

    headers = {
        "User-Agent": ANTIGRAVITY_USER_AGENT,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }
    try:
        response = await post_async(
            f"{api_base_url.rstrip('/')}/v1internal:loadCodeAssist",
            json={"metadata": {"ideType": "ANTIGRAVITY"}},
            headers=headers,
            timeout=30.0,
            proxy_url=proxy_url,
        )
        payload = _response_payload(response)
        reason = _safe_reason(response, payload)
        decision = classify_antigravity_403(reason)

        if decision.category == "geo_blocked":
            eligibility_status = "geo_blocked"
        elif response.status_code == 200 and isinstance(payload, Mapping) and (
            payload.get("currentTier") or payload.get("cloudaicompanionProject")
        ):
            eligibility_status = "eligible"
        elif response.status_code == 403 and decision.category == "account_forbidden":
            eligibility_status = "account_blocked"
        else:
            eligibility_status = "error"

        return await storage.update_antigravity_account_health(
            credential_name,
            egress_ip=egress_ip,
            egress_country=egress_country,
            binding_status="healthy",
            eligibility_status=eligibility_status,
            eligibility_reason=reason,
            eligibility_checked_at=timestamp,
        )
    except Exception as exc:
        # 资格调用异常（超时、连接重置等）是检测设施故障，不包含账号证据。
        # 出口 IP 已成功回显，绑定视为正常；资格状态保留上次结论，等待下次检查。
        return await storage.update_antigravity_account_health(
            credential_name,
            egress_ip=egress_ip,
            egress_country=egress_country,
            binding_status="healthy",
            eligibility_reason=(
                f"Eligibility check temporarily failed: {type(exc).__name__}: {exc}"
            )[:1000],
            eligibility_checked_at=timestamp,
        )
