import asyncio
from contextlib import asynccontextmanager
import json

import httpx
import pytest

from src.capture_config import capture_policies, capture_settings, validate_capture_value
from src.request_capture import capture_request, get_capture_store
from src.httpx_client import post_async, stream_post_async, http_client


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUEST_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("REQUEST_CAPTURE_MIN_FREE_MB", "0")
    monkeypatch.setenv("REQUEST_CAPTURE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("CALL_CONTENT_CAPTURE_ENABLED", "false")
    monkeypatch.setenv("ANTIGRAVITY_NATIVE_TRANSPORT_ENABLED", "false")
    return get_capture_store()


async def run_request(app, payload=None, path="/antigravity/v1/chat/completions"):
    raw = json.dumps(payload or {"model": "test", "messages": [{"role": "user", "content": "hello"}]}).encode()
    async def receive():
        return {"type": "http.request", "body": raw}
    sent = []
    async def send(message):
        sent.append(message)
    await capture_request({"type": "http", "method": "POST", "path": path,
                           "headers": [(b"content-type", b"application/json")]}, receive, send, app)
    return sent


def rows(store, name):
    return store.query(name, capture_policies()[name], limit=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize('close_early', [False, True])
async def test_capture_stream_cross_task_advance_and_close(enabled, close_early):
    from src.request_capture import capture_stream, active_attempt, observe_stream_headers
    sentinel = object()
    @capture_stream
    async def chunks(url, body):
        observe_stream_headers(httpx.Response(200))
        assert active_attempt.get() is not sentinel
        yield b'data: {"response":{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}}\n\n'
        assert active_attempt.get() is not sentinel
        yield b'data: [DONE]\n\n'
    async def app(scope, receive, send):
        await receive()
        token = active_attempt.set(sentinel)
        try:
            stream = chunks('https://test/streamGenerateContent', {'model': 'm'})
            assert await asyncio.create_task(anext(stream))
            assert active_attempt.get() is sentinel
            if not close_early:
                assert await anext(stream)
                with pytest.raises(StopAsyncIteration):
                    await asyncio.create_task(anext(stream))
            await asyncio.create_task(stream.aclose())
            assert active_attempt.get() is sentinel
        finally:
            active_attempt.reset(token)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'done'})
    await run_request(app)
    finished = [r for r in rows(enabled, 'attempts') if r.get('phase') == 'finished']
    assert len(finished) == 1 and finished[0]['status_code'] == 200


@pytest.mark.asyncio
async def test_internal_403_survives_success_and_promotes_previous_success(enabled, monkeypatch):
    responses = iter([httpx.Response(200, json={"ok": True}),
                      httpx.Response(403, json={"error": {"code": 403, "details": [{"reason": "TOS_VIOLATION"}]}},
                                     headers={"x-cloud-trace-context": "trace-403"}),
                      httpx.Response(200, json={"ok": True})])
    class Client:
        async def post(self, *args, **kwargs):
            assert "capture_credential" not in kwargs
            return next(responses)
    @asynccontextmanager
    async def client(**kwargs):
        yield Client()
    monkeypatch.setattr(http_client, "get_client", client)
    async def app(scope, receive, send):
        await receive()
        await post_async("https://example.test/v1internal:generateContent", json={"model": "m", "request": {"sessionId": "-42"}}, capture_credential="account-a")
        if scope.get("retry"):
            await post_async("https://example.test/v1internal:generateContent", json={"model": "m2"}, capture_credential="account-b")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})
    await run_request(app)
    async def retry_app(scope, receive, send):
        scope["retry"] = True
        await app(scope, receive, send)
    sent = await run_request(retry_app)
    assert sent[0]["status"] == 200
    finished = [r for r in rows(enabled, "attempts") if r.get("phase") == "finished"]
    assert {r["status_code"] for r in finished} == {200, 403}
    failed = next(r for r in finished if r["status_code"] == 403)
    assert failed["credential_name"] == "account-a" and failed["reason"] == "TOS_VIOLATION"
    assert failed["trace_id"] == "trace-403"
    evidence = rows(enabled, "errors")
    assert any(r.get("kind") == "prior_success" for r in evidence)
    assert any(r.get("status_code") == 403 for r in evidence)
    assert rows(enabled, "features")  # independent of legacy content flag


