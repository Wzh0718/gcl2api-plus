"""首字降级只影响当前慢请求，并保留原模型、计费及协议语义。"""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Response
from test_antigravity_model_fallback import _FakeResponse, _patch_common

import src.api.antigravity as api
from src.antigravity_ttft import (
    FirstOutputDeadline,
    FirstOutputTimeoutError,
    gate_first_output,
)

CHAIN = ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-3.8-flash-low"]


def _frame(part):
    candidate = {"content": {"parts": [part]}}
    return "data: " + json.dumps(
        {"response": {"candidates": [candidate]}}, ensure_ascii=False
    )


@pytest.fixture
def attempts(monkeypatch):
    _patch_common(monkeypatch, CHAIN, max_retries=0, ttft_seconds=0.025)
    monkeypatch.setattr(
        api, "get_antigravity_stream2nostream", AsyncMock(return_value=True)
    )
    credential = ("ttft.json", {"access_token": "test", "project_id": "project"})
    monkeypatch.setattr(
        api.credential_manager,
        "get_valid_credential",
        AsyncMock(return_value=credential),
    )
    recorder = AsyncMock()
    monkeypatch.setattr(api, "get_billing_recorder", AsyncMock(return_value=recorder))
    return recorder


@pytest.mark.parametrize("streaming", [True, False])
async def test_only_slow_request_switches_and_bills_once(
    monkeypatch, attempts, streaming
):
    sent = {"fast": [], "slow": []}
    closed = {"fast": [], "slow": []}

    async def upstream(body, headers, first_output_deadline=None, **kwargs):
        assert streaming and ":streamGenerateContent" in kwargs["url"]
        tag = headers["X-Test"]
        model = body["model"]
        assert sent[tag] == closed[tag]
        sent[tag].append(model)
        try:
            await asyncio.sleep(0.05)
            if first_output_deadline:
                first_output_deadline.start()
            if tag == "slow" and model != CHAIN[-1]:
                yield ": heartbeat"
                yield 'data: {"response":{"modelVersion":"discarded"}}'
                await asyncio.Event().wait()
            yield _frame({"text": tag})
            await asyncio.sleep(0.06)
            yield _frame({"text": "!"})
            metadata = {
                "modelVersion": "winner",
                "usageMetadata": {"totalTokenCount": 9},
            }
            yield "data: " + json.dumps({"response": metadata})
            yield "data: [DONE]"
        finally:
            closed[tag].append(model)

    monkeypatch.setattr(api, "stream_post_async", upstream)

    async def post(*, url, headers, first_output_deadline=None, **kwargs):
        assert url.endswith(":generateContent") and not streaming

        tag, model = headers["X-Test"], kwargs["json"]["model"]
        assert sent[tag] == closed[tag]
        sent[tag].append(model)
        try:
            if first_output_deadline:
                first_output_deadline.start()
                if tag == "slow":
                    await asyncio.Event().wait()
                first_output_deadline.stop()
            await asyncio.sleep(0.06)
            payload = {
                "modelVersion": "winner",
                "usageMetadata": {"totalTokenCount": 9},
                "candidates": [{"content": {"parts": [{"text": tag + "!"}]}}],
            }
            return _FakeResponse(200, json.dumps(payload).encode())
        finally:
            closed[tag].append(model)

    monkeypatch.setattr(api, "post_async", post)

    async def request(tag):
        body = {"model": CHAIN[0], "request": {"contents": []}}
        if streaming:
            items = [
                item async for item in api.stream_request(body, headers={"X-Test": tag})
            ]
            assert items[:2] == [_frame({"text": tag}), _frame({"text": "!"})]
            assert "winner" in items[2] and items[3:] == ["data: [DONE]"]
        else:
            response = await api.non_stream_request(body, headers={"X-Test": tag})
            assert response.status_code == 200
            payload = json.loads(response.body)
            assert (
                payload["modelVersion"] == "winner"
                and payload["usageMetadata"]["totalTokenCount"] == 9
            )
            candidate = payload["candidates"][0]
            assert candidate["content"]["parts"] == [{"text": tag + "!"}]

    await asyncio.wait_for(asyncio.gather(request("fast"), request("slow")), 5)
    assert sent == closed == {"fast": CHAIN[:1], "slow": CHAIN}
    bills = [call.kwargs for call in attempts.record.await_args_list]
    assert sorted(row["model"] for row in bills) == sorted([CHAIN[0], CHAIN[-1]])
    assert all(row["request_model"] == CHAIN[0] and row["success"] for row in bills)
    assert all(row["upstream_model"] == "winner" for row in bills)


