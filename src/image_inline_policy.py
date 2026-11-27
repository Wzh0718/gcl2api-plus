"""远程图片内联策略。

策略键是**来源 IP**（客户端），不是图片站点：
命中白名单的调用方，其请求里的第三方 http(s) 图片一律下载后转 inlineData；
未命中的调用方保持 fileData 透传，由上游自行抓取。
"""

from __future__ import annotations

import ipaddress
from typing import Iterable

from src.client_ip import client_ip

MAX_ENTRY_LENGTH = 64


class InlinePolicyError(ValueError):
    """Invalid inline policy configuration supplied by the panel."""


def parse_entries(values: object) -> list:
    """Parse IP / CIDR entries from a list or a comma/space/newline separated string.

    ``#`` 起始视为注释；空条目忽略；非法条目抛 :class:`InlinePolicyError`
    并带上原文，方便面板直接回显给用户。
    """
    if values is None:
        return []
    if isinstance(values, str):
        raw: Iterable[object] = values.replace("\n", ",").replace("\r", ",").split(",")
    elif isinstance(values, (list, tuple, set)):
        raw = values
    else:
        raise InlinePolicyError("远程图片内联 IP 列表必须是字符串或字符串数组")

    entries = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            raise InlinePolicyError("远程图片内联 IP 列表只能包含字符串")
        text = item.strip()
        if not text or text.startswith("#"):
            continue
        for token in text.split():
            if not token or token.startswith("#"):
                break
            if len(token) > MAX_ENTRY_LENGTH:
                raise InlinePolicyError(f"IP 条目过长：{token[:MAX_ENTRY_LENGTH]}…")
            try:
                network = ipaddress.ip_network(token, strict=False)
            except ValueError:
                raise InlinePolicyError(f"不是合法的 IP 或网段：{token}") from None
            key = str(network)
            if key not in seen:
                seen.add(key)
                entries.append(network)
    return entries


def matches(entries: Iterable, address: str | None) -> bool:
    if not address:
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    for network in entries:
        if parsed.version == network.version and parsed in network:
            return True
    return False


# 每个请求都会判定一次，列表稳定时复用解析结果；最多缓存 4 份不同配置。
_ENTRY_CACHE: dict[tuple, list] = {}


def _cached_entries(values: object) -> list:
    key = tuple(values) if isinstance(values, (list, tuple)) else (values,)
    entries = _ENTRY_CACHE.get(key)
    if entries is None:
        entries = parse_entries(values)
        if len(_ENTRY_CACHE) >= 4:
            _ENTRY_CACHE.clear()
        _ENTRY_CACHE[key] = entries
    return entries


async def should_inline_remote_images() -> bool:
    """Decide whether this request's remote images should be downloaded and inlined."""
    import config

    mode = config.get_remote_image_inline_mode_sync()
    if mode == "off":
        return False
    if mode == "all":
        return True
    entries = _cached_entries(config.get_remote_image_inline_ips_sync())
    return matches(entries, client_ip.get())


def validate_settings(new_config: dict) -> None:
    """Normalize inline policy keys in a panel save payload; raise on invalid input."""
    import config

    mode = new_config.get("remote_image_inline_mode")
    if mode is not None:
        if not isinstance(mode, str) or mode not in config.REMOTE_IMAGE_INLINE_MODES:
            raise InlinePolicyError("远程图片内联模式必须是 all、ip_list 或 off")
        new_config["remote_image_inline_mode"] = mode

    if "remote_image_inline_ips" in new_config:
        entries = parse_entries(new_config["remote_image_inline_ips"])
        if len(entries) > config.MAX_REMOTE_IMAGE_INLINE_IPS:
            raise InlinePolicyError(
                f"远程图片内联 IP 列表最多 {config.MAX_REMOTE_IMAGE_INLINE_IPS} 条"
            )
        new_config["remote_image_inline_ips"] = [str(network) for network in entries]

    trust = new_config.get("remote_image_inline_trust_forwarded")
    if trust is not None and not isinstance(trust, bool):
        raise InlinePolicyError("信任 X-Forwarded-For 开关必须是布尔值")
