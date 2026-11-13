import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest
import config
from src import prompt_fusion as fusion
from src.converter.antigravity_system_prompt import prepare_antigravity_system_prompt


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {'prompt_fusion_enabled': True})
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    for key in ('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'ANTIGRAVITY_SYSTEM_AS_USER_ENABLED', 'SYSTEM_PROMPT_APPEND'):
        monkeypatch.delenv(key, raising=False)
    fusion._failures.clear()
    yield
    assert not fusion._inflight


def request():
    return {'systemInstruction': {'parts': [{'text': 'Ask approval before deleting. Use shell.'}]},
            'contents': [{'role': 'user', 'parts': [{'text': 'My private task'}]},
                         {'role': 'model', 'parts': [{'functionCall': {'name': 'shell', 'args': {}}}]}],
            'tools': [{'functionDeclarations': [{'name': 'shell'}]}]}


@pytest.mark.parametrize('global_on,mode,expected', [
    (False, 'inherit', False), (True, 'inherit', True),
    (False, 'on', True), (True, 'off', False), (False, 'off', False), (True, 'on', True)])
async def test_account_precedence(global_on, mode, expected):
    config._config_cache.update(prompt_fusion_enabled=global_on, prompt_fusion_accounts={'a': mode})
    assert await fusion.enabled('a') is expected
    assert await fusion.enabled('other') is global_on


@pytest.mark.parametrize('inject,as_user', [(False, False), (True, False), (False, True), (True, True)])
async def test_fusion_preserves_task_tools_and_original(monkeypatch, inject, as_user):
    config._config_cache.update(antigravity_system_prompt_enabled=inject, antigravity_system_as_user_enabled=as_user,
                               system_prompt_append='Gateway policy\n')
    generate = AsyncMock(return_value='Ask approval before deleting. Use shell.')
    monkeypatch.setattr(fusion, '_generate', generate)
    raw = request()
    prepared = await prepare_antigravity_system_prompt(raw)
    before = deepcopy(prepared)
    result = await fusion.fuse_request(prepared.copy(), 'gemini-test', 'a', {})
    assert prepared == before
    assert result['contents'] == raw['contents']
    assert result['tools'] == raw['tools']
    text = result['systemInstruction']['parts'][0]['text']
    assert text.startswith('Gateway policy\nYou are Antigravity')
    assert 'Ask approval before deleting' in text
    source = json.loads(generate.call_args.args[0])
    assert source['client_guidance'] == raw['systemInstruction']['parts'][0]['text']
    assert 'My private task' not in generate.call_args.args[0]
    assert 'fusion_instruction' not in json.dumps(result)


async def test_cache_singleflight_fixed_ttl_and_isolation(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(fusion.time, 'time', lambda: now[0])
    async def generate(*args):
        await asyncio.sleep(0.01)
        return 'Keep approvals'
    mock = AsyncMock(side_effect=generate)
    monkeypatch.setattr(fusion, '_generate', mock)
    raw = request()
    await asyncio.gather(*(fusion.fuse_request(raw, 'gemini-test', 'a', {}) for _ in range(10)))
    assert mock.await_count == 1
    now[0] += fusion.TTL - 1
    await fusion.fuse_request(raw, 'gemini-test', 'a', {})
    assert mock.await_count == 1
    now[0] += 2
    await fusion.fuse_request(raw, 'gemini-test', 'a', {})
    assert mock.await_count == 2
    await fusion.fuse_request(raw, 'gemini-test', 'b', {})
    await fusion.fuse_request(raw, 'claude-test', 'a', {})
    changed = deepcopy(raw)
    changed['tools'] = []
    await fusion.fuse_request(changed, 'gemini-test', 'a', {})
    from src import claude_routing
    token = claude_routing._route.set(claude_routing.ClaudeRoute('second-caller', 'a'))
    try:
        await fusion.fuse_request(raw, 'gemini-test', 'a', {})
    finally:
        claude_routing._route.reset(token)
    assert mock.await_count == 6


async def test_failure_keeps_exact_request_and_backs_off(monkeypatch):
    mock = AsyncMock(side_effect=ValueError('bad response'))
    monkeypatch.setattr(fusion, '_generate', mock)
    config._config_cache.update(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True)
    raw = await prepare_antigravity_system_prompt(request())
    for _ in range(2):
        assert await fusion.fuse_request(raw, 'gemini-test', 'a', {}) is raw
    assert mock.await_count == 1


async def test_disabled_and_nontext_skip_optimizer(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(fusion, '_generate', mock)
    config._config_cache['prompt_fusion_accounts'] = {'a': 'off'}
    raw = request()
    assert await fusion.fuse_request(raw, 'gemini-test', 'a', {}) is raw
    raw['systemInstruction']['parts'].append({'inlineData': {'data': 'opaque'}})
    assert await fusion.fuse_request(raw, 'gemini-test', 'b', {}) is raw
    mock.assert_not_called()


async def test_cancelled_waiter_does_not_cancel_shared_work(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def generate(*args):
        started.set()
        await release.wait()
        return 'Keep approvals'
    mock = AsyncMock(side_effect=generate)
    monkeypatch.setattr(fusion, '_generate', mock)
    first = asyncio.create_task(fusion.fuse_request(request(), 'gemini-test', 'a', {}))
    await started.wait()
    second = asyncio.create_task(fusion.fuse_request(request(), 'gemini-test', 'a', {}))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert 'Keep approvals' in str(await second)
    assert mock.await_count == 1


@pytest.mark.parametrize('value', [{'prompt_fusion_enabled': 'true'}, {'prompt_fusion_accounts': []},
                                   {'prompt_fusion_accounts': {'a': True}}, {'prompt_fusion_accounts': {'a': 'invalid'}}])
async def test_invalid_settings_rejected(value):
    from src.panel.config_routes import save_config
    from src.models import ConfigSaveRequest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await save_config(ConfigSaveRequest(config=value), token='test')
    assert exc.value.status_code == 400


@pytest.mark.parametrize('finish,output,valid', [('STOP', '{"guidance":"Keep approvals"}', True),
    ('MAX_TOKENS', '{"guidance":"partial"}', False), ('STOP', '{"guidance":""}', False), ('STOP', 'bad', False)])
async def test_generate_uses_selected_account_without_recursion(monkeypatch, finish, output, valid):
    from src.api import antigravity
    from src import httpx_client, antigravity_pacing, billing
    import httpx
    wrap = AsyncMock(return_value=({'request': {}}, 'optimization-1'))
    monkeypatch.setattr(antigravity, 'wrap_cli_request', wrap)
    post = AsyncMock(return_value=httpx.Response(200, request=httpx.Request('POST', 'https://test'), json={
        'response': {'candidates': [{'finishReason': finish, 'content': {'parts': [{'text': output}]}}]}}))
    monkeypatch.setattr(httpx_client, 'post_async', post)
    acquire = AsyncMock()
    monkeypatch.setattr(antigravity_pacing, 'acquire_credential_slot', acquire)
    monkeypatch.setattr(antigravity_pacing, 'pace_credential', AsyncMock())
    from unittest.mock import Mock
    release = Mock()
    monkeypatch.setattr(antigravity_pacing, 'release_credential_slot', release)
    recorder = Mock(record=AsyncMock())
    monkeypatch.setattr(billing, 'get_billing_recorder', AsyncMock(return_value=recorder))
    credential = {'access_token': 'test-token', 'project_id': 'test-project', 'proxy_mode': 'direct'}
    if valid:
        assert await fusion._generate('{}', 'gemini-test', 'account-a', credential, 'caller-a') == 'Keep approvals'
    else:
        with pytest.raises(ValueError):
            await fusion._generate('{}', 'gemini-test', 'account-a', credential, 'caller-a')
    assert wrap.call_args.kwargs['skip_prompt_fusion'] is True
    assert wrap.call_args.kwargs['credential_key'] == 'account-a'
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer test-token'
    assert post.call_args.kwargs['proxy_url'] is None
    acquire.assert_awaited_once_with('account-a')
    release.assert_called_once_with('account-a')
    assert recorder.record.call_args.kwargs['api_key_id'] == 'caller-a'
    assert recorder.record.call_args.kwargs['task_type'] == 'prompt_optimization'


async def test_independent_workers_share_persistent_cache(monkeypatch, tmp_path):
    async def generate(*args):
        await asyncio.sleep(0.02)
        return 'Keep approvals'
    mock = AsyncMock(side_effect=generate)
    monkeypatch.setattr(fusion, '_generate', mock)
    path = tmp_path / 'shared.sqlite3'
    results = await asyncio.gather(*(fusion._resolve(path, 'same-key', '{}', 'gemini-test', 'a', {}, 'caller') for _ in range(3)))
    assert results == ['Keep approvals'] * 3
    assert mock.await_count == 1
    assert path.stat().st_mode & 0o777 == 0o600


async def test_settings_persist_and_reload(monkeypatch, tmp_path):
    from src.panel import config_routes
    from src.models import ConfigSaveRequest
    from src.storage_adapter import StorageAdapter
    import src.storage_adapter
    adapter = StorageAdapter()
    await adapter.initialize()
    monkeypatch.setattr(config_routes, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(src.storage_adapter, 'get_storage_adapter', AsyncMock(return_value=adapter))
    values = {'prompt_fusion_enabled': True, 'prompt_fusion_accounts': {'a': 'off', 'b': 'on'}}
    try:
        await config_routes.save_config(ConfigSaveRequest(config=values), token='test')
        assert await fusion.enabled('a') is False
        assert await fusion.enabled('b') is True
        assert await fusion.enabled('other') is True
        loaded = json.loads((await config_routes.get_config(token='test')).body)['config']
        assert all(loaded[k] == v for k, v in values.items())
    finally:
        await adapter.close()


async def test_actual_wrapper_respects_account_and_internal_bypass(monkeypatch):
    from src.api import antigravity
    from src import antigravity_model_catalog
    from types import SimpleNamespace
    monkeypatch.setattr(antigravity_model_catalog, 'get_model_enum', lambda model: 'TEST')
    monkeypatch.setattr(antigravity_model_catalog, 'get_model_info', lambda model: {})
    monkeypatch.setattr(antigravity, '_get_session_state', AsyncMock(return_value=SimpleNamespace(
        conversation_id='conversation', trajectory_id='trajectory', step_index=1, session_id='session')))
    mock = AsyncMock(return_value='Adapted instructions')
    monkeypatch.setattr(fusion, '_generate', mock)
    config._config_cache['prompt_fusion_accounts'] = {'a': 'off', 'b': 'on'}
    raw = request()
    prepared = await prepare_antigravity_system_prompt(raw)
    for account, bypass, expected in [('a', False, False), ('b', False, True), ('b', True, False)]:
        payload, _ = await antigravity.wrap_cli_request(prepared, 'gemini-test', 'project',
            credential_key=account, credential_data={'access_token': 'test'}, skip_prompt_fusion=bypass)
        assert ('Adapted instructions' in str(payload['request']['systemInstruction'])) is expected
        assert payload['request']['contents'] == raw['contents']
        assert 'fusion_instruction' not in json.dumps(payload)
    assert mock.await_count == 1
