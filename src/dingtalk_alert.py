"""DingTalk alerts for Antigravity account exhaustion."""

import os
import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Dict, Iterable, Optional, Set
from urllib.parse import urlparse

from log import log
from src.httpx_client import post_async

ALERT_KEYWORD = "Gemini反代"
ALERT_COOLDOWN_SECONDS = 60 * 60
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_alert_state_lock = Lock()
_last_successful_alerts: Dict[str, float] = {}
_alerts_in_flight: Set[str] = set()


def _reset_alert_throttle_for_tests() -> None:
    with _alert_state_lock:
        _last_successful_alerts.clear()
        _alerts_in_flight.clear()


def _try_begin_alert(alert_key: str, current_time: float) -> bool:
    with _alert_state_lock:
        last_sent_at = _last_successful_alerts.get(alert_key)
        within_cooldown = (
            last_sent_at is not None and 0 <= current_time - last_sent_at < ALERT_COOLDOWN_SECONDS
        )
        if within_cooldown or alert_key in _alerts_in_flight:
            return False
        _alerts_in_flight.add(alert_key)
        return True


def _finish_alert(alert_key: str, current_time: float, *, sent: bool) -> None:
    with _alert_state_lock:
        _alerts_in_flight.discard(alert_key)
        if sent:
            _last_successful_alerts[alert_key] = current_time


def get_earliest_recovery_timestamp(
    credential_states: Dict[str, Dict[str, Any]],
    model_name: str,
    *,
    now: Optional[float] = None,
) -> Optional[float]:
    """Return the earliest future model cooldown for an enabled account."""
    current_time = time.time() if now is None else now
    recovery_times = []

    for state in credential_states.values():
        if state.get("disabled"):
            continue
        cooldown = (state.get("model_cooldowns") or {}).get(model_name)
        try:
            cooldown_timestamp = float(cooldown)
        except (TypeError, ValueError):
            continue
        if cooldown_timestamp > current_time:
            recovery_times.append(cooldown_timestamp)

    return min(recovery_times) if recovery_times else None


def all_accounts_are_unavailable(
    credential_states: Dict[str, Dict[str, Any]],
    model_name: str,
    attempted_credentials: Iterable[str] = (),
    *,
    now: Optional[float] = None,
) -> bool:
    """Check whether every configured account is disabled, cooling down, or attempted."""
    if not credential_states:
        return False

    current_time = time.time() if now is None else now
    attempted = set(attempted_credentials)
    for credential_name, state in credential_states.items():
        if credential_name in attempted or state.get("disabled"):
            continue
        cooldown = (state.get("model_cooldowns") or {}).get(model_name)
        try:
            if float(cooldown) > current_time:
                continue
        except (TypeError, ValueError):
            pass
        return False
    return True


def build_all_accounts_unavailable_message(
    model_name: str,
    credential_states: Dict[str, Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> str:
    current_time = time.time() if now is None else now
    alert_time = datetime.fromtimestamp(current_time, SHANGHAI_TZ)
    earliest_recovery = get_earliest_recovery_timestamp(
        credential_states, model_name, now=current_time
    )

    if earliest_recovery is None:
        recovery_text = "暂无法确定，请检查账号禁用或凭证状态"
    else:
        recovery_time = datetime.fromtimestamp(earliest_recovery, SHANGHAI_TZ)
        recovery_text = f"{recovery_time:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）"

    return "\n".join(
        [
            f"{ALERT_KEYWORD}告警",
            "所有账号均不可用。",
            f"模型：{model_name or '未知'}",
            f"账号数量：{len(credential_states)}",
            f"告警时间：{alert_time:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）",
            f"最快恢复时间：{recovery_text}",
        ]
    )


def _get_dingtalk_webhook_url() -> Optional[str]:
    webhook_url = os.getenv("DINGTALK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return None

    parsed = urlparse(webhook_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname == "dingtalk.com" or hostname.endswith(".dingtalk.com")
    ):
        log.warning("[DINGTALK] webhook must be an HTTPS DingTalk URL")
        return None
    return webhook_url


async def send_all_accounts_unavailable_alert(
    model_name: str,
    credential_states: Dict[str, Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> bool:
    """Send at most one successful account-exhaustion alert per hour and process."""
    if not credential_states:
        log.warning("[DINGTALK] skipped all-account alert: credential snapshot is empty")
        return False

    webhook_url = _get_dingtalk_webhook_url()
    if not webhook_url:
        log.warning("[DINGTALK] DINGTALK_WEBHOOK_URL is not configured or invalid")
        return False

    current_time = time.time() if now is None else now
    alert_key = "all_accounts_unavailable"
    if not _try_begin_alert(alert_key, current_time):
        log.info(
            "[DINGTALK] all-account alert suppressed by the one-hour cooldown "
            f"for model {model_name or 'unknown'}"
        )
        return False

    message = build_all_accounts_unavailable_message(
        model_name=model_name,
        credential_states=credential_states,
        now=current_time,
    )
    sent = False
    try:
        response = await post_async(
            url=webhook_url,
            json={"msgtype": "text", "text": {"content": message}},
            timeout=10.0,
        )
        if response.status_code != 200:
            log.warning(f"[DINGTALK] alert failed with HTTP {response.status_code}")
            return False
        result = response.json()
        if not isinstance(result, dict) or result.get("errcode") != 0:
            log.warning("[DINGTALK] alert rejected by DingTalk")
            return False
        sent = True
        log.info("[DINGTALK] all Antigravity accounts unavailable alert sent")
        return True
    except Exception as exc:
        log.warning(f"[DINGTALK] alert delivery failed: {type(exc).__name__}")
        return False
    finally:
        _finish_alert(alert_key, current_time, sent=sent)
