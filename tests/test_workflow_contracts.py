import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

import config
from src.converter.gemini_fix import normalize_gemini_request
from src.workflow_contracts import get_workflow_contract, validate_workflow_response


_REGULATION_MARKDOWN = '## Risk analysis\nSmall magnets may be detachable.'
_REGULATION_RESULT = {
    'embedding_query': 'magnetic construction toy | small detachable magnets',
    'rerank_query': 'Review ingestion risks of small detachable magnets in a children’s construction toy.',
    'policy_axes': ['toys_children', 'magnet_hazard', 'ingestion_risk'],
}
_REGULATION_WIRE = json.dumps({'analysis_markdown': _REGULATION_MARKDOWN, 'result': _REGULATION_RESULT}, ensure_ascii=False)
_REGULATION_TEXT = (_REGULATION_MARKDOWN + '\n\nFINAL_JSON_START\n'
                    + json.dumps(_REGULATION_RESULT, ensure_ascii=False, separators=(',', ':')) + '\nFINAL_JSON_END')


@pytest.fixture
def workflow_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {})
    for key in ('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'ANTIGRAVITY_SYSTEM_AS_USER_ENABLED',
                'ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE', 'ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED',
                'SYSTEM_PROMPT_APPEND'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('SENSITIVE_WORD_REPLACE_ENABLED', 'false')
    path = tmp_path / 'contracts.json'
    risk_schema = {'type': 'object', 'required': ['product_risk_score', 'unknowns'], 'properties': {
        'product_risk_score': {'type': 'integer', 'minimum': 0, 'maximum': 10},
        'unknowns': {'type': 'array', 'items': {'type': 'string'}},
    }}
    regulation_path = Path(__file__).parents[1] / 'src/prompts/workflow-contracts/regulation-search-v1.json'
    workflows = json.loads(regulation_path.read_text(encoding='utf-8'))
    workflows['contracts']['risk-v1'] = {'response_schema': risk_schema, 'prose_paragraphs': ['Evaluate the product.']}
    workflows['bindings'] = {'risk-key': 'risk-v1', 'search-key': 'regulation-search-v1'}
    path.write_text(json.dumps(workflows), encoding='utf-8')
    monkeypatch.setenv('ANTIGRAVITY_WORKFLOW_CONTRACTS_FILE', str(path))


def response(text, finish='STOP'):
    return JSONResponse({'response': {'candidates': [{
        'finishReason': finish, 'content': {'role': 'model', 'parts': [{'text': text}]},
    }]}})


async def test_bound_workflow_encodes_prose_by_format_with_legacy_config(workflow_settings):
    prose = 'Evaluate the product.'
    protocol = 'Use pending review then FINAL_JSON_END.\nLiteral fields must survive: unknowns.'
    request = {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [{'text': f'{prose}\r\n\r\n{protocol}'}]},
               'generationConfig': {'responseJsonSchema': {'type': 'string'}}}
    result = await normalize_gemini_request(request, mode='antigravity', api_key_id='risk-key')
    assert result['systemInstruction']['parts'][1]['text'] == (
        '3v4lv4t3 the pr0dvct.\r\n\r\n'
        'Use p3nd1ng r3v13w th3n FINAL_JSON_END.\nL1t3r4l f13lds mvst svrv1v3: unknowns.'
    )
    assert result['generationConfig']['responseMimeType'] == 'application/json'
    assert result['generationConfig']['responseSchema']['required'] == ['product_risk_score', 'unknowns']
    assert 'responseJsonSchema' not in result['generationConfig']
    assert 'workflow_contract' not in result
    assert request['systemInstruction']['parts'][0]['text'].startswith(prose)


async def test_host_priority_keeps_bound_regulation_output_contract(workflow_settings):
    from src.converter.antigravity_system_prompt import BRIDGE, STRUCTURED_OUTPUT_PRIORITY, baseline_prompt

    config._config_cache['antigravity_system_prompt_enabled'] = True
    contract_text = 'Keep FINAL_JSON_START and FINAL_JSON_END exact. Use {"policy_axes": []}.'
    request = {
        'model': 'gemini-2.5-flash',
        'systemInstruction': {'parts': [{'text': f'Analyze the product.\n{contract_text}'}]},
    }
    result = await normalize_gemini_request(request, mode='antigravity', api_key_id='search-key')
    host, priority, decoder, client, guidance = [
        part['text'] for part in result['systemInstruction']['parts']
    ]
    assert host == baseline_prompt() + '\n\n' + BRIDGE
    assert priority == STRUCTURED_OUTPUT_PRIORITY
    assert decoder.startswith('Leet encoding adapter.')
    assert client == '4n4lyz3 the pr0dvct.\nK33p FINAL_JSON_START and FINAL_JSON_END 3x4ct. Use {"policy_axes": []}.'
    assert guidance == get_workflow_contract('search-key').json_guidance
    schema = result['generationConfig']['responseSchema']
    assert schema['required'] == ['analysis_markdown', 'result']


@pytest.mark.parametrize('mode', ['full', 'protected'])
async def test_unbound_passes_through_native_schema_while_encoding_prose(workflow_settings, monkeypatch, mode):
    monkeypatch.setenv('ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE', mode)
    request = {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [{'text': 'Use pending review.'}]},
               'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': {'type': 'STRING'}}}
    result = await normalize_gemini_request(request, mode='antigravity', api_key_id='unbound-key')
    assert result['systemInstruction']['parts'][-1]['text'] == 'Use p3nd1ng r3v13w.'
    assert result['generationConfig']['responseSchema'] == request['generationConfig']['responseSchema']


