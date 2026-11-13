from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.panel import billing, proxy_groups


@pytest.fixture
def storage(monkeypatch):
    adapter = SimpleNamespace(
        get_credential=AsyncMock(return_value={'access_token': 'token'}),
        get_credential_network=AsyncMock(return_value={
            'proxy_mode': 'custom', 'proxy_url': 'http://alice:secret@node.example:8080',
        }),
        update_credential_network=AsyncMock(return_value=True),
        invalidate_antigravity_proxy_binding=AsyncMock(return_value=True),
        get_antigravity_account_health=AsyncMock(return_value={}),
    )
    for module in (billing, proxy_groups):
        monkeypatch.setattr(module, 'get_storage_adapter', AsyncMock(return_value=adapter))
    monkeypatch.setattr(proxy_groups, 'get_antigravity_network_check_enabled', AsyncMock(return_value=False))
    monkeypatch.setattr(proxy_groups, 'get_antigravity_network_check_ttl_seconds', AsyncMock(return_value=1800))
    monkeypatch.setattr(proxy_groups, 'get_antigravity_egress_ip_check_url', AsyncMock(return_value=''))
    return adapter


async def test_custom_proxy_blank_edit_preserves_secret_and_masks_response(storage):
    result = await billing.update_credential_network('a.json', {'proxy_mode': 'custom'}, _token='test')
    storage.update_credential_network.assert_awaited_once_with(
        'a.json', 'custom', 'http://alice:secret@node.example:8080',
        mode='antigravity', proxy_group_id=None,
    )
    assert result['proxy_url'] == 'http://alice:***@node.example:8080'
    assert 'secret' not in str(result)


async def test_custom_proxy_cannot_reuse_another_policy_url(storage):
    storage.get_credential_network.return_value = {'proxy_mode': 'group', 'proxy_url': 'http://group:8080'}
    with pytest.raises(HTTPException) as caught:
        await billing.update_credential_network('a.json', {'proxy_mode': 'custom'}, _token='test')
    assert caught.value.status_code == 400
    storage.update_credential_network.assert_not_awaited()


async def test_manual_rebind_does_not_require_ip_echo(storage):
    storage.get_credential_network.return_value = {'proxy_mode': 'group', 'proxy_group_id': 1}
    result = await proxy_groups.rebind_account_network('a.json', _token='test')
    storage.invalidate_antigravity_proxy_binding.assert_awaited_once_with('a.json', require_replacement=True)
    assert result['rebound'] is True
    assert result['check_status'] == 'skipped'


async def test_manual_rebind_no_spare_preserves_binding(storage):
    storage.get_credential_network.return_value = {'proxy_mode': 'group', 'proxy_group_id': 1}
    storage.invalidate_antigravity_proxy_binding.return_value = False
    with pytest.raises(HTTPException) as caught:
        await proxy_groups.rebind_account_network('a.json', _token='test')
    assert caught.value.status_code == 409
    assert '原绑定' in caught.value.detail


@pytest.mark.parametrize('health, status', [
    ({'binding_status': 'healthy', 'eligibility_status': 'eligible'}, 'passed'),
    ({'binding_status': 'proxy_failed', 'eligibility_status': 'error', 'eligibility_reason': 'timeout'}, 'failed'),
])
async def test_manual_rebind_reports_check_separately(storage, monkeypatch, health, status):
    storage.get_credential_network.return_value = {'proxy_mode': 'group', 'proxy_group_id': 1}
    monkeypatch.setattr(proxy_groups, 'get_antigravity_egress_ip_check_url', AsyncMock(return_value='https://echo.example'))
    monkeypatch.setattr(proxy_groups.credential_manager, 'check_antigravity_account_health', AsyncMock(return_value=health))
    result = await proxy_groups.rebind_account_network('a.json', _token='test')
    assert result['rebound'] is True
    assert result['check_status'] == status


async def test_manual_rebind_check_exception_still_reports_saved_binding(storage, monkeypatch):
    storage.get_credential_network.return_value = {'proxy_mode': 'group', 'proxy_group_id': 1}
    monkeypatch.setattr(proxy_groups, 'get_antigravity_egress_ip_check_url', AsyncMock(return_value='https://echo.example'))
    monkeypatch.setattr(proxy_groups.credential_manager, 'check_antigravity_account_health', AsyncMock(side_effect=ValueError('token refresh failed')))
    result = await proxy_groups.rebind_account_network('a.json', _token='test')
    assert result['rebound'] is True
    assert result['check_status'] == 'failed'
    assert 'token refresh failed' in result['message']


