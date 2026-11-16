import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from src.storage.sqlite_manager import SQLiteManager
from src.antigravity_telemetry import TelemetryService, encode_log_request


@pytest_asyncio.fixture
async def telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    db = SQLiteManager()
    await db.initialize()
    for name in ("a", "b"):
        await db.store_credential(name + ".json", {
            "access_token": name + "-token", "refresh_token": name + "-secret",
            "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "oauth_client_key": "consumer",
        }, "antigravity")
        await db.update_credential_state(name + ".json", {"user_email": name + "@example.test"}, "antigravity")
        await db.update_credential_network(name + ".json", "custom", f"http://{name}-proxy:8080")
    async def adapter():
        return db
    service = TelemetryService(adapter)
    sent = []
    async def send(snapshot, body):
        sent.append((snapshot, body))
        return 200
    service._send = send
    try:
        yield db, service, sent
    finally:
        await service.close()
        await db.close()


def request(name="a"):
    from src.api.antigravity import _derive_session_id, build_antigravity_headers
    payload = {"requestId": "agent/conversation/123/trajectory/1", "model": "gemini-test",
               "requestType": "agent", "request": {"sessionId": _derive_session_id("", name + ".json"),
               "labels": {"cascade_id": "conversation", "trajectory_id": "trajectory", "request_id": "trajectory-0"},
               "contents": [{"parts": [{"text": "PRIVATE PROMPT"}]}]}}
    return payload, build_antigravity_headers(name + "-token"), f"http://{name}-proxy:8080"


async def begin(service, name="a"):
    body, headers, proxy = request(name)
    return await service.begin("https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent",
                               body, headers, proxy, "antigravity")


@pytest.mark.asyncio
async def test_default_off_and_account_only_policy(telemetry):
    _, service, sent = telemetry
    assert await begin(service) is None
    a = await service.configure("a.json", "send")
    assert a["mode"] == "send"
    assert await begin(service) is not None
    assert await begin(service, "b") is None
    assert (await service.status("b.json"))["mode"] == "off"
    assert not sent


@pytest.mark.asyncio
async def test_environment_survives_workers_and_accounts_cannot_share_it(telemetry):
    db, service, _ = telemetry
    a = await service.configure("a.json", "preview")
    b = await service.configure("b.json", "preview")
    assert a["installation_id"] != b["installation_id"]
    assert (await service.configure("a.json", "send"))["installation_id"] == a["installation_id"]
    other = TelemetryService(service.adapter_provider)
    assert (await other.status("a.json"))["installation_id"] == a["installation_id"]
    await db.update_credential_state("b.json", {"user_email": "a@example.test"}, "antigravity")
    with pytest.raises(ValueError, match="重复凭证"):
        await service.configure("b.json", "send")


@pytest.mark.asyncio
async def test_payload_cannot_cross_account_auth_route_or_session(telemetry):
    _, service, sent = telemetry
    await service.configure("a.json", "send")
    await service.configure("b.json", "send")
    body, headers, proxy = request()
    body["request"]["sessionId"] = request("b")[0]["request"]["sessionId"]
    assert await service.begin("https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent", body, headers, proxy, "antigravity") is None
    body, headers, _ = request()
    assert await service.begin("https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent", body, headers, "http://b-proxy:8080", "antigravity") is None
    assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["off", "reauthorize", "delete", "network"])
async def test_queued_event_is_discarded_after_owner_or_environment_changes(telemetry, change):
    db, service, sent = telemetry
    await service.configure("a.json", "send")
    attempt = await begin(service)
    if change == "off":
        await service.configure("a.json", "off")
    elif change == "reauthorize":
        data = await db.get_credential("a.json", "antigravity")
        data["access_token"] = "replacement-token"
        await db.store_credential("a.json", data, "antigravity")
    elif change == "delete":
        await db.delete_credential("a.json", "antigravity")
    else:
        await db.update_credential_network("a.json", "custom", "http://new-proxy:8080")
    attempt.observe('data: {"response":{"usageMetadata":{"promptTokenCount":3},"candidates":[{"finishReason":"STOP"}]}}\n\n')
    attempt.finish(200)
    await service.drain()
    assert not sent


