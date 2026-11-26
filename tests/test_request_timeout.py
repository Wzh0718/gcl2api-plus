"""A model request has one deadline across upload, inference and streaming."""

import asyncio
from contextlib import asynccontextmanager
import json

import pytest

import src.request_timing as timing
from src.stream_guard import ClosingStreamingResponse


@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setattr(timing, 'REQUEST_TIMEOUT_SECONDS', 0.05, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('path', [
    '/v1/chat/completions', '/v1/responses', '/v1/images/generations',
    '/antigravity/v1/messages', '/antigravity/v1beta/models/gemini:generateContent',
    '/vertex/v1/chat/completions',
])
async def test_deadline_cancels_inference_and_releases_resources(short_deadline, path):
    baseline = timing.get_inflight_requests()
    released = []
    messages = []
    states = []

    @asynccontextmanager
    async def upstream():
        try:
            yield
        finally:
            await asyncio.sleep(0)
            released.append(True)

    async def app(scope, receive, send):
        states.append(timing.request_timing.get())
        assert timing.get_inflight_requests() == baseline + 1
        async with upstream():
            await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(timing.RequestTimingMiddleware(app)(
        {'type': 'http', 'method': 'POST', 'path': path, 'http_version': '1.1'},
        None, send,
    ), 1)
    assert released == [True]
    assert timing.get_inflight_requests() == baseline
    assert timing.request_timing.get() is None
    assert messages[0]['status'] == 504
    assert (b'connection', b'close') in messages[0]['headers']
    error = json.loads(messages[1]['body'])['error']
    assert '1 hour' in error['message']
    assert states[0].request_info['timed_out'] is True


@pytest.mark.asyncio
async def test_deadline_cancels_stalled_body_receive(short_deadline):
    baseline = timing.get_inflight_requests()
    messages = []
    received = 0
    states = []

    async def receive():
        nonlocal received
        received += 1
        if received == 1:
            return {'type': 'http.request', 'body': b'{', 'more_body': True}
        await asyncio.Event().wait()

    async def app(scope, receive, send):
        states.append(timing.request_timing.get())
        while (await receive()).get('more_body', False):
            pass
        pytest.fail('The incomplete request must not reach inference')

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(timing.RequestTimingMiddleware(app)(
        {'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions'},
        receive, send,
    ), 1)
    assert messages[0]['status'] == 504
    assert timing.get_inflight_requests() == baseline
    assert id(states[0]) not in timing._active_timing_states
    assert timing.request_timing.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('spec_version', ['2.3', '2.4'])
async def test_stream_deadline_closes_upstream_without_a_second_response(short_deadline, spec_version):
    baseline = timing.get_inflight_requests()
    messages = []
    released = []

    async def chunks():
        try:
            while True:
                yield b'data: heartbeat\n\n'
                await asyncio.sleep(0.005)
        finally:
            await asyncio.sleep(0)
            released.append(True)

    async def app(scope, receive, send):
        await ClosingStreamingResponse(chunks(), media_type='text/event-stream')(scope, receive, send)

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(timing.RequestTimingMiddleware(app)(
        {'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions',
         'asgi': {'spec_version': spec_version}}, receive, send,
    ), 1)
    assert released == [True]
    assert len([m for m in messages if m['type'] == 'http.response.start']) == 1
    assert any(m.get('body') for m in messages)
    # An aborted stream must not masquerade as a successfully completed response.
    assert not any(m['type'] == 'http.response.body' and not m.get('more_body', False)
                   for m in messages)
    assert timing.get_inflight_requests() == baseline


@pytest.mark.asyncio
async def test_upstream_timeout_is_not_reclassified_as_request_deadline(short_deadline):
    async def app(scope, receive, send):
        raise TimeoutError('upstream timeout')

    with pytest.raises(TimeoutError, match='upstream timeout'):
        await timing.RequestTimingMiddleware(app)(
            {'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions'}, None, None,
        )


def test_production_deadline_is_one_hour():
    assert timing.REQUEST_TIMEOUT_SECONDS == 3600


@pytest.mark.asyncio
async def test_deadlines_are_per_request_and_do_not_cancel_younger_requests(short_deadline):
    baseline = timing.get_inflight_requests()
    entered = asyncio.Event()
    messages = []

    async def app(scope, receive, send):
        if scope['path'].endswith('completions'):
            entered.set()
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(0.03)
            await send({'type': 'http.response.start', 'status': 200})
            await send({'type': 'http.response.body', 'body': b'ok'})

    async def send(message):
        messages.append(message)

    middleware = timing.RequestTimingMiddleware(app)
    old = asyncio.create_task(middleware(
        {'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions'}, None, send,
    ))
    await entered.wait()
    await asyncio.sleep(0.03)
    await asyncio.gather(old, middleware(
        {'type': 'http', 'method': 'POST', 'path': '/v1/responses'}, None, send,
    ))
    assert sorted(m['status'] for m in messages if m['type'] == 'http.response.start') == [200, 504]
    assert timing.get_inflight_requests() == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['upload', 'inference', 'stream'])
