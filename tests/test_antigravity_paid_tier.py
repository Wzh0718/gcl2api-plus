"""
额度查询顺带 paidTier 校准测试。

背景（2026-11-24 生产实测）：Google 从 2026-11-21 之后才开始给免费账号回填
paidTier，id="free-tier"、name="Antigravity Starter Quota"，其
upgradeSubscriptionText 写明 "Free users and Google AI Plus users receive the
minimum base limits"。也就是说 Starter Quota 是免费基础档的正式档位名，任何
没有 Google AI Pro 订阅的账号都会返回它。

早期实现把 `name == "Antigravity Starter Quota"` 直接当成「已被 Google 降档」，
于是所有免费号集体告警。而且库内 tier 列在三套存储里都是 DEFAULT 'pro'，
未判定的账号同样落 pro，不能作为「曾经付费」的证据。因此现在只输出实时档位
分类（kind）与「快照与实时不一致」（snapshot_conflict），不再输出 degraded。

覆盖：
- classify_paid_tier 的 free/paid/unknown 归类；
- fetch_paid_tier_info 的 kind/snapshot_conflict 判定、stored_tier 透传与失败兜底；
- _collect_quota_for_filename 读取库内快照并附带 paid_tier。
"""

import pytest

from src.api import antigravity as antigravity_module
from src.panel import creds as creds_module


# =====================================================================
# fetch_paid_tier_info
# =====================================================================


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _mock_load_code_assist(monkeypatch, response=None, exc=None):
    async def fake_post(path, headers, json_body=None, proxy_url=None, timeout=None):
        assert path == "/v1internal:loadCodeAssist"
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(antigravity_module, "_post_with_host_fallback", fake_post)


@pytest.mark.parametrize(
    ("tier_id", "tier_name", "expected"),
    [
        ("free-tier", "Antigravity Starter Quota", "free"),
        ("free-tier", None, "free"),
        (None, "Antigravity Starter Quota", "free"),
        ("g1-pro-tier", "Google AI Pro", "paid"),
        ("helium-tier", None, "paid"),
        (None, "Google AI Ultra", "paid"),
        ("weird-tier", "Something Else", "unknown"),
        (None, None, "unknown"),
    ],
)
def test_classify_paid_tier(tier_id, tier_name, expected):
    assert antigravity_module.classify_paid_tier(tier_id, tier_name) == expected


@pytest.mark.asyncio
async def test_paid_tier_starter_quota_with_pro_snapshot_flags_conflict(monkeypatch):
    """库内快照 pro 与实时免费档不一致：只标记冲突，不断言降档。"""
    _mock_load_code_assist(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "paidTier": {
                    "id": "free-tier",
                    "name": "Antigravity Starter Quota",
                }
            },
        ),
    )
    result = await antigravity_module.fetch_paid_tier_info("token", stored_tier="pro")
    assert result == {
        "id": "free-tier",
        "name": "Antigravity Starter Quota",
        "kind": "free",
        "snapshot_conflict": True,
        "stored_tier": "pro",
    }
    # 旧字段不再输出：tier 默认值不足以支撑「已被降档」的结论
    assert "degraded" not in result


@pytest.mark.asyncio
async def test_paid_tier_starter_quota_with_free_snapshot_no_conflict(monkeypatch):
    """库内快照本来就是 free：实时 Starter Quota 是常态。"""
    _mock_load_code_assist(
        monkeypatch,
        _FakeResponse(
            200,
            {"paidTier": {"id": "free-tier", "name": "Antigravity Starter Quota"}},
        ),
    )
    result = await antigravity_module.fetch_paid_tier_info("token", stored_tier="free")
    assert result["kind"] == "free"
    assert result["snapshot_conflict"] is False
    assert result["stored_tier"] == "free"


@pytest.mark.asyncio
async def test_paid_tier_starter_quota_without_snapshot_no_conflict(monkeypatch):
    """没有快照（未知来源）时不虚构冲突。"""
    _mock_load_code_assist(
        monkeypatch,
        _FakeResponse(
            200,
            {"paidTier": {"id": "free-tier", "name": "Antigravity Starter Quota"}},
        ),
    )
    result = await antigravity_module.fetch_paid_tier_info("token")
    assert result["snapshot_conflict"] is False
    assert result["stored_tier"] is None


@pytest.mark.asyncio
async def test_paid_tier_pro_marks_paid(monkeypatch):
    _mock_load_code_assist(
        monkeypatch,
        _FakeResponse(
            200,
            {"paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro"}},
        ),
    )
    result = await antigravity_module.fetch_paid_tier_info("token", stored_tier="pro")
    assert result == {
        "id": "g1-pro-tier",
        "name": "Google AI Pro",
        "kind": "paid",
        "snapshot_conflict": False,
        "stored_tier": "pro",
    }


