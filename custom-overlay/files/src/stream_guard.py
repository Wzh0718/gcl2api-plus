"""
流式链路资源清理工具

starlette 的 StreamingResponse 在正常结束、发送异常、客户端断连时都不会关闭
body_iterator；消费方 `async for` 提前 break/return 时也不会关闭被迭代的异步生成器。
底层 httpx 流式连接因此只能依赖 GC 兜底回收，可能造成文件描述符（fd）泄漏。

本模块提供显式关闭的原语，确保任意一层退出时都能级联关闭整条流式链路。
"""

from typing import Any, AsyncIterator

from fastapi.responses import StreamingResponse

from log import log


async def aclose_quietly(iterator: Any, context: str = "") -> None:
    """尽力关闭异步迭代器/生成器，异常只记日志不抛出。"""
    aclose = getattr(iterator, "aclose", None)
    if not callable(aclose):
        return
    try:
        await aclose()
    except Exception as e:
        log.debug(f"关闭流式迭代器失败 {context}: {e}")


async def guarded_stream(iterator: AsyncIterator[Any], context: str = "") -> AsyncIterator[Any]:
    """包装异步迭代器：本生成器被关闭或迭代结束时，级联关闭底层迭代器。"""
    try:
        async for item in iterator:
            yield item
    finally:
        await aclose_quietly(iterator, context)


class ClosingStreamingResponse(StreamingResponse):
    """StreamingResponse 变体：stream_response 退出时总是关闭 body_iterator。

    覆盖正常结束、客户端断连导致的发送异常、任务取消等所有路径，
    保证底层上游连接不依赖 GC 即可释放。
    """

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            await aclose_quietly(self.body_iterator, "body_iterator")
