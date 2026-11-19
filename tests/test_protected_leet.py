from copy import deepcopy

import config
import pytest


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {'sensitive_word_replace_enabled': False})
    for key in ('ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE', 'ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED',
                'ANTIGRAVITY_SYSTEM_AS_USER_ENABLED', 'ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'SYSTEM_PROMPT_APPEND'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('SENSITIVE_WORD_REPLACE_ENABLED', 'false')


def test_literal_protection_and_natural_language_encoding():
    from src.converter.protected_leet import protected_system_parts
    source = ('Use the read_file tool to inspect /home/user/project/config.json. '
              'Visit https://example.com/api?mode=auto. Return "file_path" and `status`.\n'
              '```python\nmode = "auto"\nprint(mode)\n```\n'
              'Return exactly {"tool":"read_file","mode":"auto"}. Keep SOME_ENV, fileName, --output and C:\\Users\\alice\\file.txt.')
    parts = protected_system_parts([{'text': source, 'custom': 'unchanged'}], [], 'generic')
    output = parts[1]['text']
    assert 'Vs3 th3' in output and 'K33p' in output
    for literal in ('read_file', '/home/user/project/config.json', 'https://example.com/api?mode=auto',
                    '"file_path"', '`status`', '```python\nmode = "auto"\nprint(mode)\n```',
                    '{"tool":"read_file","mode":"auto"}', 'SOME_ENV', 'fileName', '--output', 'C:\\Users\\alice\\file.txt'):
        assert literal in output
    assert parts[1]['custom'] == 'unchanged'
    assert 'a/e/i/o/u -> 4/3/1/0/v' in parts[0]['text']


def test_declared_tools_and_argument_names_are_protected():
    from src.converter.protected_leet import protected_system_parts
    tools = [{'functionDeclarations': [{'name': 'inspect', 'parametersJsonSchema': {
        'type': 'object', 'properties': {'location': {'type': 'string'}, 'mode': {'type': 'string', 'enum': ['auto']}}}}]}]
    parts = protected_system_parts([{'text': 'Use inspect with location and mode auto.'}], tools, 'generic')
    assert all(word in parts[1]['text'] for word in ('inspect', 'location', 'mode', 'auto'))
    assert 'Vs3' in parts[1]['text']


@pytest.mark.parametrize('text,expected', [
    ('You are Codex, an agent based on GPT.\n<environment_context>cwd: /tmp/example</environment_context>', 'codex'),
    ("You are Claude Code, Anthropic's official CLI for Claude.\n<env>Working directory: /tmp/example</env>", 'claude_code'),
    ('You are DSH, a coding agent.\n## Tools\nread_file(path)', 'dsh'),
    ('You are an expert coding assistant operating inside pi, a coding agent harness.\nAvailable tools:\nread_file', 'pi'),
    ('Compare Codex, Claude Code, DSH and pi.', 'generic'),
    ('You are Codex, a fictional character in a story.', 'generic'),
])
def test_client_profiles_require_identity_and_structure(text, expected):
    from src.converter.protected_leet import detect_client_profile
    assert detect_client_profile(text) == expected


@pytest.mark.parametrize('profile,block', [
    ('codex', '<environment_context>shell: bash\ncwd: /home/user/project</environment_context>'),
    ('claude_code', '<env>Working directory: /home/user/project</env>'),
    ('dsh', '## Tools\n### read_file\nArguments: location, mode\n'),
    ('pi', '## Available tools\nread(path): Read file contents.\n'),
])
def test_profile_environment_and_tool_sections_remain_exact(profile, block):
    from src.converter.protected_leet import protected_system_parts
    result = protected_system_parts([{'text': 'Use tools carefully.\n' + block + '\n## Task\nExplain results.'}], [], profile)
    assert block in result[1]['text']
    assert '3xpl41n r3svlts.' in result[1]['text']


@pytest.mark.parametrize('as_user', [False, True])
async def test_pipeline_protects_literals_before_role_conversion_and_is_idempotent(as_user):
    from src.converter.gemini_fix import normalize_gemini_request
    config._config_cache.update(antigravity_system_obfuscation_mode='protected',
                               antigravity_system_as_user_enabled=as_user,
                               antigravity_system_prompt_enabled=True,
                               system_prompt_append='Gateway policy\n')
    req = {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [{'text': 'Use read_file at /home/user/config.json.'}]},
           'contents': [{'role': 'user', 'parts': [{'text': 'Task /home/user/config.json'}]}]}
    original = deepcopy(req)
    result = await normalize_gemini_request(req, mode='antigravity')
    parts = result['contents'][0]['parts'] if as_user else result['systemInstruction']['parts']
    text = '\n'.join(p.get('text', '') for p in parts)
    assert 'Vs3' in text and '/home/user/config.json' in text
    assert 'Leet' in text and '4/3/1/0/v' in text
    assert result['systemInstruction']['parts'][0]['text'].startswith('Gateway policy\nYou are Antigravity')
    assert result['contents'][int(as_user):] == original['contents']
    assert req == original
    assert await normalize_gemini_request(result, mode='antigravity') == result


