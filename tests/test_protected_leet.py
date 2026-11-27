from copy import deepcopy

import config
import pytest


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, '_config_initialized', True)
    # 本文件只测混淆/编码；基础提示注入显式关闭，默认开启的行为由专门用例覆盖。
    monkeypatch.setattr(config, '_config_cache', {'sensitive_word_replace_enabled': False,
                                                 'antigravity_system_prompt_enabled': False,
                                                 'antigravity_system_obfuscation_mode': 'protected'})
    for key in ('ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE', 'ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED',
                'ANTIGRAVITY_SYSTEM_AS_USER_ENABLED', 'ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'SYSTEM_PROMPT_APPEND',
                'ANTIGRAVITY_WORKFLOW_CONTRACTS_FILE'):
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
    assert 'Use the' in output and 'K33p' in output
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
    assert 'Use' in parts[1]['text']


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
    assert 'Use' in text and '/home/user/config.json' in text
    assert 'Leet' in text and '4/3/1/0/v' in text
    assert result['systemInstruction']['parts'][0]['text'].startswith('Gateway policy\n<identity>')
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
    config._config_cache.pop('antigravity_system_obfuscation_mode', None)
    assert await config.get_antigravity_system_obfuscation_mode() == 'off'
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
        structured = 'Analyze the record. Return only JSON. Use pending review for unresolved items.'
        preview = client.post('/config/system-obfuscation/preview', json={'text': structured, 'mode': 'protected'})
        assert preview.json()['client_text'] == '4n4lyz3 the r3c0rd. Return only JSON. Use p3nd1ng r3v13w for vnr3s0lv3d 1t3ms.'
        assert 'Leet encoding adapter' in preview.json()['decoder']
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
    assert '/home/user/config.json' in output and 'Use' in output and 'Leet' in output
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
    article = 'the' if protect_technical else 'th3'
    assert f'C4lcvl4t3 {article} d1ff3r3nc3.' in result[1]['text']
    assert 'Return only the integer.' in result[1]['text']
    assert 'Reply with exactly FUSION_CHECK_811 and no other text.' in result[1]['text']
    assert f'Output exactly v1.0. 3xpl41n {article} r3svlt.' in result[1]['text']
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
    assert result['systemInstruction']['parts'][-1]['text'] == (
        'Wr1t3 the r3svlt b3tw33n FINAL_JSON_START and FINAL_JSON_END.\n'
        f'Use th1s 3x4mpl3 wh3n n0th1ng is f0vnd: {example}'
    )
    assert result.workflow_contract is None


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
    literal = block if mode == 'protected' else f'<gcli_literal>{block}</gcli_literal>'
    article = 'the' if mode == 'protected' else 'th3'
    assert guidance == f'F0ll0w th1s f0rm4t.\n{literal}\n3xpl41n {article} r3svlt.'


@pytest.mark.parametrize('mode', ['full', 'protected'])
@pytest.mark.parametrize('schema_key', ['responseSchema', 'responseJsonSchema'])
async def test_generic_schema_requests_encode_prose_and_preserve_literals(mode, schema_key):
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
    parts = result['systemInstruction']['parts']
    assert parts[0]['text'].startswith('Leet encoding adapter.')
    assert parts[1]['text'] == (
        'C0mp4r3 the r3svlts. Use assessment and verdict. Ch00s3 available or unknown. '
        'R3tvrn c4t3g0ry c0d3s S/L/A/U/N 0nly. Use null, true or false as 4ppr0pr14t3.'
    )
    assert result['generationConfig'][schema_key] == schema
    assert result['contents'] == request['contents']


def test_prose_only_decoder_does_not_introduce_literal_markup():
    from src.converter.protected_leet import protected_system_parts
    parts = protected_system_parts([{'text': 'Compare the numbers. Explain in Chinese.'}], [])
    assert 'gcli_literal' not in '\n'.join(p['text'] for p in parts)
    assert 'a/e/i/o/u -> 4/3/1/0/v' in parts[0]['text']