@pytest.mark.asyncio
async def test_concurrent_accounts_keep_authentication_and_events_isolated(telemetry):
    _, service, sent = telemetry
    for name in ("a", "b"):
        await service.configure(name + ".json", "send")
    a, b = await asyncio.gather(begin(service), begin(service, "b"))
    a.observe('data: {"response":{"usageMetadata":{"promptTokenCount":11,"candidatesTokenCount":2},"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"PRIVATE ANSWER"}]}}]}}\n\n')
    b.observe('data: {"response":{"usageMetadata":{"promptTokenCount":22},"candidates":[{"finishReason":"STOP"}]}}\n\n')
    a.finish(200)
    b.finish(200)
    await service.drain()
    assert len(sent) == 2
    for snapshot, body in sent:
        name = snapshot.filename[0]
        assert snapshot.access_token == name + "-token"
        assert snapshot.proxy_url == f"http://{name}-proxy:8080"
        assert b"PRIVATE" not in body and b"-secret" not in body and b"-token" not in body
        assert snapshot.installation_id.encode() in body


@pytest.mark.asyncio
async def test_preview_never_sends_and_delete_recreate_resets_environment(telemetry):
    db, service, sent = telemetry
    profile = await service.configure("a.json", "preview")
    attempt = await begin(service)
    attempt.finish(503)
    await service.drain()
    assert not sent
    assert (await service.status("a.json"))["last_outcome"] == "preview"
    data = await db.get_credential("a.json", "antigravity")
    await db.delete_credential("a.json", "antigravity")
    await db.store_credential("a.json", data, "antigravity")
    await db.update_credential_state("a.json", {"user_email": "a@example.test"}, "antigravity")
    assert (await service.status("a.json"))["mode"] == "off"
    assert (await service.configure("a.json", "preview"))["installation_id"] != profile["installation_id"]

@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
async def test_stream_observer_parses_lines_and_fragmented_bytes(telemetry, monkeypatch, native):
    from src import antigravity_telemetry as module
    _, service, sent = telemetry
    monkeypatch.setattr(module, 'telemetry_service', service)
    await service.configure('a.json', 'send')
    message = 'data: {"usageMetadata":{"promptTokenCount":17},"candidates":[{"finishReason":"STOP"}]}'
    @module.observe_stream
    async def upstream(url, body, **kwargs):
        if native:
            wire = (message + '\n\n').encode()
            for start in range(0, len(wire), 7):
                yield wire[start:start + 7]
        else:
            yield message
            yield ''
    body, headers, proxy = request()
    chunks = [chunk async for chunk in upstream(
        'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent', body,
        headers=headers, proxy_url=proxy, transport='antigravity', native=native)]
    assert chunks
    await service.drain()
    assert len(sent) == 1
    assert b'completed' in sent[0][1] and b'promptTokenCount' in sent[0][1]
    assert b'17' in sent[0][1]


@pytest.mark.asyncio
async def test_retry_failure_does_not_block_another_account(telemetry):
    _, service, _ = telemetry
    for name in ('a', 'b'):
        await service.configure(name + '.json', 'send')
    calls = []
    async def sender(snapshot, body):
        calls.append(snapshot.filename)
        return 503 if snapshot.filename == 'a.json' else 200
    service._send = sender
    for name in ('a', 'b'):
        attempt = await begin(service, name)
        attempt.finish(200)
    await service.drain()
    assert calls.count('a.json') == 2 and calls.count('b.json') == 1
    assert (await service.status('a.json'))['last_outcome'] == 'send_failed'
    assert (await service.status('b.json'))['last_outcome'] == 'sent'
    before = await service.status('b.json')
    await service.configure('a.json', 'off')
    assert await service.status('b.json') == before


