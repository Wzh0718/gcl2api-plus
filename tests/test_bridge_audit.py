"""日志详情引导词审计（bridge_audit）的回归用例。"""
import sqlite3

import pytest

import config
from src.bridge_audit import (
    audit_details,
    detect_baseline_version,
    extract_system_text,
    render_table,
    summarize,
)
from src.converter.antigravity_system_prompt import (
    BRIDGE,
    STRUCTURED_OUTPUT_PRIORITY,
    baseline_prompt,
)
from src.storage.call_record_content import encode_details


def _details(system_text=None, tools=None, response=None):
    upstream = {'messages': [{'role': 'user', 'parts': [{'text': 'hi'}]}], 'tools': tools or []}
    if system_text is not None:
        upstream['system'] = {'systemInstruction': {'parts': [{'text': system_text}]}}
    if response is not None:
        upstream['response'] = response
    return {'upstream': upstream}


def _new_system(tail=''):
    return baseline_prompt('2.35.2') + '\n\n' + BRIDGE + tail


async def test_new_bridge_at_tail_is_clean():
    audit = audit_details(_details(_new_system()))
    assert audit['baseline'] == '2.35.x'
    assert audit['bridge_new'] == 1 and audit['bridge_legacy'] == 0
    assert audit['problems'] == []


async def test_structured_output_trailer_is_allowed():
    audit = audit_details(_details(_new_system('\n\n' + STRUCTURED_OUTPUT_PRIORITY)))
    assert audit['problems'] == []


async def test_legacy_bridge_is_flagged_not_silently_accepted():
    legacy = baseline_prompt('2.31.2') + '\n\nExecution environment adapter: old text.'
    audit = audit_details(_details(legacy))
    assert audit['baseline'] == '<=2.31.x'
    assert 'bridge_legacy' in audit['problems']


async def test_missing_system_and_missing_bridge_are_distinct():
    assert 'system_absent' in audit_details(_details(None))['problems']
    # 图片模型原生信封不带 system，不应判失败。
    image = audit_details(_details(None), model='gemini-3.1-flash-image')
    assert image['problems'] == []
    assert 'system_not_expected' in image['notes']
    assert 'bridge_missing' in audit_details(_details(baseline_prompt('2.35.2')))['problems']


async def test_bridge_must_be_unique_and_terminal():
    duplicated = _new_system('\n\n' + BRIDGE)
    assert 'bridge_duplicated' in audit_details(_details(duplicated))['problems']

    trailing = _new_system('\n\nunrelated trailing text')
    assert 'bridge_not_at_tail' in audit_details(_details(trailing))['problems']


async def test_runtime_block_leak_is_flagged():
    leaked = _new_system('\n\n<user_information>\nConversation ID: x\n</user_information>')
    assert 'runtime_block_leaked' in audit_details(_details(leaked))['problems']


async def test_response_calling_undeclared_tool_is_flagged():
    response = {'candidates': [{'content': {'parts': [
        {'functionCall': {'name': 'run_command', 'args': {'CommandLine': 'id'}}},
    ]}}]}
    details = _details(_new_system(), tools=[{'functionDeclarations': [{'name': 'read_file'}]}],
                       response=response)
    audit = audit_details(details)
    assert 'undeclared_tool_call' in audit['problems']
    assert audit['tools'] == ['read_file']


async def test_response_notes_paths_and_execution_claims_without_failing():
    response = {'candidates': [{'content': {'parts': [
        {'text': 'I ran the build; output is in /home/user/project/dist/app.js'},
    ]}}]}
    audit = audit_details(_details(_new_system(), response=response))
    assert audit['problems'] == []
    assert any(note.startswith('possible_invented_path') for note in audit['notes'])
    assert 'execution_claim' in audit['notes']


def test_detect_baseline_version():
    assert detect_baseline_version('') == 'absent'
    assert detect_baseline_version('You are Antigravity, ...') == '<=2.31.x'
    assert detect_baseline_version(baseline_prompt('2.35.2')) == '2.35.x'
    assert detect_baseline_version('random text') == 'unknown'


def test_summarize_and_render_over_records():
    reports = [
        {'id': 1, 'model': 'm1', 'audit': audit_details(_details(_new_system()))},
        {'id': 2, 'model': 'm2', 'audit': audit_details(_details(None))},
    ]
    summary = summarize(reports)
    assert summary['records'] == 2 and summary['ok'] == 1 and summary['system_absent'] == 1
    table = render_table(reports)
    assert table.splitlines()[0].startswith('id\tmodel')
    assert 'm1' in table and 'm2' in table


async def test_replay_of_structured_call_record_stays_clean(tmp_path):
    """结构化输出请求重放后仍能通过审计（回归：尾部允许结构化优先级声明）。"""
    from src.converter.gemini_fix import normalize_gemini_request

    config._config_initialized = True
    config._config_cache = {}
    request = {
        'model': 'gemini-2.5-flash-lite',
        'contents': [{'role': 'user', 'parts': [{'text': 'return json'}]}],
        'tools': [],
        'generationConfig': {
            'responseMimeType': 'application/json',
            'responseSchema': {'type': 'object', 'properties': {'a': {'type': 'string'}}},
        },
    }
    normalized = await normalize_gemini_request(request, mode='antigravity')
    audit = audit_details({'upstream': {
        'system': normalized.get('systemInstruction'),
        'tools': normalized.get('tools'),
    }})
    assert audit['baseline'] == '2.35.x'
    assert audit['bridge_new'] == 1
    assert audit['problems'] == []


def test_encoded_details_roundtrip_through_sqlite(tmp_path):
    db_path = tmp_path / 'credentials.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE call_records (id INTEGER PRIMARY KEY, channel TEXT, model_name TEXT, request_details BLOB)')
        db.execute('INSERT INTO call_records (channel, model_name, request_details) VALUES (?,?,?)',
                   ('antigravity', 'gemini-test', encode_details(_details(_new_system()))))
    with sqlite3.connect(db_path) as db:
        stored = db.execute('SELECT request_details FROM call_records').fetchone()[0]
    from src.storage.call_record_content import decode_details

    audit = audit_details(decode_details(stored))
    assert audit['problems'] == []
    assert extract_system_text(_details(_new_system())) == _new_system()
