from copy import deepcopy

import config
import pytest

from src.converter.gemini_fix import normalize_gemini_request


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {'sensitive_word_replace_enabled': False})
    monkeypatch.setenv('SENSITIVE_WORD_REPLACE_ENABLED', 'false')
    for key in ('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'ANTIGRAVITY_SYSTEM_AS_USER_ENABLED', 'SYSTEM_PROMPT_APPEND'):
        monkeypatch.delenv(key, raising=False)


def request():
    return {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [{'text': 'Client tool guidance'}, {'text': 'Keep permissions'}]},
            'contents': [{'role': 'user', 'parts': [{'text': 'Task'}]},
                         {'role': 'model', 'parts': [{'functionCall': {'name': 'shell', 'args': {}}, 'thoughtSignature': 'skip_thought_signature_validator'}]},
                         {'role': 'user', 'parts': [{'functionResponse': {'name': 'shell', 'response': {'ok': True}}}]}],
            'tools': [{'functionDeclarations': [{'name': 'shell', 'parameters': {'type': 'OBJECT'}}]}]}


@pytest.mark.parametrize('inject,as_user', [(False, False), (True, False), (False, True), (True, True)])
async def test_role_options_preserve_guidance_and_tool_history(inject, as_user):
    config._config_cache.update(antigravity_system_prompt_enabled=inject, antigravity_system_as_user_enabled=as_user)
    req = request()
    original = deepcopy(req)
    result = await normalize_gemini_request(req, mode='antigravity')
    assert req == original
    assert result['contents'][int(as_user):] == original['contents']
    if as_user:
        guidance = result['contents'][0]
        assert guidance['role'] == 'user'
        assert any(part.get('text') == 'Client tool guidance' for part in guidance['parts'])
        assert any(part.get('text') == 'Keep permissions' for part in guidance['parts'])
        assert all('Client tool guidance' not in part.get('text', '') for part in result.get('systemInstruction', {}).get('parts', []))
    if inject:
        text = '\n'.join(p.get('text', '') for p in result['systemInstruction']['parts'])
        assert 'You are Antigravity' in text
        assert '{{' not in text
        assert ('Client tool guidance' in text) == (not as_user)
    elif as_user:
        assert 'systemInstruction' not in result
    else:
        assert result['systemInstruction'] == original['systemInstruction']
    again = await normalize_gemini_request(result, mode='antigravity')
    assert again['systemInstruction'] == result['systemInstruction'] if 'systemInstruction' in result else 'systemInstruction' not in again
    assert again['contents'] == result['contents']


@pytest.mark.parametrize('mode', ['geminicli', 'vertex'])
async def test_options_are_antigravity_only(mode):
    config._config_cache.update(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True)
    req = request()
    result = await normalize_gemini_request(req, mode=mode)
    assert result['systemInstruction'] == req['systemInstruction']
    assert result['contents'] == req['contents']


@pytest.mark.parametrize('key,instruction', [('systemInstruction', None), ('system_instructions', 'Legacy guidance')])
async def test_injection_without_system_and_legacy_system(key, instruction):
    config._config_cache.update(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True)
    req = {'model': 'gemini-2.5-flash', 'contents': [], key: instruction}
    result = await normalize_gemini_request(req, mode='antigravity')
    assert 'You are Antigravity' in result['systemInstruction']['parts'][0]['text']
    assert 'system_instructions' not in result
    if instruction:
        assert any(p.get('text') == instruction for p in result['contents'][0]['parts'])
    else:
        assert result['contents'] == []


async def test_custom_prefix_stays_at_system_level():
    config._config_cache.update(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True, system_prompt_append='Gateway policy\n')
    result = await normalize_gemini_request(request(), mode='antigravity')
    assert result['systemInstruction']['parts'][0]['text'].startswith('Gateway policy\nYou are Antigravity')
    assert all('Gateway policy' not in p.get('text', '') for p in result['contents'][0]['parts'])
    again = await normalize_gemini_request(result, mode='antigravity')
    assert again['systemInstruction'] == result['systemInstruction']
    assert again['contents'] == result['contents']


async def test_panel_save_reload_and_environment_lock(tmp_path, monkeypatch):
    import json
    from unittest.mock import AsyncMock
    from src.storage_adapter import StorageAdapter
    from src.panel import config_routes
    from src.models import ConfigSaveRequest
    import src.storage_adapter

    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    adapter = StorageAdapter()
    await adapter.initialize()
    monkeypatch.setattr(config_routes, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(src.storage_adapter, 'get_storage_adapter', AsyncMock(return_value=adapter))
    flags = dict(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True)
    try:
        await config_routes.save_config(ConfigSaveRequest(config=flags), token='test')
        assert await config.get_antigravity_system_prompt_enabled()
        assert await config.get_antigravity_system_as_user_enabled()
        saved = json.loads((await config_routes.get_config(token='test')).body)
        assert all(saved['config'][key] is True for key in flags)
        monkeypatch.setenv('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'false')
        await config_routes.save_config(ConfigSaveRequest(config=flags), token='test')
        assert not await config.get_antigravity_system_prompt_enabled()
        assert await adapter.get_config('antigravity_system_prompt_enabled') is True
        saved = json.loads((await config_routes.get_config(token='test')).body)
        assert saved['config']['antigravity_system_prompt_enabled'] is False
        assert 'antigravity_system_prompt_enabled' in saved['env_locked']
    finally:
        await adapter.close()


@pytest.mark.parametrize('key', ['antigravity_system_prompt_enabled', 'antigravity_system_as_user_enabled'])
async def test_panel_rejects_string_boolean(key):
    from fastapi import HTTPException
    from src.panel.config_routes import save_config
    from src.models import ConfigSaveRequest
    with pytest.raises(HTTPException) as error:
        await save_config(ConfigSaveRequest(config={key: 'false'}), token='test')
    assert error.value.status_code == 400


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('compatibility', [False, True])
async def test_protocol_conversion_then_injection(protocol, compatibility):
    config._config_cache.update(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True,
                               compatibility_mode_enabled=compatibility)
    if protocol == 'openai':
        from src.converter.openai2gemini import convert_openai_to_gemini_request
        converted = await convert_openai_to_gemini_request({'messages': [
            {'role': 'system', 'content': 'Client guidance'},
            {'role': 'user', 'content': 'Task'}]})
    else:
        from src.converter.anthropic2gemini import anthropic_to_gemini_request
        converted = await anthropic_to_gemini_request({'system': 'Client guidance',
            'messages': [{'role': 'user', 'content': 'Task'}]})
    converted['model'] = 'gemini-2.5-flash'
    result = await normalize_gemini_request(converted, mode='antigravity')
    system = '\n'.join(p.get('text', '') for p in result['systemInstruction']['parts'])
    assert 'You are Antigravity' in system
    assert 'Client guidance' not in system
    user = '\n'.join(p.get('text', '') for c in result['contents'] if c['role'] == 'user' for p in c['parts'])
    assert user.count('Client guidance') == 1
    assert 'Task' in user
    import json
    assert set(json.loads(json.dumps(result))) <= {'model', 'systemInstruction', 'contents', 'generationConfig', 'safetySettings', 'tools', 'toolConfig'}