@pytest.mark.asyncio
async def test_panel_api_validates_account_policy_and_hides_secrets(telemetry, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from src import antigravity_telemetry as module
    from src.panel import creds
    _, service, _ = telemetry
    monkeypatch.setattr(module, 'telemetry_service', service)
    app = FastAPI()
    app.include_router(creds.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/creds/a.json/telemetry')).status_code in (401, 403)
        app.dependency_overrides[creds.verify_panel_token] = lambda: 'test'
        response = await client.patch('/creds/a.json/telemetry', json={'mode': 'preview'})
        assert response.status_code == 200 and response.json()['mode'] == 'preview'
        assert all(secret not in response.text for secret in ('a-token', 'a-secret', 'a-proxy', 'a@example'))
        assert (await client.patch('/creds/a.json/telemetry', json={'mode': 'global'})).status_code == 422
        assert (await client.get('/creds/missing.json/telemetry')).status_code == 404
        assert (await client.get('/creds/a.json/telemetry?mode=geminicli')).status_code == 400
        assert (await client.get('/creds/b.json/telemetry')).json()['mode'] == 'off'


@pytest.mark.asyncio
async def test_expired_token_discards_queued_event(telemetry):
    db, service, sent = telemetry
    await service.configure('a.json', 'send')
    attempt = await begin(service)
    credential = await db.get_credential('a.json', 'antigravity')
    credential['expiry'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    await db.store_credential('a.json', credential, 'antigravity')
    attempt.finish(200)
    await service.drain()
    assert not sent

@pytest.mark.asyncio
async def test_post_observer_preserves_response_and_records_only_metadata(telemetry, monkeypatch):
    import httpx
    from src import antigravity_telemetry as module
    _, service, sent = telemetry
    monkeypatch.setattr(module, 'telemetry_service', service)
    await service.configure('a.json', 'send')
    response = httpx.Response(200, json={
        'usageMetadata': {'totalTokenCount': 9},
        'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'PRIVATE ANSWER'}]}}]})
    @module.observe_post
    async def upstream(url, **kwargs):
        assert kwargs['proxy_url'] == 'http://a-proxy:8080'
        return response
    body, headers, proxy = request()
    result = await upstream('https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent',
                            json=body, headers=headers, proxy_url=proxy, transport='antigravity')
    assert result is response
    await service.drain()
    assert len(sent) == 1 and b'totalTokenCount' in sent[0][1]
    assert b'PRIVATE' not in sent[0][1]


@pytest.mark.asyncio
async def test_stream_close_releases_upstream_and_records_cancellation(telemetry, monkeypatch):
    from src import antigravity_telemetry as module
    _, service, sent = telemetry
    monkeypatch.setattr(module, 'telemetry_service', service)
    await service.configure('a.json', 'send')
    closed = asyncio.Event()
    @module.observe_stream
    async def upstream(url, body, **kwargs):
        try:
            yield 'data: {}'
            yield 'data: [DONE]'
        finally:
            closed.set()
    body, headers, proxy = request()
    stream = upstream('https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent',
                      body, headers=headers, proxy_url=proxy, transport='antigravity')
    assert await anext(stream) == 'data: {}'
    await stream.aclose()
    assert closed.is_set()
    await service.drain()
    assert len(sent) == 1 and b'cancelled' in sent[0][1]

@pytest.mark.asyncio
async def test_request_history_survives_policy_change_and_links_monitor(telemetry, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import FastAPI
    from src.call_monitor import CallMonitor
    from src.request_timing import RequestTiming, request_timing
    from src.panel import call_monitor as panel
    db, service, _ = telemetry
    await service.configure('a.json', 'send')
    await service.configure('b.json', 'send')
    storage = SimpleNamespace(_backend=db, list_telemetry_events=db.list_telemetry_events)
    monitor = CallMonitor(storage)
    token = request_timing.set(RequestTiming(timing_id='request-one'))
    try:
        a = await begin(service)
        b = await begin(service, 'b')
        a.finish(200)
        b.finish(200)
        await service.drain()
        await monitor.record(request_id='monitor-one', credential_name='a.json', success=True)
    finally:
        request_timing.reset(token)
    await service.configure('a.json', 'off')
    monkeypatch.setattr(panel, 'get_storage_adapter', AsyncMock(return_value=storage))
    monkeypatch.setattr(panel, 'get_call_monitor', AsyncMock(return_value=monitor))
    app = FastAPI()
    app.include_router(panel.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        url = '/call-monitor/telemetry?timing_id=request-one&credential_name=a.json'
        assert (await client.get(url)).status_code in (401, 403)
        app.dependency_overrides[panel.verify_panel_token] = lambda: 'test'
        response = await client.get(url)
        assert response.status_code == 200
        events = response.json()['events']
        assert len(events) == 1 and events[0]['mode'] == 'send' and events[0]['outcome'] == 'sent'
        assert events[0]['binding']['route_ref']
        assert events[0]['binding']['installation_id'] == a.snapshot.installation_id
        assert all(secret not in response.text for secret in ('a-token', 'b-token', 'a-secret', 'a-proxy', 'PRIVATE'))
        realtime = await client.get('/call-monitor/realtime')
        assert realtime.status_code == 200
        record = realtime.json()['records'][0]
        assert record['timing_id'] == 'request-one'
        assert record['telemetry'] == {'count': 1, 'outcomes': ['sent']}
        assert (await client.get('/call-monitor/telemetry?timing_id=other&credential_name=a.json')).json()['events'] == []
        await db.delete_credential('a.json', 'antigravity')
        assert (await client.get(url)).json()['events'] == []
        assert len(await db.list_telemetry_events(['request-one'])) == 1  # b survives a deletion

@pytest.mark.asyncio
async def test_history_retention_and_history_failure_do_not_break_delivery(telemetry, monkeypatch):
    import aiosqlite
    from src.request_timing import RequestTiming, request_timing
    db, service, sent = telemetry
    await service.configure('a.json', 'send')
    token = request_timing.set(RequestTiming(timing_id='retention-case'))
    try:
        attempt = await begin(service)
    finally:
        request_timing.reset(token)
    attempt.finish(200)
    await service.drain()
    assert len(await db.list_telemetry_events(['retention-case'])) == 1
    async with aiosqlite.connect(db._db_path) as connection:
        await connection.execute('UPDATE antigravity_telemetry_events SET created_at=0')
        await connection.commit()
    assert await db.list_telemetry_events(['retention-case']) == []
    async def fail_history(*args, **kwargs):
        raise RuntimeError('history unavailable')
    monkeypatch.setattr(db, 'save_telemetry_event', fail_history)
    next_attempt = await begin(service)
    next_attempt.finish(200)
    await service.drain()
    assert len(sent) == 2

@pytest.mark.asyncio
async def test_group_telemetry_uses_existing_bound_route_without_selecting(telemetry, monkeypatch):
    from unittest.mock import AsyncMock
    from src.panel import creds
    from src import antigravity_telemetry as module
    db, service, sent = telemetry
    group = await db.create_proxy_group(name='sticky-telemetry', exclusive_assignment=True)
    await db.import_proxy_group_nodes(group['id'], [
        {'name': 'a', 'proxy_url': 'http://a-proxy:8080'},
        {'name': 'spare', 'proxy_url': 'http://spare-proxy:8080'},
    ])
    await db.update_credential_network('a.json', 'group', proxy_group_id=group['id'])
    bound = await db.resolve_credential_network('a.json')
    stored = await db.get_credential_network('a.json')
    assert stored['proxy_url'] is None and stored['bound_proxy_url'] == bound['proxy_url']
    monkeypatch.setattr(db, 'resolve_credential_network', AsyncMock(side_effect=AssertionError('must not rebind')))
    monkeypatch.setattr(module, 'telemetry_service', service)
    profile = await creds._account_telemetry('a.json', 'antigravity', 'send')
    assert profile['mode'] == 'send' and profile['environment_matches']
    body, headers, _ = request()
    attempt = await service.begin('https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent',
                                  body, headers, bound['proxy_url'], 'antigravity')
    assert attempt is not None and attempt.snapshot.proxy_url == bound['proxy_url']
    attempt.finish(200)
    await service.drain()
    assert len(sent) == 1 and sent[0][0].proxy_url == bound['proxy_url']
    assert (await db.get_credential_network('a.json'))['bound_proxy_node_id'] == bound['bound_proxy_node_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['unbound', 'disabled', 'expired', 'cooling'])
async def test_group_telemetry_rejects_unavailable_binding_without_rebinding(telemetry, unavailable):
    import aiosqlite
    import time
    db, service, sent = telemetry
    group = await db.create_proxy_group(name='unavailable-telemetry')
    await db.import_proxy_group_nodes(group['id'], [
        {'name': 'bound', 'proxy_url': 'http://a-proxy:8080'},
        {'name': 'spare', 'proxy_url': 'http://spare-proxy:8080'},
    ])
    await db.update_credential_network('a.json', 'group', proxy_group_id=group['id'])
    if unavailable == 'unbound':
        with pytest.raises(ValueError, match='绑定'):
            await service.configure('a.json', 'send')
        assert (await db.get_credential_network('a.json'))['bound_proxy_node_id'] is None
        return
    network = await db.resolve_credential_network('a.json')
    await service.configure('a.json', 'send')
    attempt = await begin(service)
    async with aiosqlite.connect(db._db_path) as connection:
        updates = {'disabled': ('enabled', 0), 'expired': ('expires_at', time.time()-1),
                   'cooling': ('cooldown_until', time.time()+3600)}
        column, value = updates[unavailable]
        await connection.execute(f'UPDATE proxy_group_nodes SET {column}=? WHERE id=?',
                                 (value, network['bound_proxy_node_id']))
        await connection.commit()
    with pytest.raises(ValueError, match='不可用'):
        await service.configure('a.json', 'send')
    attempt.finish(200)
    await service.drain()
    assert not sent
    assert (await service.status('a.json'))['last_outcome'] == 'discarded_binding_changed'
    assert (await db.get_credential_network('a.json'))['bound_proxy_node_id'] == network['bound_proxy_node_id']
