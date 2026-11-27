import base64
import socket

import httpx
import pytest

from src.converter import remote_images


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5y8AAAAASUVORK5CYII="
)
_PUBLIC_IP = "93.184.216.34"


def _payload(host: str) -> bytes:
    # 不同 host 返回不同字节，用来核对下载结果的顺序
    return _PNG + host.encode("ascii")


def _install_transport(monkeypatch):
    calls = []

    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_PUBLIC_IP, port))]

    async def send(transport, request):
        host = request.headers["host"].partition(":")[0]
        calls.append(host)
        return httpx.Response(200, stream=httpx.ByteStream(_payload(host)))

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    return calls


async def test_downloads_more_than_eight_images_in_request_order(monkeypatch):
    calls = _install_transport(monkeypatch)
    urls = [f"https://images{i}.example/photo.png" for i in range(12)]

    images = await remote_images.download_remote_images(urls)

    assert len(images) == len(urls)
    assert sorted(calls) == sorted(url.split("/")[2] for url in urls)
    for url, image in zip(urls, images):
        assert image["mimeType"] == "image/png"
        assert base64.b64decode(image["data"]) == _payload(url.split("/")[2])


async def test_rejects_above_image_cap_without_touching_the_network(monkeypatch):
    calls = _install_transport(monkeypatch)
    urls = [f"https://images{i}.example/photo.png"
            for i in range(remote_images._MAX_REMOTE_IMAGES + 1)]

    with pytest.raises(remote_images.RemoteImageError) as excinfo:
        await remote_images.download_remote_images(urls)

    assert str(excinfo.value) == (
        f"At most {remote_images._MAX_REMOTE_IMAGES} remote images are allowed per request"
    )
    assert not calls


async def test_shared_byte_budget_applies_across_concurrent_downloads(monkeypatch):
    _install_transport(monkeypatch)
    monkeypatch.setattr(remote_images, "_MAX_REMOTE_BYTES", len(_PNG) * 3)
    urls = [f"https://images{i}.example/photo.png" for i in range(4)]

    with pytest.raises(remote_images.RemoteImageError) as excinfo:
        await remote_images.download_remote_images(urls)

    assert "10 MiB" in str(excinfo.value)


async def test_unreachable_image_reports_client_visible_error(monkeypatch):
    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_PUBLIC_IP, port))]

    async def send(transport, request):
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)

    with pytest.raises(remote_images.RemoteImageError) as excinfo:
        await remote_images.download_remote_images(["https://images.example/photo.png"])

    assert "publicly accessible" in str(excinfo.value)

@pytest.mark.parametrize('error', [remote_images.RemoteImageError('budget exceeded'), httpx.ConnectError('network')])
async def test_failure_cancels_active_downloads_and_skips_queue(monkeypatch, error):
    import asyncio
    started, closed = [], []
    all_started = asyncio.Event()

    async def download(client, url, budget):
        index = int(url.path[1:])
        started.append(index)
        if len(started) == remote_images._MAX_DOWNLOAD_CONCURRENCY:
            all_started.set()
        try:
            await all_started.wait()
            if index == 0:
                raise error
            await asyncio.Event().wait()
        finally:
            closed.append(index)

    monkeypatch.setattr(remote_images, '_download', download)
    with pytest.raises(remote_images.RemoteImageError) as exc:
        await asyncio.wait_for(remote_images.download_remote_images(
            [f'https://images.example/{i}' for i in range(100)]), timeout=1)
    assert len(started) == remote_images._MAX_DOWNLOAD_CONCURRENCY
    assert sorted(closed) == sorted(started)
    assert str(exc.value) == ('budget exceeded' if isinstance(error, remote_images.RemoteImageError)
                             else remote_images._UNREACHABLE)

async def test_external_cancellation_is_not_converted_to_image_error(monkeypatch):
    import asyncio
    started, closed = asyncio.Event(), asyncio.Event()
    async def download(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    monkeypatch.setattr(remote_images, '_download', download)
    task = asyncio.create_task(remote_images.download_remote_images(['https://images.example/0']))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
