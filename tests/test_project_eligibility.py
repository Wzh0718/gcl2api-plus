"""项目发现失败时保留 Google 返回的账号资格原因。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import google_oauth_api as oauth


@pytest.mark.parametrize("include_credits", [False, True])
@pytest.mark.parametrize("reason", ["RESTRICTED_AGE", "VALIDATION_REQUIRED"])
async def test_project_discovery_reports_eligibility_reason(monkeypatch, include_credits, reason):
    response = SimpleNamespace(status_code=200, json=lambda: {
        "ineligibleTiers": [{"reasonCode": reason}],
    })
    monkeypatch.setattr(oauth, "post_async", AsyncMock(return_value=response))
    onboard = AsyncMock(return_value=None)
    monkeypatch.setattr(oauth, "_try_onboard_user", onboard)

    with pytest.raises(ValueError, match=reason) as error:
        await oauth.fetch_project_id_and_tier(
            "test-token", "test-ua", "https://example.test", include_credits=include_credits,
        )

    if reason == "RESTRICTED_AGE":
        assert "年龄验证" in str(error.value)
        assert "https://myaccount.google.com/age-verification" in str(error.value)
    else:
        assert "年龄验证" not in str(error.value)
    onboard.assert_not_awaited()


async def test_onboarding_tier_also_checks_age_restriction(monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {
        "ineligibleTiers": [{"reasonCode": "RESTRICTED_AGE"}],
    })
    monkeypatch.setattr(oauth, "post_async", AsyncMock(return_value=response))

    with pytest.raises(ValueError, match="RESTRICTED_AGE"):
        await oauth._get_onboard_tier("https://example.test", {})


async def test_existing_project_is_not_blocked_by_other_ineligible_tiers(monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {
        "currentTier": {"id": "free-tier"},
        "cloudaicompanionProject": "actual-project",
        "ineligibleTiers": [{"reasonCode": "RESTRICTED_AGE"}],
    })
    monkeypatch.setattr(oauth, "post_async", AsyncMock(return_value=response))

    project, _, _ = await oauth._try_load_code_assist("https://example.test", {})

    assert project == "actual-project"


async def test_onboarding_age_restriction_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(oauth, "_try_load_code_assist", AsyncMock(return_value=(None, None, None)))
    response = SimpleNamespace(status_code=200, json=lambda: {
        "ineligibleTiers": [{"reasonCode": "RESTRICTED_AGE"}],
    })
    monkeypatch.setattr(oauth, "post_async", AsyncMock(return_value=response))

    with pytest.raises(oauth.ProjectEligibilityError, match="年龄验证"):
        await oauth.fetch_project_id_and_tier("test-token", "test-ua", "https://example.test")


async def test_missing_project_without_reason_does_not_invent_age_restriction(monkeypatch):
    monkeypatch.setattr(oauth, "_try_load_code_assist", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(oauth, "_try_onboard_user", AsyncMock(return_value=None))

    assert await oauth.fetch_project_id_and_tier(
        "test-token", "test-ua", "https://example.test"
    ) == (None, None)


async def test_rt_login_preserves_age_verification_error(monkeypatch):
    from src import auth

    monkeypatch.setattr(auth, "refresh_access_token_with_clients", AsyncMock(
        return_value=({"access_token": "test-token"}, "antigravity_enterprise"),
    ))
    monkeypatch.setattr(auth, "get_user_info", AsyncMock(return_value={"email": "test@example.test"}))
    monkeypatch.setattr(auth, "get_antigravity_api_url", AsyncMock(return_value="https://example.test"))
    monkeypatch.setattr(auth, "fetch_project_id_and_tier", AsyncMock(
        side_effect=oauth.ProjectEligibilityError(["RESTRICTED_AGE"]),
    ))
    save = AsyncMock()
    monkeypatch.setattr(auth, "save_credentials", save)

    result = await auth.add_antigravity_account_by_refresh_token("test-refresh-token")

    assert result["success"] is False
    assert "RESTRICTED_AGE" in result["error"]
    assert "年龄验证" in result["error"]
    save.assert_not_awaited()