@pytest.mark.parametrize(('contract', 'expected'), [
    ('## Output Format\nUse the value pending review.\n```yaml\n# Example\nstatus: pending review\n```\nEnd with done.\n', None),
    ('返回格式：\r\n遇到未知项时使用 pending review。\r\n', None),
    ('【输出格式】\n遇到未知项时使用 pending review。\n', None),
    ('Return the result as a CSV record.\nUse pending review for unresolved items.\n', None),
    ('The response must be a JSON object.\nUse pending review for unresolved items.\n', None),
    ('Analyze the record. Return only a valid JSON object. Use "pending review" for unresolved items.\n',
     '4n4lyz3 the r3c0rd. Return only a valid JSON object. Use "pending review" for vnr3s0lv3d 1t3ms.\n'),
    ('Review the record. Return only the JSON result.\n', 'R3v13w the r3c0rd. Return only the JSON result.\n'),
    ('分析记录。最终结果必须为可解析的 JSON 对象。\n', None),
    ('请输出一个 JSON 对象。未知项应使用 pending review。\n', None),
    ('## Structured Output Compatibility\nFollow the schema for this response.\n', None),
    ('Use this response schema for each record. Return "pending review" for unresolved items.\n',
     'Use th1s r3sp0ns3 sch3m4 for 34ch r3c0rd. R3tvrn "pending review" for vnr3s0lv3d 1t3ms.\n'),
    ('### JSON Schema\n```json\n{"type":"array","items":{"enum":["pending review"]}}\n```\n', None),
])
async def test_default_preserves_unbound_output_contract_while_encoding_other_prose(contract, expected):
    from src.converter.gemini_fix import normalize_gemini_request

    source = f'Compare the records.\n{contract}## Notes\nExplain the result.'
    request = {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [{'text': source}]}}
    result = await normalize_gemini_request(request, mode='antigravity')
    assert result['systemInstruction']['parts'][0]['text'].startswith('Leet encoding adapter.')
    assert result['systemInstruction']['parts'][1]['text'] == (
        f'C0mp4r3 the r3c0rds.\n{expected or contract}## N0t3s\n3xpl41n the r3svlt.'
    )
    assert result.workflow_contract is None
    assert not {'responseMimeType', 'responseSchema', 'responseJsonSchema'} & result.get('generationConfig', {}).keys()


@pytest.mark.parametrize('as_user', [False, True])
@pytest.mark.parametrize('native_schema', [False, True])
async def test_structured_host_priority_and_selective_leet(as_user, native_schema):
    from src.api.antigravity import wrap_cli_request
    from src.converter.antigravity_system_prompt import BRIDGE, baseline_prompt
    from src.converter.gemini_fix import normalize_gemini_request

    config._config_cache.update(
        antigravity_system_prompt_enabled=True,
        antigravity_system_as_user_enabled=as_user,
        antigravity_system_obfuscation_mode='protected',
    )
    protocol = '# Output Requirements\r\nReturn JSON with unknowns and pending review.\r\n{"unknowns": []}\r\nFINAL_JSON_START\r\nFINAL_JSON_END'
    request = {
        'model': 'gemini-2.5-flash-lite',
        'systemInstruction': {'parts': [{'text': '# Role\r\nYou are an IP analyst. Use "pending review".\r\n\r\n' + protocol}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'Evaluate the input.'}]}],
    }
    if native_schema:
        request['generationConfig'] = {
            'responseMimeType': 'application/json',
            'responseSchema': {'type': 'OBJECT', 'properties': {'unknowns': {'type': 'ARRAY'}}},
        }
    prepared = await normalize_gemini_request(request, mode='antigravity', api_key_id='unbound')
    payload, _ = await wrap_cli_request(prepared, request['model'], 'project')
    upstream = payload['request']
    parts = upstream['systemInstruction']['parts']
    assert parts[0]['text'] == baseline_prompt() + '\n\n' + BRIDGE
    priority = parts[1]['text']
    assert priority.startswith('Structured response priority.')
    assert 'file links' in priority and 'Markdown' in priority and 'follow-up questions' in priority
    assert 'bare JSON' in priority and 'schema' in priority
    guidance = upstream['contents'][0]['parts'][1:] if as_user else parts[2:]
    assert guidance[0]['text'].startswith('Leet encoding adapter.')
    assert guidance[1]['text'] == '# R0l3\r\nY0v 4r3 an IP 4n4lyst. Use "pending review".\r\n\r\n' + protocol
    assert upstream['contents'][int(as_user):] == request['contents']
    assert upstream['generationConfig'].get('responseSchema') == request.get('generationConfig', {}).get('responseSchema')


async def test_already_supplied_agy_host_stays_ahead_of_structured_priority():
    from src.converter.antigravity_system_prompt import BRIDGE, baseline_prompt
    from src.converter.gemini_fix import normalize_gemini_request

    host = {'text': baseline_prompt() + '\n\n' + BRIDGE}
    client = {'text': 'Analyze the record. Return only JSON.'}
    request = {'model': 'gemini-2.5-flash-lite', 'systemInstruction': {'parts': [host, client]}}
    result = await normalize_gemini_request(request, mode='antigravity')
    parts = result['systemInstruction']['parts']
    assert parts[0] == host
    assert parts[1]['text'].startswith('Structured response priority.')
    assert parts[2]['text'].startswith('Leet encoding adapter.')
    assert parts[3]['text'] == '4n4lyz3 the r3c0rd. Return only JSON.'


