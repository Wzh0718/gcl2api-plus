"""Proxy-group parsing and presentation helpers.

The storage layer keeps proxy credentials in SQLite.  This module only handles
input normalization and masking so the API, UI and request path share one
contract without logging secrets.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import yaml

from src.shadowsocks import parse_shadowsocks_url, shadowsocks_uri

SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "ss"}


def normalize_proxy_url(value: Any) -> str:
    """Normalize a URL, host:port, host:port:user:pass or structured proxy."""
    if isinstance(value, Mapping):
        direct_value = value.get("proxy_url") or value.get("url")
        if direct_value:
            return normalize_proxy_url(direct_value)
        proxy_type = str(value.get("type") or value.get("scheme") or "http").strip().lower()
        if proxy_type in {"ss", "shadowsocks"}:
            return shadowsocks_uri(
                name=str(value.get("name") or value.get("label") or "").strip(),
                server=str(value.get("server") or value.get("host") or "").strip(),
                port=int(value.get("port") or 0),
                method=str(value.get("cipher") or value.get("method") or "").strip(),
                password=str(value.get("password") or ""),
            )
        host = str(value.get("host") or value.get("server") or "").strip()
        port = value.get("port")
        scheme = proxy_type
        username = value.get("username") or value.get("user")
        password = value.get("password") or value.get("pass")
        if not host or port in (None, ""):
            raise ValueError("结构化代理必须提供 host 和 port")
        auth = ""
        if username not in (None, ""):
            auth = quote(str(username), safe="")
            if password not in (None, ""):
                auth += f":{quote(str(password), safe='')}"
            auth += "@"
        value = f"{scheme}://{auth}{host}:{port}"

    if not isinstance(value, str):
        raise ValueError("代理必须是字符串或结构化对象")
    candidate = value.strip()
    if not candidate:
        raise ValueError("代理地址不能为空")

    if "://" not in candidate:
        if "@" in candidate:
            candidate = f"http://{candidate}"
        else:
            parts = candidate.split(":")
            if len(parts) == 2:
                candidate = f"http://{parts[0]}:{parts[1]}"
            elif len(parts) == 4:
                host, port, username, password = parts
                candidate = (
                    f"http://{quote(username, safe='')}:{quote(password, safe='')}@"
                    f"{host}:{port}"
                )
            else:
                raise ValueError(
                    "代理格式应为 URL、host:port、user:pass@host:port 或 host:port:user:pass"
                )

    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.lower()
        if scheme == "ss":
            node = parse_shadowsocks_url(candidate)
            return shadowsocks_uri(
                name=node.name,
                server=node.server,
                port=node.port,
                method=node.method,
                password=node.password,
            )
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            raise ValueError(f"不支持的代理协议: {parsed.scheme or 'unknown'}")
        if not parsed.hostname or parsed.port is None:
            raise ValueError("代理地址必须包含主机和端口")
        if parsed.port < 1 or parsed.port > 65535:
            raise ValueError("代理端口必须在 1-65535 之间")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"代理地址格式无效: {type(exc).__name__}") from exc

    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def parse_proxy_import(raw: Any) -> list[dict[str, str]]:
    """Parse real-time proxy imports from text, JSON arrays or object lists."""
    items: Iterable[Any]
    if isinstance(raw, str):
        if len(raw) > 256 * 1024:
            raise ValueError("代理导入内容不能超过 256KB")
        stripped = textwrap.dedent(raw).strip()
        if not stripped:
            return []
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                decoded = None
            if decoded is not None:
                raw = decoded
        if isinstance(raw, str) and (
            "type:" in stripped or "proxies:" in stripped or stripped.startswith("-")
        ):
            try:
                decoded = yaml.safe_load(stripped)
            except yaml.YAMLError as exc:
                raise ValueError(f"代理 YAML 格式无效: {exc}") from exc
            if decoded is not None:
                raw = decoded
        if isinstance(raw, str):
            items = [line.strip() for line in raw.splitlines() if line.strip()]
        elif isinstance(raw, list):
            items = raw
        elif isinstance(raw, Mapping):
            nested = raw.get("proxies") or raw.get("nodes")
            items = nested if isinstance(nested, list) else [raw]
        else:
            raise ValueError("代理导入内容必须是文本、数组或对象")
    elif isinstance(raw, list):
        items = raw
    elif isinstance(raw, Mapping):
        nested = raw.get("proxies") or raw.get("nodes")
        items = nested if isinstance(nested, list) else [raw]
    else:
        raise ValueError("代理导入内容必须是文本、数组或对象")

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        name = ""
        value = item
        if isinstance(item, str):
            stripped = item.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "|" in stripped:
                possible_name, possible_value = stripped.split("|", 1)
                if possible_name.strip() and possible_value.strip():
                    name = possible_name.strip()
                    value = possible_value.strip()
        elif isinstance(item, Mapping):
            name = str(item.get("name") or item.get("label") or "").strip()

        proxy_url = normalize_proxy_url(value)
        if proxy_url in seen:
            continue
        seen.add(proxy_url)
        results.append({"name": name or f"节点 {index}", "proxy_url": proxy_url})
    return results


def mask_proxy_url(value: str | None) -> str | None:
    """Hide proxy passwords while retaining enough information for operators."""
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() == "ss":
            node = parse_shadowsocks_url(value)
            host = node.server if ":" not in node.server else f"[{node.server}]"
            suffix = f"#{quote(node.name, safe='')}" if node.name else ""
            return f"ss://{node.method}@{host}:{node.port}{suffix}"
        if not parsed.password:
            return value
        user = quote(parsed.username or "", safe="")
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit(
            (parsed.scheme, f"{user}:***@{host}", parsed.path, parsed.query, parsed.fragment)
        )
    except Exception:
        return "***"


def proxy_argument_from_network(network: Mapping[str, Any]) -> Any:
    """Convert stored network policy into the httpx proxy sentinel/value."""
    mode = str(network.get("proxy_mode") or "inherit")
    if mode == "direct":
        return None
    if mode == "custom":
        return network.get("proxy_url") or None
    if mode == "group":
        proxy_url = network.get("proxy_url")
        if not proxy_url:
            raise ValueError("代理组没有可用节点")
        return proxy_url
    return ...
