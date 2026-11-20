import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src import oauth_error_evidence as evidence
from src.google_oauth_api import TokenError, refresh_access_token_with_clients
from src.request_timing import RequestTiming, request_timing


@pytest.mark.asyncio
async def test_refresh_retains_diagnosis_without_changing_classification():
    response = httpx.Response(400, json={
        'error': 'invalid_grant', 'error_subtype': 'invalid_rapt',
        'error_description': 'Token has been expired or revoked.',
    })
    state = RequestTiming()
    token = request_timing.set(state)
    try:
        with patch('src.google_oauth_api.post_async', AsyncMock(return_value=response)) as post, patch.object(evidence.log, 'error') as logged:
            with pytest.raises(TokenError) as caught:
                await refresh_access_token_with_clients('private-refresh-token', proxy_url=None)
        records = [json.loads(c.args[0].split(' ', 1)[1]) for c in logged.call_args_list if c.args[0].startswith('[OAUTH_EVIDENCE] {')]
        assert len(records) == 1
        event = records[0]
        assert event['error_subtype'] == 'invalid_rapt'
        assert event['error_description'] == 'Token has been expired or revoked.'
        assert event['http_status'] == 400
        assert event['timing_id'] == state.timing_id
        assert event['proxy_mode'] == 'direct'
        assert event['event_id'] in str(caught.value)
        assert caught.value.error_code == 'invalid_grant'
        assert caught.value.status_code == 400
        assert 'Token has been expired' not in str(caught.value)
        assert post.await_count == 1
        assert 'private-refresh-token' not in str(logged.call_args_list)
    finally:
        request_timing.reset(token)


def record(response=None, **kwargs):
    return evidence.record_refresh_failure(
        response=response, refresh_token='short-secret/+', client_key='antigravity_enterprise',
        client_secret='client-secret', proxy_url='http://user:proxy-password@host:8080',
        token_url='https://oauth2.googleapis.com/token', transport='antigravity_native',
        started=time.monotonic(), **kwargs,
    )


@pytest.mark.parametrize('body', [
    {'error': 'invalid_grant', 'error_description': 'short-secret/+ short-secret%2F%2B client-secret user@example.com https://host/?code=hidden-code access_token=access-token-abc code=code-xyz ' + 'z'*80,
     'error_subtype': {'nested': 'secret'}, 'access_token': 'unknown-token', 'extra': 'hidden'},
    ['private-value'], 'private-value',
])
def test_only_redacted_allowlisted_fields_are_logged(body):
    with patch.object(evidence.log, 'error') as logged:
        record(httpx.Response(400, json=body))
    output = logged.call_args.args[0]
    event = json.loads(output.split(' ', 1)[1])
    # 断言串必须足够独特：事件里带 24 位随机 hex（event_id/credential_ref 等），
    # 用 'abc'/'xyz' 这类 3 字符做子串检查会偶发撞进随机值（CI 已因此误报）。
    for secret in ('short-secret', 'client-secret', 'user@example.com', 'hidden-code',
                   'unknown-token', 'private-value', 'proxy-password',
                   'access-token-abc', 'code-xyz', 'z'*80):
        assert secret not in output
    assert event['error_subtype'] is None
    assert event['proxy_mode'] == 'explicit'
    assert len(event['proxy_config_ref']) == 24


@pytest.mark.parametrize('content', [b'<html>private response</html>', b'not-json', b'x'*70000])
def test_non_json_and_oversized_bodies_not_logged(content):
    with patch.object(evidence.log, 'error') as logged:
        record(httpx.Response(502, content=content))
    event = json.loads(logged.call_args.args[0].split(' ', 1)[1])
    assert event['body_format'] == 'omitted'
    assert event['error_description'] is None


@pytest.mark.asyncio
async def test_network_failure_records_only_type_and_success_emits_no_evidence():
    with patch('src.google_oauth_api.post_async', AsyncMock(side_effect=httpx.ConnectError('private exception URL'))), patch.object(evidence.log, 'error') as logged:
        with pytest.raises(TokenError):
            await refresh_access_token_with_clients('secret')
    assert 'private exception URL' not in str(logged.call_args_list)
    event = json.loads(logged.call_args_list[0].args[0].split(' ', 1)[1])
    assert event['exception_type'] == 'ConnectError'
    assert event['http_status'] is None
    assert event['proxy_mode'] == 'default_unresolved'
    with patch('src.google_oauth_api.post_async', AsyncMock(return_value=httpx.Response(200, json={'access_token': 'secret'}))), patch.object(evidence.log, 'error') as logged:
        result, _ = await refresh_access_token_with_clients('secret')
    assert result['access_token'] == 'secret'
    logged.assert_not_called()


def test_logging_failure_does_not_escape():
    with patch.object(evidence.log, 'error', side_effect=OSError('disk full')):
        assert len(record(httpx.Response(400, json={'error': 'invalid_grant'}))) == 32


@pytest.mark.asyncio
async def test_cancelled_refresh_is_not_converted_to_token_error():
    with patch('src.google_oauth_api.post_async', AsyncMock(side_effect=asyncio.CancelledError)), patch.object(evidence.log, 'error') as logged:
        with pytest.raises(asyncio.CancelledError):
            await refresh_access_token_with_clients('secret')
    logged.assert_not_called()
