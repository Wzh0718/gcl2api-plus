"""TOS 封禁 403 分类回归测试。

线上曾出现一个被 Google 以 TOS_VIOLATION 永久停用的账号被持续选中：
403 响应被分类为 unknown_403（不禁用、不重试、不冷却），10 小时内吞掉
34 万次请求且全部失败。这里锁定「明确条款封禁必须立即禁用账号」。
"""

from src.antigravity_error_classifier import classify_antigravity_403


TOS_ERROR = (
    '{"error":{"code":403,"message":"This service has been disabled in this account '
    'for violation of Terms of Service. Please submit an appeal to continue using '
    'this product.","status":"PERMISSION_DENIED","details":[{"@type":'
    '"type.googleapis.com/google.rpc.ErrorInfo","reason":"TOS_VIOLATION",'
    '"domain":"cloudcode-pa.googleapis.com","metadata":{"uiMessage":"true",'
    '"appeal_url":"https://forms.gle/hGzM9MEUv2azZsrb9"}}]}}'
)


def test_tos_violation_disables_account_even_when_eligibility_is_error():
    decision = classify_antigravity_403(
        TOS_ERROR,
        account_health={"binding_status": "healthy", "eligibility_status": "error"},
    )

    assert decision.category == "account_forbidden"
    assert decision.disable_account is True


def test_tos_violation_disables_account_without_health_snapshot():
    decision = classify_antigravity_403(TOS_ERROR)

    assert decision.category == "account_forbidden"
    assert decision.disable_account is True


def test_vague_forbidden_stays_conservative():
    decision = classify_antigravity_403("Forbidden")

    assert decision.category == "unknown_403"
    assert decision.disable_account is False
    assert decision.invalidate_binding is False
    assert decision.cooldown_model is False


def test_tos_decision_is_explicitly_permanent():
    assert classify_antigravity_403(TOS_ERROR).tos_violation is True


def test_non_tos_account_permission_is_not_marked_as_tos():
    decision = classify_antigravity_403(
        "The authenticated account does not have permission",
        account_health={"binding_status": "healthy", "eligibility_status": "eligible"},
    )
    assert decision.category == "account_forbidden"
    assert decision.tos_violation is False


async def test_tos_disables_account_even_when_auto_ban_is_disabled(monkeypatch):
    from unittest.mock import AsyncMock
    from src.api import antigravity

    class Manager:
        def __init__(self):
            self.health = {}
            self.disabled = []

        async def get_antigravity_account_health(self, filename):
            return dict(self.health)

        async def update_antigravity_account_health(self, filename, **updates):
            self.health.update(updates)

        async def set_cred_disabled(self, filename, disabled, mode):
            self.disabled.append((filename, disabled, mode))
            return True

    manager = Manager()
    auto_ban = AsyncMock(return_value=False)
    monkeypatch.setattr(antigravity, "credential_manager", manager)
    monkeypatch.setattr(antigravity, "check_should_auto_ban", auto_ban)

    result = await antigravity._handle_antigravity_403(
        credential_name="tos.json",
        credential_data={},
        model_name="gemini-2.5-flash",
        error_text=TOS_ERROR,
    )

    assert manager.disabled == [("tos.json", True, "antigravity")]
    assert manager.health["last_403_category"] == "account_forbidden"
    assert result.should_retry is True
    auto_ban.assert_not_awaited()
