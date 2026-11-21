"""Bounded 429 attribution recorded at the hop that produces the response."""

from contextvars import ContextVar
from dataclasses import dataclass
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

    def as_dict(self) -> dict:
        result = {'source': self.source, 'reason': self.reason}
        if self.rules:
            result['rules'] = list(self.rules)
        return result


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
