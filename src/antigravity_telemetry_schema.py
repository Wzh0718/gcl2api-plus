"""Observed Hub 2.31.2 generation schema; see docs/antigravity-hub-wire-schema.md.

Only declared fields can be encoded. Local event IDs, implementation labels,
HTTP outcomes and delivery diagnostics are not part of this wire format.
"""
import re

# (field number, scalar type or nested message name).
MESSAGES = {
    'ClientInfo': {'client_type': (1, 'int')},
    'LogRequest': {'client_info': (1, 'ClientInfo'), 'log_source': (2, 'int'),
                   'log_event': (3, 'LogEvent[]'), 'request_time_ms': (4, 'int')},
    'LogEvent': {'event_time_ms': (1, 'int'), 'source_extension': (6, 'JetskiTelemetryExtension')},
    'JetskiTelemetryExtension': {
        'cortex_generator_metadata': (3, 'CortexGeneratorMetadata'),
        'ide_name': (16, 'str'), 'ide_version': (17, 'str'), 'os': (18, 'str'),
        'arch': (19, 'str'), 'is_redacted': (24, 'int'), 'product_name': (27, 'str')},
    'CortexGeneratorMetadata': {'trajectory_id': (1, 'str'), 'cascade_id': (2, 'str'),
                               'generator_metadata': (3, 'CortexStepGeneratorMetadata'),
                               'plan_model': (12, 'str')},
    'CortexStepGeneratorMetadata': {'chat_model': (1, 'ChatModelMetadata'), 'step_indices': (2, 'packed')},
    'ChatModelMetadata': {'usage': (4, 'ModelUsageStats'), 'response_model': (19, 'str')},
    'ModelUsageStats': {'input_tokens': (2, 'int'), 'output_tokens': (3, 'int'),
                        'cache_read_tokens': (5, 'int'), 'thinking_output_tokens': (9, 'int'),
                        'response_output_tokens': (10, 'int'), 'response_id': (11, 'str')},
}


def _varint(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError('Invalid unsigned protobuf value')
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def encode_message(name, value):
    encoded = bytearray()
    for key, item in value.items():
        number, kind = MESSAGES[name][key]  # reject undeclared fields
        for entry in item if kind.endswith('[]') else [item]:
            item_kind = kind.removesuffix('[]')
            if item_kind == 'int':
                encoded += _varint(number << 3) + _varint(entry)
                continue
            raw = (entry.encode() if item_kind == 'str' else
                   b''.join(_varint(i) for i in entry) if item_kind == 'packed' else
                   encode_message(item_kind, entry))
            encoded += _varint((number << 3) | 2) + _varint(len(raw)) + raw
    return bytes(encoded)


def hub_generation_event(snapshot, event):
    """Map observed request facts; never fabricate Hub-only execution state."""
    def text(key):
        value = event.get(key)
        return value if isinstance(value, str) and 0 < len(value) <= 256 else None

    def count(key):
        value = event.get(key)
        return value if type(value) is int and 0 <= value < 2**53 else None

    usage = {}
    for local, wire in [('promptTokenCount', 'input_tokens'), ('cachedContentTokenCount', 'cache_read_tokens'),
                        ('thoughtsTokenCount', 'thinking_output_tokens'), ('candidatesTokenCount', 'response_output_tokens')]:
        value = count(local)
        if value is not None:
            usage[wire] = value
    # Hub's output_tokens includes reasoning; Gemini candidatesTokenCount does not.
    if count('candidatesTokenCount') is not None:
        usage['output_tokens'] = count('candidatesTokenCount') + (count('thoughtsTokenCount') or 0)
    if text('response_id'):
        usage['response_id'] = text('response_id')
    chat = {'usage': usage} if usage else {}
    if text('response_model'):
        chat['response_model'] = text('response_model')
    step = {'chat_model': chat}
    index = text('last_step_index')
    if index is not None and index.isdecimal() and int(index) < 2**32:
        step['step_indices'] = [int(index)]
    generator = {'generator_metadata': step}
    for local, wire in [('trajectory_id', 'trajectory_id'), ('cascade_id', 'cascade_id'), ('model_id', 'plan_model')]:
        if text(local):
            generator[wire] = text(local)
    extension = {'cortex_generator_metadata': generator, 'ide_name': 'antigravity',
                 'is_redacted': 1, 'product_name': 'hub'}
    for name, pattern in [('ide_version', r'antigravity/hub/([^ ]+)'), ('os', r'os_type=([^; )]+)'),
                          ('arch', r'arch=([^; )]+)')]:
        match = re.search(pattern, snapshot.user_agent)
        if match:
            extension[name] = match.group(1)
    return {'source_extension': extension}
