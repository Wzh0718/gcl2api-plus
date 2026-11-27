import asyncio
import base64
import ipaddress
import re
import socket
from typing import Any

import httpx


# 上游（AGY / Gemini 2.5 端点）对“直传 http(s) 图片 URL”限制为每请求 10 张，而内联（inlineData）
# 只受请求体大小约束（Gemini API 文档口径 20 MB；AGY 通道实测 36 MiB 请求体、60 张内联图片仍返回
# 200，见 docs/ANTIGRAVITY_IMAGE_API.md）。
# 因此本地不再设产品级张数限制：真正的闸门是请求级 10 MiB 字节预算、20 s 总超时和 6 路并发。
# 这里的常量只用于快速拒绝病态输入（几千个 URL），避免先耗满 20 s 再报一个误导性的下载失败。
_MAX_REMOTE_IMAGES = 1000
_MAX_REMOTE_BYTES = 10 * 1024 * 1024
_MAX_DOWNLOAD_CONCURRENCY = 6
_MAX_REDIRECTS = 4
_DOWNLOAD_TIMEOUT = 20
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_TOO_LARGE = "Remote images exceed the 10 MiB limit per request"
_UNREACHABLE = "Could not download image_url. Check that the URL is publicly accessible"
_PORTS = {"http": 80, "https": 443}
_GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")
HTTP_IMAGE_SCHEMES = ("http://", "https://")
_GEMINI_FILE_URI = re.compile(
    r"https://generativelanguage\.googleapis\.com/v1(?:beta|alpha)?/files/[a-zA-Z0-9_-]+"
)


class RemoteImageError(ValueError):
    """A client-visible image URL error without the URL or response contents."""


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return address in _GLOBAL_UNICAST_V6 and not (address.sixtofour or address.teredo)
    return True


async def _public_address(url: httpx.URL) -> str:
    port = _PORTS.get(url.scheme)
    if not port or not url.host or url.userinfo or url.port not in (None, port):
        raise RemoteImageError("image_url must use a public HTTP or HTTPS URL on the default port")

    records = await asyncio.get_running_loop().getaddrinfo(
        url.raw_host.decode("ascii"), port, type=socket.SOCK_STREAM,
    )
    addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or not all(_is_public(address) for address in addresses):
        raise RemoteImageError("image_url must resolve to a public IP address")
    return str(min(addresses, key=lambda address: address.version))


