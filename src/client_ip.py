"""客户端来源 IP 上下文。

只服务于「来源 IP 维度」的策略判定与审计，不参与内容改写。
纯 ASGI 中间件把来源 IP 写入 contextvar，路由与转换器按需读取，
避免把 request 对象一路透传到转换层。
"""

from __future__ import annotations

import ipaddress
from contextvars import ContextVar

client_ip: ContextVar[str | None] = ContextVar("client_ip", default=None)

_FORWARDED_HEADERS = (b"x-forwarded-for", b"x-real-ip")


def normalize_client_ip(raw: object) -> str | None:
    """Normalize ``1.2.3.4``, ``1.2.3.4:5678`` or ``[::1]:443`` to a bare IP."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def pick_client_ip(scope: dict, *, trust_forwarded: bool = False) -> str | None:
    """Resolve the client IP of an ASGI scope.

    ``trust_forwarded`` 只应在服务确实部署在可信反向代理之后时开启；
    开启后取 ``X-Forwarded-For`` 最左一跳（原始客户端），回退 ``X-Real-IP``。
    """
    if trust_forwarded:
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        for name in _FORWARDED_HEADERS:
            raw = headers.get(name)
            if not raw:
                continue
            first = raw.decode("latin-1").split(",", 1)[0]
            address = normalize_client_ip(first)
            if address:
                return address
    peer = scope.get("client") or ()
    return normalize_client_ip(peer[0]) if peer else None


class ClientIpMiddleware:
    """Pure ASGI middleware: expose the client IP for the whole request lifetime."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        import config

        token = client_ip.set(pick_client_ip(
            scope, trust_forwarded=config.get_remote_image_inline_trust_forwarded_sync(),
        ))
        try:
            return await self.app(scope, receive, send)
        finally:
            client_ip.reset(token)
