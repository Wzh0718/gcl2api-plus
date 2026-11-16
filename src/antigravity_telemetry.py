"""Opt-in account telemetry from real gateway attempts, without conversation bodies.

Uses only the observed Jetski analytics envelope. It does not synthesize official
startup, tools, hooks, or conversation trajectories that the gateway did not run.
"""
import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlsplit

from log import log
from src.storage.antigravity_telemetry_store import subject_ref
from src.storage_adapter import get_storage_adapter

ENDPOINT = "https://play.googleapis.com/log"
QUEUE_LIMIT = 16
WORKER_LIMIT = 128
MAX_EVENT_AGE = 60


def _varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _number(number, value):
    return _varint(number << 3) + _varint(value)


def _bytes(number, value):
    raw = value.encode() if isinstance(value, str) else value
    return _varint((number << 3) | 2) + _varint(len(raw)) + raw


def encode_log_request(snapshot, extra):
    """Recovered field numbers from Hub 2.31.2, log_source=2747 (JETSKI)."""
    metadata = _bytes(1, "gateway_generation_finished")
    for key, value in sorted(extra.items()):
        metadata += _bytes(2, _bytes(1, key) + _bytes(2, str(value)))
    version = re.search(r"antigravity/hub/([^ ]+)", snapshot.user_agent)
    os_type = re.search(r"os_type=([^; )]+)", snapshot.user_agent)
    arch = re.search(r"arch=([^; )]+)", snapshot.user_agent)
    extension = (_bytes(6, metadata) + _bytes(16, "antigravity") +
                 _bytes(17, version.group(1) if version else "unknown") +
                 _bytes(18, os_type.group(1) if os_type else "unknown") +
                 _bytes(19, arch.group(1) if arch else "unknown") +
                 _number(24, 1) + _bytes(27, "hub"))
    event = _number(1, int(time.time() * 1000)) + _bytes(6, extension)
    return _number(2, 2747) + _bytes(3, event) + _number(4, int(time.time() * 1000))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _header(headers, name):
    return next((str(v) for k, v in (headers or {}).items() if k.lower() == name.lower()), "")


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: int
    filename: str
    subject: str
    installation_id: str
    policy_revision: str
    mode: str
    environment: str
    user_agent: str
    access_token: str = field(repr=False)
    proxy_url: str | None = field(repr=False)
    timing_id: str = ""
    route_ref: str = ""
    native_transport: bool = False
    created_at: float = field(default_factory=time.monotonic)


class GenerationAttempt:
    def __init__(self, service, snapshot, payload):
        self.service = service
        self.snapshot = snapshot
        self.started = time.monotonic()
        self.finished = False
        self.terminal = False
        self.buffer = ""
        self.usage = {}
        # Construct an allowlist rather than redacting an arbitrary prompt/response.
        inner = payload.get("request", {})
        labels = inner.get("labels", {})
        self.extra = {"implementation": "gcli2api", "event_id": uuid.uuid4().hex,
                      "installation_id": snapshot.installation_id}
        for key, value in {"request_id": payload.get("requestId"), "model_id": payload.get("model"),
                           "session_id": inner.get("sessionId"),
                           "cascade_id": labels.get("cascade_id"),
                           "trajectory_id": labels.get("trajectory_id"),
                           "label_request_id": labels.get("request_id"),
                           "last_step_index": labels.get("last_step_index")}.items():
            if isinstance(value, str) and len(value) <= 256:
                self.extra[key] = value

    def _message(self, text):
        if text.strip() == "[DONE]":
            self.terminal = True
            return
        try:
            body = json.loads(text)
            body = body.get("response", body)
            if not isinstance(body, dict):
                return
            for key in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "cachedContentTokenCount", "totalTokenCount"):
                value = (body.get("usageMetadata") or {}).get(key)
                if type(value) is int and 0 <= value < 2**53:
                    self.usage[key] = value
            if any(c.get("finishReason") for c in body.get("candidates", []) if isinstance(c, dict)):
                self.terminal = True
        except (ValueError, TypeError, AttributeError):
            pass

    def observe(self, chunk, *, complete_line=False):
        if not isinstance(chunk, (str, bytes)):
            return
        text = chunk.decode(errors="replace") if isinstance(chunk, bytes) else chunk
        # Only a bounded parser buffer exists in memory; bodies never enter the event.
        if len(text) > 65536:
            return
        if not self.buffer and not text.startswith("data:") and text.lstrip().startswith("{"):
            self._message(text)
            return
        if complete_line:
            text += "\n"
        self.buffer += text
        if len(self.buffer) > 65536:
            self.buffer = ""
            return
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.startswith("data:"):
                self._message(line[5:].strip())

    def finish(self, status, error=None):
        if self.finished:
            return
        self.finished = True
        self.buffer = ""
        event = {**self.extra, **self.usage, "http_status": status or 0,
                 "duration_ms": round((time.monotonic() - self.started) * 1000),
                 "outcome": "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else
                 "network_error" if error else "http_error" if status and status >= 400 else
                 "completed" if self.terminal else "incomplete"}
        self.service.enqueue(self.snapshot, event)


