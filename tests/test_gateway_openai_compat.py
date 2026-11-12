"""Offline regressions for OpenAI gateway requests and Gemini SSE responses."""
from copy import deepcopy
import json

import config
import pytest

from src.converter.openai2gemini import convert_openai_to_gemini_request, convert_gemini_to_openai_stream
from src.models import OpenAIChatCompletionRequest, model_to_dict


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {})
    monkeypatch.setenv('SENSITIVE_WORD_REPLACE_ENABLED', 'false')
    monkeypatch.delenv('COMPATIBILITY_MODE', raising=False)


@pytest.mark.parametrize('compatibility', [False, True])
async def test_developer_instruction_never_becomes_a_gemini_role(compatibility):
    config._config_cache['compatibility_mode_enabled'] = compatibility
    payload = model_to_dict(OpenAIChatCompletionRequest.model_validate({
        'model': 'gemini-4-argon-low',
        'messages': [
            {'role': 'system', 'content': 'System policy'},
            {'role': 'developer', 'content': [{'type': 'text', 'text': 'Use actual tools.'}]},
            {'role': 'user', 'content': 'Task'},
            {'role': 'developer', 'content': 'Later guidance'},
        ],
    }))
    original = deepcopy(payload)
    result = await convert_openai_to_gemini_request(payload)
    assert payload == original
    assert all(c['role'] in ('user', 'model') for c in result['contents'])
    text = '\n'.join(p['text'] for c in result['contents'] for p in c['parts'] if 'text' in p)
    assert 'Task' in text and 'Later guidance' in text
    if compatibility:
        assert 'systemInstruction' not in result
        assert 'System policy' in text and 'Use actual tools.' in text
    else:
        assert result['systemInstruction']['parts'] == [{'text': 'System policy'}, {'text': 'Use actual tools.'}]
        assert 'System policy' not in text


async def test_developer_only_message_is_preserved_as_instruction():
    result = await convert_openai_to_gemini_request({'model': 'gemini-4-argon-low', 'messages': [
        {'role': 'developer', 'content': 'Keep workspace permissions.'},
    ]})
    assert result['systemInstruction']['parts'] == [{'text': 'Keep workspace permissions.'}]
    assert result['contents'][0]['role'] == 'user'


async def test_compatibility_filter_also_checks_developer_guidance(monkeypatch):
    from fastapi import HTTPException
    from src.converter.sensitive_words import filter_compatibility_system_prompts
    config._config_cache.update(compatibility_mode_enabled=True, sensitive_word_replace_enabled=True,
                               sensitive_word_filter_mode='block', sensitive_word_use_default_keywords=False,
                               sensitive_word_replace_rules='blocked-client=>neutral')
    monkeypatch.setenv('SENSITIVE_WORD_REPLACE_ENABLED', 'true')
    monkeypatch.delenv('SENSITIVE_WORD_FILTER_MODE', raising=False)
    monkeypatch.delenv('SENSITIVE_WORD_USE_DEFAULT_KEYWORDS', raising=False)
    with pytest.raises(HTTPException) as error:
        await filter_compatibility_system_prompts({'messages': [
            {'role': 'developer', 'content': 'blocked-client workflow'},
            {'role': 'user', 'content': 'task'}]}, mode='antigravity')
    assert error.value.status_code == 403


@pytest.mark.parametrize('wrapped', [False, True])
def test_usage_only_tail_survives_gemini_to_openai_stream(wrapped):
    raw = {'usageMetadata': {'promptTokenCount': 120, 'candidatesTokenCount': 15, 'totalTokenCount': 145,
                             'thoughtsTokenCount': 10}}
    if wrapped:
        raw = {'response': raw}
    chunk = convert_gemini_to_openai_stream('data: '+json.dumps(raw), 'gemini-4-argon-low', 'gateway-usage-tail')
    data = json.loads(chunk.removeprefix('data: '))
    assert data['choices'] == []
    assert data['usage']['prompt_tokens'] == 120
    assert data['usage']['completion_tokens'] == 15
    assert data['usage']['completion_tokens_details']['reasoning_tokens'] == 10
    assert data['usage']['total_tokens'] == 145


