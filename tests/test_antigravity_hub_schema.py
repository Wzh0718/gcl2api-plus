"""Decode with field descriptors recovered from Hub, independent of encoder tables."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.antigravity_telemetry import encode_log_request
from src.antigravity_telemetry_schema import MESSAGES, encode_message, hub_generation_event

SCHEMA = json.loads((Path(__file__).parent / 'fixtures/hub_2_31_2_generation_schema.json').read_text())


def varint(data, pos):
    value, shift = 0, 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
        shift += 7


def decode(name, data):
    fields = {f['number']: f for f in SCHEMA[name]}
    result, pos = {}, 0
    while pos < len(data):
        tag, pos = varint(data, pos)
        field = fields[tag >> 3]  # reject unknown field numbers
        wire = tag & 7
        if wire == 0:
            value, pos = varint(data, pos)
        else:
            assert wire == 2
            size, pos = varint(data, pos)
            raw = data[pos:pos+size]
            pos += size
            if name == 'LogEvent' and field['name'] == 'source_extension':
                assert field['type'] == 12  # bytes containing the source-specific extension
                value = decode('JetskiTelemetryExtension', raw)
            elif field['type'] == 11:
                value = decode(field['type_name'].split('.')[-1], raw)
            elif field['type'] == 9:
                value = raw.decode()
            else:
                value, offset = [], 0
                while offset < len(raw):
                    integer, offset = varint(raw, offset)
                    value.append(integer)
        if field['name'] == 'log_event':
            result.setdefault('log_event', []).append(value)
        else:
            result[field['name']] = value
    return result


def snapshot():
    return SimpleNamespace(user_agent='antigravity/hub/2.31.2 (aidev_client; os_type=linux; arch=amd64; cl=1019359392)')


def test_wire_fields_agree_with_recovered_hub_descriptors():
    for name, fields in MESSAGES.items():
        recovered = {f['name']: f for f in SCHEMA[name]}
        for field, (number, kind) in fields.items():
            actual = recovered[field]
            assert actual['number'] == number
            if kind == 'str':
                assert actual['type'] == 9
            elif kind == 'int':
                assert actual['type'] in (3, 4, 5, 8, 13, 14)
            elif kind == 'packed':
                assert actual['type'] in (3, 4, 5, 13)
            elif name == 'LogEvent' and field == 'source_extension':
                assert actual['type'] == 12
            elif kind not in ('int', 'packed'):
                assert actual['type_name'].split('.')[-1] == kind.removesuffix('[]')


def test_generation_uses_hub_payload_and_excludes_local_or_injected_fields():
    event = {'implementation': 'gcli2api', 'event_name': 'gateway_generation_finished',
             'event_id': 'LOCAL-ONLY-ID', 'session_id': 'LOCAL-ONLY-SESSION',
             'installation_id': 'LOCAL-ONLY-INSTALL', 'request_id': 'LOCAL-ONLY-REQUEST',
             'model_id': 'gemini-test', 'response_model': 'gemini-test-version',
             'response_id': 'actual-response', 'cascade_id': 'cascade-a', 'trajectory_id': 'trajectory-a',
             'last_step_index': '7', 'promptTokenCount': 21, 'candidatesTokenCount': 3,
             'thoughtsTokenCount': 4, 'cachedContentTokenCount': 2,
             'prompt': 'PRIVATE PROMPT', '_delivery': {'enqueued_at': 1234, 'send_ms': 42},
             '_wire': {'malicious': 'INJECTED'}}
    body = encode_log_request(snapshot(), event)
    for forbidden in (b'gcli2api', b'implementation', b'gateway_generation_finished', b'LOCAL-ONLY',
                      b'PRIVATE', b'INJECTED', b'_delivery', b'send_ms'):
        assert forbidden not in body
    decoded = decode('LogRequest', body)
    assert decoded['client_info'] == {'client_type': 16}
    assert decoded['log_source'] == 2747
    event = decoded['log_event'][0]
    assert event['event_time_ms'] == 1234000
    extension = event['source_extension']
    assert {k: extension[k] for k in ('ide_name','ide_version','os','arch','product_name','is_redacted')} == {
        'ide_name':'antigravity','ide_version':'2.31.2','os':'linux','arch':'amd64','product_name':'hub','is_redacted':1}
    assert 'analytics_event_metadata' not in extension
    generator = extension['cortex_generator_metadata']
    assert generator['cascade_id'] == 'cascade-a' and generator['trajectory_id'] == 'trajectory-a'
    assert generator['plan_model'] == 'gemini-test'
    step = generator['generator_metadata']
    assert step['step_indices'] == [7]
    assert step['chat_model']['response_model'] == 'gemini-test-version'
    assert step['chat_model']['usage'] == {'input_tokens':21,'output_tokens':7,
        'response_output_tokens':3,'thinking_output_tokens':4,'cache_read_tokens':2,'response_id':'actual-response'}


def test_unknown_model_enums_and_hub_execution_state_are_not_fabricated():
    body = encode_log_request(snapshot(), {'model_id': 'unmapped-model', 'promptTokenCount': -1,
                                         'last_step_index': '-1'})
    generator = decode('LogRequest', body)['log_event'][0]['source_extension']['cortex_generator_metadata']
    assert generator == {'plan_model':'unmapped-model','generator_metadata':{'chat_model':{}}}
    with pytest.raises(KeyError):
        encode_message('JetskiTelemetryExtension', {'implementation':'gcli2api'})


def test_preview_structure_matches_wire_and_each_batched_account_event():
    events = [{'cascade_id':f'c-{i}', 'trajectory_id':f't-{i}'} for i in range(3)]
    decoded = decode('LogRequest', encode_log_request(snapshot(), events))
    assert len(decoded['log_event']) == 3
    for source, sent in zip(events, decoded['log_event']):
        assert sent['source_extension'] == hub_generation_event(snapshot(), source)['source_extension']
