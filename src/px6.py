"""Read already-owned PX6 proxies without persisting the provider API key."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
from typing import Literal
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.proxy_groups import normalize_proxy_url

PAGE_SIZE = 1000
MAX_PAGES = 20


class PX6Error(ValueError):
    """Safe public error; never includes provider bodies or credential URLs."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_page(key: str, page: int) -> dict:
    # urllib does not emit credential-bearing request URLs to HTTPX/httpcore logs.
    request = Request(
        f'https://px6.link/api/{key}/getproxy?state=active&nokey=1&limit={PAGE_SIZE}&page={page}',
        headers={'Accept': 'application/json'},
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=25) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise PX6Error('PX6 返回的数据过大')
        payload = json.loads(body)
    except HTTPError as exc:
        message = 'PX6 请求过于频繁，请稍后重试' if exc.code == 429 else 'PX6 接口请求失败，请稍后重试'
        raise PX6Error(message) from None
    except PX6Error:
        raise
    except Exception:
        raise PX6Error('无法读取 PX6，请检查网络后重试') from None
    return payload


async def fetch_owned_proxies(
    api_key: str, protocol: Literal['http', 'socks5'] = 'http',
) -> dict:
    key = api_key.strip()
    if not re.fullmatch(r'[A-Za-z0-9-]{8,128}', key):
        raise PX6Error('请填写有效的 PX6 API Key')
    if protocol not in {'http', 'socks5'}:
        raise PX6Error('PX6 协议必须为 HTTP 或 SOCKS5')
    nodes, items, seen = [], [], set()
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            await asyncio.sleep(0.35)
        payload = await asyncio.to_thread(_request_page, key, page)
        if not isinstance(payload, dict) or payload.get('status') != 'yes':
            error_id = payload.get('error_id') if isinstance(payload, dict) else None
            messages = {'100': 'PX6 API Key 无效', '105': 'PX6 API 拒绝当前来源 IP，请检查 PX6 API IP 白名单'}
            raise PX6Error(messages.get(str(error_id), 'PX6 验证失败，请检查 Key 与 API 访问设置'))
        entries = payload.get('list')
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list) or len(entries) > PAGE_SIZE:
            raise PX6Error('PX6 返回的代理列表格式无效')
        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    raise ValueError()
                if str(entry.get('active', '1')) == '0':
                    continue
                version = str(entry.get('version', ''))
                if version == '5':
                    continue  # MTProto cannot carry this application's HTTP traffic.
                kind = entry.get('type') or 'auto'
                scheme = protocol if kind == 'auto' else {'http': 'http', 'socks': 'socks5', 'socks5': 'socks5'}[kind]
                host = str(ipaddress.ip_address(entry['host']))
                if ':' in host:
                    host = f'[{host}]'
                url = normalize_proxy_url({'host': host, 'port': int(entry['port']),
                                           'username': entry['user'], 'password': entry['pass'], 'type': scheme})
                if version not in {'3', '4', '6'} or not all(
                    isinstance(entry.get(field), str) and entry[field] for field in ('user', 'pass')
                ):
                    raise ValueError()
                identity = str(int(entry['id']))
                if identity in seen:
                    continue
                seen.add(identity)
                # The provider's date_end has no timezone; use its absolute Unix
                # timestamp for scheduling rather than guessing the server timezone.
                raw_expiry = entry.get('unixtime_end')
                expiry = None
                if raw_expiry not in (None, ''):
                    expiry = float(raw_expiry)
                    if isinstance(raw_expiry, bool) or not math.isfinite(expiry) or expiry <= 0:
                        raise ValueError()
                nodes.append({'name': f'PX6 {identity}', 'proxy_url': url, 'expires_at': expiry})
                items.append({'id': identity, 'ip': str(ipaddress.ip_address(entry['ip'])),
                              'host': str(ipaddress.ip_address(entry['host'])), 'port': int(entry['port']),
                              'protocol': scheme, 'version': version,
                              'country': str(entry.get('country', ''))[:2].upper(),
                              'expires_at': str(entry.get('date_end', ''))[:40]})
            except (ValueError, TypeError, KeyError):
                raise PX6Error('PX6 节点数据不完整或协议不受支持，未导入任何节点') from None
        if len(entries) < PAGE_SIZE:
            return {'nodes': nodes, 'items': items}
    raise PX6Error('PX6 代理数量超出单次读取范围，未导入任何节点')