class TelemetryService:
    def __init__(self, adapter_provider=None):
        self.adapter_provider = adapter_provider or get_storage_adapter
        self.workers = {}
        self.queues = {}
        self.closed = False

    async def _environment(self, adapter, account):
        from config import get_proxy_config, get_antigravity_native_transport_enabled_sync
        from src.proxy_groups import proxy_argument_from_network
        from src.utils import get_antigravity_user_agent
        network = await adapter.get_credential_network(account["filename"], mode="antigravity")
        if network.get("proxy_mode") == "group":
            # Read the existing sticky binding. Resolving here could rotate it and
            # invalidate the environment of an in-flight inference request.
            if not network.get("bound_proxy_node_id") or not network.get("bound_proxy_url"):
                raise ValueError("账号尚未绑定代理组节点，请先检测账号出口")
            if not network.get("bound_proxy_available"):
                raise ValueError("账号绑定的代理节点不可用（停用、过期或冷却中），请先检查账号出口")
            network = {**network, "proxy_url": network["bound_proxy_url"]}
        proxy = proxy_argument_from_network(network)
        if proxy is ...:
            proxy = await get_proxy_config() or None
        credential = account["credential_data"]
        environment = {"user_agent": get_antigravity_user_agent(),
                       "route_ref": _digest([proxy, network.get("proxy_mode"), network.get("proxy_group_id"), network.get("bound_proxy_node_id")]),
                       "oauth_client_ref": _digest([credential.get("oauth_client_key"), credential.get("client_id")]),
                       "native_transport": get_antigravity_native_transport_enabled_sync()}
        return environment, proxy

    async def status(self, filename):
        adapter = await self.adapter_provider()
        account = await adapter.get_telemetry_account(filename)
        if not account:
            raise LookupError("凭证文件不存在")
        valid = bool(account["subject_ref"] and account["subject_ref"] == subject_ref(account))
        environment_matches = False
        if valid:
            try:
                current, _ = await self._environment(adapter, account)
                environment_matches = current == account["environment"]
            except ValueError:
                pass
        return {"mode": (account["mode"] or "off") if valid else "off",
                "installation_id": account["installation_id"] if valid else None,
                "environment_matches": environment_matches,
                "identity_ready": bool(subject_ref(account)),
                "last_outcome": account["last_outcome"] if valid else "",
                "last_http_status": account["last_http_status"] if valid else None,
                "last_event_at": account["last_event_at"] if valid else None}

    async def configure(self, filename, mode):
        adapter = await self.adapter_provider()
        account = await adapter.get_telemetry_account(filename)
        if not account:
            raise LookupError("凭证文件不存在")
        environment = account["environment"] if mode == "off" else (await self._environment(adapter, account))[0]
        await adapter.configure_account_telemetry(filename, account, mode, environment)
        return await self.status(filename)

    async def begin(self, url, payload, headers, proxy_url, transport):
        if (self.closed or transport != "antigravity" or
                not urlsplit(url).path.lower().endswith((":generatecontent", ":streamgeneratecontent")) or
                not isinstance(payload, dict)):
            return None
        try:
            token = _header(headers, "Authorization").removeprefix("Bearer ")
            if not token:
                return None
            adapter = await self.adapter_provider()
            account = await adapter.get_telemetry_account(access_token=token)
            if (not account or account["disabled"] or account["mode"] not in {"preview", "send"} or
                    account["subject_ref"] != subject_ref(account)):
                return None
            expiry = datetime.fromisoformat(account["credential_data"]["expiry"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                return None
            environment, expected_proxy = await self._environment(adapter, account)
            from config import get_proxy_config
            actual_proxy = (await get_proxy_config() or None) if proxy_url is ... else proxy_url
            if (actual_proxy != expected_proxy or environment != account["environment"] or
                    _header(headers, "User-Agent") != environment["user_agent"]):
                return None
            inner = payload.get("request") or {}
            if payload.get("requestType") == "agent":
                from src.api.antigravity import _derive_session_id
                if inner.get("sessionId") != _derive_session_id("", account["filename"]):
                    return None
            snapshot = AccountSnapshot(account["account_id"], account["filename"], account["subject_ref"],
                                       account["installation_id"], account["policy_revision"], account["mode"],
                                       _digest(environment), environment["user_agent"], token, actual_proxy,
                                       route_ref=environment["route_ref"], native_transport=environment["native_transport"])
            from src.request_timing import request_timing
            from dataclasses import replace
            timing = request_timing.get()
            if timing is not None:
                snapshot = replace(snapshot, timing_id=timing.timing_id)
            return GenerationAttempt(self, snapshot, payload)
        except Exception as exc:
            log.warning(f"[TELEMETRY] prepare_failed: {type(exc).__name__}")
            return None

    def enqueue(self, snapshot, event):
        key = snapshot.account_id
        queue = self.queues.get(key)
        if self.closed or (queue is None and len(self.workers) >= WORKER_LIMIT):
            return
        if queue is None:
            queue = self.queues[key] = asyncio.Queue(QUEUE_LIMIT)
        try:
            queue.put_nowait((snapshot, event))
        except asyncio.QueueFull:
            log.warning("[TELEMETRY] account_queue_full")
            return
        if key not in self.workers:
            # Empty context prevents retaining ingress bodies/API key context in workers.
            from contextvars import Context
            self.workers[key] = asyncio.create_task(self._work(key), context=Context())

    async def _still_bound(self, adapter, snapshot):
        if time.monotonic() - snapshot.created_at > MAX_EVENT_AGE:
            return False
        account = await adapter.get_telemetry_account(snapshot.filename)
        if (not account or account["disabled"] or account["account_id"] != snapshot.account_id or
                account["policy_revision"] != snapshot.policy_revision or account["mode"] != snapshot.mode or
                subject_ref(account) != snapshot.subject or account["installation_id"] != snapshot.installation_id or
                (account["credential_data"].get("access_token") or account["credential_data"].get("token")) != snapshot.access_token):
            return False
        expiry = datetime.fromisoformat(account["credential_data"]["expiry"].replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            return False
        try:
            environment, proxy = await self._environment(adapter, account)
        except ValueError:
            return False
        return _digest(environment) == snapshot.environment and proxy == snapshot.proxy_url

    async def _work(self, key):
        queue = self.queues[key]
        try:
            while not queue.empty():
                snapshot, event = queue.get_nowait()
                adapter = None
                try:
                    adapter = await self.adapter_provider()
                    await self._record_event(adapter, snapshot, event, "queued")
                    if not await self._still_bound(adapter, snapshot):
                        await self._record_event(adapter, snapshot, event, "discarded_binding_changed")
                        await adapter.record_telemetry_outcome(key, snapshot.policy_revision, "discarded_binding_changed")
                        continue
                    if snapshot.mode == "preview":
                        from src.storage.request_capture_store import CaptureStore
                        root = os.path.join(os.getenv("CREDENTIALS_DIR", "./creds"), "telemetry-preview", str(key))
                        saved = CaptureStore(root).write("attempts", {"created_at": time.time(), **event},
                            {"enabled": True, "max_bytes": 2 * 1024**2, "retention_hours": 24},
                            min_free_bytes=64 * 1024**2, max_entry_bytes=8192)
                        await adapter.record_telemetry_outcome(key, snapshot.policy_revision, "preview" if saved else "preview_write_failed")
                        await self._record_event(adapter, snapshot, event, "preview" if saved else "preview_write_failed")
                        continue
                    status = None
                    tries = 0
                    outcome = "send_failed"
                    for attempt in range(2):
                        if not await self._still_bound(adapter, snapshot):
                            outcome = "discarded_binding_changed"
                            break
                        tries += 1
                        await self._record_event(adapter, snapshot, event, "sending", status, tries)
                        try:
                            status = await self._send(snapshot, encode_log_request(snapshot, event))
                        except Exception:
                            status = None
                        if status is not None and (status < 500 and status != 429):
                            outcome = "sent" if status == 200 else "send_failed"
                            break
                        if attempt == 0:
                            await asyncio.sleep(1)
                    await adapter.record_telemetry_outcome(key, snapshot.policy_revision,
                        outcome, status)
                    await self._record_event(adapter, snapshot, event, outcome, status, tries)
                except Exception as exc:
                    if adapter is not None:
                        await self._record_event(adapter, snapshot, event, "processing_failed")
                    log.warning(f"[TELEMETRY] delivery_failed: {type(exc).__name__}")
                finally:
                    queue.task_done()
        finally:
            self.workers.pop(key, None)
            self.queues.pop(key, None)

    async def _record_event(self, adapter, snapshot, event, outcome, status=None, attempts=0):
        # Observability failure must not prevent telemetry delivery or inference.
        try:
            await adapter.save_telemetry_event(snapshot, event, outcome, status, attempts)
        except Exception as exc:
            log.warning(f"[TELEMETRY] history_failed: {type(exc).__name__}")

    async def _send(self, snapshot, body):
        from src.httpx_client import post_async
        response = await post_async(ENDPOINT, data=body, headers={
            "Authorization": "Bearer " + snapshot.access_token,
            "Content-Type": "application/octet-stream", "User-Agent": "Go-http-client/2.0"},
            proxy_url=snapshot.proxy_url, transport="antigravity", timeout=5)
        return response.status_code

    async def drain(self):
        while self.workers:
            await asyncio.gather(*list(self.workers.values()), return_exceptions=True)

    async def close(self):
        self.closed = True
        try:
            await asyncio.wait_for(self.drain(), timeout=6)
        except asyncio.TimeoutError:
            for task in list(self.workers.values()):
                task.cancel()
            await asyncio.gather(*list(self.workers.values()), return_exceptions=True)


telemetry_service = TelemetryService()


def observe_post(function):
    @wraps(function)
    async def wrapped(url, *args, **kwargs):
        attempt = await telemetry_service.begin(url, kwargs.get("json"), kwargs.get("headers"),
                                                kwargs.get("proxy_url", ...), kwargs.get("transport"))
        if attempt:
            kwargs["proxy_url"] = attempt.snapshot.proxy_url
        try:
            response = await function(url, *args, **kwargs)
            if attempt:
                attempt.observe(response.content)
                attempt.finish(response.status_code)
            return response
        except BaseException as exc:
            if attempt:
                attempt.finish(None, exc)
            raise
    return wrapped


def observe_stream(function):
    @wraps(function)
    async def wrapped(url, body, *args, **kwargs):
        attempt = await telemetry_service.begin(url, body, kwargs.get("headers"),
                                                kwargs.get("proxy_url", ...), kwargs.get("transport"))
        if attempt:
            kwargs["proxy_url"] = attempt.snapshot.proxy_url
        stream = function(url, body, *args, **kwargs)
        status, error = None, None
        try:
            async for chunk in stream:
                if attempt:
                    status = getattr(chunk, "status_code", 200)
                    attempt.observe(chunk, complete_line=not kwargs.get("native", args[0] if args else False))
                yield chunk
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                await stream.aclose()
            finally:
                if attempt:
                    attempt.finish(status, error)
    return wrapped
