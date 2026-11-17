"""Opt-in account telemetry from real gateway attempts, without conversation bodies.

Uses the observed Hub Jetski generation schema. It does not synthesize official
startup, tools, hooks, or conversation trajectories that the gateway did not run.
"""
import asyncio
import hashlib
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlsplit

from log import log
from src.storage.antigravity_telemetry_store import subject_ref
from src.storage_adapter import get_storage_adapter

ENDPOINT = "https://play.googleapis.com/log"
QUEUE_LIMIT = 256
BATCH_LIMIT = 16
RETRY_LIMIT = 3
DROP_HISTORY_LIMIT = 256
WORKER_LIMIT = 128
MAX_EVENT_AGE = 300

# 对外发送已下线：默认只允许本地预览（preview）。
# 该能力为实验性质、不建议在生产开启；仅当运维显式设置环境变量时才恢复发送。
SEND_RETIRED_MESSAGE = (
    "遥测对外发送已下线，当前仅保留本地预览（preview）。"
    "此能力属实验性质、不建议生产开启；如确需受控实验，"
    "需由运维设置环境变量 ANTIGRAVITY_TELEMETRY_ALLOW_SEND=1 并重启服务。"
)


def send_enabled() -> bool:
    """发送开关：每次调用都读环境变量，便于测试与热切换判定。"""
    return os.getenv("ANTIGRAVITY_TELEMETRY_ALLOW_SEND", "").strip() == "1"