def _mime_type(data: bytearray, content_type: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[4:8] == b"ftyp":
        if data[8:12] in (b"avif", b"avis"):
            return "image/avif"
        if content_type in {"image/heic", "image/heif", "image/avif"}:
            return content_type
    raise RemoteImageError("image_url did not return a supported image")


class _ByteBudget:
    """Share one per-request byte budget across concurrent downloads."""

    def __init__(self, total: int) -> None:
        self._remaining = total
        self._lock = asyncio.Lock()

    @property
    def remaining(self) -> int:
        return self._remaining

    async def take(self, size: int) -> bool:
        async with self._lock:
            if size > self._remaining:
                return False
            self._remaining -= size
            return True


async def _download(
    client: httpx.AsyncClient, url: httpx.URL, budget: _ByteBudget,
) -> tuple[dict[str, str], int]:
    for attempt in range(_MAX_REDIRECTS + 1):
        address = await _public_address(url)
        request = httpx.Request(
            "GET",
            url.copy_with(host=address),
            headers={"Host": url.netloc.decode("ascii"), "Accept-Encoding": "identity"},
            extensions={"sni_hostname": url.raw_host.decode("ascii")},
        )
        response = await client.send(request, stream=True)
        try:
            if response.status_code in _REDIRECT_CODES:
                location = response.headers.get("location")
                if not location or attempt == _MAX_REDIRECTS:
                    raise RemoteImageError("image_url returned an invalid or excessive redirect")
                url = url.join(location)
                continue

            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise RemoteImageError("image_url returned an unsupported content encoding")
            if int(response.headers.get("content-length", "0")) > budget.remaining:
                raise RemoteImageError(_TOO_LARGE)

            data = bytearray()
            async for chunk in response.aiter_raw():
                if not await budget.take(len(chunk)):
                    raise RemoteImageError(_TOO_LARGE)
                data.extend(chunk)
            content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
            mime_type = _mime_type(data, content_type)
            encoded = base64.b64encode(data).decode("ascii")
            return {"mimeType": mime_type, "data": encoded}, len(data)
        finally:
            await response.aclose()
    raise RemoteImageError("image_url returned too many redirects")


def remote_image_part(url: Any) -> dict[str, Any]:
    if not isinstance(url, str) or not url.lower().startswith(HTTP_IMAGE_SCHEMES):
        raise RemoteImageError("Image URL must use HTTP or HTTPS")
    return {"fileData": {"mimeType": "image/*", "fileUri": url}}


async def inline_remote_gemini_images(payload: dict[str, Any]) -> None:
    """Resolve HTTP(S) image parts on a copy of the affected Gemini contents."""
    from src.image_inline_policy import should_inline_remote_images

    if not await should_inline_remote_images():
        # 未命中内联策略：保持 fileData 透传，由上游自行抓取
        return
    contents = payload.get("contents") or []
    urls = []
    locations = []
    for content_index, content in enumerate(contents):
        if not isinstance(content, dict):
            continue
        for part_index, part in enumerate(content.get("parts") or []):
            if not isinstance(part, dict):
                continue
            file_data = part.get("fileData") or part.get("file_data")
            if not isinstance(file_data, dict):
                continue
            mime_type = file_data.get("mimeType") or file_data.get("mime_type")
            url = file_data.get("fileUri") or file_data.get("file_uri")
            if (
                isinstance(mime_type, str) and mime_type.lower().startswith("image/")
                and isinstance(url, str) and url.lower().startswith(HTTP_IMAGE_SCHEMES)
                and not (mime_type != "image/*" and _GEMINI_FILE_URI.fullmatch(url))
            ):
                urls.append(url)
                locations.append((content_index, part_index))

    if not urls:
        return
    images = await download_remote_images(urls)
    updated = list(contents)
    for (content_index, part_index), image in zip(locations, images, strict=True):
        original = contents[content_index]
        if updated[content_index] is original:
            updated[content_index] = {**original, "parts": list(original["parts"])}
        parts = updated[content_index]["parts"]
        part = parts[part_index].copy()
        part.pop("fileData", None)
        part.pop("file_data", None)
        part["inlineData"] = image
        parts[part_index] = part
    payload["contents"] = updated


async def download_remote_images(urls: list[str]) -> list[dict[str, str]]:
    """Download public images as Gemini inline data within a shared request budget.

    下载并发进行（上限 :data:`_MAX_DOWNLOAD_CONCURRENCY`），结果顺序与入参一致；
    任一图片失败或超出共享字节预算时整体失败，避免把半套图片发给上游。
    """
    if not urls:
        return []
    if len(urls) > _MAX_REMOTE_IMAGES:
        raise RemoteImageError(
            f"Too many remote image URLs in one request: {len(urls)} > {_MAX_REMOTE_IMAGES}"
        )

    budget = _ByteBudget(_MAX_REMOTE_BYTES)
    semaphore = asyncio.Semaphore(_MAX_DOWNLOAD_CONCURRENCY)
    results: list[dict[str, str] | None] = [None] * len(urls)
    failures: list[tuple[int, RemoteImageError]] = []

    async def fetch(index: int, url: str, client: httpx.AsyncClient) -> None:
        async with semaphore:
            if failures:
                return  # Do not start queued downloads after a sibling failed.
            try:
                image, _size = await _download(client, httpx.URL(url), budget)
            except RemoteImageError as exc:
                failures.append((index, exc))
                raise
            except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError):
                error = RemoteImageError(_UNREACHABLE)
                failures.append((index, error))
                raise error from None
            else:
                results[index] = image

    try:
        async with asyncio.timeout(_DOWNLOAD_TIMEOUT), httpx.AsyncClient(
            trust_env=False,
            timeout=5,
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client:
            try:
                async with asyncio.TaskGroup() as group:
                    for index, url in enumerate(urls):
                        group.create_task(fetch(index, url, client))
            except* RemoteImageError:
                # TaskGroup has cancelled and awaited siblings (including response
                # cleanup). Unwrap our client-facing error below, never leak a group.
                pass
    except TimeoutError:
        raise RemoteImageError(_UNREACHABLE) from None

    if failures:
        raise min(failures, key=lambda failure: failure[0])[1]
    return [image for image in results if image is not None]
