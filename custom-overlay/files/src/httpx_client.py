"""
通用的HTTP客户端模块
为所有需要使用httpx的模块提供统一的客户端配置和方法
保持通用性，不与特定业务逻辑耦合
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

from config import get_proxy_config, get_stream_idle_timeout
from log import log
from src.shadowsocks import shadowsocks_proxy_context


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
    proxy_url = kwargs.pop("proxy_url", ...)
    async with http_client.get_client(timeout=timeout, proxy_url=proxy_url, **kwargs) as client:
        return await client.get(url, headers=headers)


async def post_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 900.0,
    **kwargs,
) -> httpx.Response:
    """通用异步POST请求"""
    proxy_url = kwargs.pop("proxy_url", ...)
    async with http_client.get_client(timeout=timeout, proxy_url=proxy_url, **kwargs) as client:
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
            log.warning(f"[STREAM] 上游超过 {idle_timeout}s 无数据，主动断开: {url[:100]}")
            return
        yield item


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

    proxy_url = kwargs.pop("proxy_url", ...)
    async with http_client.get_streaming_client(proxy_url=proxy_url, **kwargs) as client:
        async with client.stream("POST", url, json=body, headers=headers) as r:
            # 错误直接返回
            if r.status_code != 200:
                from fastapi import Response
                yield Response(await r.aread(), r.status_code, dict(r.headers))
                return

            # 如果native=True，直接返回bytes流
            if native:
                chunk_iter = r.aiter_bytes()
            else:
                # 通过aiter_lines转化成str流返回
                chunk_iter = r.aiter_lines()

            idle_timeout = await get_stream_idle_timeout()
            if idle_timeout > 0:
                chunk_iter = _idle_watch(chunk_iter, idle_timeout, url)

            async for chunk in chunk_iter:
                yield chunk