async def test_client_detection_precedes_sensitive_word_filter(monkeypatch):
    from src.converter.gemini_fix import normalize_gemini_request
    config._config_cache.update(antigravity_system_obfuscation_mode='protected', sensitive_word_replace_enabled=True)
    monkeypatch.setenv('SENSITIVE_WORD_REPLACE_ENABLED', 'true')
    req = {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [{'text':
        'You are Codex, an agent based on GPT.\n<environment_context>shell: bash</environment_context>'}]}, 'contents': []}
    result = await normalize_gemini_request(req, mode='antigravity')
    assert 'Client profile: codex' in result['systemInstruction']['parts'][0]['text']


async def test_config_legacy_and_environment_priority(monkeypatch):
    assert await config.get_antigravity_system_obfuscation_mode() == 'protected'
    config._config_cache['antigravity_system_vowel_obfuscation_enabled'] = False
    assert await config.get_antigravity_system_obfuscation_mode() == 'off'
    config._config_cache['antigravity_system_vowel_obfuscation_enabled'] = True
    assert await config.get_antigravity_system_obfuscation_mode() == 'protected'
    config._config_cache['antigravity_system_obfuscation_mode'] = 'full'
    assert await config.get_antigravity_system_obfuscation_mode() == 'full'
    monkeypatch.setenv('ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED', 'true')
    assert await config.get_antigravity_system_obfuscation_mode() == 'protected'
    monkeypatch.setenv('ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED', 'false')
    assert await config.get_antigravity_system_obfuscation_mode() == 'off'
    monkeypatch.setenv('ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE', 'protected')
    assert await config.get_antigravity_system_obfuscation_mode() == 'protected'


def test_unclosed_code_and_tag_collision():
    from src.converter.protected_leet import protected_system_parts
    source = 'Read this code:\n```python\nprint("auto")\n<gcli_literal>example</gcli_literal>'
    result = protected_system_parts([{'text': source}], [], 'generic')
    assert '```python\nprint("auto")\n<gcli_literal>example</gcli_literal>' in result[1]['text']
    assert 'R34d th1s c0d3:' in result[1]['text']