@pytest.mark.asyncio
async def test_local_rejection_and_auth_routes(enabled):
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 429, "headers": []})
        await send({"type": "http.response.body", "body": b'{"error":"capacity busy"}'})
    await run_request(app)
    final = rows(enabled, "attempts")[0]
    assert final["phase"] == "final" and final["upstream_attempts"] == 0
    assert rows(enabled, "errors")[0]["status_code"] == 429
    count = len(rows(enabled, "attempts"))
    await run_request(app, path="/auth/callback")
    assert len(rows(enabled, "attempts")) == count


def test_configuration_rejects_unbounded_or_invalid_values():
    for key, value in [("request_capture_normal_max_mb", 0),
                       ("request_capture_errors_retention_hours", -1),
                       ("request_capture_context_count", 1.5),
                       ("request_capture_enabled", "wrong")]:
        with pytest.raises(ValueError):
            validate_capture_value(key, value)
    assert all(p["max_bytes"] > 0 for p in capture_policies(capture_settings()).values())


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/responses", "/v1/chat/completions",
                                 "/antigravity/v1/images/generations",
                                 "/antigravity/v1beta/models/gemini:generateContent",
                                 "/vertex/v1beta/models/gemini:streamGenerateContent",
                                 "/geminicli/v1/chat/completions"])
async def test_all_protocols_keep_features_without_raw_content(enabled, monkeypatch, path):
    monkeypatch.setenv("REQUEST_CAPTURE_NORMAL_ENABLED", "false")
    monkeypatch.setenv("REQUEST_CAPTURE_ERRORS_ENABLED", "false")
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    await run_request(app, path=path)
    assert len(rows(enabled, "features")) == 1
    assert not rows(enabled, "normal") and not rows(enabled, "errors")
    assert "hello" not in json.dumps(rows(enabled, "features"))


@pytest.mark.asyncio
async def test_stream_embedded_error_cancellation_and_native_transport(enabled, monkeypatch):
    from fastapi import Response
    from src import httpx_client as http
    from src.request_capture import capture_stream

    @capture_stream
    async def chunks(url, body):
        yield b'data: {"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}\n\n'
    async def native(*args, **kwargs):
        return httpx.Response(502, text="upstream request failed", headers={"x-request-id": "native-trace"})
    monkeypatch.setattr(http.agy_native_transport, "request", native)
    monkeypatch.setenv("ANTIGRAVITY_NATIVE_TRANSPORT_ENABLED", "true")
    async def app(scope, receive, send):
        await receive()
        stream = chunks("https://test/streamGenerateContent", {"model": "m"})
        await anext(stream)
        await stream.aclose()
        await post_async("https://test/generateContent", json={"model": "image"}, transport="antigravity", capture_credential="img-account")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})
    await run_request(app)
    finished = [r for r in rows(enabled, "attempts") if r.get("phase") == "finished"]
    assert {r["status_code"] for r in finished} == {429, 502}
    assert next(r for r in finished if r["status_code"] == 502)["trace_id"] == "native-trace"


