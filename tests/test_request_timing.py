import asyncio

import httpx
import pytest

from src.request_timing import RequestTimingMiddleware, request_timing, timing_hook


@pytest.mark.asyncio
async def test_stream_is_forwarded_without_buffering_and_context_is_released():
    forwarded = asyncio.Event()
    seen = []
    states = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'first'
            await asyncio.wait_for(forwarded.wait(), 1)
            yield b'last'

    async def app(scope, receive, send):
        state = request_timing.get()
        states.append(state)
        hook = timing_hook('https://example.com/v1internal:streamGenerateContent')
        response = httpx.Response(200, stream=Body())
        await hook(response)
        await send({'type': 'http.response.start', 'status': 200})
        async for chunk in response.aiter_bytes():
            await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
        await send({'type': 'http.response.body', 'body': b''})

    async def send(message):
        if message.get('body'):
            seen.append(message['body'])
            forwarded.set()

    await RequestTimingMiddleware(app)({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions'}, None, send)
    assert seen == [b'first', b'last']
    state = states[0]
    assert state.upstream_first_byte_seconds is not None
    assert state.downstream_first_byte_seconds >= state.upstream_first_byte_seconds
    assert state.response_total_seconds >= state.downstream_first_byte_seconds
    assert request_timing.get() is None


@pytest.mark.asyncio
async def test_timing_persists_without_content_capture_and_legacy_is_unknown(tmp_path, monkeypatch):
    from src.call_monitor import CallMonitor
    from src.storage_adapter import StorageAdapter

    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    monkeypatch.setenv('CALL_CONTENT_CAPTURE_ENABLED', 'false')
    adapter = StorageAdapter()
    await adapter.initialize()
    monitor = CallMonitor(adapter)
    try:
        await monitor.record(request_id='legacy', success=True)
        async def app(scope, receive, send):
            hook = timing_hook('https://example.com/v1internal:generateContent')
            # A real async response, without storing the response body in timing state.
            class Body(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'ok'
            response = httpx.Response(200, stream=Body())
            await hook(response)
            await response.aread()
            await monitor.record(request_id='new', success=True)
            await send({'type': 'http.response.body', 'body': b'ok'})
        async def send(message):
            pass
        await RequestTimingMiddleware(app)({'type': 'http', 'method': 'POST', 'path': '/v1/messages'}, None, send)
        rows = {r['request_id']: r for r in await adapter._backend.list_call_records()}
        assert rows['legacy']['downstream_first_byte_seconds'] is None
        assert rows['new']['upstream_first_byte_seconds'] is not None
        assert rows['new']['upstream_response_seconds'] is not None
        assert rows['new']['downstream_send_seconds'] >= 0
        assert rows['legacy']['upstream_response_seconds'] is None
        assert rows['legacy']['downstream_send_seconds'] is None
        assert rows['new']['response_total_seconds'] >= rows['new']['downstream_first_byte_seconds']
        assert monitor.realtime()[0]['response_total_seconds'] == rows['new']['response_total_seconds']
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_retry_replaces_upstream_timing_and_closes_underlying_stream():
    from src.request_timing import RequestTiming
    state = RequestTiming()
    token = request_timing.set(state)
    closed = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data'
        async def aclose(self):
            closed.append(True)
    try:
        for _ in range(3):
            hook = timing_hook('https://example.com/v1internal:generateContent')
            assert state.upstream_first_byte_seconds is None
            response = httpx.Response(200, stream=Body())
            await hook(response)
            await response.aread()
            assert state.upstream_first_byte_seconds is not None
        measured = state.upstream_first_byte_seconds
        assert timing_hook('https://oauth2.googleapis.com/token') is None
        assert state.upstream_first_byte_seconds == measured
        assert len(closed) == 3
    finally:
        request_timing.reset(token)


@pytest.mark.asyncio
async def test_concurrent_requests_and_cancellation_release_context():
    states = []
    ready = asyncio.Event()
    async def app(scope, receive, send):
        state = request_timing.get()
        states.append(state)
        if len(states) == 2:
            ready.set()
        await ready.wait()
        assert request_timing.get() is state
        raise asyncio.CancelledError()
    middleware = RequestTimingMiddleware(app)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await middleware({'type': 'http', 'method': 'POST', 'path': '/v1/messages'}, None, None)
        assert request_timing.get() is None
    await asyncio.wait_for(asyncio.gather(run(), run()), 2)
    assert states[0] is not states[1]
    assert states[0].response_total_seconds is None


@pytest.mark.asyncio
async def test_completed_requests_do_not_accumulate_timing_state(monkeypatch):
    import gc
    import tracemalloc
    import time
    from types import SimpleNamespace
    from src.call_monitor import CallMonitor
    import config
    monkeypatch.setenv('CALL_RECORDS_BUFFER_SIZE', '1000')
    monkeypatch.setattr(config, 'get_call_content_capture_enabled_sync', lambda: False)
    monitor = CallMonitor(SimpleNamespace(_backend=None))
    monitor._last_cleanup = time.time()
    async def app(scope, receive, send):
        await monitor.record(request_id=request_timing.get().timing_id, success=True)
        await send({'type': 'http.response.body', 'body': b'ok'})
    async def send(message):
        pass
    middleware = RequestTimingMiddleware(app)
    scope = {'type': 'http', 'method': 'POST', 'path': '/v1/messages'}
    async def batch():
        for _ in range(3000):
            await middleware(scope, None, send)
    tracemalloc.start()
    try:
        await batch()
        gc.collect()
        before = tracemalloc.get_traced_memory()[0]
        await batch()
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]
        # Thousands of completed requests must not leave thousands of timer objects.
        assert after - before < 64 * 1024
        assert len(monitor._buffer) == 1000
        assert all(row["response_total_seconds"] is not None for row in monitor._buffer)
        assert request_timing.get() is None
    finally:
        tracemalloc.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('native_transport', [False, True])
async def test_httpx_inference_transport_captures_first_byte(monkeypatch, streaming, native_transport):
    from contextlib import asynccontextmanager
    from src import httpx_client
    from src.request_timing import RequestTiming
    closed = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: first\n\n'
            yield b'data: last\n\n'
        async def aclose(self):
            closed.append(True)
    async def handler(request):
        return httpx.Response(200, stream=Body())
    @asynccontextmanager
    async def client(**kwargs):
        kwargs.pop('proxy_url', None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs) as c:
            yield c
    monkeypatch.setattr(httpx_client.http_client, 'get_client', client)
    monkeypatch.setattr(httpx_client.http_client, 'get_streaming_client', client)
    monkeypatch.setattr(httpx_client, 'get_antigravity_native_transport_enabled_sync', lambda: native_transport)
    if native_transport:
        from src.agy_native_transport import agy_native_transport
        from unittest.mock import AsyncMock
        @asynccontextmanager
        async def proxy(_):
            yield None
        monkeypatch.setattr(agy_native_transport, 'start', AsyncMock())
        monkeypatch.setattr(agy_native_transport, '_socket', lambda: '/tmp/test-timing.sock')
        monkeypatch.setattr(agy_native_transport, '_upstream_proxy', proxy)
        monkeypatch.setattr(httpx, 'AsyncHTTPTransport', lambda **kwargs: httpx.MockTransport(handler))
    state = RequestTiming()
    token = request_timing.set(state)
    try:
        url = 'https://example.com/v1internal:streamGenerateContent'
        if streaming:
            chunks = [chunk async for chunk in httpx_client.stream_post_async(url, {}, native=True, transport='antigravity')]
            assert chunks == [b'data: first\n\n', b'data: last\n\n']
        else:
            response = await httpx_client.post_async(url, json={}, transport='antigravity')
            assert response.content == b'data: first\n\ndata: last\n\n'
        assert state.upstream_first_byte_seconds is not None
        assert len(closed) == 1
    finally:
        request_timing.reset(token)


@pytest.mark.asyncio
async def test_downstream_send_time_excludes_waiting_for_upstream(monkeypatch):
    import src.request_timing as timing_module
    now = [0.0]
    from types import SimpleNamespace
    monkeypatch.setattr(timing_module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    states = []
    async def app(scope, receive, send):
        states.append(request_timing.get())
        states[-1].started = now[0]
        now[0] += 40  # Waiting for upstream: never counted as downstream send time.
        await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
        now[0] += 60  # Waiting for a later upstream chunk is also excluded.
        await send({'type': 'http.response.body', 'body': b'last'})
    async def send(message):
        now[0] += 0.01
    await RequestTimingMiddleware(app)({'type': 'http', 'method': 'POST', 'path': '/v1/messages'}, None, send)
    state = states[0]
    assert state.downstream_send_seconds == pytest.approx(0.02)
    assert state.response_total_seconds == pytest.approx(100.02)
    assert state.downstream_first_byte_seconds == pytest.approx(40)


@pytest.mark.asyncio
async def test_upstream_duration_ends_at_last_data_not_after_downstream_send(monkeypatch):
    import src.request_timing as timing_module
    from src.request_timing import RequestTiming
    now = [0.0]
    from types import SimpleNamespace
    monkeypatch.setattr(timing_module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    state = RequestTiming()
    token = request_timing.set(state)
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            now[0] = 5
            yield b'first'
            now[0] = 12
            yield b'last'
    try:
        hook = timing_hook('https://example.com/v1internal:generateContent')
        response = httpx.Response(200, stream=Body())
        await hook(response)
        async for chunk in response.aiter_bytes():
            now[0] += 3  # Consumer processing or downstream transmission.
        assert state.upstream_first_byte_seconds == 5
        assert state.upstream_response_seconds == 12
    finally:
        request_timing.reset(token)