async def test_hypercorn_closes_timed_out_tcp_requests(monkeypatch, phase):
    from hypercorn.asyncio.run import worker_serve
    from hypercorn.config import Config
    from hypercorn.utils import wrap_app

    monkeypatch.setattr(timing, 'REQUEST_TIMEOUT_SECONDS', 0.1, raising=False)
    baseline = timing.get_inflight_requests()
    baseline_tasks = set(asyncio.all_tasks())
    startup = asyncio.Event()
    shutdown = asyncio.Event()
    released = []

    async def app(scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                message = await receive()
                await send({'type': message['type'] + '.complete'})
                if message['type'] == 'lifespan.startup':
                    startup.set()
                else:
                    return
        if phase == 'upload':
            while (await receive()).get('more_body', False):
                pass
        if phase == 'stream':
            async def chunks():
                try:
                    while True:
                        yield b'data: heartbeat\n\n'
                        await asyncio.sleep(0.005)
                finally:
                    await asyncio.sleep(0)
                    released.append(True)
            await ClosingStreamingResponse(chunks(), media_type='text/event-stream')(scope, receive, send)
        else:
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                released.append(True)

    config = Config()
    config.bind = ['127.0.0.1:0']
    config.errorlog = None
    sockets = config.create_sockets()
    port = sockets.insecure_sockets[0].getsockname()[1]
    server = asyncio.create_task(worker_serve(
        wrap_app(timing.RequestTimingMiddleware(app), config.wsgi_max_body_size, 'asgi'),
        config, sockets=sockets, shutdown_trigger=shutdown.wait,
    ))
    writers = []
    try:
        await asyncio.wait_for(startup.wait(), 2)
        readers = []
        # Reproduce the production symptom with ten simultaneous incomplete uploads.
        for _ in range(10 if phase == 'upload' else 1):
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writers.append(writer)
            readers.append(reader)
            size = 1000 if phase == 'upload' else 1
            writer.write((f'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                          f'Content-Length: {size}\r\n\r\n').encode() + b'{')
            await writer.drain()
        await asyncio.sleep(0.02)
        assert timing.get_inflight_requests() == baseline + len(readers)
        responses = await asyncio.wait_for(asyncio.gather(*(r.read() for r in readers)), 2)
        for response in responses:
            if phase == 'stream':
                assert b'200 ' in response and b'data: heartbeat' in response
                assert not response.endswith(b'0\r\n\r\n')
            else:
                assert b'504 ' in response and b'connection: close' in response.lower()
        assert timing.get_inflight_requests() == baseline
        if phase != 'upload':
            assert released == [True]
    finally:
        for writer in writers:
            writer.close()
        await asyncio.gather(*(w.wait_closed() for w in writers), return_exceptions=True)
        shutdown.set()
        await asyncio.wait_for(server, 3)
        for sock in sockets.insecure_sockets:
            sock.close()
    await asyncio.sleep(0)
    assert not (set(asyncio.all_tasks()) - baseline_tasks)


@pytest.mark.asyncio
async def test_http2_timeout_ends_only_the_request_stream(short_deadline):
    import h2.config
    import h2.connection
    import h2.events
    from hypercorn.asyncio.run import worker_serve
    from hypercorn.config import Config
    from hypercorn.utils import wrap_app

    baseline = timing.get_inflight_requests()
    startup = asyncio.Event()
    shutdown = asyncio.Event()
    released = []

    async def app(scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                message = await receive()
                await send({'type': message['type'] + '.complete'})
                if message['type'] == 'lifespan.startup':
                    startup.set()
                else:
                    return
        if scope['method'] == 'GET':
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
            return
        async def chunks():
            try:
                yield b'data: first\n\n'
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                released.append(True)
        await ClosingStreamingResponse(chunks(), media_type='text/event-stream')(scope, receive, send)

    config = Config()
    config.bind = ['127.0.0.1:0']
    config.errorlog = None
    sockets = config.create_sockets()
    port = sockets.insecure_sockets[0].getsockname()[1]
    server = asyncio.create_task(worker_serve(
        wrap_app(timing.RequestTimingMiddleware(app), config.wsgi_max_body_size, 'asgi'),
        config, sockets=sockets, shutdown_trigger=shutdown.wait,
    ))
    writer = None
    try:
        await asyncio.wait_for(startup.wait(), 2)
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        h2client = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
        h2client.initiate_connection()
        h2client.send_headers(1, [(':method', 'POST'), (':scheme', 'http'),
                                 (':authority', 'localhost'), (':path', '/v1/chat/completions')],
                              end_stream=True)
        writer.write(h2client.data_to_send())
        await writer.drain()

        async def read_stream(stream_id):
            body = b''
            while True:
                data = await reader.read(65536)
                assert data, 'HTTP/2 connection closed before the stream ended'
                events = h2client.receive_data(data)
                ended = False
                for event in events:
                    if isinstance(event, h2.events.DataReceived):
                        h2client.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        if event.stream_id == stream_id:
                            body += event.data
                    if isinstance(event, h2.events.StreamEnded) and event.stream_id == stream_id:
                        ended = True
                writer.write(h2client.data_to_send())
                await writer.drain()
                if ended:
                    return body

        body = await asyncio.wait_for(read_stream(1), 2)
        assert b'data: first' in body
        assert b'event: error' in body and b'request_timeout' in body
        assert timing.get_inflight_requests() == baseline
        assert released == [True]
        # The deadline closes one logical request, not the reusable TCP connection.
        h2client.send_headers(3, [(':method', 'GET'), (':scheme', 'http'),
                                 (':authority', 'localhost'), (':path', '/keepalive')],
                              end_stream=True)
        writer.write(h2client.data_to_send())
        await writer.drain()
        assert await asyncio.wait_for(read_stream(3), 2) == b'ok'
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        shutdown.set()
        await asyncio.wait_for(server, 3)
        for sock in sockets.insecure_sockets:
            sock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['complete', 'disconnect', 'send_error'])
async def test_legacy_stream_still_closes_resources_on_other_exits(ending):
    released = []
    sent = asyncio.Event()
    messages = []

    async def chunks():
        try:
            yield b'first'
            if ending != 'complete':
                await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            released.append(True)

    async def receive():
        await sent.wait()
        if ending == 'disconnect':
            return {'type': 'http.disconnect'}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)
        if message.get('body'):
            sent.set()
            if ending == 'send_error':
                raise OSError('send failed')

    response = ClosingStreamingResponse(chunks())
    scope = {'type': 'http', 'asgi': {'spec_version': '2.3'}}
    if ending == 'send_error':
        with pytest.raises(OSError, match='send failed'):
            await response(scope, receive, send)
    else:
        await response(scope, receive, send)
    assert released == [True]
    if ending == 'complete':
        assert messages[-1] == {'type': 'http.response.body', 'body': b'', 'more_body': False}