@pytest.mark.parametrize(
    "part",
    [
        {"text": "首字"},
        {"functionCall": {"name": "hello"}},
        {"thoughtSignature": "sig"},
    ],
)
async def test_fragmented_native_event_commits_before_slow_remainder(part):
    frame = (_frame(part) + "\r\n\r\n").encode()
    split = frame.index("首".encode()) + 1 if "text" in part else len(frame) // 2
    chunks = [b": ping\r\n\r\n", frame[:split], frame[split:]]
    deadline = FirstOutputDeadline(0.025)

    async def upstream():
        deadline.start()
        for chunk in chunks:
            yield chunk
        await asyncio.sleep(0.06)
        yield b"data: [DONE]\n\n"

    assert [
        item async for item in gate_first_output(upstream(), deadline.timer, "unused")
    ] == [*chunks, b"data: [DONE]\n\n"]


async def test_client_cancel_never_starts_next_attempt(monkeypatch, attempts):
    started = asyncio.Event()
    closed = asyncio.Event()
    models = []

    async def upstream(body, first_output_deadline, **kwargs):
        models.append(body["model"])
        first_output_deadline.start()
        try:
            yield ": ping"
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(api, "stream_post_async", upstream)
    generator = api.stream_request({"model": CHAIN[0], "request": {}})
    task = asyncio.create_task(anext(generator))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await generator.aclose()
    assert closed.is_set() and models == CHAIN[:1]
    attempts.record.assert_not_awaited()


@pytest.mark.parametrize("case", ["outside", "tail", "disabled", "empty", "locked"])
async def test_ineligible_non_stream_uses_original_transport(
    monkeypatch, attempts, case
):
    model = {"tail": CHAIN[-1], "outside": "gemini-2.5-flash"}.get(case, CHAIN[0])
    if case == "disabled":
        monkeypatch.setattr(
            api, "get_antigravity_ttft_fallback_seconds", AsyncMock(return_value=0)
        )
    if case == "empty":
        monkeypatch.setattr(
            api, "get_antigravity_model_fallback_chain", AsyncMock(return_value=[])
        )
    if case == "locked":
        monkeypatch.setattr("src.claude_routing.key_allowed_model", lambda: model)

    post = AsyncMock(return_value=_FakeResponse(200, b'{"response":{"candidates":[]}}'))
    monkeypatch.setattr(api, "post_async", post)
    await api.non_stream_request({"model": model, "request": {}})
    assert post.await_count == 1 and post.await_args.kwargs["json"]["model"] == model


@pytest.mark.parametrize("native_transport", [False, True])
@pytest.mark.parametrize("status", [200, 429])
async def test_transport_times_only_successful_first_output(
    monkeypatch, native_transport, status
):
    from src import httpx_client as transport

    deadline = FirstOutputDeadline(0.025)

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.08)
            yield b"quota"

    def handle(request):
        assert deadline.timer.when() is not None
        return httpx.Response(status, headers={"Retry-After": "12"}, stream=SlowBody())

    mock_transport = httpx.MockTransport(handle)

    @asynccontextmanager
    async def client(**kwargs):
        await asyncio.sleep(0.06)
        async with httpx.AsyncClient(transport=mock_transport) as connection:
            yield connection

    monkeypatch.setattr(
        transport,
        "get_antigravity_native_transport_enabled_sync",
        lambda: native_transport,
    )
    monkeypatch.setattr(transport.http_client, "get_streaming_client", client)
    monkeypatch.setattr(transport.agy_native_transport, "ensure_ready", AsyncMock())
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: mock_transport)
    send = transport.stream_post_async.__wrapped__.__wrapped__
    raw = send(
        "https://mock.test/streamGenerateContent",
        {},
        proxy_url=None,
        transport="antigravity",
        first_output_deadline=deadline,
    )
    guarded = gate_first_output(raw, deadline.timer, "next")
    try:
        if status == 429:
            chunks = [chunk async for chunk in guarded]
            assert len(chunks) == 1 and isinstance(chunks[0], Response)
            assert (
                chunks[0].status_code == 429
                and chunks[0].headers["Retry-After"] == "12"
            )
        else:
            with pytest.raises(FirstOutputTimeoutError):
                await anext(guarded)
    finally:
        await guarded.aclose()
        await raw.aclose()


