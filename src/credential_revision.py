"""Internal credential generations and the limited state writable by OAuth CAS."""
import uuid

REVISION_KEY = '_credential_revision'
NETWORK_FIELDS = {'proxy_mode', 'proxy_url', 'proxy_group_id', 'proxy_node_id', 'bound_proxy_node_id'}
CAS_STATE_FIELDS = {'disabled', 'error_codes', 'error_messages', 'tier', 'preview'}


def credential_snapshot(data):
    return {k: v for k, v in data.items() if k not in NETWORK_FIELDS}


def next_credential_revision(data):
    result = dict(data)
    result[REVISION_KEY] = uuid.uuid4().hex
    return result


def checked_state_updates(updates):
    updates = dict(updates or {})
    if set(updates) - CAS_STATE_FIELDS:
        raise ValueError('Unsupported conditional credential state field')
    return updates