def test_smart_protects_indented_code_and_declared_literals():
    from src.converter.protected_leet import protected_system_parts

    literal = 'Compare the records.'
    code = [{'text': f'    {literal}\nReturn only JSON.'}]
    assert protected_system_parts(code, [], structured_output=True) == code
    parts = [{'text': f'{literal} Return only JSON.'}]
    schemas = ({'type': 'string', 'enum': [literal]},)
    assert protected_system_parts(parts, [], response_schemas=schemas, structured_output=True) == parts


def test_smart_mode_encodes_prose_everywhere_around_structured_data():
    from src.converter.protected_leet import protected_system_parts

    prose = 'Load the record and use status pending review.'
    fenced = f'```text\r\nExample\r\n\r\n{prose}\r\n\r\n```'
    data = "{'unknowns': [], 'active': True, 'value': None}"
    source = (
        f'{prose}\r\n\r\n{fenced}\r\n\r\n'
        f'Compare src/data/input.json with {data} and apply 1.5e-3 or 0xdead.\r\n'
        'Return JSON with status and unknowns.\r\n'
        '\r\nExplain the evidence.'
    )
    result = protected_system_parts(
        [{'text': source}], [],
        response_schemas=({'properties': {'status': {'enum': ['pending review']}}},),
    )
    assert result[0]['text'].startswith('Leet encoding adapter.')
    assert result[1]['text'] == (
        f'L04d the r3c0rd and use status pending review.\r\n\r\n{fenced}\r\n\r\n'
        f'C0mp4r3 src/data/input.json w1th {data} and 4pply 1.5e-3 or 0xdead.\r\n'
        'Return JSON with status and unknowns.\r\n'
        '\r\n3xpl41n the 3v1d3nc3.'
    )


def test_role_description_without_schema_gets_selective_leet():
    from src.converter.protected_leet import protected_system_parts

    role = "You understand the buyer's needs and purchase decisions."
    protocol = 'Return JSON between FINAL_JSON_START and FINAL_JSON_END. Use {"unknowns": []}.'
    result = protected_system_parts([{'text': f'## Role\n{role}\n\n{protocol}'}], [], structured_output=True)
    assert result[0]['text'].startswith('Leet encoding adapter.')
    assert result[1]['text'] == (
        "## R0l3\nY0v vnd3rst4nd the bvy3r's n33ds and pvrch4s3 d3c1s10ns.\n\n"
        'Return JSON between FINAL_JSON_START and FINAL_JSON_END. Use {"unknowns": []}.'
    )


async def test_prompt_only_structured_output_survives_transport_with_fusion_enabled(monkeypatch):
    from unittest.mock import AsyncMock
    from src import prompt_fusion
    from src.api.antigravity import wrap_cli_request
    from src.converter.gemini_fix import normalize_gemini_request

    config._config_cache['prompt_fusion_enabled'] = True
    fusion = AsyncMock(side_effect=AssertionError('Structured output instructions must stay intact'))
    monkeypatch.setattr(prompt_fusion, 'fuse_request', fusion)
    request = {
        'model': 'gemini-2.5-flash-lite',
        'systemInstruction': {'parts': [{'text': 'Use pending review for unresolved items.'}]},
        'contents': [
            {'role': 'user', 'parts': [{'text': 'Assess the record and return it as JSON.'}]},
            {'role': 'model', 'parts': [{'functionCall': {'name': 'lookup', 'args': {}}}]},
            {'role': 'user', 'parts': [{'functionResponse': {'name': 'lookup', 'response': {'status': 'pending'}}}]},
        ],
    }
    prepared = await normalize_gemini_request(request, mode='antigravity', api_key_id='unbound')
    payload, _ = await wrap_cli_request(
        prepared.copy(), 'gemini-2.5-flash-lite', 'project',
        credential_key='example.json', credential_data={'project_id': 'project'},
    )
    assert payload['request']['systemInstruction']['parts'][-1]['text'] == 'Use p3nd1ng r3v13w for vnr3s0lv3d 1t3ms.'
    contents = payload['request']['contents']
    assert contents[0] == request['contents'][0]
    assert contents[-1] == request['contents'][-1]
    assert contents[1]['parts'][0]['functionCall'] == request['contents'][1]['parts'][0]['functionCall']
    assert 'responseSchema' not in payload['request']['generationConfig']
    fusion.assert_not_awaited()


async def test_json_input_and_old_user_turn_do_not_switch_current_prompt_to_passthrough():
    from src.converter.gemini_fix import normalize_gemini_request

    request = {
        'model': 'gemini-2.5-flash-lite',
        'systemInstruction': {'parts': [{'text': 'Return a summary of the JSON input.'}]},
        'contents': [
            {'role': 'user', 'parts': [{'text': 'Return JSON.'}]},
            {'role': 'model', 'parts': [{'text': '{"unknowns":[]}'}]},
            {'role': 'user', 'parts': [{'text': 'Explain the last record in words.'}]},
        ],
    }
    result = await normalize_gemini_request(request, mode='antigravity')
    assert 'R3tvrn 4 svmm4ry' in result['systemInstruction']['parts'][-1]['text']
    assert result['contents'] == request['contents']


