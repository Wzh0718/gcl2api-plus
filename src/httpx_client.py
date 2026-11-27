"""
通用的HTTP客户端模块
为所有需要使用httpx的模块提供统一的客户端配置和方法
保持通用性，不与特定业务逻辑耦合
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

from config import (
    get_antigravity_native_transport_enabled_sync,
    get_proxy_config,
    get_stream_idle_timeout,
)
from log import log
from src.antigravity_ttft import buffered_post
from src.agy_native_transport import agy_native_transport, antigravity_profile_for_url
from src.shadowsocks import shadowsocks_proxy_context
from src.request_timing import add_timing_hook
from src.request_capture import capture_post, capture_stream, active_attempt, observe_stream_headers
from src.antigravity_telemetry import observe_post, observe_stream


def get_decoded_response_headers(headers: Any) -> Dict[str, str]:
    """Return upstream headers that match an HTTPX-decoded response body."""
    return {
        key: value
        for key, value in dict(headers).items()
        if key.lower() not in ("content-encoding", "content-length")
    }


class HttpxClientManager:
    """通用HTTP客户端管理器"""

    async def get_client_kwargs(self, timeout: float = 30.0, proxy_url: Any = ... , **kwargs) -> Dict[str, Any]:
        """获取httpx客户端的通用配置参数"""
        client_kwargs = {"timeout": timeout, **kwargs}

        # proxy_url=... 表示调用方未指定账号级策略，继续继承全局代理；
        # None 表示 direct，字符串表示 custom。
        if proxy_url is ...:
            current_proxy_config = await get_proxy_config()
            if current_proxy_config:
                client_kwargs["proxy"] = current_proxy_config
        else:
            client_kwargs["proxy"] = proxy_url

        return client_kwargs

    @asynccontextmanager
    async def get_client(
        self, timeout: float = 30.0, **kwargs
    ) -> AsyncGenerator[httpx.AsyncClient, None]:
        """获取配置好的异步HTTP客户端"""
        proxy_url = kwargs.pop("proxy_url", ...)
        async with shadowsocks_proxy_context(proxy_url) as resolved_proxy:
            client_kwargs = await self.get_client_kwargs(
                timeout=timeout, proxy_url=resolved_proxy, **kwargs
            )

            async with httpx.AsyncClient(**client_kwargs) as client:
                yield client

    @asynccontextmanager
    async def get_streaming_client(
        self, timeout: float = None, **kwargs
    ) -> AsyncGenerator[httpx.AsyncClient, None]:
        """获取用于流式请求的HTTP客户端（无读超时限制，但保留连接超时）"""
        proxy_url = kwargs.pop("proxy_url", ...)
        async with shadowsocks_proxy_context(proxy_url) as resolved_proxy:
            if timeout is None:
                # 读超时设为 None 以支持长 SSE 流，但连接建立仍需超时兜底
                timeout = httpx.Timeout(None, connect=30.0)
            client_kwargs = await self.get_client_kwargs(
                timeout=timeout, proxy_url=resolved_proxy, **kwargs
            )

            # 创建独立的客户端实例用于流式处理
            client = httpx.AsyncClient(**client_kwargs)
            try:
                yield client
            finally:
                # 确保无论发生什么都关闭客户端
                try:
                    await client.aclose()
                except Exception as e:
                    log.warning(f"Error closing streaming client: {e}")


# 全局HTTP客户端管理器实例
http_client = HttpxClientManager()


# 通用的异步方法
async def get_async(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0, **kwargs
) -> httpx.Response:
    """通用异步GET请求"""
    transport = kwargs.pop("transport", None)
    proxy_url = kwargs.pop("proxy_url", ...)
    if transport == "antigravity" and get_antigravity_native_transport_enabled_sync():
        return await agy_native_transport.request(
            "GET", url, headers=headers, proxy_url=proxy_url, timeout=timeout
        )
    async with http_client.get_client(timeout=timeout, proxy_url=proxy_url, **kwargs) as client:
        return await client.get(url, headers=headers)


@capture_post
@observe_post
async def post_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 900.0,
    **kwargs,
) -> httpx.Response:
    """通用异步POST请求"""
    deadline = kwargs.pop("first_output_deadline", None)
    transport = kwargs.pop("transport", None)
    proxy_url = kwargs.pop("proxy_url", ...)
    if transport == "antigravity" and get_antigravity_native_transport_enabled_sync():
        return await agy_native_transport.request(
            "POST",
            url,
            headers=headers,
            data=data,
            json=json,
            proxy_url=proxy_url,
            timeout=timeout,
            **({"first_output_deadline": deadline} if deadline else {}),
        )
    add_timing_hook(kwargs, url)
    async with http_client.get_client(timeout=timeout, proxy_url=proxy_url, **kwargs) as client:
        if deadline:
            return await buffered_post(client, "POST", url, deadline, data=data, json=json, headers=headers)
        return await client.post(url, data=data, json=json, headers=headers)


# 调试用：设为 True 时所有流式请求都返回 429
_MOCK_STREAM_429 = False


async def _idle_watch(iterator, idle_timeout: float, url: str):
    """上游流空闲看门狗：超过 idle_timeout 秒无任何数据则主动结束迭代。

    客户端静默断连后上游流可能永远挂起（读超时为 None），看门狗保证
    流最终会被终结，配合 async with 释放底层连接，避免 fd 泄漏。
    """
    it = iterator.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(it.__anext__(), timeout=idle_timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            if (attempt := active_attempt.get()) is not None:
                attempt.incomplete = "idle_timeout"
            log.warning(f"[STREAM] 上游超过 {idle_timeout}s 无数据，主动断开: {url[:100]}")
            return
        yield item


@capture_stream
@observe_stream
async def stream_post_async(
    url: str,
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    **kwargs,
):
    """流式异步POST请求"""
    if _MOCK_STREAM_429:
        from fastapi import Response
        import json
        log.warning(f"[MOCK] stream_post_async: 返回模拟429错误")
        yield Response(
            content=json.dumps({"error": {"code": 429, "message": "mock rate limit", "status": "RESOURCE_EXHAUSTED"}}),
            status_code=429,
        )
        return

    deadline = kwargs.pop("first_output_deadline", None)
    transport = kwargs.pop("transport", None)
    proxy_url = kwargs.pop("proxy_url", ...)
    async with AsyncExitStack() as stack:
        try:
            if transport == "antigravity" and get_antigravity_native_transport_enabled_sync():
                context = agy_native_transport.stream(
                    "POST", url, headers=headers, json=body, proxy_url=proxy_url,
                    **({"on_send": deadline.start} if deadline else {}),
                )
            else:
                add_timing_hook(kwargs, url)
                client = await stack.enter_async_context(
                    http_client.get_streaming_client(proxy_url=proxy_url, **kwargs)
                )
                context = client.stream("POST", url, json=body, headers=headers)
                if deadline:
                    deadline.start()
            response = await stack.enter_async_context(context)
            observe_stream_headers(response)
            if response.status_code != 200:
                if deadline:
                    deadline.stop()
                from fastapi import Response
                yield Response(await response.aread(), response.status_code,
                               get_decoded_response_headers(response.headers))
                return
            chunks = response.aiter_bytes() if native else response.aiter_lines()
            idle_timeout = await get_stream_idle_timeout()
            if idle_timeout > 0:
                chunks = _idle_watch(chunks, idle_timeout, url)
            async for chunk in chunks:
                yield chunk
        finally:
            if deadline:
                deadline.stop()
