"""Shadowsocks SOCKS5 桥接：复用监听器与解析行为回归测试。"""

import asyncio

from src.shadowsocks import (
    ShadowsocksBridge,
    close_shared_bridges,
    parse_shadowsocks_url,
    shadowsocks_proxy_context,
)


SS_URL = "ss://YWVzLTEyOC1nY206cGFzc3dvcmQ=@127.0.0.1:8388#node"


def test_parse_shadowsocks_url():
    node = parse_shadowsocks_url(SS_URL)
    assert node.server == "127.0.0.1"
    assert node.port == 8388
    assert node.method == "aes-128-gcm"
    assert node.password == "password"
    assert node.name == "node"


async def test_non_ss_proxy_passes_through():
    async with shadowsocks_proxy_context("socks5h://127.0.0.1:1080") as url:
        assert url == "socks5h://127.0.0.1:1080"
    async with shadowsocks_proxy_context(None) as url:
        assert url is None


async def test_shared_bridge_reuses_single_listener():
    """同一 ss:// 节点在高并发请求间必须复用同一个本地监听器。

    旧实现每个 httpx 客户端各开一个 127.0.0.1 监听器，高并发下 LISTEN fd
    会随在途请求线性增长，低 nofile 环境会打满文件描述符。
    """
    urls = await asyncio.gather(
        *(shadowsocks_proxy_context(SS_URL).__aenter__() for _ in range(20))
    )
    assert len(set(urls)) == 1

    # 手工创建的桥接实例独立监听，不进入共享缓存
    bridge = ShadowsocksBridge(parse_shadowsocks_url(SS_URL))
    await bridge.start()
    assert bridge.url not in urls
    await bridge.stop()

    await close_shared_bridges()

    # 关闭缓存后重新建立监听器（旧缓存不残留）
    async with shadowsocks_proxy_context(SS_URL) as fresh_url:
        assert fresh_url not in urls
    await close_shared_bridges()
