"""Request content snapshots scoped to one ASGI request; never capture headers/keys."""
from __future__ import annotations

from contextvars import ContextVar
import hashlib
import json
from typing import Any

import config


request_content: ContextVar[dict | None] = ContextVar("request_content", default=None)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_snapshot(payload: dict) -> dict:
    """Keep original protocol structures, including tool calls/results and multimodal parts."""
    if isinstance(payload.get("request"), dict):
        payload = payload["request"]
    messages = payload.get("messages", payload.get("contents", payload.get("input", [])))
    system = {key: payload[key] for key in
              ("system", "systemInstruction", "system_instruction", "instructions") if key in payload}
    if isinstance(messages, list):
        system_messages = [m for m in messages if isinstance(m, dict)
                           and m.get("role") in {"system", "developer"}]
        if system_messages:
            system["messages"] = system_messages
    parameters = {key: payload[key] for key in (
        "generationConfig", "generation_config", "toolConfig", "tool_choice", "temperature",
        "top_p", "top_k", "max_tokens", "max_completion_tokens", "max_output_tokens",
        "stream", "thinking", "reasoning", "response_format", "parallel_tool_calls",
        "prompt", "negative_prompt", "size", "quality", "n",
    ) if key in payload}
    return {"system": system, "messages": messages, "tools": payload.get("tools", []),
            "parameters": parameters}


def build_request_features(upstream: dict | None = None) -> tuple[dict, dict]:
    if not config.get_call_content_capture_enabled_sync():
        return {}, {}
    details = {}
    incoming = request_content.get()
    if incoming is not None:
        details["client"] = incoming
    if upstream is not None:
        details["upstream"] = content_snapshot(upstream)
    features = {}
    for stage, snapshot in details.items():
        parts = {}
        for name, value in snapshot.items():
            encoded = canonical_json(value).encode("utf-8")
            parts[name] = {"sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}
        messages = snapshot["messages"]
        tools = snapshot["tools"]
        features[stage] = {
            "sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
            "parts": parts,
            "message_count": len(messages) if isinstance(messages, list) else int(bool(messages)),
            "tool_count": len(tools) if isinstance(tools, list) else int(bool(tools)),
        }
    return features, details


class RequestContentMiddleware:
    """Observe the existing receive stream without consuming or replaying it.

    Pure ASGI middleware keeps the context alive through StreamingResponse completion.
    Only inference JSON routes are observed; panel/OAuth credentials are excluded.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from src.request_capture import capture_managed, capture_request
        token = capture_managed.set(True)
        try:
            return await capture_request(scope, receive, send, self._observe)
        finally:
            capture_managed.reset(token)

    async def _observe(self, scope, receive, send):
        path = scope.get("path", "")
        inference = path.startswith(("/v1/", "/v1beta/", "/antigravity/v1/", "/antigravity/v1beta/", "/vertex/v1/", "/vertex/v1beta/"))
        if scope["type"] != "http" or scope.get("method") != "POST" or not inference:
            return await self.app(scope, receive, send)
        if not config.get_call_content_capture_enabled_sync():
            token = request_content.set(None)
            try:
                return await self.app(scope, receive, send)
            finally:
                request_content.reset(token)
        content_type = dict(scope.get("headers", [])).get(b"content-type", b"").split(b";")[0].strip()
        token = request_content.set(None)
        chunks = bytearray()
        from src.capture_config import capture_settings
        max_body = capture_settings()["request_capture_max_body_kb"] * 1024
        overflow = False

        async def observe_receive():
            nonlocal overflow
            message = await receive()
            if content_type == b"application/json" and message["type"] == "http.request":
                chunk = message.get("body", b"")
                if len(chunks) + len(chunk) > max_body:
                    overflow = True
                    chunks.clear()
                if not overflow:
                    chunks.extend(chunk)
                if not message.get("more_body", False):
                    try:
                        payload = json.loads(chunks) if not overflow else None
                        if isinstance(payload, dict):
                            request_content.set(content_snapshot(payload))
                    except (ValueError, UnicodeDecodeError):
                        pass
                    finally:
                        chunks.clear()
            return message

        try:
            await self.app(scope, observe_receive, send)
        finally:
            request_content.reset(token)
