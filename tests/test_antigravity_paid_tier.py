"""
额度查询顺带 paidTier 校准（检出「已被降档」）测试。

背景：凭证库 tier 是导入时快照；账号被 Google 降档后 paidTier 变为
"Antigravity Starter Quota"（仅周限额、无 5h 窗口）。额度查询返回需携带
paid_tier = {"id", "name", "degraded"}，供面板标红校准。

覆盖：
- fetch_paid_tier_info 的降档检出与失败兜底（best-effort 返回 None）；
- _collect_quota_for_filename 在额度结果上附带 paid_tier。
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


@pytest.mark.asyncio
async def test_paid_tier_starter_quota_marked_degraded(monkeypatch):
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
    result = await antigravity_module.fetch_paid_tier_info("token")
    assert result == {
        "id": "free-tier",
        "name": "Antigravity Starter Quota",
        "degraded": True,
    }


@pytest.mark.asyncio
async def test_paid_tier_pro_not_degraded(monkeypatch):
    _mock_load_code_assist(
        monkeypatch,
        _FakeResponse(
            200,
            {"paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro"}},
        ),
    )
    result = await antigravity_module.fetch_paid_tier_info("token")
    assert result == {"id": "g1-pro-tier", "name": "Google AI Pro", "degraded": False}


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
    def __init__(self, credential_data):
        self._credential_data = credential_data

    async def get_credential(self, filename, mode="antigravity"):
        return self._credential_data

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


def _mock_collect_deps(monkeypatch, paid_tier):
    credential_data = {"access_token": "token", "project_id": "proj"}

    async def fake_get_storage_adapter():
        return _FakeStorageAdapter(credential_data)

    async def fake_fetch_quota_info(access_token, project_id=None, proxy_url=None):
        return {"success": True, "models": {"gemini-3-flash": {"remaining": 1}}, "groups": None}

    async def fake_fetch_paid_tier_info(access_token, proxy_url=None):
        return paid_tier

    monkeypatch.setattr(creds_module, "get_storage_adapter", fake_get_storage_adapter)
    monkeypatch.setattr(creds_module, "Credentials", _FakeCredentials)
    monkeypatch.setattr(creds_module, "proxy_argument_from_network", lambda network: None)
    monkeypatch.setattr(creds_module, "fetch_quota_info", fake_fetch_quota_info)
    monkeypatch.setattr(creds_module, "fetch_paid_tier_info", fake_fetch_paid_tier_info)


@pytest.mark.asyncio
async def test_collect_quota_attaches_paid_tier(monkeypatch):
    degraded = {"id": "free-tier", "name": "Antigravity Starter Quota", "degraded": True}
    _mock_collect_deps(monkeypatch, degraded)
    result = await creds_module._collect_quota_for_filename("a.json", "antigravity")
    assert result["success"] is True
    assert result["paid_tier"] == degraded


@pytest.mark.asyncio
async def test_collect_quota_paid_tier_failure_keeps_success(monkeypatch):
    """loadCodeAssist 失败（paid_tier=None）不影响额度查询主流程。"""
    _mock_collect_deps(monkeypatch, None)
    result = await creds_module._collect_quota_for_filename("a.json", "antigravity")
    assert result["success"] is True
    assert result["paid_tier"] is None
    assert "gemini-3-flash" in result["models"]
