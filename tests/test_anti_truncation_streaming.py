"""Anti-truncation must keep native text deltas and resume interrupted output."""

import asyncio
import copy
import json

import pytest
from fastapi.responses import StreamingResponse

from src.converter.anti_truncation import (
    AntiTruncationStreamProcessor,
    apply_anti_truncation,
)


def event(parts, finish=None):
    candidate = {"content": {"role": "model", "parts": parts}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    return f'data: {json.dumps({"candidates": [candidate]})}\n\n'.encode()


def text_from(chunks):
    result = ""
    for chunk in chunks:
        if chunk.strip() == b"data: [DONE]":
            continue
        data = json.loads(chunk.decode()[6:])
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                result += part.get("text", "")
    return result


def test_request_injects_synthetic_tool_without_mutating_client_request():
    payload = {"model": "claude-sonnet-4-6", "request": {
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        "systemInstruction": {"parts": [{"text": "Be helpful"}]},
        "tools": [{"functionDeclarations": [{"name": "lookup"}]}],
        "toolConfig": {"functionCallingConfig": {"mode": "NONE"}},
    }}
    original = copy.deepcopy(payload)
    prepared = apply_anti_truncation(payload)
    declarations = [decl for tool in prepared["request"]["tools"] for decl in tool["functionDeclarations"]]
    assert [decl["name"] for decl in declarations] == ["lookup", "emit_answer"]
    assert prepared["request"]["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
    assert "streamFunctionCallArguments" not in prepared["request"]["toolConfig"]["functionCallingConfig"]
    assert "emit_answer" in prepared["request"]["systemInstruction"]["parts"][-1]["text"]
    assert apply_anti_truncation(prepared) == prepared
    prepared["request"]["contents"].append({"role": "user", "parts": []})
    assert payload == original


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_first_text_arrives_before_upstream_finishes(wrapped):
    release = asyncio.Event()
    first = event([{"text": "first"}])
    if wrapped:
        first = f'data: {json.dumps({"response": json.loads(first.decode()[6:])})}\n\n'.encode()

    async def upstream():
        yield first
        await release.wait()
        yield event([{"text": "second"}], "STOP")
        yield b"data: [DONE]\n\n"

    async def request(payload):
        return StreamingResponse(upstream())

    processor = AntiTruncationStreamProcessor(request, apply_anti_truncation({"request": {}}))
    stream = processor.process_stream()
    try:
        chunk = await asyncio.wait_for(anext(stream), timeout=1)
        assert chunk == first
        assert not release.is_set()
        release.set()
        chunks = [chunk] + [item async for item in stream]
        # Unwrap for the common assertion; routers perform this step as well.
        if wrapped:
            chunks[0] = event([{"text": "first"}])
        assert text_from(chunks) == "firstsecond"
        assert processor.current_attempt == 1
        assert chunks.count(b"data: [DONE]\n\n") == 1
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["MAX_TOKENS", "disconnect"])
@pytest.mark.parametrize("prefill", [False, True])
async def test_continuation_keeps_text_history_and_streams_remaining_text(failure, prefill):
    requests = []
    payload = {"request": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}}
    original = copy.deepcopy(payload)

    async def upstream(attempt):
        if attempt == 1:
            yield event([{"text": "first"}])
            if failure == "disconnect":
                raise ConnectionError("interrupted")
            yield event([], failure)
        else:
            yield event([{"text": "second"}], "STOP")
        yield b"data: [DONE]\n\n"

    async def request(body):
        requests.append(copy.deepcopy(body))
        return StreamingResponse(upstream(len(requests)))

    processor = AntiTruncationStreamProcessor(
        request, apply_anti_truncation(payload), enable_prefill_mode=prefill
    )
    chunks = [chunk async for chunk in processor.process_stream()]
    assert text_from(chunks) == "firstsecond"
    assert len(requests) == 2
    history = requests[1]["request"]["contents"]
    assert history[-1 if prefill else -2] == {"role": "model", "parts": [{"text": "first"}]}
    assert "emit_answer" in json.dumps(requests[1])
    assert payload == original
    assert chunks.count(b"data: [DONE]\n\n") == 1