@pytest.mark.parametrize('raw', [
    pytest.param('{"product_risk_score":8,"unknowns":/[]/}', id='8654044'),
    pytest.param('{"product_risk_score":0,"unknowns":[href="file://"]}', id='8765699'),
    pytest.param('{"product_risk_score":"8","unknowns":[]}', id='schema-mismatch'),
])
def test_risk_regression_rejects_bad_model_output(workflow_settings, raw):
    result = validate_workflow_response(response(raw), get_workflow_contract('risk-key'))
    assert result.status_code == 502
    assert json.loads(result.body)['error']['code'] == 'invalid_workflow_output'


def test_regulation_regression_uses_exact_server_markers(workflow_settings):
    contract = get_workflow_contract('search-key')
    result = validate_workflow_response(response(_REGULATION_WIRE), contract)
    assert result.status_code == 200
    assert json.loads(result.body)['response']['candidates'][0]['content']['parts'][0]['text'] == _REGULATION_TEXT
    wrong = f'F1N4L_JS0N_ST4RT\n{_REGULATION_WIRE}\nF1N4L_JS0N_3ND'
    assert validate_workflow_response(response(wrong), contract).status_code == 502


@pytest.mark.parametrize('value', [
    {'analysis_markdown': 'Example FINAL_JSON_START', 'result': _REGULATION_RESULT},
    {'analysis_markdown': _REGULATION_MARKDOWN, 'result': {**_REGULATION_RESULT, 'rerank_query': 'FINAL_JSON_END'}},
    {'analysis_markdown': _REGULATION_MARKDOWN, 'result': {**_REGULATION_RESULT, 'policy_axes': ['only_one']}},
    _REGULATION_RESULT,
])
def test_regulation_rejects_ambiguous_markers_or_wrong_shape(workflow_settings, value):
    assert validate_workflow_response(response(json.dumps(value)), get_workflow_contract('search-key')).status_code == 502


def test_structured_output_rejects_truncation_even_when_json_parses(workflow_settings):
    raw = '{"product_risk_score":8,"unknowns":[]}'
    assert validate_workflow_response(response(raw, 'MAX_TOKENS'), get_workflow_contract('risk-key')).status_code == 502


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('key, product, raw, expected', [
    pytest.param('risk-key', '8654044 grass signs', '{"product_risk_score":8,"unknowns":/[]/}', None, id='8654044'),
    pytest.param('risk-key', '8765699 pet wedding dress', '{"product_risk_score":0,"unknowns":[href="file://"]}', None, id='8765699'),
    pytest.param('risk-key', '8654044 grass signs', '{"product_risk_score":8,"unknowns":[]}',
                 '{"product_risk_score":8,"unknowns":[]}', id='valid-risk'),
    pytest.param('search-key', 'children magnetic toys', _REGULATION_WIRE, _REGULATION_TEXT, id='regulation'),
])
async def test_workflows_across_openai_route_and_stream_collector(
    workflow_settings, monkeypatch, stream, key, product, raw, expected,
):
    from src.api import antigravity
    from src.claude_routing import ClaudeRoute
    from src.models import OpenAIChatCompletionRequest
    from src.router.antigravity.openai import chat_completions

    monkeypatch.setenv('ANTIGRAVITY_STREAM2NOSTREAM', 'true')

    async def load_route(api_key_id, model):
        return ClaudeRoute(api_key_id, None)

    async def fake_stream(*, body, api_key_id, **kwargs):
        assert api_key_id == key
        assert body['request']['generationConfig']['responseSchema']['type'] == 'OBJECT'
        assert 'workflow_contract' not in body['request']
        if key == 'search-key':
            schema = body['request']['generationConfig']['responseSchema']
            assert schema['required'] == ['analysis_markdown', 'result']
            assert schema['properties']['result']['required'] == ['embedding_query', 'rerank_query', 'policy_axes']
        split_at = len(raw) // 2
        for text, finish in ((raw[:split_at], None), (raw[split_at:], 'STOP')):
            frame = response(text, finish)
            yield b'data: ' + frame.body + b'\n\n'

    monkeypatch.setattr('src.claude_routing._load_route', load_route)
    monkeypatch.setattr(antigravity, '_stream_request_inner', fake_stream)
    request = OpenAIChatCompletionRequest.model_validate({
        'model': 'gemini-2.5-flash', 'stream': stream,
        'messages': [{'role': 'system', 'content': 'Evaluate the product.'},
                     {'role': 'user', 'content': product}],
    })
    result = await chat_completions(request, principal=SimpleNamespace(api_key_id=key))
    if expected is None:
        assert result.status_code == 502
        assert 'invalid_workflow_output' in result.body.decode()
    elif stream:
        chunks = []
        async for chunk in result.body_iterator:
            chunks.extend((chunk.decode() if isinstance(chunk, bytes) else chunk).splitlines())
        frames = [json.loads(line[6:]) for line in chunks if line.startswith('data: ') and line != 'data: [DONE]']
        assert ''.join(choice['delta'].get('content') or '' for frame in frames for choice in frame.get('choices', [])) == expected
    else:
        assert json.loads(result.body)['choices'][0]['message']['content'] == expected
