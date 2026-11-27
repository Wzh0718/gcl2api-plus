"""工具声明形态对齐（官方 2.35.2 顺序 + protojson 大小写）。"""
import pytest

import config
from src.converter.tool_alignment import (
    OFFICIAL_TOOL_ORDER_2_35_2,
    align_tool_declarations,
)

pytestmark = pytest.mark.asyncio


def _tools():
    return [
        {'functionDeclarations': [
            {'name': 'ask_question', 'parameters': {
                'type': 'object',
                'properties': {'questions': {'type': 'array', 'minItems': 1, 'items': {
                    'type': 'object', 'properties': {'title': {'type': 'string', 'minLength': 1}}}}},
                'required': ['questions']}},
            {'name': 'read_file', 'parameters': {
                'type': 'object',
                'properties': {'Path': {'type': 'string', 'minLength': 1}},
                'required': ['Path']}},
        ]},
        {'functionDeclarations': [
            {'name': 'write_file', 'parameters': {'type': 'object', 'properties': {}}},
            {'name': 'custom_tool', 'parameters': {'type': 'object', 'properties': {}}},
        ]},
    ]


async def test_alignment_merges_orders_and_normalizes():
    aligned = align_tool_declarations(_tools())
    assert len(aligned) == 1
    declarations = aligned[0]['functionDeclarations']
    names = [item['name'] for item in declarations]
    assert names == ['read_file', 'write_file', 'ask_question', 'custom_tool']
    assert [n for n in names if n in OFFICIAL_TOOL_ORDER_2_35_2] == [
        'read_file', 'write_file', 'ask_question',
    ]

    read_file = declarations[0]['parameters']
    assert read_file['type'] == 'OBJECT'
    assert read_file['properties']['Path']['type'] == 'STRING'
    assert read_file['properties']['Path']['minLength'] == '1'

    ask = declarations[2]['parameters']
    assert ask['properties']['questions']['type'] == 'ARRAY'
    assert ask['properties']['questions']['minItems'] == '1'
    assert ask['properties']['questions']['items']['type'] == 'OBJECT'
    assert ask['properties']['questions']['items']['properties']['title']['minLength'] == '1'


async def test_alignment_is_idempotent_and_does_not_mutate_input():
    source = _tools()
    once = align_tool_declarations(source)
    twice = align_tool_declarations(once)
    assert once == twice
    assert source[0]['functionDeclarations'][0]['parameters']['type'] == 'object'


async def test_non_function_entries_are_preserved():
    tools = [{'google_search': {}}, {'functionDeclarations': [
        {'name': 'read_file', 'parameters': {'type': 'object', 'properties': {}}},
    ]}]
    aligned = align_tool_declarations(tools)
    assert aligned[0] == {'google_search': {}}
    assert aligned[1]['functionDeclarations'][0]['name'] == 'read_file'
    assert aligned[1]['functionDeclarations'][0]['parameters']['type'] == 'OBJECT'


async def test_empty_or_unknown_shapes_pass_through():
    assert align_tool_declarations([]) == []
    assert align_tool_declarations(None) is None
    weird = [{'notDeclarations': []}]
    assert align_tool_declarations(weird) == weird


async def test_wrap_cli_request_applies_alignment_only_when_enabled(monkeypatch):
    from types import SimpleNamespace

    from src.api import antigravity as ag

    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {})
    monkeypatch.setenv('ANTIGRAVITY_OFFICIAL_TOOL_ALIGNMENT', 'true')
    assert await config.get_antigravity_official_tool_alignment_enabled() is True

    from src import claude_routing

    monkeypatch.setattr(claude_routing, 'enforce_key_model', lambda model: None)
    monkeypatch.setattr(ag, 'is_antigravity_image_request', lambda model, request: False)
    monkeypatch.setattr(ag, 'bind_capture_credential', lambda *a, **k: None)

    async def fake_state(inner, model, key):
        return SimpleNamespace(
            session_id='1', conversation_id='c', trajectory_id='t', step_index=0,
            allocation_backend=None, allocation_key_ref=None,
        )

    monkeypatch.setattr(ag, '_get_session_state', fake_state)

    from src import antigravity_model_catalog as catalog

    monkeypatch.setattr(catalog, 'get_model_enum', lambda model: 'MODEL_X')
    monkeypatch.setattr(catalog, 'get_model_info', lambda model: {})

    payload, _ = await ag.wrap_cli_request(
        {'model': 'gemini-3.8-flash-high', 'tools': _tools(), 'contents': []},
        'gemini-3.8-flash-high', 'proj',
        credential_key='acct', credential_data={'refresh_token': 'x'},
        skip_prompt_fusion=True,
    )
    tools = payload['request']['tools']
    assert len(tools) == 1
    assert [d['name'] for d in tools[0]['functionDeclarations']][:2] == ['read_file', 'write_file']
    assert tools[0]['functionDeclarations'][0]['parameters']['type'] == 'OBJECT'


def _decls(*names):
    return [{'functionDeclarations': [
        {'name': name, 'parameters': {'type': 'object', 'properties': {}}} for name in names
    ]}]


async def test_dsh_tools_use_their_canonical_order():
    aligned = align_tool_declarations(_decls('grep', 'bash', 'read', 'write'))
    assert [d['name'] for d in aligned[0]['functionDeclarations']] == ['bash', 'read', 'write', 'grep']


async def test_claude_code_tools_use_their_canonical_order():
    aligned = align_tool_declarations(_decls('Bash', 'Read', 'Task', 'Edit'))
    assert [d['name'] for d in aligned[0]['functionDeclarations']] == ['Task', 'Read', 'Edit', 'Bash']


async def test_codex_tools_use_their_canonical_order():
    aligned = align_tool_declarations(_decls('apply_patch', 'shell', 'update_plan'))
    names = [d['name'] for d in aligned[0]['functionDeclarations']]
    assert names == ['shell', 'apply_patch', 'update_plan']


async def test_unknown_toolset_keeps_declaration_order():
    aligned = align_tool_declarations(_decls('zeta_tool', 'alpha_tool', 'middle_tool'))
    assert [d['name'] for d in aligned[0]['functionDeclarations']] == ['zeta_tool', 'alpha_tool', 'middle_tool']


async def test_explicit_client_overrides_detection():
    aligned = align_tool_declarations(_decls('grep', 'bash', 'read'), client='agy')
    names = [d['name'] for d in aligned[0]['functionDeclarations']]
    assert names == ['grep', 'bash', 'read']  # 都不是 agy 官方名，保持原顺序


async def test_alignment_is_enabled_by_default(monkeypatch):
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {})
    monkeypatch.delenv('ANTIGRAVITY_OFFICIAL_TOOL_ALIGNMENT', raising=False)
    assert await config.get_antigravity_official_tool_alignment_enabled() is True