@pytest.mark.asyncio
async def test_paid_tier_http_error_returns_none(monkeypatch):
    _mock_load_code_assist(monkeypatch, _FakeResponse(403, {"error": "denied"}, text="denied"))
    assert await antigravity_module.fetch_paid_tier_info("token") is None


@pytest.mark.asyncio
async def test_paid_tier_missing_paid_tier_returns_none(monkeypatch):
    _mock_load_code_assist(monkeypatch, _FakeResponse(200, {"currentTier": {"id": "free-tier"}}))
    assert await antigravity_module.fetch_paid_tier_info("token") is None


@pytest.mark.asyncio
async def test_paid_tier_empty_paid_tier_returns_none(monkeypatch):
    _mock_load_code_assist(monkeypatch, _FakeResponse(200, {"paidTier": {}}))
    assert await antigravity_module.fetch_paid_tier_info("token") is None


@pytest.mark.asyncio
async def test_paid_tier_exception_returns_none(monkeypatch):
    _mock_load_code_assist(monkeypatch, exc=RuntimeError("conn reset"))
    assert await antigravity_module.fetch_paid_tier_info("token") is None


# =====================================================================
# _collect_quota_for_filename 附带 paid_tier
# =====================================================================


class _FakeStorageAdapter:
    def __init__(self, credential_data, stored_tier="pro"):
        self._credential_data = credential_data
        self._stored_tier = stored_tier

    async def get_credential(self, filename, mode="antigravity"):
        return self._credential_data

    async def get_credential_state(self, filename, mode="antigravity"):
        return {"tier": self._stored_tier}

    async def resolve_credential_network(self, filename, mode="antigravity"):
        return None

    async def store_credential(self, filename, data, mode="antigravity"):
        return None


class _FakeCredentials:
    def __init__(self, data):
        self._data = data
        self.proxy_url = None

    @classmethod
    def from_dict(cls, data):
        return cls(data)

    async def refresh_if_needed(self):
        return False

    def to_dict(self):
        return self._data


def _mock_collect_deps(monkeypatch, paid_tier, stored_tier="pro"):
    credential_data = {"access_token": "token", "project_id": "proj"}
    captured = {}

    async def fake_get_storage_adapter():
        return _FakeStorageAdapter(credential_data, stored_tier)

    async def fake_fetch_quota_info(access_token, project_id=None, proxy_url=None, **kwargs):
        return {"success": True, "models": {"gemini-3-flash": {"remaining": 1}}, "groups": None}

    async def fake_fetch_paid_tier_info(access_token, proxy_url=None, **kwargs):
        captured.update(kwargs)
        return paid_tier

    monkeypatch.setattr(creds_module, "get_storage_adapter", fake_get_storage_adapter)
    monkeypatch.setattr(creds_module, "Credentials", _FakeCredentials)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(creds_module.credential_manager, "get_account_credentials",
                        AsyncMock(return_value=(credential_data, _FakeCredentials(credential_data))))
    monkeypatch.setattr(creds_module, "proxy_argument_from_network", lambda network: None)
    monkeypatch.setattr(creds_module, "fetch_quota_info", fake_fetch_quota_info)
    monkeypatch.setattr(creds_module, "fetch_paid_tier_info", fake_fetch_paid_tier_info)
    return captured


@pytest.mark.asyncio
async def test_collect_quota_attaches_paid_tier_and_passes_snapshot(monkeypatch):
    paid_tier = {
        "id": "free-tier",
        "name": "Antigravity Starter Quota",
        "kind": "free",
        "snapshot_conflict": True,
        "stored_tier": "pro",
    }
    captured = _mock_collect_deps(monkeypatch, paid_tier, stored_tier="pro")
    result = await creds_module._collect_quota_for_filename("a.json", "antigravity")
    assert result["success"] is True
    assert result["paid_tier"] == paid_tier
    # 库内快照必须透传给上游判定，否则分不清「降档」和「本来就是免费档」
    assert captured.get("stored_tier") == "pro"


@pytest.mark.asyncio
async def test_collect_quota_paid_tier_failure_keeps_success(monkeypatch):
    """loadCodeAssist 失败（paid_tier=None）不影响额度查询主流程。"""
    _mock_collect_deps(monkeypatch, None)
    result = await creds_module._collect_quota_for_filename("a.json", "antigravity")
    assert result["success"] is True
    assert result["paid_tier"] is None
    assert "gemini-3-flash" in result["models"]
