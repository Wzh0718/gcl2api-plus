"""选号失败时的冷却探测：官方额度恢复后不再被自家冷却挡住。"""

import time

import pytest

from src.api import antigravity as ag

MODEL = "gemini-3.1-flash-image"


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
    def __init__(self, states, backend):
        self._states = states
        self._storage_adapter = FakeAdapter(backend)
        self.status_calls = 0
        self.credential_calls = []

    async def get_creds_status(self, mode="antigravity"):
        self.status_calls += 1
        return self._states

    async def get_account_credentials(self, filename, mode="antigravity"):
        self.credential_calls.append(filename)
        return {"access_token": "tok", "project_id": "proj"}, type(
            "Creds", (), {"proxy_url": None}
        )()


def _groups(remaining, *, five_hour=None):
    buckets = [
        {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": remaining}
    ]
    if five_hour is not None:
        buckets.append(
            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": five_hour}
        )
    return [{"displayName": "Gemini Models", "buckets": buckets}]


def _patch_storage(monkeypatch, backend):
    async def _adapter():
        return FakeAdapter(backend)

    monkeypatch.setattr("src.storage_adapter.get_storage_adapter", _adapter)


@pytest.fixture(autouse=True)
def _reset_probe_state():
    ag.reset_cooldown_probe_state()
    yield
    ag.reset_cooldown_probe_state()


@pytest.mark.asyncio
async def test_probe_clears_weekly_recovered_cooldown(monkeypatch):
    """周额度已有剩余 → 长冷却失去依据，探测后清除。"""
    backend = FakeBackend()
    states = {
        "c.json": {"disabled": False, "model_cooldowns": {MODEL: time.time() + 128 * 3600}}
    }
    manager = FakeManager(states, backend)
    monkeypatch.setattr(ag, "credential_manager", manager)
    _patch_storage(monkeypatch, backend)

    async def fake_quota(token, **kwargs):
        return {"success": True, "models": {}, "groups": _groups(1.0, five_hour=1.0)}

    monkeypatch.setattr(ag, "fetch_quota_info", fake_quota)

    cleared = await ag.release_stale_model_cooldowns(MODEL)

    assert cleared == 1
    assert backend.cleared == [("c.json", MODEL)]
    assert manager.credential_calls == ["c.json"]


@pytest.mark.asyncio
async def test_probe_keeps_cooldown_when_weekly_still_empty(monkeypatch):
    """周额度仍为 0 → 冷却保留，不做无依据的清除。"""
    backend = FakeBackend()
    states = {
        "c.json": {"disabled": False, "model_cooldowns": {MODEL: time.time() + 128 * 3600}}
    }
    manager = FakeManager(states, backend)
    monkeypatch.setattr(ag, "credential_manager", manager)
    _patch_storage(monkeypatch, backend)

    async def fake_quota(token, **kwargs):
        return {"success": True, "models": {}, "groups": _groups(0.0, five_hour=0.0)}

    monkeypatch.setattr(ag, "fetch_quota_info", fake_quota)

    cleared = await ag.release_stale_model_cooldowns(MODEL)

    assert cleared == 0
    assert backend.cleared == []


@pytest.mark.asyncio
async def test_probe_skips_disabled_forbidden_and_uncooled(monkeypatch):
    """禁用、条款封禁、无冷却、冷却已到期的账号都不应该被探测。"""
    backend = FakeBackend()
    now = time.time()
    states = {
        "disabled.json": {"disabled": True, "model_cooldowns": {MODEL: now + 3600}},
        "forbidden.json": {
            "disabled": False,
            "warmup_status": "account_forbidden",
            "model_cooldowns": {MODEL: now + 3600},
        },
        "free.json": {"disabled": False, "model_cooldowns": {}},
        "expired.json": {"disabled": False, "model_cooldowns": {MODEL: now - 10}},
        "other-model.json": {"disabled": False, "model_cooldowns": {"gemini-2.5-pro": now + 3600}},
    }
    manager = FakeManager(states, backend)
    monkeypatch.setattr(ag, "credential_manager", manager)
    _patch_storage(monkeypatch, backend)

    async def fake_quota(token, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("没有候选时不应访问上游")

    monkeypatch.setattr(ag, "fetch_quota_info", fake_quota)

    cleared = await ag.release_stale_model_cooldowns(MODEL)

    assert cleared == 0
    assert manager.credential_calls == []


@pytest.mark.asyncio
async def test_probe_runs_at_most_twice_and_is_throttled(monkeypatch):
    """多账号时只探最接近到期的两个；同一轮之后立刻再调用被节流。"""
    backend = FakeBackend()
    now = time.time()
    states = {
        "a.json": {"disabled": False, "model_cooldowns": {MODEL: now + 60}},
        "b.json": {"disabled": False, "model_cooldowns": {MODEL: now + 120}},
        "c.json": {"disabled": False, "model_cooldowns": {MODEL: now + 180}},
    }
    manager = FakeManager(states, backend)
    monkeypatch.setattr(ag, "credential_manager", manager)
    _patch_storage(monkeypatch, backend)

    async def fake_quota(token, **kwargs):
        return {"success": True, "models": {}, "groups": _groups(1.0, five_hour=1.0)}

    monkeypatch.setattr(ag, "fetch_quota_info", fake_quota)

    cleared = await ag.release_stale_model_cooldowns(MODEL)
    assert cleared == 2
    assert manager.credential_calls == ["a.json", "b.json"]

    again = await ag.release_stale_model_cooldowns(MODEL)
    assert again == 0
    assert manager.status_calls == 1


@pytest.mark.asyncio
async def test_probe_survives_quota_failure(monkeypatch):
    """探测接口失败时保留冷却并正常返回，不向上抛。"""
    backend = FakeBackend()
    states = {"c.json": {"disabled": False, "model_cooldowns": {MODEL: time.time() + 3600}}}
    manager = FakeManager(states, backend)
    monkeypatch.setattr(ag, "credential_manager", manager)
    _patch_storage(monkeypatch, backend)

    async def fake_quota(token, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(ag, "fetch_quota_info", fake_quota)

    assert await ag.release_stale_model_cooldowns(MODEL) == 0
    assert backend.cleared == []
