from typing import Any, AsyncIterator

from fastapi import Response

from src.stream_guard import ClosingStreamingResponse, aclose_quietly


async def prepend_async_item(first_item: Any, iterator: AsyncIterator[Any]):
    """Yield a prefetched item before continuing the original iterator.

    生成器被关闭或迭代结束时，级联关闭底层迭代器，避免上游连接泄漏。
    """
    try:
        yield first_item
        async for item in iterator:
            yield item
    finally:
        await aclose_quietly(iterator, "prepend_async_item")


async def read_first_async_item(iterator: AsyncIterator[Any]) -> Any:
    """Python 3.9-compatible async equivalent of built-in anext()."""
    return await iterator.__anext__()


async def build_streaming_response_or_error(
    iterator: AsyncIterator[Any],
    media_type: str = "text/event-stream",
):
    """
    Prefetch the first async item so router code can return an upstream error
    response directly before FastAPI commits a 200 streaming response.
    """
    try:
        first_item = await read_first_async_item(iterator)
    except StopAsyncIteration:
        return Response(status_code=204)

    if isinstance(first_item, Response):
        # 错误直接返回时预取的迭代器不再被消费，必须显式关闭释放上游连接
        await aclose_quietly(iterator, "prefetch-error")
        return first_item

    return ClosingStreamingResponse(
        prepend_async_item(first_item, iterator),
        media_type=media_type,
    )