@pytest.mark.parametrize("terminal", ["eof", "done", "error", "disconnect", "waiting"])
async def test_metadata_does_not_commit_or_restart_clock(terminal):
    deadline = FirstOutputDeadline(0.03)

    async def source():
        deadline.start()
        yield ": heartbeat"
        yield 'data: {"response":{"usageMetadata":{"totalTokenCount":1}}}'
        if terminal == "done":
            yield "data: [DONE]"
        elif terminal == "error":
            yield 'data: {"error":{"code":429}}'
        elif terminal == "disconnect":
            raise httpx.ReadError("disconnected")
        elif terminal == "waiting":
            for _ in range(20):
                await asyncio.sleep(0.005)
                yield ": ping"

    guarded = gate_first_output(source(), deadline.timer, "next")
    if terminal == "waiting":
        with pytest.raises(FirstOutputTimeoutError):
            await anext(guarded)
    elif terminal == "disconnect":
        with pytest.raises(httpx.ReadError):
            await anext(guarded)
    else:
        items = [item async for item in guarded]
        assert [item.status_code for item in items] == (
            [429] if terminal == "error" else []
        )


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize(
    "case", ["first_body", "whitespace", "error", "empty", "disconnect"]
)
async def test_plain_json_transport_waits_only_for_first_return(
    monkeypatch, native, case
):
    from src import httpx_client as transport

    deadline = FirstOutputDeadline(0.03)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if case == "empty":
                return
            if case == "disconnect":
                raise httpx.ReadError("disconnected")
            yield b"{" if case == "first_body" else b" "
            await asyncio.sleep(0.08)
            yield b'"candidates":[]}'

    def handle(request):
        assert deadline.timer.when() is not None
        if native:
            assert request.headers.get("X-Agy-Streaming") != "1"
        else:
            assert request.url.path.endswith(":generateContent")
        return httpx.Response(429 if case == "error" else 200, stream=Body())

    mock = httpx.MockTransport(handle)

    @asynccontextmanager
    async def client(**kwargs):
        async with httpx.AsyncClient(transport=mock) as connection:
            yield connection

    monkeypatch.setattr(
        transport, "get_antigravity_native_transport_enabled_sync", lambda: native
    )
    monkeypatch.setattr(transport.http_client, "get_client", client)
    monkeypatch.setattr(transport.agy_native_transport, "ensure_ready", AsyncMock())
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: mock)
    send = transport.post_async.__wrapped__.__wrapped__

    async def request():
        async with deadline.timer:
            return await send(
                "https://mock.test/v1internal:generateContent",
                json={},
                proxy_url=None,
                transport="antigravity",
                first_output_deadline=deadline,
            )

    if case == "whitespace":
        with pytest.raises(TimeoutError):
            await request()
        assert deadline.timer.expired()
    elif case == "disconnect":
        with pytest.raises(httpx.ReadError):
            await request()
        assert not deadline.timer.expired()
    else:
        result = await request()
        assert not deadline.timer.expired()
        assert result.status_code == (429 if case == "error" else 200)
        if case != "error":
            assert result.content == (
                b'{"candidates":[]}' if case == "first_body" else b""
            )
