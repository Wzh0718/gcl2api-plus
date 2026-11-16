"""Secret-free operational events, independent of short-lived request bodies."""
import hashlib
import json
import os
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from log import log
from src.storage.request_capture_store import CaptureStore

refresh_context = ContextVar("refresh_audit", default=None)
INSTANCE = uuid.uuid4().hex
POLICY = {"enabled": True, "max_bytes": 256 * 1024**2, "retention_hours": 720}
_stores = {}
FIELDS = frozenset("account_ref mode refresh_event_id source reason outcome revision_ref old_revision_ref new_revision_ref expected_revision_ref duration_ms remaining_seconds expiry_known error_type http_status oauth_event_id lease_owner_ref waited_ms attempts refresh_token_changed operation committed state_fields proxy_config_ref proxy_mode oauth_client_key endpoint_ref error error_description error_subtype transport_config body_format credential_ref client_key timing_id network_event_id route_ref probe_ref observed_at checked_at ip country status".split())


def reference(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24] if value is not None else None


def identity():
    return {"schema_version": 1, "instance_id": INSTANCE, "pid": os.getpid(),
            "build_revision": os.getenv("APP_BUILD_REVISION", "unknown")[:64]}


def make_event(event, **fields):
    row = {**identity(), "event": event, "event_id": uuid.uuid4().hex,
           "created_at": time.time()}
    context = refresh_context.get() or {}
    row.update({k: v for k, v in {**context, **fields}.items() if k in FIELDS
                and (v is None or isinstance(v, (bool, int, float))
                     or isinstance(v, str) and len(v) <= 512)})
    # Import lazily: request_capture also uses this module for identity metadata.
    from src.request_capture import capture_context
    request = capture_context.get()
    if request is not None:
        row["request_id"] = request.request_id
    return row


def emit(event, **fields):
    """Best effort, bounded and process-safe; failure never changes OAuth outcome."""
    try:
        row = make_event(event, **fields)
        root = Path(os.getenv("CREDENTIALS_DIR", "./creds")) / "operational-audit"
        store = _stores.setdefault(str(root), CaptureStore(root))
        if not store.write("attempts", row, POLICY, min_free_bytes=64 * 1024**2,
                           max_entry_bytes=8192):
            log.warning("[OPERATIONAL_AUDIT] write_failed")
        log.info("[OPERATIONAL_AUDIT] " + json.dumps(row, ensure_ascii=True))
    except Exception:
        try:
            log.warning("[OPERATIONAL_AUDIT] collection_failed")
        except Exception:
            pass


def expiry_state(data):
    if not (data.get("access_token") or data.get("token")):
        return {"reason": "missing_access_token", "expiry_known": False}
    try:
        expiry = datetime.fromisoformat(str(data.get("expiry", "")).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        remaining = int((expiry - datetime.now(timezone.utc)).total_seconds())
        return {"reason": "expiry_window", "expiry_known": True, "remaining_seconds": remaining}
    except (ValueError, TypeError, OverflowError):
        return {"reason": "missing_or_invalid_expiry", "expiry_known": False}
