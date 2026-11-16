"""Bounded, redacted OAuth refresh diagnostics; never retain raw responses."""
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, quote_plus

from src.operational_audit import emit as audit
from log import log
from src.request_timing import request_timing


def _reference(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _safe_text(value, secrets):
    if not isinstance(value, str):
        return None
    # Redact before truncation so a token crossing the limit cannot leak a prefix.
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        for variant in {secret, quote(secret, safe=''), quote_plus(secret)}:
            value = value.replace(variant, '[REDACTED]')
    value = re.sub(r'(?i)\b(?:https?|ss|socks5h?)://\S+', '[URL]', value)
    value = re.sub(r'[^\s<>"\x27]+@[^\s<>"\x27]+', '[EMAIL]', value)
    value = re.sub(r'(?i)\b(?:bearer\s+|(?:access_token|refresh_token|id_token|client_secret|code|password)\s*[=:]\s*)[^\s,;]+', '[SECRET]', value)
    value = re.sub(r'[A-Za-z0-9_./+~=-]{32,}', '[OPAQUE]', value)
    return ' '.join(value.split())[:512]


def record_refresh_failure(*, response=None, exception=None, refresh_token,
                           client_key, client_secret, proxy_url, token_url,
                           transport, started):
    """Return a correlation ID only; diagnostic contents stay in operator logs."""
    event_id = uuid.uuid4().hex
    try:
        payload = None
        if response is not None:
            try:
                # Reject huge/non-JSON bodies rather than dumping HTML or credentials.
                if len(response.content) <= 65536:
                    payload = response.json()
            except (ValueError, UnicodeError):
                pass
        secrets = [refresh_token, client_secret]
        if isinstance(payload, dict):
            secrets += [payload.get(k) for k in ('access_token', 'refresh_token', 'id_token', 'code', 'client_secret')]
        timing = request_timing.get()
        event = {
            'event': 'oauth_refresh_failure', 'event_id': event_id,
            'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'duration_ms': round((time.monotonic() - started) * 1000),
            'credential_ref': _reference(refresh_token),
            'client_key': _safe_text(client_key, secrets),
            'transport_config': transport,
            'endpoint_ref': _reference(token_url),
            'proxy_mode': 'default_unresolved' if proxy_url is ... else ('direct' if proxy_url is None else 'explicit'),
            'proxy_config_ref': _reference(proxy_url) if isinstance(proxy_url, str) else None,
            'timing_id': timing.timing_id if timing else None,
            'http_status': response.status_code if response is not None else None,
            'exception_type': type(exception).__name__ if exception is not None else None,
            'body_format': 'json_object' if isinstance(payload, dict) else 'omitted',
        }
        for key in ('error', 'error_description', 'error_subtype'):
            event[key] = _safe_text(payload.get(key), secrets) if isinstance(payload, dict) else None
        audit('oauth_failure_evidence', oauth_event_id=event_id, **{k:v for k,v in event.items() if k not in ('event','event_id','timestamp_utc')})
        log.error('[OAUTH_EVIDENCE] ' + json.dumps(event, ensure_ascii=True))
    except Exception:
        # Evidence collection must never change credential selection/error handling.
        try:
            log.error(f'[OAUTH_EVIDENCE] collection_failed event_id={event_id}')
        except Exception:
            pass
    return event_id