@pytest.mark.parametrize('mode', ['off', 'full', 'protected'])
async def test_panel_mode_save_reload(tmp_path, monkeypatch, mode):
    import json
    from unittest.mock import AsyncMock
    from src.storage_adapter import StorageAdapter
    import src.storage_adapter
    from src.panel import config_routes
    from src.models import ConfigSaveRequest
    adapter = StorageAdapter()
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    await adapter.initialize()
    monkeypatch.setattr(config_routes, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(src.storage_adapter, 'get_storage_adapter', AsyncMock(return_value=adapter))
    try:
        await config_routes.save_config(ConfigSaveRequest(config={'antigravity_system_obfuscation_mode': mode}), token='test')
        data = json.loads((await config_routes.get_config(token='test')).body)
        assert data['config']['antigravity_system_obfuscation_mode'] == mode
        assert await adapter.get_config('antigravity_system_vowel_obfuscation_enabled') is (mode != 'off')
        assert await config.get_antigravity_system_vowel_obfuscation_enabled() is (mode != 'off')
        if mode == 'protected':
            # An older panel can still turn the feature off.
            await config_routes.save_config(ConfigSaveRequest(config={'antigravity_system_vowel_obfuscation_enabled': False}), token='test')
            assert await config.get_antigravity_system_obfuscation_mode() == 'off'
            await config_routes.save_config(ConfigSaveRequest(config={'antigravity_system_vowel_obfuscation_enabled': True}), token='test')
            assert await config.get_antigravity_system_obfuscation_mode() == 'protected'
    finally:
        await adapter.close()


@pytest.mark.parametrize('mode', ['invalid', True, [], {}])
async def test_panel_rejects_invalid_modes(mode):
    from fastapi import HTTPException
    from src.panel.config_routes import save_config
    from src.models import ConfigSaveRequest
    with pytest.raises(HTTPException) as exc:
        await save_config(ConfigSaveRequest(config={'antigravity_system_obfuscation_mode': mode}), token='test')
    assert exc.value.status_code == 400


def test_preview_uses_same_encoder_and_validates_input():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.panel.config_routes import router, verify_panel_token
    from src.converter.protected_leet import protected_system_parts
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_panel_token] = lambda: 'test'
    text = 'Use read_file at /home/user/config.json.'
    with TestClient(app) as client:
        response = client.post('/config/system-obfuscation/preview', json={'text': text, 'mode': 'protected'})
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert 'Leet encoding adapter' in response.json()['decoder']
        assert '/home/user/config.json' in response.json()['client_text']
        assert 'Leet encoding adapter' not in response.json()['client_text']
        assert response.json()['text'] == '\n'.join(p['text'] for p in protected_system_parts([{'text': text}], []))
        full = client.post('/config/system-obfuscation/preview', json={'text': text, 'mode': 'full'}).json()
        assert 'Leet encoding adapter' in full['decoder']
        assert full['client_text'] == 'Vs3 r34d_f1l3 4t /h0m3/vs3r/c0nf1g.js0n.'
        assert full['text'] == '\n'.join(p['text'] for p in protected_system_parts(
            [{'text': text}], [], protect_technical=False))
        assert client.post('/config/system-obfuscation/preview', json={'text': 'x' * 20001, 'mode': 'protected'}).status_code == 422
        assert client.post('/config/system-obfuscation/preview', json={'text': text, 'mode': 'bad'}).status_code == 422


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('as_user', [False, True])
async def test_protected_mode_survives_compatibility_conversion(protocol, as_user):
    from src.converter.gemini_fix import normalize_gemini_request
    config._config_cache.update(antigravity_system_obfuscation_mode='protected',
                               antigravity_system_as_user_enabled=as_user, compatibility_mode_enabled=True)
    text = 'Use read_file at /home/user/config.json.'
    if protocol == 'openai':
        from src.converter.openai2gemini import convert_openai_to_gemini_request
        converted = await convert_openai_to_gemini_request({'messages': [
            {'role': 'system', 'content': text}, {'role': 'user', 'content': 'Task'}]}, mode='antigravity')
    else:
        from src.converter.anthropic2gemini import anthropic_to_gemini_request
        converted = await anthropic_to_gemini_request({'system': text, 'messages': [{'role': 'user', 'content': 'Task'}]}, mode='antigravity')
    converted['model'] = 'gemini-2.5-flash'
    result = await normalize_gemini_request(converted, mode='antigravity')
    parts = result['contents'][0]['parts'] if as_user else result['systemInstruction']['parts']
    output = '\n'.join(p['text'] for p in parts)
    assert '/home/user/config.json' in output and 'Vs3' in output and 'Leet' in output
    assert result['contents'][int(as_user):] == [{'role': 'user', 'parts': [{'text': 'Task'}]}]


@pytest.mark.parametrize('mode', ['geminicli', 'vertex'])
async def test_protected_mode_does_not_change_other_channels(mode):
    from src.converter.gemini_fix import normalize_gemini_request
    config._config_cache['antigravity_system_obfuscation_mode'] = 'protected'
    req = {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [{'text': 'Use read_file.'}]}, 'contents': []}
    result = await normalize_gemini_request(req, mode=mode)
    assert result['systemInstruction'] == req['systemInstruction']


def test_nontext_parts_and_empty_prompt_are_untouched():
    from src.converter.protected_leet import protected_system_parts
    parts = [{'inlineData': {'mimeType': 'image/png', 'data': 'aeiou'}}, {'text': '   '}]
    assert protected_system_parts(parts, []) == parts
    result = protected_system_parts([{'text': 'Use tools.'}, parts[0]], [])
    assert result[-1] == parts[0]


