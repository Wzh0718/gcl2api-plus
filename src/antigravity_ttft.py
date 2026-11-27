"""在首个有效模型输出前暂存 SSE 前缀，防止拼接两个模型的回复。"""

import asyncio
import json
from collections.abc import AsyncIterator
from enum import Enum

import httpx
from fastapi import Response


class FirstOutputTimeoutError(TimeoutError):
    """当前模型在截止时间前没有输出，调用方可以转向链内下一项。"""

    def __init__(self, next_model: str) -> None:
        super().__init__(next_model)
        self.next_model = next_model


class FirstOutputDeadline:
    """单次上游尝试等待首次有效返回的计时器，不计本地准备时间。"""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.timer = asyncio.timeout(None)

    def start(self) -> None:
        """由传输层在发出 HTTP 请求前启动。"""
        self.timer.reschedule(asyncio.get_running_loop().time() + self.seconds)

    def stop(self) -> None:
        """结束等待；后续读取、转发和资源释放不受延迟阈值限制。"""
        if not self.timer.expired() and self.timer.when() is not None:
            self.timer.reschedule(None)


async def buffered_post(client, method, url, deadline, **kwargs):
    """保持普通 HTTP 请求，以首批非空白响应体数据结束等待，完整读取后返回。"""
    deadline.start()
    try:
        async with client.stream(method, url, **kwargs) as response:
            try:
                if response.status_code != 200:
                    deadline.stop()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if deadline.timer.when() is not None and chunk.strip():
                        deadline.stop()
                    body.extend(chunk)
                headers = [
                    (key, value)
                    for key, value in response.headers.multi_items()
                    if key.lower() not in ("content-encoding", "content-length")
                ]
                return httpx.Response(
                    response.status_code,
                    headers=headers,
                    content=bytes(body),
                    request=response.request,
                    extensions=response.extensions,
                )
            finally:
                deadline.stop()
    finally:
        deadline.stop()


class _Terminal(Enum):
    EMPTY = "empty"


def _commits_response(line: bytes) -> bool | Response | _Terminal:
    if not line.startswith(b"data:"):
        return False
    raw = line[5:].strip()
    if raw == b"[DONE]":
        return _Terminal.EMPTY
    try:
        frame = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(frame, dict):
        return False
    payload = frame.get("response", frame)
    if not isinstance(payload, dict):
        return False
    feedback = payload.get("promptFeedback")
    error = payload.get("error") or frame.get("error")
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        status = code if isinstance(code, int) and 400 <= code <= 599 else 502
        return Response(
            json.dumps({"error": error}), status, media_type="application/json"
        )
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        return True
    for candidate in payload.get("candidates") or ():
        if not isinstance(candidate, dict):
            continue
        if candidate.get("finishReason"):
            return True
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or ():
            if isinstance(part, dict) and (
                any(value for key, value in part.items() if key != "thought")
            ):
                return True
    return False


async def gate_first_output(
    stream: AsyncIterator, timer: asyncio.Timeout, next_model: str
) -> AsyncIterator:
    """首次有效事件前应用绝对截止时间；随后原样转发，不限制生成时长。

    Args:
        stream: 上游字节流或已按行拆分的文本流，由调用方负责关闭。
        timer: 传输层在实际发送请求时启动的计时器。
        next_model: 仅在等待有效事件超时时交给调用方的后继模型。

    Yields:
        胜出尝试的原始响应块，或上游错误响应。

    Raises:
        FirstOutputTimeoutError: 到达截止时间仍无有效内容或终止事件。
    """
    pending = []
    size = 0
    partial = bytearray()
    ready = False
    try:
        async with timer:
            async for chunk in stream:
                if isinstance(chunk, Response):
                    timer.reschedule(None)
                    pending = [chunk]
                    break
                pending.append(chunk)
                if isinstance(chunk, bytes):
                    lines = chunk.split(b"\n")
                    partial.extend(lines[0])
                    ready = len(lines) > 1 and (
                        _commits_response(partial)
                        or next(
                            filter(None, map(_commits_response, lines[1:-1])), False
                        )
                    )
                    if len(lines) > 1:
                        partial = bytearray(lines[-1])
                    size += len(chunk)
                else:
                    encoded = chunk.encode()
                    ready = next(
                        filter(None, map(_commits_response, encoded.splitlines())),
                        False,
                    )
                    size += len(encoded) + 1
                if ready:
                    timer.reschedule(None)
                    if ready is _Terminal.EMPTY:
                        return
                    if isinstance(ready, Response):
                        pending = [ready]
                    break
                if size > 4 * 1024 * 1024:
                    timer.reschedule(None)
                    pending = [
                        Response(
                            '{"error":"SSE prefix too large"}',
                            502,
                            media_type="application/json",
                        )
                    ]
                    break
            else:
                timer.reschedule(None)
                ready = _commits_response(partial)
                if not ready or ready is _Terminal.EMPTY:
                    return
                if isinstance(ready, Response):
                    pending = [ready]
    except TimeoutError as exc:
        if timer.expired():
            raise FirstOutputTimeoutError(next_model) from exc
        raise
    for chunk in pending:
        yield chunk
    if not pending or isinstance(pending[-1], Response):
        return
    pending.clear()
    async for chunk in stream:
        yield chunk
