"""Lifecycle and local Unix-socket client for the Antigravity Go transport."""

from __future__ import annotations

import asyncio
import json as json_module
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional
from urllib.parse import urlencode, urlsplit

import httpx

from src.shadowsocks import shadowsocks_proxy_context


def encode_request_body(data: Any, json: Any) -> Optional[bytes]:
    if json is not None:
        return json_module.dumps(
            json, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    if data is None:
        return None
    if isinstance(data, dict):
        return urlencode(data, doseq=True).encode("utf-8")
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    return str(data).encode("utf-8")


def antigravity_profile_for_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname == "oauth2.googleapis.com":
        return "oauth"
    return "cloudcode"


class AgyNativeTransport:
    def __init__(self) -> None:
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._socket_path: Optional[Path] = None

    def _binary_path(self) -> Path:
        configured = os.getenv("ANTIGRAVITY_NATIVE_TRANSPORT_BINARY", "").strip()
        if configured:
            return Path(configured).resolve()
        return (Path.cwd() / "native/agytransport/agytransport").resolve()

    def _socket(self) -> Path:
        configured = os.getenv("ANTIGRAVITY_NATIVE_TRANSPORT_SOCKET", "").strip()
        if configured:
            return Path(configured).resolve()
        credentials_dir = Path(os.getenv("CREDENTIALS_DIR", "./creds")).resolve()
        return credentials_dir / "agytransport.sock"

    async def _allowed_hosts(self) -> set[str]:
        from config import (
            get_antigravity_api_url_candidates,
            get_googleapis_proxy_url,
            get_oauth_proxy_url,
        )

        urls = list(await get_antigravity_api_url_candidates())
        urls.extend([await get_oauth_proxy_url(), await get_googleapis_proxy_url()])
        hosts = {
            parsed.hostname
            for parsed in (urlsplit(value) for value in urls)
            if parsed.hostname
        }
        hosts.update(
            host.strip()
            for host in os.getenv(
                "ANTIGRAVITY_NATIVE_TRANSPORT_ALLOWED_HOSTS", ""
            ).split(",")
            if host.strip()
        )
        return hosts

    async def start(self) -> None:
        async with self._lock:
            if self._process is not None and self._process.returncode is None:
                return
            binary = self._binary_path()
            if not binary.is_file():
                raise RuntimeError("Antigravity native transport binary is unavailable")
            socket_path = self._socket()
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            if socket_path.exists():
                socket_path.unlink()
            allowed_hosts = ",".join(sorted(await self._allowed_hosts()))
            environment = {}
            ca_file = os.getenv("ANTIGRAVITY_NATIVE_TRANSPORT_CA_FILE", "").strip()
            if ca_file:
                environment["SSL_CERT_FILE"] = ca_file
            self._process = await asyncio.create_subprocess_exec(
                str(binary),
                "-socket",
                str(socket_path),
                "-allowed-hosts",
                allowed_hosts,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
            self._socket_path = socket_path
            deadline = asyncio.get_running_loop().time() + 5.0
            while True:
                if self._process.returncode is not None:
                    raise RuntimeError("Antigravity native transport exited during startup")
                try:
                    _, writer = await asyncio.open_unix_connection(str(socket_path))
                    writer.close()
                    await writer.wait_closed()
                    return
                except (FileNotFoundError, ConnectionRefusedError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError("Antigravity native transport startup timed out")
                    await asyncio.sleep(0.02)

    async def stop(self) -> None:
        async with self._lock:
            process, socket_path = self._process, self._socket_path
            self._process, self._socket_path = None, None
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if socket_path is not None:
                try:
                    socket_path.unlink()
                except FileNotFoundError:
                    pass

    def _headers(
        self,
        *,
        url: str,
        headers: Optional[Dict[str, str]],
        proxy_url: Any,
        timeout: float,
        streaming: bool,
    ) -> Dict[str, str]:
        result = {
            key: value
            for key, value in dict(headers or {}).items()
            if key.lower() not in {"accept-encoding", "content-length", "connection"}
        }
        result.update(
            {
                "X-Agy-Target-Url": url,
                "X-Agy-Proxy-Url": "" if proxy_url is None else str(proxy_url),
                "X-Agy-Profile": antigravity_profile_for_url(url),
                "X-Agy-Timeout-Ms": str(max(int(timeout * 1000), 1)),
                "X-Agy-Streaming": "1" if streaming else "0",
            }
        )
        return result

    @asynccontextmanager
    async def _upstream_proxy(self, proxy_url: Any) -> AsyncGenerator[Any, None]:
        if proxy_url is ...:
            from config import get_proxy_config

            proxy_url = await get_proxy_config()
        async with shadowsocks_proxy_context(proxy_url) as resolved:
            yield resolved

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        data: Any = None,
        json: Any = None,
        proxy_url: Any = ...,
        timeout: float = 30.0,
    ) -> httpx.Response:
        await self.start()
        async with self._upstream_proxy(proxy_url) as resolved_proxy:
            transport = httpx.AsyncHTTPTransport(uds=str(self._socket()))
            async with httpx.AsyncClient(
                transport=transport, timeout=httpx.Timeout(timeout + 5.0, connect=30.0)
            ) as client:
                return await client.request(
                    method,
                    "http://localhost/request",
                    headers=self._headers(
                        url=url,
                        headers=headers,
                        proxy_url=resolved_proxy,
                        timeout=timeout,
                        streaming=False,
                    ),
                    content=encode_request_body(data, json),
                )

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        json: Any = None,
        proxy_url: Any = ...,
        timeout: float = 300.0,
    ) -> AsyncGenerator[httpx.Response, None]:
        await self.start()
        async with self._upstream_proxy(proxy_url) as resolved_proxy:
            transport = httpx.AsyncHTTPTransport(uds=str(self._socket()))
            async with httpx.AsyncClient(
                transport=transport, timeout=httpx.Timeout(None, connect=30.0)
            ) as client:
                async with client.stream(
                    method,
                    "http://localhost/request",
                    headers=self._headers(
                        url=url,
                        headers=headers,
                        proxy_url=resolved_proxy,
                        timeout=timeout,
                        streaming=True,
                    ),
                    content=encode_request_body(None, json),
                ) as response:
                    yield response


agy_native_transport = AgyNativeTransport()