@pytest.mark.parametrize('protect_technical', [False, True])
def test_exact_response_contract_is_preserved(protect_technical):
    from src.converter.protected_leet import protected_system_parts
    result = protected_system_parts([{'text': (
        'Calculate the difference. Return only the integer.\n'
        'Reply with exactly FUSION_CHECK_811 and no other text.\n'
        'Output exactly v1.0. Explain the result.'
    )}], [], protect_technical=protect_technical)
    assert 'C4lcvl4t3 th3 d1ff3r3nc3.' in result[1]['text']
    assert 'Return only the integer.' in result[1]['text']
    assert 'Reply with exactly FUSION_CHECK_811 and no other text.' in result[1]['text']
    assert 'Output exactly v1.0. 3xpl41n th3 r3svlt.' in result[1]['text']
    assert '<gcli_literal>Return only' not in result[1]['text']
    assert '<gcli_literal>Reply with exactly' not in result[1]['text']


@pytest.mark.parametrize('mode', ['full', 'protected'])
async def test_normalization_preserves_bare_output_delimiters_and_json_example(mode):
    from src.converter.gemini_fix import normalize_gemini_request

    config._config_cache['antigravity_system_obfuscation_mode'] = mode
    example = '{"chunk_id": 1, "violation_signal": [], "unknowns": []}'
    source = (
        'Write the result between FINAL_JSON_START and FINAL_JSON_END.\n'
        f'Use this example when nothing is found: {example}'
    )
    request = {
        'model': 'gemini-2.5-flash-lite',
        'systemInstruction': {'parts': [{'text': source}]},
    }
    result = await normalize_gemini_request(request, mode='antigravity')
    guidance = result['systemInstruction']['parts'][-1]['text']
    assert 'Wr1t3 th3 r3svlt b3tw33n' in guidance
    for literal in ('FINAL_JSON_START', 'FINAL_JSON_END', example):
        assert f'<gcli_literal>{literal}</gcli_literal>' in guidance


@pytest.mark.parametrize('mode', ['full', 'protected'])
async def test_normalization_preserves_explicit_multiline_literals(mode):
    from src.converter.gemini_fix import normalize_gemini_request

    config._config_cache['antigravity_system_obfuscation_mode'] = mode
    block = '<literal>\nopen payload v1.0\n  {"unknowns": []}\nclose payload\n</literal>'
    source = f'Follow this format.\n{block}\nExplain the result.'
    request = {
        'model': 'gemini-2.5-flash-lite',
        'systemInstruction': {'parts': [{'text': source}]},
    }
    result = await normalize_gemini_request(request, mode='antigravity')
    guidance = result['systemInstruction']['parts'][-1]['text']
    assert guidance == (
        f'F0ll0w th1s f0rm4t.\n<gcli_literal>{block}</gcli_literal>\n3xpl41n th3 r3svlt.'
    )


@pytest.mark.parametrize('mode', ['full', 'protected'])
@pytest.mark.parametrize('schema_key', ['responseSchema', 'responseJsonSchema'])
async def test_normalization_passes_through_generic_schema_requests(mode, schema_key):
    from src.converter.gemini_fix import normalize_gemini_request

    config._config_cache['antigravity_system_obfuscation_mode'] = mode
    schema = {
        'type': 'object',
        'properties': {
            'assessment': {'type': 'string', 'enum': ['available', 'unknown']},
            'verdict': {'type': 'string', 'enum': ['S', 'L', 'A', 'U', 'N']},
        },
    }
    request = {
        'model': 'gemini-2.5-flash-lite',
        'systemInstruction': {'parts': [{'text': (
            'Compare the results. Use assessment and verdict. Choose available or unknown. '
            'Return category codes S/L/A/U/N only. Use null, true or false as appropriate.'
        )}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'Task'}]}],
        'generationConfig': {schema_key: schema},
    }
    result = await normalize_gemini_request(request, mode='antigravity')
    assert result['systemInstruction'] == request['systemInstruction']
    assert result['generationConfig'][schema_key] == schema
    assert result['contents'] == request['contents']


def test_prose_only_decoder_does_not_introduce_literal_markup():
    from src.converter.protected_leet import protected_system_parts
    parts = protected_system_parts([{'text': 'Compare the numbers. Explain in Chinese.'}], [])
    assert 'gcli_literal' not in '\n'.join(p['text'] for p in parts)
    assert 'a/e/i/o/u -> 4/3/1/0/v' in parts[0]['text']