@pytest.mark.asyncio
async def test_sensitive_fields_media_body_limit_and_credential_path(enabled, monkeypatch):
    from src.request_capture import capture_post
    @capture_post
    async def fake(url, **kwargs):
        return httpx.Response(403, json={"error": {"code": 403, "access_token": "secret-error"}})
    async def app(scope, receive, send):
        await receive()
        await fake("https://test/generateContent?key=secret-query", json={
            "access_token": "secret-upstream", "request": {"contents": [{"parts": [
                {"inlineData": {"mimeType": "image/png", "data": "secret-base64"}}]}]}},
            headers={"Authorization": "secret-header"})
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"denied"})
    await run_request(app, payload={"api_key": "secret-client", "messages": ["hello"]},
                      path="/antigravity/v1beta/models/m:generateContent/key=secret-path")
    stored = json.dumps([rows(enabled, p) for p in capture_policies()])
    for secret in ("secret-header", "secret-query", "secret-path", "secret-client", "secret-upstream", "secret-base64", "secret-error"):
        assert secret not in stored
    monkeypatch.setenv("REQUEST_CAPTURE_MAX_BODY_KB", "1")
    huge = {"messages": ["X" * 5000]}
    raw_bytes = json.dumps(huge).encode()
    async def local(scope, receive, send):
        assert (await receive())["body"] == raw_bytes
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b"invalid"})
    await run_request(local, payload=huge)
    assert rows(enabled, "features")[0]["features"]["client"]["wire"]["bytes"] == len(raw_bytes)
    assert rows(enabled, "errors")[0]["details"]["client"]["capture_omitted"] == "body_limit_or_content_type"


@pytest.mark.asyncio
async def test_transport_exception_records_attempt_before_reraising(enabled):
    from src.request_capture import capture_post
    @capture_post
    async def broken(url, **kwargs):
        raise httpx.ConnectError("contains-proxy-password-never-record")
    async def app(scope, receive, send):
        await receive()
        with pytest.raises(httpx.ConnectError):
            await broken("https://test/generateContent", json={"model": "m"}, capture_credential="a")
        await send({"type": "http.response.start", "status": 502, "headers": []})
        await send({"type": "http.response.body", "body": b"failed"})
    await run_request(app)
    finished = next(r for r in rows(enabled, "attempts") if r.get("phase") == "finished")
    assert finished["incomplete"] == "ConnectError"
    assert "contains-proxy-password" not in json.dumps(rows(enabled, "errors"))


@pytest.mark.asyncio
async def test_vertex_logical_retries_keep_recaptcha_private(enabled, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.api import vertex
    class Response:
        headers = {"x-request-id": "vertex-trace"}
        def __init__(self, status):
            self.status = status
        async def text(self):
            return '{"error":{"code":429}}' if self.status == 429 else '{}'
        def close(self):
            pass
    responses = iter([Response(429), Response(200)])
    monkeypatch.setattr(vertex, "WREQ_AVAILABLE", True)
    monkeypatch.setattr(vertex, "Emulation", SimpleNamespace(Chrome131="test"))
    monkeypatch.setattr(vertex, "wreq", SimpleNamespace(post=AsyncMock(side_effect=lambda *a, **k: next(responses))))
    monkeypatch.setattr(vertex, "fetch_recaptcha_token", AsyncMock(return_value="secret-recaptcha"))
    monkeypatch.setattr(vertex, "_build_non_stream_response", lambda text: {"candidates": []})
    async def app(scope, receive, send):
        await receive()
        result = await vertex.non_stream_request({"model": "vertex-m", "request": {"contents": []}})
        await send({"type": "http.response.start", "status": result.status_code, "headers": []})
        await send({"type": "http.response.body", "body": result.body})
    await run_request(app, path="/vertex/v1/chat/completions")
    finished = [r for r in rows(enabled, "attempts") if r.get("phase") == "finished"]
    assert {r["status_code"] for r in finished} == {200, 429}
    assert all(r["credential_name"] == "vertex:anonymous" for r in finished)
    assert "secret-recaptcha" not in json.dumps(rows(enabled, "normal"))


@pytest.mark.asyncio
async def test_empty_200_and_local_backpressure_are_not_lost(enabled):
    from src.request_capture import capture_post, record_local_capture
    @capture_post
    async def empty(url, **kwargs):
        return httpx.Response(200, content=b"")
    async def app(scope, receive, send):
        await receive()
        await record_local_capture("busy-account", "m")
        await empty("https://test/generateContent", json={"model": "m"}, capture_credential="empty-account")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"retried successfully"})
    await run_request(app)
    evidence = rows(enabled, "errors")
    assert any(r.get("incomplete") == "empty_response" and r["status_code"] == 200 for r in evidence)
    assert any(r.get("kind") == "local_queue" and r["status_code"] == 429 for r in evidence)
