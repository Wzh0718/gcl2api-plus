"""
OpenAI Responses API 支持测试

覆盖：
- src/converter/responses_api.py 请求/响应/流式事件转换
- src/router/antigravity/responses.py 路由（非流式、流式、错误透传）

路由测试通过 monkeypatch 模块内导入的 chat_completions 来复用/隔离管道，
不触碰真实上游。
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from src.api_keys import ApiKeyPrincipal
from src.converter.responses_api import (
    ResponsesStreamTranslator,
    chat_completion_to_responses,
    responses_request_to_chat_dict,
)


MODEL = "gemini-3-pro-preview"


def principal():
    return ApiKeyPrincipal(
        api_key_id="managed-key",
        name="Test key",
        kind="managed",
        key_prefix="test",
    )


# ==================== 请求转换 ====================


def test_request_with_string_input_and_instructions():
    chat = responses_request_to_chat_dict({
        "model": MODEL,
        "instructions": "You are helpful.",
        "input": "hello",
        "max_output_tokens": 1024,
        "temperature": 0.5,
    })
    assert chat["model"] == MODEL
    assert chat["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hello"},
    ]
    assert chat["max_tokens"] == 1024
    assert chat["temperature"] == 0.5
    assert chat["stream"] is False


def test_request_with_items_and_tool_round_trip():
    chat = responses_request_to_chat_dict({
        "model": MODEL,
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "weather in "},
                {"type": "input_text", "text": "Shenzhen?"},
            ]},
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "function_call", "name": "get_weather", "arguments": '{"city":"SZ"}', "call_id": "call_abc"},
            {"type": "function_call_output", "call_id": "call_abc", "output": '{"temp":30}'},
            {"role": "assistant", "content": [{"type": "output_text", "text": "30 degrees."}], "type": "message"},
        ],
    })
    assert chat["messages"][0] == {"role": "user", "content": "weather in Shenzhen?"}
    # function_call 合并为 assistant tool_calls 消息
    assert chat["messages"][1]["role"] == "assistant"
    assert chat["messages"][1]["tool_calls"] == [{
        "id": "call_abc",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"SZ"}'},
    }]
    assert chat["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_abc",
        "content": '{"temp":30}',
    }
    assert chat["messages"][3] == {"role": "assistant", "content": "30 degrees."}
    # reasoning 项被跳过
    assert len(chat["messages"]) == 4


def test_request_tools_and_tool_choice():
    chat = responses_request_to_chat_dict({
        "model": MODEL,
        "input": "hi",
        "tools": [
            {"type": "function", "name": "get_weather", "description": "d", "parameters": {"type": "object"}},
            {"type": "web_search"},
        ],
        "tool_choice": {"type": "function", "name": "get_weather"},
        "stream": True,
    })
    assert chat["tools"] == [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "d",
            "parameters": {"type": "object"},
        },
    }]
    assert chat["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}
    assert chat["stream"] is True
    assert chat["stream_options"] == {"include_usage": True}


def test_request_missing_model_raises():
    with pytest.raises(ValueError):
        responses_request_to_chat_dict({"input": "hi"})


# ==================== 非流式响应转换 ====================


def test_nonstream_response_conversion():
    chat_response = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "hello world",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"SZ"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }
    resp = chat_completion_to_responses(chat_response, fallback_model=MODEL)
    assert resp["object"] == "response"
    assert resp["status"] == "completed"
    assert resp["model"] == MODEL
    assert resp["output"][0]["type"] == "message"
    assert resp["output"][0]["content"][0] == {
        "type": "output_text", "text": "hello world", "annotations": [],
    }
    assert resp["output"][1]["type"] == "function_call"
    assert resp["output"][1]["name"] == "get_weather"
    assert resp["output"][1]["arguments"] == '{"city":"SZ"}'
    assert resp["output"][1]["call_id"] == "call_1"
    assert resp["usage"]["input_tokens"] == 10
    assert resp["usage"]["output_tokens"] == 5
    assert resp["usage"]["total_tokens"] == 15
    assert resp["usage"]["output_tokens_details"]["reasoning_tokens"] == 2


# ==================== 流式事件翻译 ====================


def _text_chunk(delta=None, finish=None, usage=None):
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage:
        chunk["usage"] = usage
    return chunk


def test_stream_translation_text_flow():
    translator = ResponsesStreamTranslator(MODEL)
    events = translator.start()
    events += translator.on_chat_chunk(_text_chunk({"content": "Hel"}))
    events += translator.on_chat_chunk(_text_chunk({"content": "lo"}))
    events += translator.on_chat_chunk(_text_chunk({}, finish="stop", usage={
        "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
    }))
    events += translator.finalize()

    types = [e["type"] for e in events]
    assert types == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]

    deltas = [e for e in events if e["type"] == "response.output_text.delta"]
    assert [d["delta"] for d in deltas] == ["Hel", "lo"]
    # sequence_number 连续递增
    assert [e["sequence_number"] for e in events] == list(range(len(events)))

    completed = events[-1]["response"]
    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0]["text"] == "Hello"
    assert completed["usage"]["input_tokens"] == 3
    assert completed["usage"]["output_tokens"] == 2


def test_stream_translation_tool_call_flow():
    translator = ResponsesStreamTranslator(MODEL)
    events = translator.start()
    events += translator.on_chat_chunk(_text_chunk({"tool_calls": [
        {"index": 0, "id": "call_9", "function": {"name": "get_weather", "arguments": '{"city":'}},
    ]}))
    events += translator.on_chat_chunk(_text_chunk({"tool_calls": [
        {"index": 0, "function": {"arguments": '"SZ"}'}},
    ]}))
    events += translator.on_chat_chunk(_text_chunk({}, finish="tool_calls"))
    events += translator.finalize()

    types = [e["type"] for e in events]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types

    added = next(e for e in events if e["type"] == "response.output_item.added")
    assert added["item"]["type"] == "function_call"
    assert added["item"]["name"] == "get_weather"
    assert added["item"]["call_id"] == "call_9"

    completed = events[-1]["response"]
    assert completed["output"][0]["type"] == "function_call"
    assert completed["output"][0]["arguments"] == '{"city":"SZ"}'


def test_stream_translation_error_and_incomplete():
    translator = ResponsesStreamTranslator(MODEL)
    translator.start()
    events = translator.on_chat_chunk({"error": {"message": "boom"}})
    assert [e["type"] for e in events] == ["response.failed"]
    assert events[0]["response"]["status"] == "failed"
    assert events[0]["response"]["error"]["message"] == "boom"
    # 终止后不再产生事件
    assert translator.finalize() == []

    translator2 = ResponsesStreamTranslator(MODEL)
    translator2.start()
    translator2.on_chat_chunk(_text_chunk({"content": "partial"}))
    events2 = translator2.on_chat_chunk(_text_chunk({}, finish="length"))
    events2 += translator2.finalize()
    completed = events2[-1]
    assert completed["type"] == "response.completed"
    assert completed["response"]["status"] == "incomplete"
    assert completed["response"]["incomplete_details"] == {"reason": "max_output_tokens"}


# ==================== 路由（antigravity） ====================


def _make_client(monkeypatch, fake_chat_completions):
    from src.router.antigravity import responses as responses_module
    from src.utils import authenticate_antigravity_key

    monkeypatch.setattr(responses_module, "chat_completions", fake_chat_completions)
    app = FastAPI()
    app.include_router(responses_module.router)
    app.dependency_overrides[authenticate_antigravity_key] = lambda: principal()
    return TestClient(app)


def _sse_events(text):
    events = []
    for frame in text.split("\n\n"):
        data_lines = [l[5:].strip() for l in frame.splitlines() if l.startswith("data:")]
        if data_lines and data_lines[0] != "[DONE]":
            events.append(json.loads(data_lines[0]))
    return events


async def test_router_nonstream(monkeypatch):
    async def fake_chat_completions(openai_request, principal):
        assert openai_request.model == MODEL
        assert openai_request.messages[0].content == "hi"
        return JSONResponse(content={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        })

    client = _make_client(monkeypatch, fake_chat_completions)
    resp = client.post("/v1/responses", json={"model": MODEL, "input": "hi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "hello!"
    assert body["usage"]["total_tokens"] == 3

    # /antigravity/v1/responses 同样可用
    resp2 = client.post("/antigravity/v1/responses", json={"model": MODEL, "input": "hi"})
    assert resp2.status_code == 200


async def test_router_stream(monkeypatch):
    async def fake_chat_completions(openai_request, principal):
        async def gen():
            for chunk in [
                _text_chunk({"content": "Hel"}),
                _text_chunk({"content": "lo"}),
                _text_chunk({}, finish="stop", usage={
                    "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
                }),
            ]:
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    client = _make_client(monkeypatch, fake_chat_completions)
    resp = client.post("/v1/responses", json={"model": MODEL, "input": "hi", "stream": True})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert "response.output_text.delta" in types
    completed = events[-1]["response"]
    assert completed["output"][0]["content"][0]["text"] == "Hello"
    assert completed["usage"]["total_tokens"] == 5


async def test_router_upstream_error_passthrough(monkeypatch):
    async def fake_chat_completions(openai_request, principal):
        return JSONResponse(
            content={"error": {"message": "quota exceeded", "type": "rate_limit_error"}},
            status_code=429,
        )

    client = _make_client(monkeypatch, fake_chat_completions)
    resp = client.post("/v1/responses", json={"model": MODEL, "input": "hi"})
    assert resp.status_code == 429
    assert resp.json()["error"]["message"] == "quota exceeded"


async def test_router_bad_request(monkeypatch):
    async def fake_chat_completions(openai_request, principal):  # pragma: no cover
        raise AssertionError("不应进入管道")

    client = _make_client(monkeypatch, fake_chat_completions)
    resp = client.post("/v1/responses", json={"input": "no model"})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"
