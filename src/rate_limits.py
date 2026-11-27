"""Bounded 429 attribution recorded at the hop that produces the response."""

import math
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Literal

# Earliest recovery among otherwise eligible accounts for this task’s last selection.
selection_retry_at = ContextVar("selection_retry_at", default=0)
selection_usage_resume_at = ContextVar("selection_usage_resume_at", default=0)

CREDITS_EXHAUSTED_MARKER = "INSUFFICIENT_G1_CREDITS_BALANCE"


@dataclass(frozen=True)
class RateLimit:
    source: Literal['gateway', 'upstream']
    reason: str
    rules: tuple[str, ...] = ()
    # Unix 秒。上游给出的额度恢复时刻（quotaResetTimeStamp / Retry-After），
    # 让监控能直接看出「这个号什么时候能再用」，不必回读日志。
    reset_at: float | None = None

    def as_dict(self) -> dict:
        result = {'source': self.source, 'reason': self.reason}
        if self.rules:
            result['rules'] = list(self.rules)
        if self.reset_at is not None:
            result['reset_at'] = round(float(self.reset_at), 3)
        return result


def with_reset_at(rate_limit: RateLimit | None, deadline: float | None) -> RateLimit | None:
    """Attach the resolved cooldown deadline to an existing attribution."""
    if rate_limit is None or deadline is None:
        return rate_limit
    try:
        value = float(deadline)
    except (TypeError, ValueError):
        return rate_limit
    if not math.isfinite(value):
        return rate_limit
    return replace(rate_limit, reset_at=value)


def retry_after_seconds(deadline: float | None, *, now: float | None = None) -> str | None:
    """Whole seconds until ``deadline`` as a Retry-After value, or None when unusable."""
    if deadline is None:
        return None
    try:
        remaining = float(deadline) - (time.time() if now is None else float(now))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(remaining):
        return None
    return str(max(int(remaining), 1))


def upstream_rate_limit(error_body: str | None) -> RateLimit:
    """Called only for a 429 received from upstream, never a gateway rejection."""
    body = error_body or ''
    if 'MODEL_CAPACITY_EXHAUSTED' in body:
        return RateLimit('upstream', 'model_capacity')
    if CREDITS_EXHAUSTED_MARKER in body:
        return RateLimit('upstream', 'credits_exhausted')
    if any(marker in body for marker in ('QUOTA_EXHAUSTED', 'quotaResetTimeStamp', 'quotaResetDelay')):
        return RateLimit('upstream', 'quota_exhausted')
    return RateLimit('upstream', 'rate_limited')


def retry_after_headers(response):
    """Carry a retry deadline across protocol conversion of a 429 response."""
    if getattr(response, "status_code", None) == 429:
        value = getattr(response, "headers", {}).get("retry-after")
        if value is not None:
            return {"Retry-After": value}
    return {}
