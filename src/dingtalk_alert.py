"""DingTalk alerts for Antigravity account exhaustion."""

import asyncio
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from string import Formatter
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
_status_events: deque[float] = deque(maxlen=1000)
_status_rule = None
_status_delivery_task: Optional[asyncio.Task] = None


def _reset_alert_throttle_for_tests() -> None:
    global _status_rule
    _status_rule = None
    with _alert_state_lock:
        _last_successful_alerts.clear()
        _alerts_in_flight.clear()
        _status_events.clear()


def _try_begin_alert(alert_key: str, current_time: float, cooldown_seconds: float = ALERT_COOLDOWN_SECONDS) -> bool:
    with _alert_state_lock:
        last_sent_at = _last_successful_alerts.get(alert_key)
        within_cooldown = (
            last_sent_at is not None and 0 <= current_time - last_sent_at < cooldown_seconds
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


def valid_dingtalk_webhook(webhook_url: str) -> bool:
    try:
        parsed = urlparse(webhook_url)
        hostname = (parsed.hostname or "").lower()
        return (len(webhook_url) <= 2048 and parsed.scheme == "https"
                and not parsed.username and not parsed.password and not parsed.fragment
                and parsed.port in (None, 443)
                and (hostname == "dingtalk.com" or hostname.endswith(".dingtalk.com")))
    except ValueError:
        return False


async def _get_dingtalk_webhook_url() -> Optional[str]:
    from config import get_dingtalk_webhook_url
    webhook_url = await get_dingtalk_webhook_url()
    if not webhook_url:
        return None
    if not valid_dingtalk_webhook(webhook_url):
        log.warning("[DINGTALK] webhook must be an HTTPS DingTalk URL")
        return None
    return webhook_url


async def send_all_accounts_unavailable_alert(
    model_name: str,
    credential_states: Dict[str, Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> bool:
    """Send account-exhaustion alerts with a configurable process-local cooldown."""
    if not credential_states:
        log.warning("[DINGTALK] skipped all-account alert: credential snapshot is empty")
        return False

    from config import (get_dingtalk_all_accounts_alert_enabled,
                        get_dingtalk_all_accounts_alert_cooldown_minutes,
                        get_dingtalk_all_accounts_alert_template)

    if not await get_dingtalk_all_accounts_alert_enabled():
        return False

    webhook_url = await _get_dingtalk_webhook_url()
    if not webhook_url:
        log.warning("[DINGTALK] DINGTALK_WEBHOOK_URL is not configured or invalid")
        return False

    current_time = time.time() if now is None else now
    alert_key = "all_accounts_unavailable"
    if not _try_begin_alert(alert_key, current_time,
                            await get_dingtalk_all_accounts_alert_cooldown_minutes() * 60):
        log.info(
            "[DINGTALK] all-account alert suppressed by cooldown "
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
        template = await get_dingtalk_all_accounts_alert_template()
        if template:
            recovery = get_earliest_recovery_timestamp(credential_states, model_name, now=current_time)
            message = render_alert_template(template, {
                "keyword": ALERT_KEYWORD, "model": model_name or "未知",
                "account_count": len(credential_states),
                "timestamp": _format_time(current_time),
                "recovery_time": _format_time(recovery) if recovery else "暂无法确定，请检查账号禁用或凭证状态",
            }, ACCOUNT_TEMPLATE_FIELDS)
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



STATUS_TEMPLATE_FIELDS = {"keyword", "status_code", "status_codes", "count", "window_minutes", "threshold", "channel", "model", "timestamp"}
ACCOUNT_TEMPLATE_FIELDS = {"keyword", "model", "account_count", "timestamp", "recovery_time"}


def validate_alert_template(template: str, allowed_fields: set[str]) -> None:
    for _, name, spec, conversion in Formatter().parse(template):
        if name is not None and (name not in allowed_fields or spec or conversion):
            raise ValueError("模板仅支持指定变量，不支持属性、格式说明或转换")


def render_alert_template(template: str, values: dict, allowed_fields: set[str]) -> str:
    validate_alert_template(template, allowed_fields)
    text = template.format_map(values)
    # 保留现有钉钉机器人的关键词校验能力。
    return text if ALERT_KEYWORD in text else f"{ALERT_KEYWORD}告警\n{text}"


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S（UTC+8）")


async def flush_status_alerts() -> None:
    """Drain the single bounded delivery task at shutdown (and in tests)."""
    global _status_delivery_task
    task = _status_delivery_task
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=11)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            if _status_delivery_task is task:
                _status_delivery_task = None


async def send_status_frequency_alert(
    *, status_code: int, channel: str = "", model: str = "",
    now: Optional[float] = None,
) -> bool:
    """Count final call-monitor outcomes; schedule at most one delivery task.

    Codes are combined into one process-local sliding window. Keep only the
    latest threshold events: enough to establish whether the threshold is met.
    Return True when delivery is scheduled, not when delivery succeeds.
    """
    global _status_rule, _status_events, _status_delivery_task
    from config import (
        get_dingtalk_alert_enabled, get_dingtalk_alert_cooldown_minutes,
        get_dingtalk_alert_status_codes, get_dingtalk_alert_template,
        get_dingtalk_alert_threshold, get_dingtalk_alert_window_minutes,
    )
    enabled = await get_dingtalk_alert_enabled()
    codes = tuple(sorted(await get_dingtalk_alert_status_codes()))
    window = await get_dingtalk_alert_window_minutes()
    threshold = await get_dingtalk_alert_threshold()
    cooldown = await get_dingtalk_alert_cooldown_minutes()
    webhook = await _get_dingtalk_webhook_url()
    template = await get_dingtalk_alert_template()
    rule = (enabled, codes, window, threshold, cooldown, webhook, template)
    current_time = time.monotonic() if now is None else now
    alert_key = "status_frequency"
    # No await between rule update, counting and reserving delivery.
    if _status_rule != rule:
        _status_rule = rule
        _status_events = deque(maxlen=threshold)
    if not enabled or not webhook or status_code not in codes:
        return False
    while _status_events and _status_events[0] <= current_time - window * 60:
        _status_events.popleft()
    _status_events.append(current_time)
    if len(_status_events) < threshold:
        return False
    if not _try_begin_alert(alert_key, current_time, cooldown * 60):
        return False
    values = {
        "keyword": ALERT_KEYWORD, "status_code": status_code,
        "status_codes": ",".join(map(str, codes)), "count": len(_status_events),
        "window_minutes": window, "threshold": threshold,
        "channel": channel or "未知通道", "model": model or "未知模型",
        "timestamp": _format_time(time.time()),
    }

    async def deliver():
        sent = False
        try:
            # UI changes can disable delivery before the task begins.
            if not await get_dingtalk_alert_enabled() or await _get_dingtalk_webhook_url() != webhook:
                return
            message = render_alert_template(template, values, STATUS_TEMPLATE_FIELDS)
            response = await post_async(url=webhook, json={"msgtype": "text", "text": {"content": message}}, timeout=10.0)
            result = response.json() if response.status_code == 200 else {}
            sent = isinstance(result, dict) and result.get("errcode") == 0
            if not sent:
                log.warning("[DINGTALK] status frequency alert rejected or failed")
        except Exception as exc:
            log.warning(f"[DINGTALK] status frequency delivery failed: {type(exc).__name__}")
        finally:
            _finish_alert(alert_key, current_time, sent=sent)

    _status_delivery_task = asyncio.create_task(deliver(), name="dingtalk_status_alert")
    return True