def encode_log_request(snapshot, extra):
    from src.antigravity_telemetry_schema import hub_generation_event, encode_message
    events = extra if isinstance(extra, list) else [extra]
    encoded = []
    for item in events:
        event = hub_generation_event(snapshot, item)
        event['event_time_ms'] = int(item.get('_delivery', {}).get('enqueued_at', time.time()) * 1000)
        encoded.append(event)
    return encode_message('LogRequest', {'client_info': {'client_type': 16}, 'log_source': 2747,
                          'log_event': encoded, 'request_time_ms': int(time.time() * 1000)})


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
        self.extra = {"event_id": uuid.uuid4().hex,
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
            for source, target in [('modelVersion', 'response_model'), ('responseId', 'response_id')]:
                value = body.get(source)
                if isinstance(value, str) and 0 < len(value) <= 256:
                    self.extra[target] = value
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
        self.history_tasks = set()
        self.recovery_after = {}
        self.identity_tasks = {}
        self.identity_after = {}

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

    async def _lookup_email(self, account, proxy):
        from config import get_googleapis_proxy_url
        from src.httpx_client import get_async
        token = account["credential_data"].get("access_token") or account["credential_data"].get("token")
        response = await get_async(
            f"{(await get_googleapis_proxy_url()).rstrip('/')}/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {token}"}, proxy_url=proxy,
            transport="antigravity", timeout=5)
        response.raise_for_status()
        email = response.json().get("email")
        return email.strip().lower() if isinstance(email, str) and "@" in email else None

    async def _ensure_identity(self, adapter, account):
        # Existing identity is never replaced by an automatic lookup.
        if subject_ref(account) or account["disabled"] or self.closed:
            return account
        key = account["account_id"]
        task = self.identity_tasks.get(key)
        if task is None:
            if self.identity_after.get(key, 0) > time.monotonic():
                return account
            if len(self.identity_tasks) >= WORKER_LIMIT:
                return account
            if key not in self.identity_after and len(self.identity_after) >= WORKER_LIMIT:
                self.identity_after.pop(min(self.identity_after, key=self.identity_after.get))
            self.identity_after[key] = time.monotonic() + 60

            async def lookup():
                try:
                    async with asyncio.timeout(5):
                        current = await adapter.get_telemetry_account(account["filename"])
                        if not current or current["account_id"] != key or current["disabled"]:
                            return
                        if subject_ref(current):
                            return
                        expiry = datetime.fromisoformat(current["credential_data"]["expiry"].replace("Z", "+00:00"))
                        if expiry.tzinfo is None:
                            expiry = expiry.replace(tzinfo=timezone.utc)
                        if expiry <= datetime.now(timezone.utc):
                            await self._refresh_credentials(adapter, current)
                            current = await adapter.get_telemetry_account(account["filename"])
                            if not current or current["account_id"] != key or current["disabled"]:
                                return
                        _, proxy = await self._environment(adapter, current)
                        email = await self._lookup_email(current, proxy)
                        if email:
                            await adapter.save_telemetry_identity(current, email)
                except Exception as exc:
                    log.warning(f"[TELEMETRY] identity_lookup_failed: account={key} type={type(exc).__name__}")
                finally:
                    self.identity_tasks.pop(key, None)

            task = asyncio.create_task(lookup())
            self.identity_tasks[key] = task
        await asyncio.shield(task)
        current = await adapter.get_telemetry_account(account["filename"])
        if not current or current["account_id"] != key:
            raise ValueError("账号已变化，请重新加载")
        return current

    async def status(self, filename):
        adapter = await self.adapter_provider()
        account = await adapter.get_telemetry_account(filename)
        if not account:
            raise LookupError("凭证文件不存在")
        account = await self._ensure_identity(adapter, account)
        valid = bool(account["subject_ref"] and account["subject_ref"] == subject_ref(account))
        environment_matches = False
        if valid:
            try:
                current, _ = await self._environment(adapter, account)
                if not account["disabled"] and account["mode"] in {"preview", "send"}:
                    repaired = await self._sync_environment(adapter, account, current)
                    if repaired is not None:
                        account = repaired
                environment_matches = current == account["environment"]
            except ValueError:
                pass
        return {"mode": (account["mode"] or "off") if valid else "off",
                "installation_id": account["installation_id"] if valid else None,
                "environment_matches": environment_matches,
                # 面板据此禁用「发送至 Google」并提示已下线。
                "send_enabled": send_enabled(),
                "identity_ready": bool(subject_ref(account)),
                "identity_retry_seconds": max(0, round(self.identity_after.get(account["account_id"], 0) - time.monotonic())) if not subject_ref(account) else 0,
                "last_outcome": account["last_outcome"] if valid else "",
                "last_http_status": account["last_http_status"] if valid else None,
                "last_event_at": account["last_event_at"] if valid else None,
                "queue_depth": self.queues[account["account_id"]].qsize() if account["account_id"] in self.queues else 0,
                "queue_capacity": QUEUE_LIMIT, "batch_limit": BATCH_LIMIT}

    async def configure(self, filename, mode):
        adapter = await self.adapter_provider()
        account = await adapter.get_telemetry_account(filename)
        if not account:
            raise LookupError("凭证文件不存在")
        if mode == "send" and not send_enabled():
            raise ValueError(SEND_RETIRED_MESSAGE)
        if mode != "off":
            account = await self._ensure_identity(adapter, account)
            if not subject_ref(account):
                raise ValueError("账号邮箱自动获取暂未成功，系统将在冷却后自动重试；请检查账号授权和出口")
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
            # 历史遗留的 send 配置不再产生事件，避免任何对外发送。
            if account["mode"] == "send" and not send_enabled():
                return None
            expiry = datetime.fromisoformat(account["credential_data"]["expiry"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                return None
            environment, expected_proxy = await self._environment(adapter, account)
            from config import get_proxy_config
            actual_proxy = (await get_proxy_config() or None) if proxy_url is ... else proxy_url
            if (actual_proxy != expected_proxy or
                    _header(headers, "User-Agent") != environment["user_agent"]):
                return None
            account = await self._sync_environment(adapter, account, environment)
            if account is None:
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

    async def _sync_environment(self, adapter, account, environment):
        if environment == account["environment"]:
            return account
        # CAS on both account authorization and policy prevents resurrection of
        # a disabled policy while requests race a manual settings change.
        try:
            await adapter.configure_account_telemetry(
                account["filename"], account, account["mode"], environment)
        except ValueError:
            pass  # another request may already have repaired this environment
        current = await adapter.get_telemetry_account(account["filename"])
        if (not current or current["account_id"] != account["account_id"] or
                current["disabled"] or current["mode"] != account["mode"] or
                current["subject_ref"] != account["subject_ref"] or
                subject_ref(current) != account["subject_ref"] or
                current["environment"] != environment):
            return None
        return current

    def enqueue(self, snapshot, event):
        # Age starts when generation finishes, not when inference starts.
        snapshot = replace(snapshot, created_at=time.monotonic())
        event["_delivery"] = {"enqueued_at": time.time(), "send_ms": 0}
        from src.antigravity_telemetry_schema import hub_generation_event
        event["_wire"] = hub_generation_event(snapshot, event)
        event["_wire"]["event_time_ms"] = int(event["_delivery"]["enqueued_at"] * 1000)
        key = snapshot.account_id
        queue = self.queues.get(key)
        if self.closed or (queue is None and len(self.workers) >= WORKER_LIMIT):
            self._drop(snapshot, event, "discarded_shutdown" if self.closed else "discarded_worker_limit")
            return
        if queue is None:
            queue = self.queues[key] = asyncio.Queue(QUEUE_LIMIT)
        try:
            queue.put_nowait((snapshot, event))
        except asyncio.QueueFull:
            self._drop(snapshot, event, "discarded_queue_full")
            return
        if key not in self.workers:
            from contextvars import Context
            self.workers[key] = asyncio.create_task(self._work(key), context=Context())

    def _drop(self, snapshot, event, reason):
        log.warning(f"[TELEMETRY] {reason}: account={snapshot.filename}")
        # Keep overload diagnostics bounded as well as the delivery queues.
        if len(self.history_tasks) >= DROP_HISTORY_LIMIT:
            return
        async def record():
            try:
                adapter = await self.adapter_provider()
                await self._record_event(adapter, snapshot, event, reason)
                await adapter.record_telemetry_outcome(snapshot.account_id, snapshot.policy_revision, reason)
            except Exception as exc:
                log.warning(f"[TELEMETRY] drop_history_failed: {type(exc).__name__}")
        from contextvars import Context
        task = asyncio.create_task(record(), context=Context())
        self.history_tasks.add(task)
        task.add_done_callback(self.history_tasks.discard)

    async def _refresh_credentials(self, adapter, account, rejected_token=None):
        from src.credential_manager import credential_manager
        if rejected_token:
            key, now = account["account_id"], time.monotonic()
            if self.recovery_after.get(key, 0) > now:
                return
            if key not in self.recovery_after and len(self.recovery_after) >= WORKER_LIMIT:
                self.recovery_after.pop(min(self.recovery_after, key=self.recovery_after.get))
            self.recovery_after[key] = now + 60
        # Reuse the manager's account-specific single-flight refresh/lease/CAS.
        # Telemetry never chooses a substitute account.
        await credential_manager._refresh_token(
            account["credential_data"], account["filename"], mode="antigravity",
            rejected_access_token=rejected_token)

    async def _delivery_snapshot(self, adapter, snapshot, *, recover=True):
        if time.monotonic() - snapshot.created_at > MAX_EVENT_AGE:
            return None, "discarded_expired"
        account = await adapter.get_telemetry_account(snapshot.filename)
        if not account or account["account_id"] != snapshot.account_id:
            return None, "discarded_account_deleted"
        if account["disabled"]:
            return None, "discarded_account_disabled"
        if subject_ref(account) != snapshot.subject or account["subject_ref"] != snapshot.subject:
            return None, "discarded_identity_changed"
        if account["policy_revision"] != snapshot.policy_revision or account["mode"] != snapshot.mode:
            return None, "discarded_policy_changed"
        try:
            environment, proxy = await self._environment(adapter, account)
        except ValueError:
            return None, "discarded_route_unavailable"
        if _digest(environment) != snapshot.environment or proxy != snapshot.proxy_url:
            await self._sync_environment(adapter, account, environment)
            return None, "discarded_environment_changed"
        expiry = datetime.fromisoformat(account["credential_data"]["expiry"].replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            if recover:
                await self._refresh_credentials(adapter, account)
                return await self._delivery_snapshot(adapter, snapshot, recover=False)
            return None, "authentication_expired"
        token = account["credential_data"].get("access_token") or account["credential_data"].get("token")
        if not token:
            return None, "authentication_unavailable"
        return replace(snapshot, access_token=token), None

    async def _still_bound(self, adapter, snapshot):
        current, _ = await self._delivery_snapshot(adapter, snapshot, recover=False)
        return current is not None

    async def _finish_delivery(self, adapter, items, outcome, status=None, attempts=0):
        for snapshot, event in items:
            event["_delivery"]["total_ms"] = round((time.monotonic()-snapshot.created_at)*1000)
            await self._record_event(adapter, snapshot, event, outcome, status, attempts)
        if items:
            snapshot = items[-1][0]
            await adapter.record_telemetry_outcome(snapshot.account_id, snapshot.policy_revision, outcome, status)

    async def _deliver(self, adapter, items):
        # 兜底：开关关闭时绝不进入网络发送流程。
        if not send_enabled():
            await self._finish_delivery(adapter, items, "send_retired")
            return
        status, outcome = None, "send_failed"
        for attempt in range(RETRY_LIMIT):
            ready = []
            for snapshot, event in items:
                current, reason = await self._delivery_snapshot(adapter, snapshot)
                if reason:
                    await self._finish_delivery(adapter, [(snapshot, event)], reason, status, attempt)
                else:
                    ready.append((current, event))
            items = ready
            if not items:
                return
            for snapshot, event in items:
                event["_delivery"]["batch_size"] = len(items)
                await self._record_event(adapter, snapshot, event, "sending", status, attempt+1)
            started = time.monotonic()
            retry_after = None
            try:
                # Every item was grouped by account, policy and environment;
                # use the latest token from that same account for the whole batch.
                result = await asyncio.wait_for(
                    self._send(items[-1][0], encode_log_request(items[-1][0], [event for _, event in items])),
                    timeout=6)
                status, retry_after = result if isinstance(result, tuple) else (result, None)
                outcome = ("sent" if status == 200 else "rate_limited" if status == 429 else
                           "authentication_failed" if status == 401 else "http_rejected" if status < 500 else "server_failed")
            except asyncio.TimeoutError:
                status, outcome = None, "send_timeout"
            except Exception:
                status, outcome = None, "transport_failed"
            duration = round((time.monotonic()-started)*1000)
            for _, event in items:
                event["_delivery"]["send_ms"] += duration
            if status == 200 or (status is not None and status < 500 and status not in (401, 408, 429)):
                break
            if attempt+1 < RETRY_LIMIT:
                if status == 401 and attempt == 0:
                    account = await adapter.get_telemetry_account(items[-1][0].filename)
                    if (account and not account["disabled"] and account["account_id"] == items[-1][0].account_id
                            and subject_ref(account) == items[-1][0].subject
                            and account["mode"] == items[-1][0].mode
                            and account["policy_revision"] == items[-1][0].policy_revision):
                        await self._refresh_credentials(adapter, account, items[-1][0].access_token)
                delay = retry_after if retry_after is not None else 2**attempt
                # Respect Retry-After rather than retrying earlier to meet our TTL.
                if any(time.monotonic()-snapshot.created_at+delay > MAX_EVENT_AGE for snapshot, _ in items):
                    break
                for snapshot, event in items:
                    await self._record_event(adapter, snapshot, event, "retrying", status, attempt+1)
                await asyncio.sleep(max(0, delay))
        await self._finish_delivery(adapter, items, outcome, status, attempt+1)

    async def _work(self, key):
        queue = self.queues[key]
        pending = []
        adapter = None
        try:
            adapter = await self.adapter_provider()
            while not queue.empty():
                pending = [queue.get_nowait() for _ in range(min(queue.qsize(), BATCH_LIMIT))]
                groups = {}
                for snapshot, event in pending:
                    event["_delivery"]["queue_ms"] = round((time.monotonic()-snapshot.created_at)*1000)
                    await self._record_event(adapter, snapshot, event, "queued")
                    current, reason = await self._delivery_snapshot(adapter, snapshot)
                    if reason:
                        await self._finish_delivery(adapter, [(snapshot, event)], reason)
                    elif snapshot.mode == "preview":
                        from src.storage.request_capture_store import CaptureStore
                        root = os.path.join(os.getenv("CREDENTIALS_DIR", "./creds"), "telemetry-preview", str(key))
                        saved = CaptureStore(root).write("attempts", {"created_at": time.time(), **event},
                            {"enabled": True, "max_bytes": 2 * 1024**2, "retention_hours": 24},
                            min_free_bytes=64 * 1024**2, max_entry_bytes=8192)
                        await self._finish_delivery(adapter, [(snapshot, event)], "preview" if saved else "preview_write_failed")
                    else:
                        group = (current.account_id, current.policy_revision, current.environment, current.access_token)
                        groups.setdefault(group, []).append((current, event))
                for items in groups.values():
                    await self._deliver(adapter, items)
                for _ in pending:
                    queue.task_done()
                pending = []
        except asyncio.CancelledError:
            if adapter:
                unfinished = pending + [queue.get_nowait() for _ in range(queue.qsize())]
                # Don't overwrite final records from earlier groups in this batch.
                for snapshot, event in unfinished:
                    if "total_ms" not in event["_delivery"]:
                        await self._finish_delivery(adapter, [(snapshot, event)], "discarded_shutdown")
            raise
        except Exception as exc:
            log.warning(f"[TELEMETRY] delivery_failed: account={key} type={type(exc).__name__}")
            for snapshot, event in pending + [queue.get_nowait() for _ in range(queue.qsize())]:
                if adapter and "total_ms" not in event["_delivery"]:
                    await self._finish_delivery(adapter, [(snapshot, event)], "processing_failed")
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
        retry_after = None
        value = response.headers.get("Retry-After")
        if value:
            try:
                retry_after = max(0, float(value))
                if not math.isfinite(retry_after):
                    retry_after = None
            except ValueError:
                from email.utils import parsedate_to_datetime
                try:
                    retry_after = max(0, parsedate_to_datetime(value).timestamp()-time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        return response.status_code, retry_after

    async def drain(self):
        while self.workers or self.history_tasks:
            await asyncio.gather(*list(self.workers.values()), *list(self.history_tasks), return_exceptions=True)

    async def close(self):
        self.closed = True
        for task in list(self.identity_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.identity_tasks.values()), return_exceptions=True)
        try:
            await asyncio.wait_for(self.drain(), timeout=6)
        except asyncio.TimeoutError:
            for task in [*self.workers.values(), *self.history_tasks]:
                task.cancel()
            await asyncio.gather(*list(self.workers.values()), *list(self.history_tasks), return_exceptions=True)


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