@pytest.mark.asyncio
async def test_real_tool_call_completes_without_continuation():
    calls = 0
    tool = event([{"functionCall": {"name": "lookup", "args": {"query": "test"}}}])

    async def upstream():
        yield tool
        yield b"data: [DONE]\n\n"

    async def request(payload):
        nonlocal calls
        calls += 1
        return StreamingResponse(upstream())

    processor = AntiTruncationStreamProcessor(request, {"request": {}})
    chunks = [chunk async for chunk in processor.process_stream()]
    assert calls == 1
    assert chunks.count(tool) == 1
    assert chunks[-1] == b"data: [DONE]\n\n"


def test_partial_args_are_extracted_as_incremental_synthetic_content():
    from src.converter.anti_truncation import extract_synthetic_content_from_response

    data = {"candidates": [{"content": {"parts": [{
        "functionCall": {
            "name": "emit_answer",
            "partialArgs": {"content": "第一段"},
            "willContinue": True,
        }
    }]}}]}
    content, real_calls, found = extract_synthetic_content_from_response(data)
    assert (content, real_calls, found) == ("第一段", [], True)


def test_partial_args_array_and_complete_tool_result_are_supported():
    from src.converter.anti_truncation import (
        build_synthetic_text_chunks,
        extract_synthetic_content_from_response,
    )

    data = {"candidates": [{"content": {"parts": [{
        "functionCall": {
            "name": "emit_answer",
            "partialArgs": [
                {"jsonPath": "$.content", "stringValue": "第一"},
                {"jsonPath": "$.content", "stringValue": "段"},
            ],
        }
    }]}}]}
    content, _, found = extract_synthetic_content_from_response(data)
    assert (content, found) == ("第一段", True)

    chunks = build_synthetic_text_chunks(data, "a" * 130, [], chunk_size=64)
    assert len(chunks) == 3
    assert all(chunk.startswith(b"data: ") for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["antigravity", "geminicli"])
@pytest.mark.parametrize("prefix", ["", "流式抗截断/", "抗截断/"])
async def test_openai_route_delivers_delta_before_upstream_completion(monkeypatch, backend, prefix):
    import importlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.converter import gemini_fix, openai2gemini, sensitive_words
    from src.models import OpenAIChatCompletionRequest

    route = importlib.import_module(f"src.router.{backend}.openai")
    api = importlib.import_module(f"src.api.{backend}")
    release = asyncio.Event()
    requests = []

    async def normalize(body, **kwargs):
        return body

    async def upstream(body, **kwargs):
        requests.append(body)
        # Exercise the actual tool-wrapped answer when the feature is enabled.
        assert ("emit_answer" in json.dumps(body)) == bool(prefix)
        def answer(text):
            if prefix:
                return [{"functionCall": {"name": "emit_answer", "args": {"content": text}}}]
            return [{"text": text}]
        yield event(answer("first"))
        await release.wait()
        yield event(answer("second"), "STOP")
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(sensitive_words, "filter_compatibility_system_prompts", AsyncMock())
    monkeypatch.setattr(gemini_fix, "normalize_gemini_request", normalize)
    monkeypatch.setattr(openai2gemini, "convert_openai_to_gemini_request", AsyncMock(return_value={"contents": []}))
    monkeypatch.setattr(route, "get_anti_truncation_max_attempts", AsyncMock(return_value=3))
    monkeypatch.setattr(api, "stream_request", upstream)
    request = OpenAIChatCompletionRequest(
        model=prefix + "gemini-2.5-flash", stream=True,
        messages=[{"role": "user", "content": "Please write a short story"}],
    )
    kwargs = {"principal": SimpleNamespace(api_key_id="test")} if backend == "antigravity" else {"token": "test"}
    response = await asyncio.wait_for(route.chat_completions(request, **kwargs), timeout=1)
    stream = response.body_iterator
    try:
        first = await asyncio.wait_for(anext(stream), timeout=1)
        data = json.loads(first.decode()[6:])
        assert data["choices"][0]["delta"]["content"] == "first"
        assert not release.is_set()
        release.set()
        chunks = [first] + [chunk async for chunk in stream]
        text = "".join(
            json.loads(chunk.decode()[6:])["choices"][0]["delta"].get("content", "")
            for chunk in chunks if chunk.strip() != b"data: [DONE]"
        )
        assert text == "firstsecond"
        assert len(requests) == 1
        assert chunks.count(b"data: [DONE]\n\n") == 1
    finally:
        release.set()
        await stream.aclose()
