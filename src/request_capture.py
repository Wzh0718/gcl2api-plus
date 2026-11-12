"""Request-scoped evidence for every generation attempt, including hidden retries."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import re
import time
from urllib.parse import urlsplit
import uuid

from log import log
from src.capture_config import capture_directory, capture_policies, capture_settings
from src.storage.request_capture_store import CaptureStore, POLICIES


capture_context: ContextVar[CaptureContext | None] = ContextVar("capture_context", default=None)
capture_managed: ContextVar[bool] = ContextVar("capture_managed", default=False)
active_attempt: ContextVar[CaptureAttempt | None] = ContextVar("capture_attempt", default=None)
_store = None


def get_capture_store():
    global _store
    root = capture_directory()
    if _store is None or _store.root != root:
        _store = CaptureStore(root)
    return _store


def fingerprint(value):
    digest = hashlib.sha256()
    size = 0
    for text in json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":")).iterencode(value):
        raw = text.encode()
        digest.update(raw)
        size += len(raw)
    return {"sha256": digest.hexdigest(), "bytes": size}


def sanitize(value, max_bytes):
    """Bound the copied tree; redact credential fields and binary media."""
    remaining = max_bytes
    nodes = 0
    secret_keys = {"authorization", "proxyauthorization", "apikey", "xgoogapikey",
                   "accesstoken", "refreshtoken", "clientsecret", "recaptchatoken",
                   "cookie", "setcookie", "password", "secret", "token"}
    def visit(item, parent="", depth=0):
        nonlocal remaining, nodes
        nodes += 1
        if remaining <= 0 or nodes > 10000 or depth > 32:
            return {"capture_omitted": "size_or_depth_limit"}
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                key = str(key)
                normalized = key.lower().replace("_", "").replace("-", "")
                if normalized in secret_keys:
                    result[key] = "[REDACTED]"
                elif normalized == "data" and parent in {"inlineData", "inline_data", "image", "audio", "source", "input_audio", "inputAudio", "video", "file"}:
                    result[key] = {"capture_omitted": "binary", **fingerprint(child)}
                else:
                    remaining -= len(key.encode()) + 8
                    result[key] = visit(child, key, depth + 1)
                if remaining <= 0 or nodes > 10000:
                    result["capture_truncated"] = True
                    break
            return result
        if isinstance(item, (list, tuple)):
            result = []
            for child in item:
                result.append(visit(child, parent, depth + 1))
                if remaining <= 0 or nodes > 10000:
                    result.append({"capture_omitted": "remaining_items"})
                    break
            return result
        if isinstance(item, str):
            item = re.sub(r"(?i)\b((?:https?|socks5h?|ss)://)[^/\s@]+@", r"\1[REDACTED]@", item)
            item = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", item)
            item = re.sub(r'''(?i)([?&](?:key|api_key|access_token)=)[^&\s"'<>]+''', r"\1[REDACTED]", item)
            item = re.sub(r'''(?i)("(?:access_token|refresh_token|client_secret|api_key|authorization)"\s*:\s*")[^"]*''', r"\1[REDACTED]", item)
            raw = item.encode()
            if item.startswith("data:") and ";base64," in item[:200]:
                return {"capture_omitted": "binary", **fingerprint(item)}
            if len(raw) > remaining:
                prefix = raw[:max(remaining, 0)].decode(errors="ignore")
                remaining = 0
                return {"capture_truncated": True, "prefix": prefix, **fingerprint(item)}
            remaining -= len(raw) + 4
            return item
        remaining -= 16
        return item if item is None or isinstance(item, (int, float, bool)) else str(type(item).__name__)
    return visit(value)


def features(payload):
    from src.request_features import content_snapshot
    snapshot = content_snapshot(payload) if isinstance(payload, dict) else {}
    # Credential fields are not part of content_snapshot. Features describe the
    # complete protocol sections BEFORE raw-content truncation.
    payload = payload if isinstance(payload, dict) else {}
    inner = payload.get("request") if isinstance(payload.get("request"), dict) else payload
    envelope = {k: fingerprint(payload[k]) for k in ("project", "model", "userAgent", "requestType") if k in payload}
    for key in ("sessionId", "labels", "metadata"):
        if key in inner:
            envelope[key] = fingerprint(inner[key])
    return {"parts": {k: fingerprint(v) for k, v in snapshot.items()}, "envelope": envelope,
            "message_count": len(snapshot.get("messages", [])) if isinstance(snapshot.get("messages"), list) else 0,
            "tool_count": len(snapshot.get("tools", [])) if isinstance(snapshot.get("tools"), list) else 0}


@dataclass
class CaptureContext:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    client: dict = field(default_factory=dict)
    client_features: dict = field(default_factory=dict)
    attempt_count: int = 0
    credentials: dict = field(default_factory=dict)
    api_key_id: str = ""
    last_account: str = ""
    ingress: dict = field(default_factory=dict)
    account_networks: dict = field(default_factory=dict)
    request_model: str = ""


def bind_capture_credential(credential_name, payload, credential_data=None):
    context = capture_context.get()
    if context is not None and credential_name and isinstance(payload, dict):
        key = payload.get("requestId") or f"payload:{id(payload)}"
        if key:
            if len(context.credentials) >= 64:
                context.credentials.pop(next(iter(context.credentials)))
            context.credentials[key] = credential_name
            if credential_data:
                if len(context.account_networks) >= 64:
                    context.account_networks.pop(next(iter(context.account_networks)))
                context.account_networks[credential_name] = {k: credential_data[k] for k in (
                    "proxy_mode", "proxy_group_id", "bound_proxy_node_id", "oauth_client_key",
                ) if k in credential_data}


async def record_local_capture(credential_name, model, reason="capacity_busy"):
    context = capture_context.get()
    if context is not None:
        context.last_account = credential_name
        event = {"request_id": context.request_id, "created_at": time.time(),
                 "kind": "local_queue", "phase": "rejected",
                 "credential_name": credential_name, "model": model,
                 "status_code": 429, "reason": reason, "api_key_id": context.api_key_id}
        await write_event("attempts", event)
        await write_event("errors", {**event, "details": {"client": context.client}})


async def write_event(policy, event):
    settings = capture_settings()
    if not settings["request_capture_enabled"]:
        return False
    try:
        return await asyncio.to_thread(
            get_capture_store().write, policy, event, capture_policies(settings)[policy],
            min_free_bytes=capture_policies(settings)[policy]["min_free_bytes"],
            max_entry_bytes=settings["request_capture_max_entry_kb"] * 1024,
        )
    except Exception as exc:
        # Never let evidence capture change the response/retry behaviour.
        log.warning(f"[CAPTURE] evidence unavailable: {type(exc).__name__}")
        return False


def _error_fields(body, limit):
    raw = body.encode() if isinstance(body, str) else bytes(body or b"")
    truncated = len(raw) > limit
    text = raw[:limit].decode(errors="replace")
    reason = ""
    try:
        payload = json.loads(text)
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        details = error.get("details") if isinstance(error, dict) else None
        for item in details if isinstance(details, list) else []:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item["reason"])[:128]
                break
        if not reason and isinstance(error, dict):
            reason = str(error.get("status") or "")[:128]
        content = sanitize(payload, limit)
    except (ValueError, TypeError):
        content = sanitize({"text": text}, limit)
    return reason, content, truncated


class CaptureAttempt:
    def __init__(self, url, payload, credential_name="", proxy_url=..., streaming=False):
        self.context = capture_context.get()
        self.enabled = self.context is not None and capture_settings()["request_capture_enabled"]
        self.finished = False
        self.status = None
        self.headers = {}
        self.error_body = b""
        self.incomplete = None
        self.stream_buffer = ""
        self.stream_terminal = False
        self.received_content = False
        self.started = time.monotonic()
        self.event = {}
        self.details = {}
        self.feature_data = {}
        if not self.enabled:
            return
        context = self.context
        settings = capture_settings()
        context.attempt_count += 1
        payload = payload if isinstance(payload, dict) else {}
        account = credential_name or context.credentials.get(payload.get("requestId") or f"payload:{id(payload)}", "")
        context.last_account = account
        target = urlsplit(url)
        proxy = urlsplit(proxy_url) if isinstance(proxy_url, str) else None
        self.event = {"request_id": context.request_id, "attempt_id": uuid.uuid4().hex,
                      "attempt": context.attempt_count, "created_at": time.time(),
                      "credential_name": account, "api_key_id": context.api_key_id,
                      "upstream_request_id": str(payload.get("requestId") or "")[:512],
                      "model": str(payload.get("model") or "")[:256],
                      "request_model": context.request_model,
                      "target_host": target.hostname, "target_path": target.path,
                      "proxy_host": proxy.hostname if proxy else ("inherit" if proxy_url is ... else "direct"),
                      "streaming": streaming, "kind": "upstream", "phase": "started"}
        self.event.update(ingress=context.ingress, network=context.account_networks.get(account, {}))
        self.feature_data = {"client": context.client_features, "upstream": features(payload)}
        if settings["request_capture_normal_enabled"] or settings["request_capture_errors_enabled"]:
            self.details = {"client": context.client,
                            "upstream": sanitize(payload, settings["request_capture_max_entry_kb"] * 384)}

    async def start(self):
        if self.enabled:
            await write_event("attempts", self.event)
            await write_event("features", {**self.event, "features": self.feature_data})

    def response(self, status, headers=None, body=b""):
        self.status = status
        # Only allowlisted response identifiers; never capture request headers.
        headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
        self.headers = {k: headers[k][:512] for k in (
            "x-cloud-trace-context", "x-request-id", "x-goog-request-id", "traceparent", "retry-after",
        ) if k in headers}
        if status and status >= 400:
            limit = capture_settings()["request_capture_error_body_kb"] * 1024
            raw = body.encode() if isinstance(body, str) else body
            self.error_body = bytes(raw or b"")[:limit + 1]

    def observe(self, chunk):
        if not self.enabled:
            return
        if hasattr(chunk, "status_code"):
            self.response(chunk.status_code, getattr(chunk, "headers", {}), getattr(chunk, "body", b""))
            return
        if self.status is None:
            self.status = 200
        text = chunk.decode(errors="replace") if isinstance(chunk, bytes) else str(chunk)
        self.received_content |= bool(text.strip())
        limit = capture_settings()["request_capture_error_body_kb"] * 1024
        if len(self.stream_buffer) + len(text) > limit:
            self.stream_buffer = ""  # Successful content is never retained.
            return
        self.stream_buffer += text
        candidates = self.stream_buffer.splitlines()
        for line in candidates:
            line = line.removeprefix("data:").strip()
            if line == "[DONE]":
                self.stream_terminal = True
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get("error"), dict):
                code = item["error"].get("code", 500)
                self.response(code if isinstance(code, int) else 500, self.headers, line)
            elif isinstance(item, dict):
                content = item.get("response", item)
                if isinstance(content, dict):
                    candidates = content.get("candidates") or content.get("choices") or []
                    self.stream_terminal |= any(isinstance(c, dict) and bool(c.get("finishReason") or c.get("finish_reason")) for c in candidates)
        if self.stream_buffer.endswith(("\n", "\r")):
            self.stream_buffer = ""

    async def finish(self, exception=None):
        if not self.enabled or self.finished:
            return
        self.finished = True
        if exception and not (isinstance(exception, GeneratorExit) and self.stream_terminal):
            self.incomplete = type(exception).__name__
        reason, error, truncated = _error_fields(
            self.error_body, capture_settings()["request_capture_error_body_kb"] * 1024)
        event = {**self.event, "phase": "finished", "created_at": time.time(),
                 "status_code": self.status, "seconds": time.monotonic() - self.started,
                 "response_headers": self.headers,
                 "trace_id": self.headers.get("x-cloud-trace-context") or self.headers.get("x-request-id") or self.headers.get("x-goog-request-id"),
                 "reason": reason, "incomplete": self.incomplete}
        await write_event("attempts", event)
        raw_event = {**event, "details": self.details, "error": error if self.error_body else None,
                     "error_truncated": truncated}
        failed = bool(self.incomplete or self.status is None or self.status >= 400)
        if failed:
            await self._save_error(raw_event)
        await write_event("normal", raw_event)

    async def _save_error(self, event):
        settings = capture_settings()
        if not settings["request_capture_errors_enabled"]:
            return
        await write_event("errors", event)
        count = settings["request_capture_context_count"]
        account = event.get("credential_name")
        if count and account:
            try:
                prior = await asyncio.to_thread(
                    get_capture_store().query, "normal", capture_policies(settings)["normal"],
                    credential_name=account, status_code=200, limit=count,
                    before=event["created_at"], complete_only=True,
                )
                for sample in prior:
                    if sample.get("incomplete"):
                        continue
                    await write_event("errors", {**sample, "kind": "prior_success",
                                                 "request_id": event["request_id"],
                                                 "source_request_id": sample["request_id"],
                                                 "source_created_at": sample["created_at"],
                                                 "created_at": time.time(),
                                                 "trigger_request_id": event["request_id"],
                                                 "trigger_attempt_id": event.get("attempt_id")})
            except Exception as exc:
                log.warning(f"[CAPTURE] comparison evidence unavailable: {type(exc).__name__}")


def observe_stream_headers(response):
    attempt = active_attempt.get()
    if attempt is not None:
        attempt.response(response.status_code, response.headers)


def capture_post(function):
    @wraps(function)
    async def wrapped(url, *args, **kwargs):
        credential = kwargs.pop("capture_credential", "")
        payload = kwargs.get("json") or (args[1] if len(args) > 1 else None)
        if capture_context.get() is None or "generatecontent" not in url.lower():
            return await function(url, *args, **kwargs)
        attempt = CaptureAttempt(url, payload, credential, kwargs.get("proxy_url", ...))
        await attempt.start()
        try:
            response = await function(url, *args, **kwargs)
            attempt.response(response.status_code, response.headers,
                             response.content if response.status_code >= 400 else b"")
            if response.status_code == 200:
                if not response.content:
                    attempt.incomplete = "empty_response"
                limit = capture_settings()["request_capture_error_body_kb"] * 1024
                if len(response.content) <= limit:
                    attempt.observe(response.content)
            await attempt.finish()
            return response
        except BaseException as exc:
            await attempt.finish(exc)
            raise
    return wrapped


def capture_stream(function):
    @wraps(function)
    async def wrapped(url, body, *args, **kwargs):
        credential = kwargs.pop("capture_credential", "")
        attempt = CaptureAttempt(url, body, credential, kwargs.get("proxy_url", ...), streaming=True)
        await attempt.start()
        token = active_attempt.set(attempt)
        stream = function(url, body, *args, **kwargs)
        try:
            async for chunk in stream:
                attempt.observe(chunk)
                if hasattr(chunk, "status_code") and chunk.status_code >= 400:
                    await attempt.finish()
                yield chunk
        except BaseException as exc:
            await attempt.finish(exc)
            raise
        finally:
            try:
                await stream.aclose()
                if attempt.enabled and (attempt.status is None or not attempt.received_content) and not attempt.error_body:
                    attempt.incomplete = attempt.incomplete or "empty_stream"
                await attempt.finish()
            finally:
                active_attempt.reset(token)
    return wrapped


async def capture_request(scope, receive, send, app):
    path = scope.get("path", "")
    prefixes = ("/v1/", "/v1beta/", "/antigravity/v1/", "/antigravity/v1beta/",
                "/vertex/v1/", "/vertex/v1beta/", "/geminicli/v1/", "/geminicli/v1beta/")
    if (scope["type"] != "http" or scope.get("method") != "POST" or
            not path.startswith(prefixes) or not capture_settings()["request_capture_enabled"]):
        return await app(scope, receive, send)
    context = CaptureContext()
    if "/models/" in path:
        context.request_model = path.split("/models/", 1)[1].split(":", 1)[0][:256]
    incoming_headers = dict(scope.get("headers", []))
    context.ingress = {"path": path.split("/key=", 1)[0],
                       "user_agent": incoming_headers.get(b"user-agent", b"")[:512].decode(errors="replace"),
                       "client_ip": str((scope.get("client") or ("",))[0])[:128]}
    token = capture_context.set(context)
    settings = capture_settings()
    max_body = settings["request_capture_max_body_kb"] * 1024
    chunks = bytearray()
    body_hash = hashlib.sha256()
    body_bytes = 0
    status = None
    response_complete = False
    final_error = bytearray()
    exception_name = None
    content_type = dict(scope.get("headers", [])).get(b"content-type", b"").split(b";")[0].strip().lower()

    async def observe_receive():
        nonlocal body_bytes
        message = await receive()
        if message["type"] == "http.request":
            raw = message.get("body", b"")
            body_hash.update(raw)
            body_bytes += len(raw)
            if body_bytes <= max_body:
                chunks.extend(raw)
            else:
                chunks.clear()
            if not message.get("more_body", False):
                if body_bytes <= max_body and content_type == b"application/json":
                    try:
                        payload = json.loads(chunks)
                        if isinstance(payload, dict):
                            context.request_model = str(payload.get("model") or context.request_model)[:256]
                            context.client_features = features(payload)
                            context.client = sanitize(payload, settings["request_capture_max_entry_kb"] * 384)
                    except (ValueError, RecursionError):
                        context.client = {"capture_omitted": "invalid_json"}
                else:
                    context.client = {"capture_omitted": "body_limit_or_content_type"}
                context.client_features["wire"] = {"sha256": body_hash.hexdigest(), "bytes": body_bytes}
                chunks.clear()
        return message

    async def observe_send(message):
        nonlocal status, response_complete
        if message["type"] == "http.response.start":
            status = message["status"]
            message = {**message, "headers": list(message.get("headers", [])) +
                       [(b"x-gcli-capture-id", context.request_id.encode())]}
        if message["type"] == "http.response.body":
            if status and status >= 400:
                limit = settings["request_capture_error_body_kb"] * 1024
                final_error.extend(message.get("body", b"")[:max(limit + 1 - len(final_error), 0)])
            response_complete = not message.get("more_body", False)
        return await send(message)

    try:
        return await app(scope, observe_receive, observe_send)
    except BaseException as exc:
        exception_name = type(exc).__name__
        raise
    finally:
        try:
            final = {"request_id": context.request_id, "created_at": time.time(),
                     "phase": "final", "kind": "gateway", "path": path.split("/key=", 1)[0],
                     "api_key_id": context.api_key_id, "credential_name": context.last_account,
                     "status_code": status, "upstream_attempts": context.attempt_count,
                     "ingress": context.ingress,
                     "request_model": context.request_model,
                     "incomplete": exception_name or (None if response_complete else "response_incomplete")}
            await write_event("attempts", final)
            await write_event("features", {**final, "features": {"client": context.client_features}})
            if context.attempt_count == 0:
                await write_event("normal", {**final, "details": {"client": context.client}})
            if status is None or status >= 400 or final["incomplete"]:
                reason, error, truncated = _error_fields(final_error, settings["request_capture_error_body_kb"] * 1024)
                await write_event("errors", {**final, "details": {"client": context.client},
                                             "reason": reason, "error": error, "error_truncated": truncated})
        finally:
            capture_context.reset(token)


async def maintain_capture():
    settings = capture_settings()
    store = get_capture_store()
    if not store.root.exists():
        return
    for name, policy in capture_policies(settings).items():
        await asyncio.to_thread(store.maintain, name, policy)


async def capture_maintenance_loop():
    while True:
        try:
            await maintain_capture()
        except Exception as exc:
            log.warning(f"[CAPTURE] maintenance failed: {type(exc).__name__}")
        await asyncio.sleep(capture_settings()["request_capture_cleanup_seconds"])
