import hashlib
from copy import deepcopy

import config
import pytest

from src.converter.gemini_fix import normalize_gemini_request


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, '_config_initialized', True)
    # 基础提示默认开启；本文件逐项显式开关，避免默认值掩盖被测分支。
    monkeypatch.setattr(config, '_config_cache', {'sensitive_word_replace_enabled': False,
                                                  'antigravity_system_prompt_enabled': False})
    monkeypatch.setenv('SENSITIVE_WORD_REPLACE_ENABLED', 'false')
    for key in ('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'ANTIGRAVITY_SYSTEM_AS_USER_ENABLED',
                'ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE', 'ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED',
                'SYSTEM_PROMPT_APPEND'):
        monkeypatch.delenv(key, raising=False)


def request():
    return {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [{'text': 'Client tool guidance'}, {'text': 'Keep permissions'}]},
            'contents': [{'role': 'user', 'parts': [{'text': 'Task'}]},
                         {'role': 'model', 'parts': [{'functionCall': {'name': 'shell', 'args': {}}, 'thoughtSignature': 'skip_thought_signature_validator'}]},
                         {'role': 'user', 'parts': [{'functionResponse': {'name': 'shell', 'response': {'ok': True}}}]}],
            'tools': [{'functionDeclarations': [{'name': 'shell', 'parameters': {'type': 'OBJECT'}}]}]}


@pytest.mark.parametrize('family', ['sonnet', 'opus'])
@pytest.mark.parametrize('tier', ['low', 'medium', 'high'])
async def test_current_claude_tier_is_execution_model(family, tier):
    model = f'claude-{family}-5-5-{tier}'
    req = request()
    req['model'] = model
    normalized = await normalize_gemini_request(req, mode='antigravity')
    assert normalized['model'] == model
    assert 'thinkingConfig' not in normalized.get('generationConfig', {})
    assert (await normalize_gemini_request(normalized, mode='antigravity'))['model'] == model


@pytest.mark.parametrize('alias,execution', [
    ('claude-sonnet-4-6-high', 'claude-sonnet-4-6'),
    ('claude-opus-4-6', 'claude-opus-4-6-thinking'),
    ('claude-haiku', 'gemini-2.5-flash'),
])
async def test_legacy_claude_aliases_still_normalize(alias, execution):
    req = request()
    req['model'] = alias
    assert (await normalize_gemini_request(req, mode='antigravity'))['model'] == execution


@pytest.mark.parametrize('inject,as_user', [(False, False), (True, False), (False, True), (True, True)])
async def test_role_options_preserve_guidance_and_tool_history(inject, as_user):
    config._config_cache.update(antigravity_system_prompt_enabled=inject, antigravity_system_as_user_enabled=as_user,
                               antigravity_system_obfuscation_mode='off')
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
    config._config_cache.update(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True,
                               antigravity_system_vowel_obfuscation_enabled=True)
    req = request()
    result = await normalize_gemini_request(req, mode=mode)
    assert result['systemInstruction'] == req['systemInstruction']
    assert result['contents'] == req['contents']


