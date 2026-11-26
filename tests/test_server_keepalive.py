"""Exercise startup wiring and a real connection across the old 5s idle boundary."""

import asyncio
import socket
from unittest.mock import AsyncMock

import pytest


def capture_server_config(monkeypatch, workers):
    import hypercorn.asyncio
    import hypercorn.run
    import web

    captured = []

    async def capture_serve(app, config):
        captured.append(config)

    with monkeypatch.context() as patch:
        patch.setenv("WORKERS", str(workers))
        patch.setattr(web, "get_server_host", AsyncMock(return_value="127.0.0.1"))
        patch.setattr(web, "get_server_port", AsyncMock(return_value=7861))
        patch.setattr(hypercorn.asyncio, "serve", capture_serve)
        patch.setattr(hypercorn.run, "run", captured.append)
        web.main()
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("value,expected", [(None, 60.0), ("", 60.0), (" 90.5 ", 90.5)])
def test_idle_timeout_reaches_single_and_multi_worker_startup(monkeypatch, workers, value, expected):
    monkeypatch.delenv("HTTP_KEEP_ALIVE_TIMEOUT", raising=False)
    if value is not None:
        monkeypatch.setenv("HTTP_KEEP_ALIVE_TIMEOUT", value)
    assert capture_server_config(monkeypatch, workers).keep_alive_timeout == expected


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "invalid"])
def test_invalid_idle_timeout_rejects_startup(monkeypatch, value):
    monkeypatch.setenv("HTTP_KEEP_ALIVE_TIMEOUT", value)
    with pytest.raises(ValueError, match="HTTP_KEEP_ALIVE_TIMEOUT"):
        capture_server_config(monkeypatch, 1)


def test_same_tcp_connection_survives_the_old_idle_boundary(monkeypatch):
    from fastapi import FastAPI
    from hypercorn.asyncio import serve
    from web import keepalive

    monkeypatch.delenv("HTTP_KEEP_ALIVE_TIMEOUT", raising=False)
    config = capture_server_config(monkeypatch, 1)
    app = FastAPI()
    app.add_api_route("/keepalive", keepalive, methods=["HEAD"])
    config.accesslog = None

    async def exercise():
        shutdown = asyncio.Event()
        writer = None
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            address = listener.getsockname()
            # Hypercorn takes ownership of an fd:// listener and closes it on exit.
            config.bind = [f"fd://{listener.detach()}"]
            server = asyncio.create_task(serve(app, config, shutdown_trigger=shutdown.wait))
            try:
                reader, writer = await asyncio.open_connection(*address)

                async def head():
                    writer.write(b"HEAD /keepalive HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    await writer.drain()
                    headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                    assert headers.startswith(b"HTTP/1.1 200")

                await head()
                await asyncio.sleep(5.2)
                # Use the exact same TCP connection: reconnecting would hide the regression.
                await head()
            finally:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
                shutdown.set()
                await asyncio.wait_for(server, 3)

    asyncio.run(exercise())
