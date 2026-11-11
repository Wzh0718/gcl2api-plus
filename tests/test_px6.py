from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src import px6


def proxy(number=1, **changes):
    return dict(id=str(number), host='192.0.2.1', ip=f'198.51.100.{number}',
                port=str(10000 + number), user='demo', **{'pass': 'private-password'},
                type='auto', version='3', active='1', country='us',
                date_end='2026-12-05 00:00:00', **changes)


@pytest.mark.asyncio
async def test_key_reads_eighty_owned_shared_ips(monkeypatch):
    monkeypatch.setattr(px6, '_request_page', lambda key, page: {
        'status': 'yes', 'list': [proxy(i) for i in range(1, 81)]})
    result = await px6.fetch_owned_proxies('test-key')
    assert len(result['nodes']) == 80
    assert result['items'][0]['ip'] == '198.51.100.1'
    assert result['items'][0]['version'] == '3'
    assert result['items'][0]['expires_at'] == '2026-12-05 00:00:00'
    assert result['nodes'][0]['proxy_url'] == 'http://demo:private-password@192.0.2.1:10001'
    assert 'private-password' not in str(result['items'])


@pytest.mark.asyncio
async def test_expiry_uses_provider_unix_time_not_timezone_ambiguous_date(monkeypatch):
    entry = proxy()
    entry['unixtime_end'] = '1900000000'
    monkeypatch.setattr(px6, '_request_page', lambda *args: {'status': 'yes', 'list': [entry]})
    result = await px6.fetch_owned_proxies('test-key')
    assert result['nodes'][0]['expires_at'] == 1900000000
    del entry['unixtime_end']
    result = await px6.fetch_owned_proxies('test-key')
    assert result['nodes'][0]['expires_at'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('expiry', ['bad', 'nan', 'inf', -1, True])
async def test_malformed_provider_expiry_does_not_import_nodes(monkeypatch, expiry):
    entry = proxy()
    entry['unixtime_end'] = expiry
    monkeypatch.setattr(px6, '_request_page', lambda *args: {'status': 'yes', 'list': [entry]})
    with pytest.raises(px6.PX6Error):
        await px6.fetch_owned_proxies('test-key')


@pytest.mark.asyncio
async def test_pagination_and_socks_gateway(monkeypatch):
    monkeypatch.setattr(px6, 'PAGE_SIZE', 2)
    pages = []
    def get(key, page):
        pages.append(page)
        return {'status': 'yes', 'list': {'1':proxy(1), '2':proxy(2)} if page == 1 else [proxy(3)]}
    monkeypatch.setattr(px6, '_request_page', get)
    result = await px6.fetch_owned_proxies('test-key', 'socks5')
    assert pages == [1, 2]
    assert len(result['nodes']) == 3
    assert result['nodes'][0]['proxy_url'].startswith('socks5://')


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'status': 'no', 'error_id': 100, 'error':'private-provider-error'},
    {'status': 'yes', 'list': 'bad'},
    {'status': 'yes', 'list': [{'host': 'bad', 'port':'private-port'}]},
])
async def test_provider_errors_are_safe(monkeypatch, payload):
    monkeypatch.setattr(px6, '_request_page', lambda *args: payload)
    with pytest.raises(px6.PX6Error) as error:
        await px6.fetch_owned_proxies('test-key')
    assert 'private' not in str(error.value)


@pytest.mark.asyncio
async def test_inactive_nodes_skipped_and_empty_result_is_valid(monkeypatch):
    p=proxy();p['active']='0'
    monkeypatch.setattr(px6, '_request_page', lambda *args: {'status':'yes', 'list':[p]})
    assert (await px6.fetch_owned_proxies('test-key'))['nodes'] == []


@pytest.mark.asyncio
async def test_panel_auth_preview_and_import(monkeypatch):
    from src.panel import proxy_groups as panel
    app=FastAPI();app.include_router(panel.router)
    fetched={'nodes':[{'name':'PX6 1','proxy_url':'http://demo:private-password@192.0.2.1:1234'}],
             'items':[{'ip':'198.51.100.1','expires_at':'2026-12-05'}]}
    fetch=AsyncMock(return_value=fetched)
    monkeypatch.setattr(panel,'fetch_owned_proxies',fetch)
    storage=AsyncMock()
    storage.import_proxy_group_nodes.return_value={'imported_count':1,'updated_count':0}
    monkeypatch.setattr(panel,'get_storage_adapter',AsyncMock(return_value=storage))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/proxy-groups/px6/preview',json={'api_key':'test-key'})
        assert response.status_code in {401,403}
        fetch.assert_not_awaited()
        app.dependency_overrides[panel.verify_panel_token]=lambda:'test'
        response=await client.post('/proxy-groups/px6/preview',json={'api_key':'test-key'})
        assert response.status_code == 200
        assert response.json()['count']==1
        assert 'private-password' not in response.text
        storage.import_proxy_group_nodes.assert_not_awaited()
        response=await client.post('/proxy-groups/3/px6',json={'api_key':'test-key'})
        assert response.status_code==200
        assert 'private-password' not in response.text
        assert storage.import_proxy_group_nodes.await_args.args[0]==3
        storage.import_proxy_group_nodes.reset_mock()
        fetch.return_value={'nodes':[],'items':[]}
        response=await client.post('/proxy-groups/3/px6',json={'api_key':'test-key','replace':True})
        assert response.status_code==400
        storage.import_proxy_group_nodes.assert_not_awaited()


def test_network_error_does_not_expose_key(monkeypatch):
    class BrokenOpener:
        def open(self, *args, **kwargs):
            raise OSError('https://px6.link/api/private-test-key/getproxy')
    monkeypatch.setattr(px6, 'build_opener', lambda *args: BrokenOpener())
    with pytest.raises(px6.PX6Error) as error:
        px6._request_page('private-test-key', 1)
    assert 'private-test-key' not in str(error.value)


@pytest.mark.asyncio
async def test_invalid_key_never_contacts_provider(monkeypatch):
    def unexpected(*args):
        pytest.fail('Invalid key contacted provider')
    monkeypatch.setattr(px6, '_request_page', unexpected)
    with pytest.raises(px6.PX6Error):
        await px6.fetch_owned_proxies('bad/key')


@pytest.mark.asyncio
async def test_whitelist_error_explains_required_verification(monkeypatch):
    monkeypatch.setattr(px6, '_request_page', lambda *args: {
        'status': 'no', 'error_id': 105, 'error': 'private-details'})
    with pytest.raises(px6.PX6Error, match='IP 白名单'):
        await px6.fetch_owned_proxies('test-key')
