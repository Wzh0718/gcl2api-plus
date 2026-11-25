"""额度/档位低频对账：租约、优先级、tier 写回。"""

import time

import pytest

import config
import src.antigravity_quota_sweep as sweep
from src import antigravity_quota_sweep as sweep_module


class FakeBackend:
    def __init__(self):
        self.cleared = []

    async def set_model_cooldown(self, filename, model_name, cooldown_until, mode="antigravity"):
        assert cooldown_until is None
        self.cleared.append((filename, model_name))


class FakeAdapter:
    def __init__(self, backend):
        self._backend = backend


class FakeManager:
    def __init__(self, states):
        self.states = states
        self.backend = FakeBackend()
        self._storage_adapter = FakeAdapter(self.backend)
        self.probed = []
        self.state_updates = []

    async def get_creds_status(self, mode="antigravity"):
        return self.states

    async def get_account_credentials(self, filename, mode="antigravity"):
        self.probed.append(filename)
        return {"access_token": "tok", "project_id": "proj"}, type(
            "Creds", (), {"proxy_url": None}
        )()

    async def update_credential_state(self, filename, updates, mode="antigravity"):
        self.state_updates.append((filename, dict(updates)))
        return True


def _patch_storage(monkeypatch, backend):
    async def _adapter():
        return FakeAdapter(backend)

    monkeypatch.setattr("src.storage_adapter.get_storage_adapter", _adapter)


@pytest.fixture(autouse=True)
def _patch_environment(monkeypatch):
    sweep_module.reset_memory_leases()
    monkeypatch.setattr(sweep_module, "_redis", lambda: _none())

    async def _none():
        return None

    async def batch():
        return 2

    async def account_interval():
        return 3600

    monkeypatch.setattr(config, "get_antigravity_quota_sweep_batch", batch)
    monkeypatch.setattr(
        config, "get_antigravity_quota_sweep_account_interval_seconds", account_interval
    )
    yield
    sweep_module.reset_memory_leases()


def _weekly(remaining):
    return [
        {
            "displayName": "Gemini Models",
            "buckets": [
                {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": remaining},
                {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": remaining},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_sweep_prefers_cooled_account_and_clears(monkeypatch):
    """冷却中的账号优先探测；上游额度已恢复时清冷却。"""
    now = time.time()
    states = {
        "hot.json": {
            "disabled": False,
            "tier": "pro",
            "model_cooldowns": {"gemini-3.1-flash-image": now + 3600},
            "last_success": now,
        },
        "idle.json": {"disabled": False, "tier": "pro", "model_cooldowns": {}, "last_success": now},
    }
    manager = FakeManager(states)
    monkeypatch.setattr("src.credential_manager.credential_manager", manager)
    _patch_storage(monkeypatch, manager.backend)

    async def fake_paid(token, **kwargs):
        return {"id": "g1-pro-tier", "name": "Google AI Pro", "degraded": False}

    async def fake_quota(token, **kwargs):
        return {"success": True, "models": {}, "groups": _weekly(1.0)}

    monkeypatch.setattr("src.api.antigravity.fetch_paid_tier_info", fake_paid)
    monkeypatch.setattr("src.api.antigravity.fetch_quota_info", fake_quota)

    stats = await sweep.sweep_once(batch=1)

    assert manager.probed == ["hot.json"]
    assert manager.backend.cleared == [("hot.json", "gemini-3.1-flash-image")]
    assert stats["scanned"] == 1


@pytest.mark.asyncio
async def test_sweep_writes_tier_change(monkeypatch):
    """paidTier 降档为 Starter 时把 tier 写回 free。"""
    states = {"a.json": {"disabled": False, "tier": "pro", "model_cooldowns": {}, "last_success": 1}}
    manager = FakeManager(states)
    monkeypatch.setattr("src.credential_manager.credential_manager", manager)
    _patch_storage(monkeypatch, manager.backend)

    async def fake_paid(token, **kwargs):
        return {"id": "free-tier", "name": "Antigravity Starter Quota", "degraded": True}

    monkeypatch.setattr("src.api.antigravity.fetch_paid_tier_info", fake_paid)

    await sweep.sweep_once(batch=1)

    assert manager.state_updates == [("a.json", {"tier": "free"})]
    assert manager.backend.cleared == []


@pytest.mark.asyncio
async def test_sweep_lease_blocks_repeat_within_interval(monkeypatch):
    """同一账号在租约期内不会被第二轮重复探测。"""
    states = {"a.json": {"disabled": False, "tier": "pro", "model_cooldowns": {}, "last_success": 1}}
    manager = FakeManager(states)
    monkeypatch.setattr("src.credential_manager.credential_manager", manager)
    _patch_storage(monkeypatch, manager.backend)

    async def fake_paid(token, **kwargs):
        return {"id": "g1-pro-tier", "name": "Google AI Pro", "degraded": False}

    monkeypatch.setattr("src.api.antigravity.fetch_paid_tier_info", fake_paid)

    first = await sweep.sweep_once(batch=2)
    second = await sweep.sweep_once(batch=2)

    assert first["scanned"] == 1
    assert second["scanned"] == 0
    assert manager.probed == ["a.json"]


@pytest.mark.asyncio
async def test_sweep_skips_disabled_and_forbidden(monkeypatch):
    """禁用与条款封禁账号不进入候选。"""
    states = {
        "off.json": {"disabled": True, "tier": "pro", "model_cooldowns": {}},
        "tos.json": {"disabled": False, "tier": "pro", "warmup_status": "account_forbidden", "model_cooldowns": {}},
    }
    manager = FakeManager(states)
    monkeypatch.setattr("src.credential_manager.credential_manager", manager)
    _patch_storage(monkeypatch, manager.backend)

    stats = await sweep.sweep_once(batch=2)

    assert stats["scanned"] == 0
    assert manager.probed == []


@pytest.mark.asyncio
async def test_sweep_survives_single_account_failure(monkeypatch):
    """单账号探测异常不打断整轮。"""
    states = {
        "bad.json": {"disabled": False, "tier": "pro", "model_cooldowns": {}, "last_success": 2},
        "good.json": {"disabled": False, "tier": "pro", "model_cooldowns": {}, "last_success": 1},
    }
    manager = FakeManager(states)
    monkeypatch.setattr("src.credential_manager.credential_manager", manager)
    _patch_storage(monkeypatch, manager.backend)

    async def fake_paid(token, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("src.api.antigravity.fetch_paid_tier_info", fake_paid)

    stats = await sweep.sweep_once(batch=2)

    assert stats["scanned"] == 2
    assert stats["error"] == 2