@pytest.mark.parametrize('key,instruction', [('systemInstruction', None), ('system_instructions', 'Legacy guidance')])
async def test_injection_without_system_and_legacy_system(key, instruction):
    config._config_cache.update(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True,
                               antigravity_system_obfuscation_mode='off')
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
    assert result['systemInstruction']['parts'][0]['text'].startswith('Gateway policy\n<identity>')
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
    flags = dict(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True,
                 antigravity_system_vowel_obfuscation_enabled=True)
    try:
        await config_routes.save_config(ConfigSaveRequest(config=flags), token='test')
        assert await config.get_antigravity_system_prompt_enabled()
        assert await config.get_antigravity_system_as_user_enabled()
        assert await config.get_antigravity_system_vowel_obfuscation_enabled()
        monkeypatch.setenv('ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED', 'false')
        assert not await config.get_antigravity_system_vowel_obfuscation_enabled()
        saved = json.loads((await config_routes.get_config(token='test')).body)
        assert all(saved['config'][key] is True for key in flags if key != 'antigravity_system_vowel_obfuscation_enabled')
        assert saved['config']['antigravity_system_vowel_obfuscation_enabled'] is False
        assert 'antigravity_system_vowel_obfuscation_enabled' in saved['env_locked']
        monkeypatch.setenv('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'false')
        await config_routes.save_config(ConfigSaveRequest(config=flags), token='test')
        assert not await config.get_antigravity_system_prompt_enabled()
        assert await adapter.get_config('antigravity_system_prompt_enabled') is True
        saved = json.loads((await config_routes.get_config(token='test')).body)
        assert saved['config']['antigravity_system_prompt_enabled'] is False
        assert 'antigravity_system_prompt_enabled' in saved['env_locked']
    finally:
        await adapter.close()


@pytest.mark.parametrize('key', ['antigravity_system_prompt_enabled', 'antigravity_system_as_user_enabled',
                                 'antigravity_system_vowel_obfuscation_enabled'])
async def test_panel_rejects_string_boolean(key):
    from fastapi import HTTPException
    from src.panel.config_routes import save_config
    from src.models import ConfigSaveRequest
    with pytest.raises(HTTPException) as error:
        await save_config(ConfigSaveRequest(config={key: 'false'}), token='test')
    assert error.value.status_code == 400


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('compatibility', [False, True])
@pytest.mark.parametrize('obfuscate', [False, True])
async def test_protocol_conversion_then_injection(protocol, compatibility, obfuscate):
    config._config_cache.update(antigravity_system_prompt_enabled=True, antigravity_system_as_user_enabled=True,
                               compatibility_mode_enabled=compatibility,
                               antigravity_system_vowel_obfuscation_enabled=obfuscate)
    if protocol == 'openai':
        from src.converter.openai2gemini import convert_openai_to_gemini_request
        converted = await convert_openai_to_gemini_request({'messages': [
            {'role': 'system', 'content': 'Client guidance'},
            {'role': 'user', 'content': 'Task'}]}, mode='antigravity')
    else:
        from src.converter.anthropic2gemini import anthropic_to_gemini_request
        converted = await anthropic_to_gemini_request({'system': 'Client guidance',
            'messages': [{'role': 'user', 'content': 'Task'}]}, mode='antigravity')
    converted['model'] = 'gemini-2.5-flash'
    result = await normalize_gemini_request(converted, mode='antigravity')
    system = '\n'.join(p.get('text', '') for p in result['systemInstruction']['parts'])
    assert 'You are Antigravity' in system
    assert 'Client guidance' not in system
    user = '\n'.join(p.get('text', '') for c in result['contents'] if c['role'] == 'user' for p in c['parts'])
    assert user.count('Cl13nt gv1d4nc3' if obfuscate else 'Client guidance') == 1
    assert 'Task' in user
    import json
    assert set(json.loads(json.dumps(result))) <= {'model', 'systemInstruction', 'contents', 'generationConfig', 'safetySettings', 'tools', 'toolConfig'}


@pytest.mark.parametrize('inject', [False, True])
@pytest.mark.parametrize('as_user', [False, True])
@pytest.mark.parametrize('obfuscate', [False, True])
async def test_vowel_options_preserve_history_and_server_prompt(inject, as_user, obfuscate):
    config._config_cache.update(
        antigravity_system_prompt_enabled=inject,
        antigravity_system_as_user_enabled=as_user,
        antigravity_system_obfuscation_mode='full' if obfuscate else 'off',
    )
    req = request()
    text = 'aeiou AEIOU 中文 xyz https://example.com'
    req['systemInstruction']['parts'] = [
        {'text': text, 'custom': 'aeiou'},
        {'inlineData': {'mimeType': 'image/png', 'data': 'aeiou'}},
    ]
    original = deepcopy(req)
    result = await normalize_gemini_request(req, mode='antigravity')
    parts = result['contents'][0]['parts'] if as_user else result['systemInstruction']['parts']
    expected = '4310v 4310V 中文 xyz https://3x4mpl3.c0m' if obfuscate else text
    assert {'text': expected, 'custom': 'aeiou'} in parts
    assert sum('a/e/i/o/u -> 4/3/1/0/v' in p.get('text', '') for p in parts) == int(obfuscate)
    assert {'inlineData': {'mimeType': 'image/png', 'data': 'aeiou'}} in parts
    assert result['contents'][int(as_user):] == original['contents']
    assert req == original
    if inject:
        assert 'You are Antigravity' in result['systemInstruction']['parts'][0]['text']
    again = await normalize_gemini_request(result, mode='antigravity')
    assert again == result


async def test_user_guidance_requires_reading_before_task_and_no_extra_reply():
    config._config_cache['antigravity_system_as_user_enabled'] = True
    result = await normalize_gemini_request(request(), mode='antigravity')
    lead = result['contents'][0]['parts'][0]['text']
    assert 'Read and understand' in lead
    assert 'before responding to subsequent user tasks' in lead
    assert 'Do not send a separate acknowledgment' in lead
    assert 'user-level priority' in lead


@pytest.mark.parametrize('instruction', [None, '', {'parts': []}, {'parts': [{'text': '   '}]}])
async def test_no_guidance_bridge_for_empty_prompt(instruction):
    config._config_cache.update(antigravity_system_as_user_enabled=True,
                               antigravity_system_vowel_obfuscation_enabled=True)
    req = request()
    req['systemInstruction'] = instruction
    result = await normalize_gemini_request(req, mode='antigravity')
    assert result['contents'] == req['contents']


@pytest.mark.parametrize('key', ['systemInstruction', 'system_instructions'])
async def test_legacy_text_obfuscation_keeps_gateway_prefix(key):
    config._config_cache.update(antigravity_system_vowel_obfuscation_enabled=True,
                               system_prompt_append='Gateway policy\n')
    req = {'model': 'gemini-2.5-flash', key: 'Client guidance', 'contents': []}
    result = await normalize_gemini_request(req, mode='antigravity')
    decoder, guidance = result['systemInstruction']['parts']
    assert decoder['text'].startswith('Gateway policy\nLeet encoding adapter.')
    assert 'a/e/i/o/u -> 4/3/1/0/v' in decoder['text']
    assert guidance['text'] == 'Cl13nt gv1d4nc3'
    assert 'system_instructions' not in result


@pytest.mark.parametrize('legacy_full_prompt', ['0', '1'])
async def test_default_switches_on_official_agy_baseline(monkeypatch, legacy_full_prompt):
    monkeypatch.setenv('ANTIGRAVITY_FULL_PROMPT_BASELINE', legacy_full_prompt)
    from src.converter.antigravity_system_prompt import (
        BRIDGE,
        DEFAULT_PROMPT_VERSION,
        PROMPT_PATH,
        baseline_prompt,
    )

    assert config._config_cache.get('antigravity_system_prompt_enabled') is False  # 夹具隔离
    config._config_cache.pop('antigravity_system_prompt_enabled', None)
    assert await config.get_antigravity_system_prompt_enabled() is True

    # 注入内容必须是官方 AGY 基础模块，而不是自定义前置文本或融合产物。
    expected = PROMPT_PATH.read_text(encoding='utf-8').strip()
    assert DEFAULT_PROMPT_VERSION == '2.35.2'
    assert expected.startswith('<identity>')
    assert baseline_prompt() == expected

    # 基线来源完整性：2.35.2 整段基线与 2.31.2 历史模块都不得被替换。
    from src.converter.antigravity_system_prompt import PROMPT_DIR

    digests = {
        'antigravity-2.35.2.txt': '7ead810a96593a0394485665cb7c3851f8b704645679c45d3cc77b7447d0c528',
        'antigravity-2.31.2.txt': '85a8a9070376c8b0a90c0c97b6001991e490a919351cf827e36a446ddacbdd9f',
        'antigravity-2.31.2/identity.tmpl': 'bd8957e589c97f9fa5d5fe3077b01c49b0329f0987bd58cf73bab123ea0a199b',
        'antigravity-2.31.2/communication_style.tmpl': '96e2f45838771003bbfb05206623bb6be4b64187eaa97a4e4d1f2d9e9de86c8f',
        'antigravity-2.31.2/guidelines.tmpl': 'f4ede326caaa79f7454af6dac251cf0436ab8375c1b06eade975bcaf85a7c439',
    }
    for relative, digest in digests.items():
        assert hashlib.sha256((PROMPT_DIR / relative).read_bytes()).hexdigest() == digest

    req = {'model': 'gemini-2.5-flash',
           'contents': [{'role': 'user', 'parts': [{'text': 'Task'}]}]}
    result = await normalize_gemini_request(req, mode='antigravity')
    head = result['systemInstruction']['parts'][0]['text']
    assert head == expected + '\n\n' + BRIDGE
    assert head.startswith('<identity>')


async def test_default_injection_keeps_the_complete_official_prefix_for_older_client_guidance():
    from src.converter.antigravity_system_prompt import BRIDGE, baseline_prompt

    config._config_cache.pop('antigravity_system_prompt_enabled')
    guidance = baseline_prompt().splitlines()[0]
    payload = {'model': 'gemini-2.5-flash', 'contents': [],
               'systemInstruction': {'parts': [{'text': guidance}]}}
    result = await normalize_gemini_request(payload, mode='antigravity')
    assert result['systemInstruction']['parts'][0]['text'] == baseline_prompt() + '\n\n' + BRIDGE


def test_bridge_covers_every_host_adaptation_requirement():
    """引导词必须持续覆盖宿主适配的全部约束，缺一条就是回归。"""
    from src.converter.antigravity_system_prompt import (
        BRIDGE,
        BRIDGE_REQUIREMENTS,
        validate_bridge,
    )

    assert validate_bridge() == []
    assert len(BRIDGE_REQUIREMENTS) >= 8
    for _, needle in BRIDGE_REQUIREMENTS:
        assert needle in BRIDGE
    assert '=== END OF HOST DOCUMENT ===' in BRIDGE
    assert 'override the host document' in BRIDGE


async def test_baseline_is_the_official_2352_prompt_without_runtime_block():
    from src.converter.antigravity_system_prompt import baseline_prompt

    text = baseline_prompt('2.35.2')
    for tag in ('<identity>', '<terminal_sandbox>', '<artifacts>', '<guidelines>',
                '<work_efficiently>', '<communication_style>', '<slash_commands>',
                '<messaging>'):
        assert tag in text
    # 运行时块（workspace / conversation / appDataDir 实例值）不得进仓库基线。
    assert '<user_information>' not in text
    assert 'Conversation ID:' not in text