def test_usage_tail_follows_finish_without_creating_another_choice():
    finish = {'candidates': [{'index': 0, 'content': {'role': 'model', 'parts': [{'text': 'ok'}]}, 'finishReason': 'STOP'}]}
    first = convert_gemini_to_openai_stream('data: '+json.dumps(finish), 'gemini-4-argon-low', 'gateway-two-tail')
    usage = {'usageMetadata': {'promptTokenCount': 1, 'candidatesTokenCount': 1, 'totalTokenCount': 2}}
    last = convert_gemini_to_openai_stream('data: '+json.dumps(usage), 'gemini-4-argon-low', 'gateway-two-tail')
    assert json.loads(first.removeprefix('data: '))['choices'][0]['finish_reason'] == 'stop'
    tail = json.loads(last.removeprefix('data: '))
    assert tail['choices'] == [] and tail['usage']['total_tokens'] == 2


@pytest.mark.parametrize('channel', ['antigravity', 'geminicli'])
@pytest.mark.parametrize('stream', [False, True])
async def test_gateway_chat_route_with_simulated_upstream(channel, stream, monkeypatch):
    import importlib
    from types import SimpleNamespace
    from fastapi.responses import JSONResponse

    route = importlib.import_module(f'src.router.{channel}.openai')
    backend = importlib.import_module(f'src.api.{channel}')
    seen = []
    final = {'candidates': [{'index': 0, 'content': {'role': 'model', 'parts': [{'text': 'ok'}]}, 'finishReason': 'STOP'}]}
    usage = {'usageMetadata': {'promptTokenCount': 2, 'candidatesTokenCount': 1, 'totalTokenCount': 3}}

    async def fake_non_stream(*, body, **kwargs):
        seen.append(body)
        return JSONResponse({**final, **usage})

    async def fake_stream(*, body, **kwargs):
        seen.append(body)
        for frame in (final, usage):
            yield ('data: '+json.dumps({'response': frame})+'\n\n').encode()

    async def forbidden_network(*args, **kwargs):
        raise AssertionError('A real upstream transport must never be invoked')

    import src.httpx_client
    monkeypatch.setattr(src.httpx_client, 'post_async', forbidden_network)
    monkeypatch.setattr(src.httpx_client, 'stream_post_async', forbidden_network)
    monkeypatch.setattr(backend, 'non_stream_request', fake_non_stream)
    monkeypatch.setattr(backend, 'stream_request', fake_stream)
    # GeminiCLI imports these helpers at module level, Antigravity imports inside the handler.
    if hasattr(route, 'non_stream_request'):
        monkeypatch.setattr(route, 'non_stream_request', fake_non_stream)
    if hasattr(route, 'stream_request'):
        monkeypatch.setattr(route, 'stream_request', fake_stream)
    payload = OpenAIChatCompletionRequest.model_validate({
        'model': 'gemini-4-argon-low', 'stream': stream,
        'stream_options': {'include_usage': True},
        'messages': [{'role': 'developer', 'content': 'Keep actual tool schemas.'},
                     {'role': 'user', 'content': 'Task'}],
    })
    if channel == 'antigravity':
        response = await route.chat_completions(payload, principal=SimpleNamespace(api_key_id='offline-key'))
    else:
        response = await route.chat_completions(payload, token='offline-token')
    if stream:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        frames = [json.loads(line[6:]) for line in ''.join(chunks).splitlines()
                  if line.startswith('data: ') and line != 'data: [DONE]']
        assert any(frame.get('choices') == [] and frame.get('usage', {}).get('total_tokens') == 3 for frame in frames)
    else:
        data = json.loads(response.body)
        assert data['choices'][0]['message']['content'] == 'ok'
        assert data['usage']['total_tokens'] == 3
    assert len(seen) == 1
    assert seen[0]['request']['systemInstruction']['parts'][0]['text'] == 'Keep actual tool schemas.'
    assert all(c['role'] in ('user', 'model') for c in seen[0]['request']['contents'])
