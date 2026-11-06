"""Constant-space request timing: no body copies, queues, tasks or global request map."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
import uuid

import httpx

from log import log


@dataclass(slots=True)
class RequestTiming:
    timing_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.monotonic)
    upstream_first_byte_seconds: float | None = None
    downstream_first_byte_seconds: float | None = None
    response_total_seconds: float | None = None
    upstream_response_seconds: float | None = None
    downstream_send_seconds: float | None = None
    monitor: object = None
    stages: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)

    def details(self):
        return {"stages": dict(self.stages), "parameters": dict(self.parameters)}


request_timing: ContextVar[RequestTiming | None] = ContextVar('request_timing', default=None)


_STAGE_NAMES = frozenset({"body_received", "auth_started", "auth_finished", "api_started",
                          "first_upstream_send", "last_upstream_send", "last_upstream_data",
                          "billing_started", "monitor_started", "monitor_finished"})


def mark_stage(name, *, replace=False):
    state = request_timing.get()
    if state is not None and name in _STAGE_NAMES and (replace or name not in state.stages):
        state.stages[name] = time.monotonic() - state.started


def capture_request_parameters(body):
    """Fixed metadata only: never retain prompts, tool schemas, URLs or identifiers."""
    state = request_timing.get()
    if state is None:
        return
    meta = {"stream": bool(body.get("stream")), "messages": len(body.get("messages") or []),
            "tools": len(body.get("tools") or [])}
    for name in ("max_tokens", "temperature", "top_p"):
        value = body.get(name)
        if isinstance(value, (int, float)):
            meta[name] = value
    raw_format = body.get("response_format")
    fmt = raw_format.get("type") if isinstance(raw_format, dict) else None
    if isinstance(fmt, str) and fmt in {"json_object", "json_schema", "text"}:
        meta["response_format"] = fmt
    state.parameters = meta


class _TimedStream(httpx.AsyncByteStream):
    def __init__(self, stream, state, started):
        self.stream = stream
        self.state = state
        self.started = started
        self.seen = False

    async def __aiter__(self):
        async for chunk in self.stream:
            if chunk:
                elapsed = time.monotonic() - self.started
                if not self.seen:
                    self.seen = True
                    self.state.upstream_first_byte_seconds = elapsed
                # Last received data, not iterator teardown after downstream sends.
                self.state.upstream_response_seconds = elapsed
                self.state.stages["last_upstream_data"] = time.monotonic() - self.state.started
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


def timing_hook(url):
    """Measure model inference only; OAuth/credential probes must not overwrite it.

    Each attempt replaces the previous attempt's first-byte value. The downstream
    timer includes all preceding retries and queueing. Native transport includes
    the local helper hop, without changing its buffering or protocol.
    """
    state = request_timing.get()
    if state is None or not any(part in url for part in (':generateContent', ':streamGenerateContent')):
        return None
    mark_stage("first_upstream_send")
    mark_stage("last_upstream_send", replace=True)
    started = time.monotonic()
    state.upstream_first_byte_seconds = None
    state.upstream_response_seconds = None

    async def on_response(response):
        response.stream = _TimedStream(response.stream, state, started)

    return on_response


def add_timing_hook(kwargs, url):
    hook = timing_hook(url)
    if hook is not None:
        hooks = dict(kwargs.get('event_hooks') or {})
        hooks['response'] = [*hooks.get('response', []), hook]
        kwargs['event_hooks'] = hooks


class RequestTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if (scope['type'] != 'http' or scope.get('method') != 'POST'
                or not path.startswith(('/v1/', '/v1beta/', '/antigravity/v1/',
                                        '/antigravity/v1beta/', '/vertex/v1/', '/vertex/v1beta/'))):
            return await self.app(scope, receive, send)
        state = RequestTiming()
        token = request_timing.set(state)

        async def timed_receive():
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                mark_stage("body_received")
            return message

        async def timed_send(message):
            if (message['type'] == 'http.response.body' and message.get('body')
                    and state.downstream_first_byte_seconds is None):
                state.downstream_first_byte_seconds = time.monotonic() - state.started
            if message['type'] == 'http.response.body':
                send_started = time.monotonic()
                try:
                    await send(message)
                finally:
                    # Sum only ASGI write calls; upstream waits between chunks are excluded.
                    state.downstream_send_seconds = (state.downstream_send_seconds or 0.0) + (time.monotonic() - send_started)
            else:
                await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                state.response_total_seconds = time.monotonic() - state.started

        try:
            await self.app(scope, timed_receive, timed_send)
        finally:
            request_timing.reset(token)
            # Await one bounded update inline; never leave a background task per request.
            if state.monitor is not None:
                try:
                    await state.monitor.finish_timing(state)
                except Exception as exc:
                    log.warning(f'[CALL MONITOR] timing update failed: {type(exc).__name__}')
                finally:
                    state.monitor = None
