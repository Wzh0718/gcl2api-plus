"""地区资格拒绝与代理绑定故障的处理。"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.antigravity_error_classifier import classify_antigravity_403


LOCATION_BODY = (
    '{"error":{"code":403,"status":"PERMISSION_DENIED","message":'
    '"Request is not allowed for this project",'
    '"details":[{"reason":"UNSUPPORTED_LOCATION"}]}}'
)


def test_unsupported_location_code_is_a_region_rejection():
    decision = classify_antigravity_403(LOCATION_BODY)

    assert decision.category == "geo_blocked"
    assert decision.invalidate_binding is False
    assert decision.retry_after_rebind is False
    assert decision.disable_account is False


@pytest.mark.parametrize(
    "text",
    [
        "LOCATION_RESTRICTED",
        "location_not_supported",
        "unsupported_region",
    ],
)
def test_location_code_variants_are_recognized(text):
    assert classify_antigravity_403(text).category == "geo_blocked"


def test_400_classifier_accepts_location_reason_code():
    from src.antigravity_400_classifier import classify_antigravity_400

    decision = classify_antigravity_400(LOCATION_BODY)

    assert decision.category == "geo_blocked"
    assert decision.reason == "LOCATION_RESTRICTED"
    assert decision.disable_account is False


class _FakeStorage:
    def __init__(
        self,
        *,
        network,
        rotated=True,
        health=None,
        checks=None,
    ):
        self.network = network
        self.rotated = rotated
        self.health = health or {
            "credential_name": "a.json",
            "binding_status": "unchecked",
            "eligibility_status": "unchecked",
        }
        self.checks = list(checks or [])
        self.invalidations = []

    async def get_credential_network(self, filename, mode="antigravity"):
        return dict(self.network)

    async def invalidate_antigravity_proxy_binding(
        self, filename, cooldown_seconds=300, *, require_replacement=False
    ):
        self.invalidations.append((filename, cooldown_seconds, require_replacement))
        if not self.rotated:
            return False
        self.network = {
            **self.network,
            "bound_proxy_node_id": 2,
            "proxy_node_id": 2,
            "proxy_url": "http://proxy-b.example:8080",
        }
        return True

    async def get_antigravity_account_health(self, filename):
        return dict(self.health)

    async def resolve_credential_network(self, filename, mode="antigravity"):
        return dict(self.network)


def _manager(storage):
    from src.credential_manager import CredentialManager

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = storage
    manager.check_antigravity_account_health = AsyncMock(
        side_effect=storage.checks or None
    )
    return manager


@pytest.mark.asyncio
async def test_group_account_rotates_to_another_free_node():
    storage = _FakeStorage(
        network={
            "proxy_mode": "group",
            "proxy_group_id": 7,
            "bound_proxy_node_id": 1,
            "proxy_url": "http://proxy-a.example:8080",
        }
    )
    manager = _manager(storage)

    rotated = await manager.rotate_antigravity_egress("a.json")

    assert rotated is not None
    assert storage.invalidations == [("a.json", 600, True)]
    assert storage.network["bound_proxy_node_id"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "network",
    [
        {"proxy_mode": "custom", "proxy_url": "http://proxy-a.example:8080"},
        {"proxy_mode": "direct"},
        {
            "proxy_mode": "group",
            "proxy_group_id": 7,
            "fixed_proxy_ip": "203.0.113.9",
        },
        {"proxy_mode": "group", "proxy_group_id": None},
    ],
)
async def test_non_group_bindings_are_not_rotated(network):
    storage = _FakeStorage(network=network)
    manager = _manager(storage)

    assert await manager.rotate_antigravity_egress("a.json") is None
    assert storage.invalidations == []


@pytest.mark.asyncio
async def test_exhausted_group_returns_none_instead_of_failing_the_request_path():
    storage = _FakeStorage(
        network={
            "proxy_mode": "group",
            "proxy_group_id": 7,
            "bound_proxy_node_id": 1,
        },
        rotated=False,
    )
    manager = _manager(storage)

    assert await manager.rotate_antigravity_egress("a.json") is None


@pytest.mark.parametrize("binding", ["proxy_drift", "proxy_failed"])
def test_explicit_location_refusal_is_terminal_even_with_stale_binding_error(binding):
    decision = classify_antigravity_403(LOCATION_BODY, account_health={"binding_status": binding})
    assert decision.category == "geo_blocked"
    assert not decision.retry_after_rebind
    assert not decision.invalidate_binding


@pytest.mark.asyncio
async def test_reload_returns_the_new_egress_for_same_account_retry():
    storage = _FakeStorage(
        network={
            "proxy_mode": "group",
            "proxy_group_id": 7,
            "bound_proxy_node_id": 2,
            "proxy_url": "http://proxy-b.example:8080",
        }
    )
    manager = _manager(storage)
    manager._storage_adapter.get_credential = AsyncMock(
        return_value={
            "access_token": "token",
            "project_id": "project",
            "proxy_mode": "group",
            "proxy_group_id": 7,
        }
    )

    fresh = await manager.reload_antigravity_credential("a.json")

    assert fresh["access_token"] == "token"
    assert fresh["proxy_url"] == "http://proxy-b.example:8080"
    assert fresh["bound_proxy_node_id"] == 2


@pytest.mark.asyncio
async def test_request_helper_reports_rebound_credential(monkeypatch):
    from src.api import antigravity

    manager = SimpleNamespace(
        rotate_antigravity_egress=AsyncMock(return_value={"binding_status": "unchecked"}),
        reload_antigravity_credential=AsyncMock(
            return_value={
                "access_token": "token",
                "project_id": "project",
                "proxy_url": "http://proxy-b.example:8080",
            }
        ),
    )
    monkeypatch.setattr(antigravity, "credential_manager", manager)

    fresh = await antigravity._retry_same_account_on_new_egress(
        "a.json", {"access_token": "token"}
    )

    assert fresh["proxy_url"] == "http://proxy-b.example:8080"
    manager.rotate_antigravity_egress.assert_awaited_once_with(
        "a.json"
    )


@pytest.mark.asyncio
async def test_request_helper_returns_none_when_no_free_node(monkeypatch):
    from src.api import antigravity

    manager = SimpleNamespace(
        rotate_antigravity_egress=AsyncMock(return_value=None),
        reload_antigravity_credential=AsyncMock(return_value={"access_token": "token"}),
    )
    monkeypatch.setattr(antigravity, "credential_manager", manager)

    assert (
        await antigravity._retry_same_account_on_new_egress(
            "a.json", {"access_token": "token"}
        )
        is None
    )
    manager.reload_antigravity_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_geo_403_does_not_disable_or_pause_the_account(monkeypatch):
    from src.api import antigravity

    class _Manager:
        def __init__(self):
            self.disabled = []
            self.paused = []
            self.health = {"binding_status": "healthy", "eligibility_status": "eligible"}

        async def get_antigravity_account_health(self, name):
            return dict(self.health)

        async def update_antigravity_account_health(self, name, **updates):
            self.health.update(updates)
            return dict(self.health)

        async def set_cred_disabled(self, name, disabled, mode="geminicli"):
            self.disabled.append((name, disabled))

    manager = _Manager()
    monkeypatch.setattr(antigravity, "credential_manager", manager)
    monkeypatch.setattr(
        antigravity, "check_should_auto_ban", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        antigravity, "note_dynamic_warmup_outcome",
        lambda name, outcome: manager.paused.append((name, outcome)),
    )

    result = await antigravity._handle_antigravity_403(
        credential_name="a.json",
        credential_data={"access_token": "token"},
        model_name="gemini-test",
        error_text=LOCATION_BODY,
    )

    assert result.decision.category == "geo_blocked"
    assert result.rebind_retry is False
    assert manager.disabled == []
    assert manager.paused == []
    assert manager.health["eligibility_status"] == "geo_blocked"


@pytest.mark.asyncio
async def test_gate_resolves_unbound_pg_selection_instead_of_raising(monkeypatch):
    """PostgreSQL 的领取结果在「组内尚未绑定节点」时 network 为 None。

    门禁必须回退到 resolve_credential_network 绑定一个空闲节点，而不是
    在这个快照上取字段（此前会 AttributeError）。
    """
    import config
    from src.credential_manager import CredentialManager
    from src.storage.sqlite_manager import SelectedAntigravityCredential

    manager = CredentialManager()
    manager._initialized = True
    resolved = {
        "proxy_mode": "group",
        "proxy_group_id": 7,
        "proxy_url": "http://proxy-a.example:8080",
        "proxy_node_id": 1,
        "bound_proxy_node_id": 1,
        "exclusive_assignment": False,
    }
    manager._storage_adapter = SimpleNamespace(
        resolve_credential_network=AsyncMock(return_value=resolved),
        get_antigravity_account_health=AsyncMock(),
    )
    monkeypatch.setattr(
        config, "get_antigravity_network_check_enabled", AsyncMock(return_value=False)
    )

    selected = SelectedAntigravityCredential(
        {"access_token": "token", "project_id": "project"}, "a.json", None, None
    )
    ready = await manager.ensure_antigravity_account_ready("a.json", selected)

    assert ready is not None
    assert ready["proxy_url"] == "http://proxy-a.example:8080"
    manager._storage_adapter.resolve_credential_network.assert_awaited_once_with(
        "a.json", mode="antigravity"
    )


@pytest.mark.asyncio
async def test_unbound_account_does_not_get_a_new_exit_after_denial():
    storage = _FakeStorage(network={"proxy_mode": "group", "proxy_group_id": 7}, rotated=False)
    storage.resolve_credential_network = AsyncMock()
    assert await _manager(storage).rotate_antigravity_egress("a.json") is None
    storage.resolve_credential_network.assert_not_awaited()
