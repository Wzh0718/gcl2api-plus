import asyncio
import base64
import ipaddress
import re
import socket
from typing import Any

import httpx


_MAX_REMOTE_IMAGES = 8
_MAX_REMOTE_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 4
_DOWNLOAD_TIMEOUT = 20
_REDIRECT_CODES = {301, 302, 303, 307, 308}
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


async def _download(
    client: httpx.AsyncClient, url: httpx.URL, max_bytes: int,
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
            if int(response.headers.get("content-length", "0")) > max_bytes:
                raise RemoteImageError("Remote images exceed the 10 MiB limit per request")

            data = bytearray()
            async for chunk in response.aiter_raw():
                if len(data) + len(chunk) > max_bytes:
                    raise RemoteImageError("Remote images exceed the 10 MiB limit per request")
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
    """Download public images as Gemini inline data within a shared request budget."""
    if not urls:
        return []
    if len(urls) > _MAX_REMOTE_IMAGES:
        raise RemoteImageError(f"At most {_MAX_REMOTE_IMAGES} remote images are allowed per request")

    images = []
    remaining_bytes = _MAX_REMOTE_BYTES
    try:
        async with asyncio.timeout(_DOWNLOAD_TIMEOUT), httpx.AsyncClient(
            trust_env=False,
            timeout=5,
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client:
            for url in urls:
                image, size = await _download(client, httpx.URL(url), remaining_bytes)
                images.append(image)
                remaining_bytes -= size
    except RemoteImageError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError):
        raise RemoteImageError(
            "Could not download image_url. Check that the URL is publicly accessible"
        ) from None
    return images
