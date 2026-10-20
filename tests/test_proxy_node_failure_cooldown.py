"""代理节点失联自愈：节点冷却 + 账号解绑回归测试。"""

import pytest

from src.credential_manager import (
    PROXY_NODE_FAILURE_COOLDOWN_SECONDS,
    CredentialManager,
)


class _StubNetworkAdapter:
    def __init__(self, proxy_mode="group", proxy_url="ss://x@1.2.3.4:1081#node"):
        self.proxy_mode = proxy_mode
        self.proxy_url = proxy_url
        self.invalidate_calls = []

    async def get_credential_network(self, filename, mode="antigravity"):
        return {"proxy_mode": self.proxy_mode, "proxy_url": self.proxy_url}

    async def invalidate_antigravity_proxy_binding(self, filename, cooldown_seconds=300):
        self.invalidate_calls.append((filename, cooldown_seconds))
        return True


def _manager_with(adapter):
    manager = CredentialManager()
    manager._storage_adapter = adapter
    manager._initialized = True
    return manager


@pytest.mark.parametrize(
    "error_msg",
    [
        "刷新请求异常 (client=antigravity_enterprise): Proxy Server could not connect: General SOCKS server failure.",
        "ConnectError: All connection attempts failed",
        "ReadTimeout: timed out",
    ],
)
async def test_connection_errors_cool_group_node(error_msg):
    adapter = _StubNetworkAdapter()
    manager = _manager_with(adapter)
    cooled = await manager._cool_dead_proxy_node("acc.json", error_msg)
    assert cooled is True
    assert adapter.invalidate_calls == [
        ("acc.json", PROXY_NODE_FAILURE_COOLDOWN_SECONDS)
    ]


async def test_http_status_errors_do_not_cool_node():
    adapter = _StubNetworkAdapter()
    manager = _manager_with(adapter)
    # 429/5xx 属于上游限流/服务错误，由账号级冷却处理，节点应保持可用
    assert await manager._cool_dead_proxy_node("acc.json", "刷新请求异常: HTTP 429") is False
    assert adapter.invalidate_calls == []


async def test_direct_accounts_are_not_affected():
    adapter = _StubNetworkAdapter(proxy_mode="inherit")
    manager = _manager_with(adapter)
    assert (
        await manager._cool_dead_proxy_node(
            "acc.json", "Proxy Server could not connect: General SOCKS server failure."
        )
        is False
    )
    assert adapter.invalidate_calls == []
