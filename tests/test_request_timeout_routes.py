"""Deadline cleanup must cross real router conversion and downstream flow control."""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.request_timing as timing


@pytest.mark.asyncio
@pytest.mark.parametrize('backend,protocol,prefix', [
    ('antigravity', 'openai', ''), ('antigravity', 'gemini', ''), ('antigravity', 'anthropic', ''),
    ('vertex', 'openai', ''), ('vertex', 'gemini', ''),
    ('antigravity', 'openai', '流式抗截断/'), ('antigravity', 'gemini', '流式抗截断/'),
    ('antigravity', 'anthropic', '流式抗截断/'),
])
@pytest.mark.parametrize('upstream_error', [False, True])
async def test_blocked_router_send_waits_for_upstream_cleanup_before_releasing_inflight(monkeypatch, backend, protocol, prefix, upstream_error):
    from fastapi import Response
    from src.converter import anthropic2gemini, gemini_fix, openai2gemini, sensitive_words
    from src.models import ClaudeRequest, GeminiRequest, OpenAIChatCompletionRequest

    route = importlib.import_module(f'src.router.{backend}.{protocol}')
    api = importlib.import_module(f'src.api.{backend}')
    monkeypatch.setattr(timing, 'REQUEST_TIMEOUT_SECONDS', 0.05)
    monkeypatch.setattr(sensitive_words, 'filter_compatibility_system_prompts', AsyncMock())
    monkeypatch.setattr(sensitive_words, 'filter_request_system_instruction', AsyncMock())

    async def normalize(body, **kwargs):
        return body

    monkeypatch.setattr(gemini_fix, 'normalize_gemini_request', normalize)
    monkeypatch.setattr(openai2gemini, 'convert_openai_to_gemini_request', AsyncMock(return_value={'contents': []}))
    monkeypatch.setattr(anthropic2gemini, 'anthropic_to_gemini_request', AsyncMock(return_value={'contents': []}))
    if hasattr(route, 'get_anti_truncation_max_attempts'):
        monkeypatch.setattr(route, 'get_anti_truncation_max_attempts', AsyncMock(return_value=3))
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    released = asyncio.Event()
    blocked_send = asyncio.Event()
    baseline = timing.get_inflight_requests()

    async def upstream(*args, **kwargs):
        try:
            if upstream_error:
                yield Response(content='upstream unavailable', status_code=503)
            else:
                parts = [{'functionCall': {'name': 'emit_answer', 'args': {'content': 'first'}}}] if prefix else [{'text': 'first'}]
                yield ('data: ' + json.dumps({'candidates': [{'content': {'role': 'model', 'parts': parts}}]}) + '\n\n').encode()
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()
            released.set()

    monkeypatch.setattr(api, 'stream_request', upstream)
    principal = SimpleNamespace(api_key_id='test') if backend == 'antigravity' else 'test'
    model = prefix + 'gemini-2.5-flash'

    async def app(scope, receive, send):
        if protocol == 'openai':
            response = await route.chat_completions(OpenAIChatCompletionRequest(
                model=model, stream=True, messages=[{'role': 'user', 'content': 'Please write a story'}],
            ), principal)
        elif protocol == 'gemini':
            response = await route.stream_generate_content(GeminiRequest(
                contents=[{'role': 'user', 'parts': [{'text': 'Please write a story'}]}],
            ), model, principal)
        else:
            response = await route.messages(ClaudeRequest(
                model=model, stream=True, max_tokens=128,
                messages=[{'role': 'user', 'content': 'Please write a story'}],
            ), principal)
        await response(scope, receive, send)

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message.get('body') and not upstream_error:
            blocked_send.set()
            await asyncio.Event().wait()

    request = asyncio.create_task(timing.RequestTimingMiddleware(app)(
        {'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions',
         'asgi': {'spec_version': '2.1'}}, receive, send,
    ))
    try:
        if not upstream_error:
            await asyncio.wait_for(blocked_send.wait(), 1)
        await asyncio.wait_for(cleanup_started.wait(), 1)
        if upstream_error:
            # The deadline may arrive after an upstream error has already
            # started closing its response/leases. That cleanup must finish.
            await asyncio.sleep(0.08)
        await asyncio.sleep(0)
        assert not request.done(), 'Cleanup must be awaited by the request, not left to asyncgen GC'
        assert timing.get_inflight_requests() == baseline + 1
        finish_cleanup.set()
        await asyncio.wait_for(request, 1)
        assert released.is_set()
        assert timing.get_inflight_requests() == baseline
    finally:
        finish_cleanup.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_deadline_during_warmup_release_clears_callers_context_and_finishes_db(monkeypatch):
    from src import antigravity_dynamic_warmup as warmup

    started = asyncio.Event()
    finish = asyncio.Event()
    released = []
    contexts = []

    async def release(lease_id):
        started.set()
        await finish.wait()
        released.append(lease_id)
        return True

    backend = SimpleNamespace(release_antigravity_warmup_lease=release)
    monkeypatch.setattr(warmup, 'get_storage_adapter', AsyncMock(return_value=SimpleNamespace(_backend=backend)))

    async def request():
        warmup._WARMUP_LEASE.set('test-lease')
        warmup._WARMUP_OUTCOME.set(None)
        try:
            async with asyncio.timeout(0.05):
                await warmup.release_dynamic_warmup_slot('test.json')
        except TimeoutError:
            pass
        finally:
            contexts.append(warmup._WARMUP_LEASE.get())

    task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0.08)
        assert not task.done()
        finish.set()
        await asyncio.wait_for(task, 1)
        assert released == ['test-lease']
        assert contexts == [None]
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