@pytest.mark.parametrize('contract', [
    '## Output Format\nUse the value pending review.\n```yaml\n# Example\nstatus: pending review\n```\nEnd with done.\n',
    '返回格式：\r\n遇到未知项时使用 pending review。\r\n',
    '【输出格式】\n遇到未知项时使用 pending review。\n',
    'Return the result as a CSV record.\nUse pending review for unresolved items.\n',
    'The response must be a JSON object.\nUse pending review for unresolved items.\n',
])
async def test_default_preserves_output_contract_without_schema(contract):
    from src.converter.gemini_fix import normalize_gemini_request

    source = f'Compare the records.\n{contract}## Notes\nExplain the result.'
    request = {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [{'text': source}]}}
    result = await normalize_gemini_request(request, mode='antigravity')
    guidance = result['systemInstruction']['parts'][-1]['text']
    assert guidance == f'C0mp4r3 th3 r3c0rds.\n{contract}## N0t3s\n3xpl41n th3 r3svlt.'


async def test_default_preserves_system_text_in_json_mode_without_schema():
    from src.converter.gemini_fix import normalize_gemini_request

    request = {
        'model': 'gemini-2.5-flash',
        'systemInstruction': {'parts': [{'text': 'Use pending review for unresolved items.'}]},
        'generationConfig': {'responseMimeType': 'application/json'},
    }
    result = await normalize_gemini_request(request, mode='antigravity')
    assert result['systemInstruction'] == request['systemInstruction']
    assert result['generationConfig']['responseMimeType'] == 'application/json'


async def test_default_preserves_declared_stop_text_without_schema():
    from src.converter.gemini_fix import normalize_gemini_request

    request = {
        'model': 'gemini-2.5-flash',
        'systemInstruction': {'parts': [{'text': 'Use the delimiter finished now when done.'}]},
        'generationConfig': {'stopSequences': ['finished now']},
    }
    result = await normalize_gemini_request(request, mode='antigravity')
    guidance = result['systemInstruction']['parts'][-1]['text']
    assert guidance == 'Vs3 th3 d3l1m1t3r <gcli_literal>finished now</gcli_literal> wh3n d0n3.'


@pytest.mark.parametrize('literal', [
    '{{ product_title }}', '{customer.name!r}', '%(status)s', '%APP_HOME%',
    '<result data-mode="raw">', '</result>', 'file://', '12.5 mg/mL',
])
def test_protected_preserves_technical_literals_without_schema(literal):
    from src.converter.protected_leet import protected_system_parts

    parts = protected_system_parts([{'text': f'Compare {literal} carefully.'}], [])
    assert parts[-1]['text'] == f'C0mp4r3 <gcli_literal>{literal}</gcli_literal> c4r3fvlly.'


def test_protected_preserves_technical_literals_adjacent_to_chinese_prose():
    from src.converter.protected_leet import protected_system_parts

    source = '请记录chunk_id及riskScore，容量20oz，查询https://example.com/api，并用FINAL_JSON_START开头。'
    parts = protected_system_parts([{'text': source}], [])
    for literal in ('chunk_id', 'riskScore', '20oz', 'https://example.com/api', 'FINAL_JSON_START'):
        assert f'<gcli_literal>{literal}</gcli_literal>' in parts[-1]['text']


def test_protected_does_not_treat_prose_about_input_format_as_output_contract():
    from src.converter.protected_leet import protected_system_parts

    parts = protected_system_parts([{'text': 'Return a summary of the JSON input.\nExplain the result.'}], [])
    assert parts[-1]['text'].endswith('\n3xpl41n th3 r3svlt.')


def test_protected_keeps_uninspected_structured_text_at_json_scan_limit():
    from src.converter.protected_leet import protected_system_parts

    example = '{"checks": [true, false, null], "unknowns": []}'
    source = 'Compare the records.\n' + '[annotation] ' * 140 + '\nExample: ' + example
    parts = protected_system_parts([{'text': source}], [])
    assert parts[-1]['text'].startswith('C0mp4r3 th3 r3c0rds.')
    assert example in parts[-1]['text']


def test_preserves_system_text_when_declared_literal_limit_is_exceeded():
    from src.converter.protected_leet import protected_system_parts

    parts = [{'text': 'Use pending review for unresolved items.'}]
    values = [f'longer configured state {index}' for index in range(512)] + ['pending review']
    schema = {'type': 'string', 'enum': values}
    assert protected_system_parts(parts, [], response_schemas=(schema,)) == parts