async def test_proxy_reveal_requires_panel_auth_and_is_not_cached(storage):
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(billing.network_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        endpoint = '/creds/a.json/network/reveal'
        assert (await client.post(endpoint)).status_code in (401, 403)
        app.dependency_overrides[billing.verify_panel_token] = lambda: 'test'
        response = await client.post(endpoint)
        assert response.status_code == 200
        assert response.json()['proxy_url'] == 'http://alice:secret@node.example:8080'
        assert response.headers['cache-control'] == 'no-store'
        assert 'secret' not in (await client.get('/creds/a.json/network')).text
        storage.get_credential.return_value = None
        assert (await client.post(endpoint)).status_code == 404


async def test_proxy_reveal_only_returns_custom_policy(storage):
    storage.get_credential_network.return_value = {'proxy_mode': 'group', 'proxy_url': 'http://group:8080'}
    with pytest.raises(HTTPException) as caught:
        await billing.reveal_credential_proxy('a.json', _token='test')
    assert caught.value.status_code == 400


@pytest.mark.parametrize('strategy', ['round_robin', 'random', 'failover'])
async def test_manual_rebind_uses_only_same_imported_group_without_px6(tmp_path, monkeypatch, strategy):
    from src.storage.sqlite_manager import SQLiteManager
    from src.shadowsocks import shadowsocks_uri

    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path))
    backend = SQLiteManager()
    await backend.initialize()
    try:
        group = await backend.create_proxy_group(name='手动导入', strategy=strategy)
        nodes = [
            {'name': 'http', 'proxy_url': 'http://node-a.example:8080'},
            {'name': 'socks5', 'proxy_url': 'socks5://node-b.example:1080'},
            {'name': 'ss', 'proxy_url': shadowsocks_uri(name='ss', server='node-c.example', port=8388, method='aes-256-gcm', password='test')},
        ]
        await backend.import_proxy_group_nodes(group['id'], nodes)
        foreign = await backend.create_proxy_group(name='不能跨组')
        await backend.import_proxy_group_nodes(foreign['id'], [{'name': 'foreign', 'proxy_url': 'http://foreign.example:8080'}])
        await backend.store_credential('a.json', {'access_token': 'test'}, mode='antigravity')
        await backend.update_credential_network('a.json', proxy_mode='group', proxy_group_id=group['id'])
        first = await backend.resolve_credential_network('a.json')
        monkeypatch.setattr(proxy_groups, 'get_storage_adapter', AsyncMock(return_value=backend))
        monkeypatch.setattr(proxy_groups, 'get_antigravity_egress_ip_check_url', AsyncMock(return_value=''))
        monkeypatch.setattr(proxy_groups, 'get_antigravity_network_check_enabled', AsyncMock(return_value=False))
        monkeypatch.setattr(proxy_groups, 'get_antigravity_network_check_ttl_seconds', AsyncMock(return_value=1800))
        px6 = AsyncMock(side_effect=AssertionError('手动导入组不应访问 PX6'))
        monkeypatch.setattr(proxy_groups, 'fetch_owned_proxies', px6)
        result = await proxy_groups.rebind_account_network('a.json', _token='test')
        after = await backend.resolve_credential_network('a.json')
        assert result['rebound'] is True
        assert after['proxy_node_id'] != first['proxy_node_id']
        assert after['proxy_group_id'] == group['id']
        assert after['proxy_url'] in {node['proxy_url'] for node in nodes}
        px6.assert_not_awaited()
    finally:
        await backend.close()


async def test_network_policy_shows_bound_group_node_with_masked_url(storage):
    storage.get_credential_network.return_value = {
        'proxy_mode': 'group', 'proxy_group_id': 2,
        'bound_proxy_node_id': 7, 'bound_proxy_node_name': 'SOCKS5 节点',
        'bound_proxy_url': 'socks5://alice:secret@node.example:1080',
    }
    storage.get_proxy_group = AsyncMock(return_value={
        'id': 2, 'name': '手动导入', 'strategy': 'random', 'enabled_node_count': 3,
    })
    result = await billing.get_credential_network('a.json', _token='test')
    assert result['bound_proxy_node_id'] == 7
    assert result['bound_proxy_node_name'] == 'SOCKS5 节点'
    assert result['bound_proxy_url'] == 'socks5://alice:***@node.example:1080'
    assert result['effective_proxy'] == result['bound_proxy_url']
    assert 'secret' not in str(result)
