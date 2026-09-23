"""
OpenAI Responses API Router - Antigravity
通过 Antigravity 处理 OpenAI Responses API (POST /v1/responses) 请求的路由模块

实现方式：把 Responses 请求转换为 OpenAI chat completions 请求，
复用 openai.py 中现有的 chat_completions 处理管道（假流式、抗截断、
上游重试、计费等全部复用），再把 chat 响应翻译回 Responses API 格式。
"""

import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import json
from typing import Any, AsyncIterator, Dict

# 第三方库
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

# 本地模块 - 日志
from log import log

# 本地模块 - 工具和认证
from src.utils import authenticate_antigravity_key
from src.api_keys import ApiKeyPrincipal

# 本地模块 - 数据模型
from src.models import OpenAIChatCompletionRequest

# 本地模块 - Responses API 转换器
from src.converter.responses_api import (
    ResponsesStreamTranslator,
    chat_completion_to_responses,
    responses_request_to_chat_dict,
)

# 本地模块 - 复用现有 chat completions 管道
from src.router.antigravity.openai import chat_completions

# 本地模块 - 流保护
from src.stream_guard import aclose_quietly


# ==================== 路由器初始化 ====================

router = APIRouter()


# ==================== 内部工具 ====================

def _sse_frame(event: Dict[str, Any]) -> str:
    """把 Responses 事件格式化为 SSE 帧（event + data 两行）。"""
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "invalid_request",
            }
        },
    )


async def _translate_chat_stream(
    body_iterator: AsyncIterator[Any],
    translator: ResponsesStreamTranslator,
) -> AsyncIterator[str]:
    """
    消费 chat completions SSE 流（data: {chat.completion.chunk}），
    翻译为 Responses API SSE 事件序列，以 response.completed / response.failed 收尾。
    """
    try:
        for event in translator.start():
            yield _sse_frame(event)

        buffer = ""
        async for piece in body_iterator:
            chunk_str = piece.decode("utf-8") if isinstance(piece, bytes) else str(piece)
            buffer += chunk_str

            # SSE 帧以空行分隔；流末尾可能有不完整帧，留在 buffer 里
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                frame = frame.strip()
                if not frame:
                    continue

                data_line = ""
                for line in frame.splitlines():
                    if line.startswith("data:"):
                        data_line = line[len("data:"):].strip()
                        break
                if not data_line:
                    continue

                if data_line == "[DONE]":
                    for event in translator.finalize():
                        yield _sse_frame(event)
                    return

                try:
                    chat_chunk = json.loads(data_line)
                except json.JSONDecodeError:
                    log.warning(f"[RESPONSES-API] 跳过无法解析的 chat chunk: {data_line[:200]}")
                    continue

                for event in translator.on_chat_chunk(chat_chunk):
                    yield _sse_frame(event)

        # 流正常结束但未收到 [DONE]（异常断流时上游可能不发终止帧），
        # 此时若已有输出则正常收尾，否则按错误收尾
        if translator.has_output():
            for event in translator.finalize():
                yield _sse_frame(event)
        else:
            for event in translator.on_error("Upstream stream ended unexpectedly"):
                yield _sse_frame(event)
    except Exception as e:
        log.error(f"[RESPONSES-API] 流式翻译出错: {e}")
        for event in translator.on_error(str(e)):
            yield _sse_frame(event)
    finally:
        await aclose_quietly(body_iterator, "[RESPONSES-API]")


# ==================== API 路由 ====================

@router.post("/antigravity/v1/responses")
@router.post("/v1/responses")
async def responses(
    request: Request,
    principal: ApiKeyPrincipal = Depends(authenticate_antigravity_key),
):
    """
    处理 OpenAI Responses API 请求（流式和非流式）

    请求体为 Responses API 格式（input/instructions/tools 等），
    内部转换为 chat completions 走现有管道，响应翻译回 Responses 格式。
    """
    try:
        body = await request.json()
    except Exception:
        return _bad_request("Request body must be valid JSON")
    if not isinstance(body, dict):
        return _bad_request("Request body must be a JSON object")

    try:
        chat_dict = responses_request_to_chat_dict(body)
    except ValueError as e:
        return _bad_request(str(e))

    log.debug(f"[ANTIGRAVITY-RESPONSES] Request for model: {chat_dict['model']}")

    try:
        openai_request = OpenAIChatCompletionRequest(**chat_dict)
    except Exception as e:
        return _bad_request(f"Invalid request parameters: {e}")

    # 复用现有 chat completions 管道（健康检查、模型特性、假流式、抗截断、上游调用）
    result = await chat_completions(openai_request, principal)

    # ========== 非流式（含健康检查与上游错误 JSONResponse） ==========
    if not isinstance(result, StreamingResponse):
        try:
            payload = json.loads(result.body)
        except Exception:
            return result
        if isinstance(payload, dict) and "error" in payload:
            return JSONResponse(content=payload, status_code=result.status_code)
        return JSONResponse(
            content=chat_completion_to_responses(
                payload, fallback_model=chat_dict["model"]
            )
        )

    # ========== 流式 ==========
    translator = ResponsesStreamTranslator(model=chat_dict["model"])
    return StreamingResponse(
        _translate_chat_stream(result.body_iterator, translator),
        media_type="text/event-stream",
    )