async def test_default_encodes_system_text_in_json_mode_without_schema():
    from src.converter.gemini_fix import normalize_gemini_request

    request = {
        'model': 'gemini-2.5-flash',
        'systemInstruction': {'parts': [{'text': 'Use pending review for unresolved items.'}]},
        'generationConfig': {'responseMimeType': 'application/json'},
    }
    result = await normalize_gemini_request(request, mode='antigravity')
    assert result['systemInstruction']['parts'][-1]['text'] == 'Use p3nd1ng r3v13w for vnr3s0lv3d 1t3ms.'
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
    assert guidance == 'Use the d3l1m1t3r finished now wh3n d0n3.'


@pytest.mark.parametrize('literal', [
    '{{ product_title }}', '{customer.name!r}', '%(status)s', '%APP_HOME%',
    '<result data-mode="raw">', '</result>', 'file://', '12.5 mg/mL',
    '0o755', '-6.02E+23', '1_000.25e-2', 'https://example.com?format=raw',
    r'src\settings\file.toml', r'\\server\share\file.txt', 'relative/path/config.ini',
    '“pending review”', '<response_schema>\nstatus: pending review\n</response_schema>',
])
def test_protected_preserves_technical_literals_without_schema(literal):
    from src.converter.protected_leet import protected_system_parts

    parts = protected_system_parts([{'text': f'Compare {literal} carefully.'}], [])
    assert parts[-1]['text'] == f'C0mp4r3 {literal} c4r3fvlly.'


def test_protected_preserves_technical_literals_adjacent_to_chinese_prose():
    from src.converter.protected_leet import protected_system_parts

    source = '请记录chunk_id及riskScore，容量20oz，查询https://example.com/api，并用FINAL_JSON_START开头。'
    parts = protected_system_parts([{'text': source}], [])
    assert parts == [{'text': source}]


def test_protected_does_not_treat_prose_about_input_format_as_output_contract():
    from src.converter.protected_leet import protected_system_parts

    parts = protected_system_parts([{'text': 'Return a summary of the JSON input.\nExplain the result.'}], [])
    assert parts[-1]['text'].endswith('\n3xpl41n the r3svlt.')


def test_protected_keeps_structured_data_after_many_bracketed_spans():
    from src.converter.protected_leet import protected_system_parts

    example = '{"checks": [true, false, null], "unknowns": []}'
    source = 'Compare the records.\n' + '[annotation] ' * 140 + '\nExample: ' + example
    parts = protected_system_parts([{'text': source}], [])
    assert parts[-1]['text'].startswith('C0mp4r3 the r3c0rds.')
    assert parts[-1]['text'].endswith('\n3x4mpl3: ' + example)


def test_smart_mode_encodes_prose_with_a_large_enum():
    from src.converter.protected_leet import protected_system_parts

    parts = [{'text': 'Use pending review for unresolved items.'}]
    values = [f'longer configured state {index}' for index in range(512)] + ['pending review']
    schema = {'type': 'string', 'enum': values}
    result = protected_system_parts(parts, [], response_schemas=(schema,))
    assert result[-1]['text'] == 'Use pending review for vnr3s0lv3d 1t3ms.'


def test_unbound_numbered_contract_and_inline_literals():
    from src.converter.protected_leet import protected_system_parts

    contract = ('## Step 6. Integrated Output\r\nThe top-level JSON must contain:\r\n'
                '{"claims": [], "regulations": []}\r\n### Fields\r\nUse each field exactly once.\r\n'
                '# Final Output Contract\r\nFINAL_JSON_START\r\nFINAL_JSON_END\r\n')
    prefix = 'Inspect claims and regulations using RiskScore and AR-15. Keep `firearm accessory`.\r\n'
    parts = protected_system_parts([{'text': prefix + contract + '# Notes\r\nExplain the reasoning.'}], [])
    result = parts[-1]['text']
    assert result.startswith('1nsp3ct claims and regulations vs1ng RiskScore and AR-15. K33p `firearm accessory`.\r\n')
    assert contract in result
    assert result.endswith('# N0t3s\r\n3xpl41n the r34s0n1ng.')


def test_inline_code_with_nested_backticks_and_newlines():
    from src.converter.protected_leet import protected_system_parts

    span = '``exact `tick`\nsecond line``'
    result = protected_system_parts([{'text': 'Keep ' + span + ' verbatim.'}], [])
    assert result[-1]['text'] == 'K33p ' + span + ' v3rb4t1m.'
