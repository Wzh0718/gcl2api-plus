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

from log import log
from src.shadowsocks import shadowsocks_proxy_context
from src.request_timing import add_timing_hook

# listener 探活间隔：太频繁会给每次请求多加一次 unix connect，
# 5 秒足够早发现「进程活着但不再 accept」的僵死状态。
_PROBE_INTERVAL_SECONDS = 5.0


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
    if parsed.hostname == "play.googleapis.com":
        return "telemetry"
    return "cloudcode"


class AgyNativeTransport:
    def __init__(self) -> None:
        self._process: Optional[asyncio.subprocess.Process] = None
        # Lock 不能在建实例时创建：单例在导入期构造，asyncio.Lock 首次使用会
        # 绑定当时的事件循环，跨循环（测试、多次 run_until_complete）会报
        # "attached to a different loop"。这里按当前循环惰性创建。
        self._lock_obj: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._socket_path: Optional[Path] = None
        self._stderr_task: Optional[asyncio.Task] = None
        # 探活状态：进程存活不等于 listener 可用，两者要分开记。
        self._last_probe_at: float = 0.0
        self._listener_healthy: bool = False

    @property
    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock_obj is None or self._lock_loop is not loop:
            self._lock_obj = asyncio.Lock()
            self._lock_loop = loop
        return self._lock_obj

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
        hosts.add("play.googleapis.com")
        # 官方客户端 bootstrap 的配置端点（GET /v1alpha:fetchConfig）。
        # 固定 Google 主机，不随部署变化，写死比留一个容易漏配的环境变量可靠：
        # 漏配时 sidecar 会直接拒连，而调用方只看到 best-effort 跳过。
        hosts.add("aicode.googleapis.com")
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
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            self._socket_path = socket_path
            self._stderr_task = asyncio.ensure_future(
                self._drain_stderr(self._process)
            )
            deadline = asyncio.get_running_loop().time() + 5.0
            while True:
                if self._process.returncode is not None:
                    raise RuntimeError("Antigravity native transport exited during startup")
                try:
                    _, writer = await asyncio.open_unix_connection(str(socket_path))
                    writer.close()
                    await writer.wait_closed()
                    self._mark_probe(True)
                    return
                except (FileNotFoundError, ConnectionRefusedError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError("Antigravity native transport startup timed out")
                    await asyncio.sleep(0.02)

    async def _drain_stderr(self, process) -> None:
        """把 helper 的 stderr 接进应用日志。

        原先这里是 DEVNULL：helper 内部崩了、panic 了、或者主动关掉 listener，
        日志里一个字都看不到，只能看到上层的 Connection refused。
        """
        stream = getattr(process, "stderr", None)
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.warning(f"[NATIVE] {text[:400]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 读取失败不能影响主流程
            log.warning(f"[NATIVE] stderr reader stopped: {type(exc).__name__}")

    def _mark_probe(self, healthy: bool) -> None:
        loop = asyncio.get_running_loop()
        self._last_probe_at = loop.time()
        self._listener_healthy = healthy

    async def _listener_ok(self) -> bool:
        """listener 是否还能 accept；socket 文件存在不等于可用。"""
        socket_path = self._socket()
        try:
            _, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.close()
            await writer.wait_closed()
            return True
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            return False

    async def ensure_ready(self, *, force: bool = False) -> None:
        """确保 helper 既能跑、也能 accept。

        进程活着但 listener 已关（本次事故的形态）时，returncode 仍是 None，
        旧的只判 returncode 的逻辑永远不会重建，于是所有请求一直 refused。
        """
        if self._process is None:
            # 冷启动：直接拉起，不发「不可用」告警。
            await self.start()
            return
        # 注意：这里不持有 self._lock，下面会调用 stop()/start()，
        # 而它们各自加锁，持锁调用会自锁。
        loop = asyncio.get_running_loop()
        process = self._process
        alive = process is not None and process.returncode is None
        fresh = loop.time() - self._last_probe_at < _PROBE_INTERVAL_SECONDS
        if not force and alive and self._listener_healthy and fresh:
            return
        healthy = await self._listener_ok() if alive else False
        self._mark_probe(healthy)
        if healthy:
            return
        log.warning(
            f"[NATIVE] 传输不可用（进程存活={alive} listener={healthy}），重建 helper"
        )
        try:
            await self.stop()
        except Exception as exc:  # 句柄已不可用（跨循环/僵尸）时强制丢弃重来
            log.warning(f"[NATIVE] 停止旧 helper 失败（{type(exc).__name__}），强制丢弃句柄")
            self._process = None
            self._socket_path = None
            self._listener_healthy = False
        await self.start()

    async def stop(self) -> None:
        async with self._lock:
            process, socket_path = self._process, self._socket_path
            self._process, self._socket_path = None, None
            task, self._stderr_task = self._stderr_task, None
            self._listener_healthy = False
            self._last_probe_at = 0.0
            if task is not None:
                task.cancel()
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
        timing_kwargs = {}
        add_timing_hook(timing_kwargs, url)
        await self.ensure_ready()
        try:
            return await self._request_once(
                method, url, headers=headers, data=data, json=json,
                proxy_url=proxy_url, timeout=timeout, timing_kwargs=timing_kwargs,
            )
        except httpx.ConnectError:
            # 连接被拒通常就是 listener 死了；重建后只重试一次，
            # 不把一次死传输拖成上层的多次长重试。
            log.warning(f"[NATIVE] 连接被拒，重建 helper 后重试一次: {url[:120]}")
            await self.ensure_ready(force=True)
            return await self._request_once(
                method, url, headers=headers, data=data, json=json,
                proxy_url=proxy_url, timeout=timeout, timing_kwargs=timing_kwargs,
            )

    async def _request_once(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]],
        data: Any,
        json: Any,
        proxy_url: Any,
        timeout: float,
        timing_kwargs: Dict[str, Any],
    ) -> httpx.Response:
        async with self._upstream_proxy(proxy_url) as resolved_proxy:
            transport = httpx.AsyncHTTPTransport(uds=str(self._socket()))
            async with httpx.AsyncClient(
                **timing_kwargs, transport=transport, timeout=httpx.Timeout(timeout + 5.0, connect=30.0)
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
        timing_kwargs = {}
        add_timing_hook(timing_kwargs, url)
        # 流式路径不在这里重试：一旦开始 yield 就无法安全重放。
        # 但只要本请求前确保 listener 可用，僵死状态最多影响一个请求。
        await self.ensure_ready()
        async with self._upstream_proxy(proxy_url) as resolved_proxy:
            transport = httpx.AsyncHTTPTransport(uds=str(self._socket()))
            async with httpx.AsyncClient(
                **timing_kwargs, transport=transport, timeout=httpx.Timeout(None, connect=30.0)
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
