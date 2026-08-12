import importlib
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class AntigravityUserAgentTests(unittest.TestCase):
    def test_builder_matches_real_cli_header_shape(self):
        from src.utils import build_antigravity_user_agent

        self.assertEqual(
            build_antigravity_user_agent(
                version="1.1.8",
                os_type="linux",
                arch="amd64",
                auth_method="consumer",
            ),
            "antigravity/cli/1.1.8 (aidev_client; os_type=linux; "
            "arch=amd64; auth_method=consumer)",
        )

    def test_default_header_does_not_use_obsolete_windows_signature(self):
        with patch.dict(os.environ, {}, clear=False):
            import src.utils as utils

            utils = importlib.reload(utils)

        self.assertNotIn("1.0.1 windows/amd64", utils.ANTIGRAVITY_USER_AGENT)
        self.assertIn("aidev_client", utils.ANTIGRAVITY_USER_AGENT)


class Antigravity403ClassifierTests(unittest.TestCase):
    def classify(self, text, health=None):
        from src.antigravity_error_classifier import classify_antigravity_403

        return classify_antigravity_403(text, account_health=health or {})

    def test_health_geo_block_wins_and_invalidates_binding(self):
        decision = self.classify(
            "permission denied",
            {
                "binding_status": "healthy",
                "eligibility_status": "geo_blocked",
            },
        )

        self.assertEqual(decision.category, "geo_blocked")
        self.assertFalse(decision.disable_account)
        self.assertTrue(decision.invalidate_binding)
        self.assertTrue(decision.retry_after_rebind)

    def test_explicit_model_denial_wins_over_stale_geo_health(self):
        decision = self.classify(
            "Model gemini-image is not available for this account.",
            {
                "binding_status": "healthy",
                "eligibility_status": "geo_blocked",
            },
        )

        self.assertEqual(decision.category, "model_forbidden")
        self.assertFalse(decision.disable_account)
        self.assertTrue(decision.cooldown_model)

    def test_proxy_drift_is_not_an_account_ban(self):
        decision = self.classify(
            "forbidden",
            {
                "binding_status": "proxy_drift",
                "eligibility_status": "eligible",
            },
        )

        self.assertEqual(decision.category, "proxy_drift")
        self.assertFalse(decision.disable_account)
        self.assertTrue(decision.invalidate_binding)

    def test_location_unavailable_response_is_geo_blocked(self):
        decision = self.classify(
            "Your current account is not eligible for Antigravity, because it "
            "is not currently available in your location."
        )

        self.assertEqual(decision.category, "geo_blocked")
        self.assertFalse(decision.disable_account)

    def test_confirmed_account_permission_denial_can_disable_account(self):
        decision = self.classify(
            "The authenticated account does not have permission to use Antigravity.",
            {
                "binding_status": "healthy",
                "eligibility_status": "eligible",
            },
        )

        self.assertEqual(decision.category, "account_forbidden")
        self.assertTrue(decision.disable_account)

    def test_model_permission_denial_only_cools_down_model(self):
        decision = self.classify(
            "Model gemini-test is not available or permitted for this account.",
            {
                "binding_status": "healthy",
                "eligibility_status": "eligible",
            },
        )

        self.assertEqual(decision.category, "model_forbidden")
        self.assertFalse(decision.disable_account)
        self.assertTrue(decision.cooldown_model)

    def test_unknown_403_is_conservative(self):
        decision = self.classify("Forbidden")

        self.assertEqual(decision.category, "unknown_403")
        self.assertFalse(decision.disable_account)
        self.assertFalse(decision.invalidate_binding)
        self.assertFalse(decision.retry_after_rebind)

    def test_validation_required_cools_down_model_without_ban(self):
        for text in (
            "Account validation is required: VALIDATION_REQUIRED",
            "Please verify your account to continue",
            "Please complete the verification at validation_url",
        ):
            decision = self.classify(
                text,
                {"binding_status": "healthy", "eligibility_status": "eligible"},
            )

            self.assertEqual(decision.category, "validation_required")
            self.assertFalse(decision.disable_account)
            self.assertFalse(decision.invalidate_binding)
            self.assertTrue(decision.cooldown_model)


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        if isinstance(payload, str):
            self.text = payload
            self.content = payload.encode()
            try:
                self._payload = json.loads(payload)
            except json.JSONDecodeError:
                self._payload = None
        else:
            self._payload = payload
            self.text = json.dumps(payload)
            self.content = self.text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeHealthStorage:
    def __init__(self, health=None):
        self.health = health or {
            "credential_name": "account.json",
            "egress_ip": None,
            "egress_country": None,
            "binding_status": "unchecked",
            "eligibility_status": "unchecked",
            "eligibility_reason": None,
            "eligibility_checked_at": None,
        }
        self.updates = []

    async def get_antigravity_account_health(self, filename):
        return dict(self.health)

    async def update_antigravity_account_health(self, filename, **updates):
        self.health.update(updates)
        self.updates.append((filename, updates))
        return dict(self.health)


class AntigravityNetworkCheckTests(unittest.IsolatedAsyncioTestCase):
    def test_fresh_geo_block_is_cached_but_not_eligible(self):
        from src.antigravity_account_health import (
            account_health_check_is_fresh,
            account_health_is_fresh_eligible,
        )

        health = {
            "binding_status": "healthy",
            "eligibility_status": "geo_blocked",
            "eligibility_checked_at": 900.0,
        }
        self.assertTrue(
            account_health_check_is_fresh(health, ttl_seconds=300, now=1000.0)
        )
        self.assertFalse(
            account_health_is_fresh_eligible(
                health, ttl_seconds=300, now=1000.0
            )
        )

    def test_parse_egress_identity_accepts_common_payloads(self):
        from src.antigravity_account_health import parse_egress_identity

        self.assertEqual(
            parse_egress_identity({"ip": "203.0.113.8", "country_code": "US"}),
            ("203.0.113.8", "US"),
        )
        self.assertEqual(
            parse_egress_identity({"origin": "203.0.113.9, 10.0.0.1"}),
            ("203.0.113.9", None),
        )

    async def test_check_uses_bound_proxy_for_ip_and_eligibility(self):
        from src.antigravity_account_health import check_antigravity_account_health

        storage = _FakeHealthStorage()
        get_mock = AsyncMock(
            return_value=_FakeResponse(
                200, {"ip": "203.0.113.8", "country_code": "US"}
            )
        )
        post_mock = AsyncMock(
            return_value=_FakeResponse(
                200,
                {
                    "currentTier": {"id": "free-tier"},
                    "cloudaicompanionProject": "project-1",
                },
            )
        )

        with (
            patch("src.antigravity_account_health.get_async", get_mock),
            patch("src.antigravity_account_health.post_async", post_mock),
        ):
            result = await check_antigravity_account_health(
                storage=storage,
                credential_name="account.json",
                access_token="token",
                network={
                    "proxy_mode": "group",
                    "proxy_url": "http://proxy.example:8080",
                    "bound_proxy_node_id": 7,
                },
                ip_check_url="https://ip-check.example/json",
                api_base_url="https://daily-cloudcode-pa.googleapis.com",
                checked_at=1000.0,
            )

        self.assertEqual(result["binding_status"], "healthy")
        self.assertEqual(result["eligibility_status"], "eligible")
        self.assertEqual(result["egress_ip"], "203.0.113.8")
        self.assertEqual(
            get_mock.await_args.kwargs["proxy_url"],
            "http://proxy.example:8080",
        )
        self.assertEqual(
            post_mock.await_args.kwargs["proxy_url"],
            "http://proxy.example:8080",
        )

    async def test_ip_drift_stops_before_eligibility_request(self):
        from src.antigravity_account_health import check_antigravity_account_health

        storage = _FakeHealthStorage(
            {
                "credential_name": "account.json",
                "egress_ip": "203.0.113.7",
                "egress_country": "US",
                "binding_status": "healthy",
                "eligibility_status": "eligible",
                "eligibility_reason": None,
                "eligibility_checked_at": 900.0,
            }
        )
        get_mock = AsyncMock(
            return_value=_FakeResponse(200, {"ip": "203.0.113.8"})
        )
        post_mock = AsyncMock()

        with (
            patch("src.antigravity_account_health.get_async", get_mock),
            patch("src.antigravity_account_health.post_async", post_mock),
        ):
            result = await check_antigravity_account_health(
                storage=storage,
                credential_name="account.json",
                access_token="token",
                network={"proxy_mode": "custom", "proxy_url": "http://proxy"},
                ip_check_url="https://ip-check.example/json",
                api_base_url="https://daily-cloudcode-pa.googleapis.com",
                checked_at=1000.0,
            )

        self.assertEqual(result["binding_status"], "proxy_drift")
        self.assertEqual(result["eligibility_status"], "unchecked")
        post_mock.assert_not_awaited()

    async def test_echo_429_preserves_existing_eligibility(self):
        from src.antigravity_account_health import check_antigravity_account_health

        storage = _FakeHealthStorage(
            {
                "credential_name": "account.json",
                "egress_ip": "203.0.113.8",
                "egress_country": "US",
                "binding_status": "healthy",
                "eligibility_status": "eligible",
                "eligibility_reason": None,
                "eligibility_checked_at": 900.0,
            }
        )
        get_mock = AsyncMock(return_value=_FakeResponse(429, {"error": "slow down"}))
        post_mock = AsyncMock()

        with (
            patch("src.antigravity_account_health.get_async", get_mock),
            patch("src.antigravity_account_health.post_async", post_mock),
        ):
            result = await check_antigravity_account_health(
                storage=storage,
                credential_name="account.json",
                access_token="token",
                network={"proxy_mode": "custom", "proxy_url": "http://proxy"},
                ip_check_url="https://ip-check.example/json",
                api_base_url="https://daily-cloudcode-pa.googleapis.com",
                checked_at=1000.0,
            )

        # 429 是回显服务限流，不覆盖既有的健康结论
        self.assertEqual(result["binding_status"], "healthy")
        self.assertEqual(result["eligibility_status"], "eligible")
        self.assertIn("temporarily unavailable", result["eligibility_reason"])
        self.assertEqual(result["eligibility_checked_at"], 1000.0)
        post_mock.assert_not_awaited()

    async def test_echo_429_keeps_new_account_unchecked(self):
        from src.antigravity_account_health import check_antigravity_account_health

        storage = _FakeHealthStorage()
        get_mock = AsyncMock(return_value=_FakeResponse(429, {"error": "slow down"}))

        with (
            patch("src.antigravity_account_health.get_async", get_mock),
            patch("src.antigravity_account_health.post_async", AsyncMock()),
        ):
            result = await check_antigravity_account_health(
                storage=storage,
                credential_name="account.json",
                access_token="token",
                network={"proxy_mode": "custom", "proxy_url": "http://proxy"},
                ip_check_url="https://ip-check.example/json",
                api_base_url="https://daily-cloudcode-pa.googleapis.com",
                checked_at=1000.0,
            )

        # 从未检查过的账号不因此获得资格，但也不会被标记为失败
        self.assertEqual(result["binding_status"], "unchecked")
        self.assertEqual(result["eligibility_status"], "unchecked")

    async def test_eligibility_call_exception_preserves_previous_state(self):
        from src.antigravity_account_health import check_antigravity_account_health

        storage = _FakeHealthStorage(
            {
                "credential_name": "account.json",
                "egress_ip": "203.0.113.8",
                "egress_country": "US",
                "binding_status": "healthy",
                "eligibility_status": "eligible",
                "eligibility_reason": None,
                "eligibility_checked_at": 900.0,
            }
        )
        get_mock = AsyncMock(return_value=_FakeResponse(200, {"ip": "203.0.113.8"}))
        post_mock = AsyncMock(side_effect=TimeoutError("read timeout"))

        with (
            patch("src.antigravity_account_health.get_async", get_mock),
            patch("src.antigravity_account_health.post_async", post_mock),
        ):
            result = await check_antigravity_account_health(
                storage=storage,
                credential_name="account.json",
                access_token="token",
                network={"proxy_mode": "custom", "proxy_url": "http://proxy"},
                ip_check_url="https://ip-check.example/json",
                api_base_url="https://daily-cloudcode-pa.googleapis.com",
                checked_at=1000.0,
            )

        # 资格调用超时是检测设施故障，保留上次资格结论
        self.assertEqual(result["binding_status"], "healthy")
        self.assertEqual(result["eligibility_status"], "eligible")
        self.assertIn("temporarily failed", result["eligibility_reason"])


class _Fake403CredentialManager:
    def __init__(self, health, rebound=False):
        self.health = dict(health)
        self.rebound = rebound
        self.updates = []
        self.disabled = []
        self.rebind_calls = []
        self.credential_data = {
            "access_token": "token",
            "project_id": "project-1",
            "proxy_mode": "direct",
            "proxy_url": None,
        }

    async def get_valid_credential(self, **kwargs):
        return "account.json", dict(self.credential_data)

    async def get_antigravity_account_health(self, credential_name):
        return dict(self.health)

    async def update_antigravity_account_health(self, credential_name, **updates):
        self.health.update(updates)
        self.updates.append((credential_name, updates))
        return dict(self.health)

    async def set_cred_disabled(self, credential_name, disabled, mode):
        self.disabled.append((credential_name, disabled, mode))
        return True

    async def rebind_antigravity_account(self, credential_name, credential_data):
        self.rebind_calls.append((credential_name, credential_data))
        return self.rebound


class Antigravity403HandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_stream_unknown_403_returns_original_without_generic_auto_ban(self):
        import httpx
        from src.api import antigravity

        manager = _Fake403CredentialManager(
            {
                "binding_status": "unchecked",
                "eligibility_status": "unchecked",
            }
        )
        generic_retry = AsyncMock(return_value=True)
        switch_retry = AsyncMock(return_value=(True, None))

        class Recorder:
            async def record(self, **kwargs):
                return True

        with (
            patch.object(antigravity, "credential_manager", manager),
            patch.object(
                antigravity,
                "check_should_auto_ban",
                AsyncMock(return_value=False),
            ),
            patch.object(
                antigravity,
                "get_antigravity_stream2nostream",
                AsyncMock(return_value=False),
            ),
            patch.object(
                antigravity,
                "get_antigravity_api_url",
                AsyncMock(return_value="https://example.invalid"),
            ),
            patch.object(
                antigravity,
                "get_retry_config",
                AsyncMock(
                    return_value={
                        "retry_enabled": True,
                        "max_retries": 1,
                        "retry_interval": 0,
                    }
                ),
            ),
            patch.object(
                antigravity,
                "get_auto_ban_error_codes",
                AsyncMock(return_value=[403]),
            ),
            patch.object(
                antigravity,
                "get_antigravity_network_check_enabled",
                AsyncMock(return_value=True),
            ),
            patch.object(
                antigravity,
                "wrap_cli_request",
                AsyncMock(
                    return_value=(
                        {"project": "project-1", "request": {}},
                        "request-1",
                    )
                ),
            ),
            patch.object(
                antigravity,
                "post_async",
                AsyncMock(return_value=httpx.Response(403, text="Forbidden")),
            ),
            patch.object(antigravity, "handle_error_with_retry", generic_retry),
            patch.object(antigravity, "_switch_credential_for_retry", switch_retry),
            patch.object(
                antigravity,
                "record_api_call_error",
                AsyncMock(return_value=None),
            ),
            patch.object(
                antigravity,
                "get_billing_recorder",
                AsyncMock(return_value=Recorder()),
            ),
            patch.object(
                antigravity,
                "_alert_all_accounts_unavailable_if_needed",
                AsyncMock(return_value=False),
            ),
        ):
            response = await antigravity.non_stream_request(
                {"model": "gemini-test", "request": {"contents": []}}
            )

        # 上游 403 在最终出口处对客户端改写为 503（响应体保留）
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.body, b"Forbidden")
        generic_retry.assert_not_awaited()
        switch_retry.assert_not_awaited()
        self.assertEqual(manager.health["last_403_category"], "unknown_403")

    async def test_stream_unknown_403_returns_original_without_generic_auto_ban(self):
        from fastapi import Response
        from src.api import antigravity

        manager = _Fake403CredentialManager(
            {
                "binding_status": "unchecked",
                "eligibility_status": "unchecked",
            }
        )
        generic_retry = AsyncMock(return_value=True)

        async def fake_stream_post_async(**kwargs):
            yield Response(content="Forbidden", status_code=403)

        class Recorder:
            async def record(self, **kwargs):
                return True

        with (
            patch.object(antigravity, "credential_manager", manager),
            patch.object(
                antigravity,
                "check_should_auto_ban",
                AsyncMock(return_value=False),
            ),
            patch.object(
                antigravity,
                "get_antigravity_api_url",
                AsyncMock(return_value="https://example.invalid"),
            ),
            patch.object(
                antigravity,
                "get_retry_config",
                AsyncMock(
                    return_value={
                        "retry_enabled": True,
                        "max_retries": 1,
                        "retry_interval": 0,
                    }
                ),
            ),
            patch.object(
                antigravity,
                "get_auto_ban_error_codes",
                AsyncMock(return_value=[403]),
            ),
            patch.object(
                antigravity,
                "get_antigravity_network_check_enabled",
                AsyncMock(return_value=True),
            ),
            patch.object(
                antigravity,
                "wrap_cli_request",
                AsyncMock(
                    return_value=(
                        {"project": "project-1", "request": {}},
                        "request-1",
                    )
                ),
            ),
            patch.object(
                antigravity, "stream_post_async", fake_stream_post_async
            ),
            patch.object(antigravity, "handle_error_with_retry", generic_retry),
            patch.object(
                antigravity,
                "record_api_call_error",
                AsyncMock(return_value=None),
            ),
            patch.object(
                antigravity,
                "get_billing_recorder",
                AsyncMock(return_value=Recorder()),
            ),
            patch.object(
                antigravity,
                "_alert_all_accounts_unavailable_if_needed",
                AsyncMock(return_value=False),
            ),
        ):
            chunks = [
                chunk
                async for chunk in antigravity.stream_request(
                    {"model": "gemini-test", "request": {"contents": []}}
                )
            ]

        self.assertEqual(len(chunks), 1)
        # 上游 403 在最终出口处对客户端改写为 503（响应体保留）
        self.assertEqual(chunks[0].status_code, 503)
        generic_retry.assert_not_awaited()
        self.assertEqual(manager.health["last_403_category"], "unknown_403")

    async def test_geo_403_is_recorded_without_disabling_account(self):
        from src.api import antigravity

        manager = _Fake403CredentialManager(
            {
                "binding_status": "healthy",
                "eligibility_status": "unchecked",
            }
        )
        with (
            patch.object(antigravity, "credential_manager", manager),
            patch.object(
                antigravity,
                "get_antigravity_network_check_enabled",
                AsyncMock(return_value=False),
            ),
            patch.object(
                antigravity,
                "check_should_auto_ban",
                AsyncMock(return_value=False),
            ),
        ):
            result = await antigravity._handle_antigravity_403(
                credential_name="account.json",
                credential_data={"access_token": "token"},
                model_name="gemini-test",
                error_text="Antigravity is not currently available in your location.",
            )

        self.assertEqual(result.decision.category, "geo_blocked")
        self.assertFalse(result.should_retry)
        self.assertEqual(manager.disabled, [])
        self.assertEqual(manager.health["eligibility_status"], "geo_blocked")
        self.assertEqual(manager.health["last_403_category"], "geo_blocked")

    async def test_geo_403_never_disables_account_when_auto_ban_enabled(self):
        from src.api import antigravity

        manager = _Fake403CredentialManager(
            {
                "binding_status": "healthy",
                "eligibility_status": "unchecked",
            }
        )
        with (
            patch.object(antigravity, "credential_manager", manager),
            patch.object(
                antigravity,
                "check_should_auto_ban",
                AsyncMock(return_value=True),
            ),
        ):
            result = await antigravity._handle_antigravity_403(
                credential_name="account.json",
                credential_data={"access_token": "token"},
                model_name="gemini-test",
                error_text="Antigravity is not currently available in your location.",
            )

        self.assertEqual(result.decision.category, "geo_blocked")
        self.assertFalse(result.should_retry)
        self.assertEqual(manager.disabled, [])
        self.assertEqual(manager.health["eligibility_status"], "geo_blocked")

    async def test_confirmed_account_403_disables_only_when_auto_ban_is_enabled(self):
        from src.api import antigravity

        manager = _Fake403CredentialManager(
            {
                "binding_status": "healthy",
                "eligibility_status": "eligible",
            }
        )
        with (
            patch.object(antigravity, "credential_manager", manager),
            patch.object(
                antigravity,
                "check_should_auto_ban",
                AsyncMock(return_value=True),
            ),
        ):
            result = await antigravity._handle_antigravity_403(
                credential_name="account.json",
                credential_data={"access_token": "token"},
                model_name="gemini-test",
                error_text=(
                    "The authenticated account does not have permission to use "
                    "Antigravity."
                ),
            )

        self.assertEqual(result.decision.category, "account_forbidden")
        self.assertTrue(result.should_retry)
        self.assertEqual(
            manager.disabled,
            [("account.json", True, "antigravity")],
        )

    async def test_unknown_403_is_recorded_and_not_retried(self):
        from src.api import antigravity

        manager = _Fake403CredentialManager(
            {
                "binding_status": "unchecked",
                "eligibility_status": "unchecked",
            }
        )
        with (
            patch.object(antigravity, "credential_manager", manager),
            patch.object(
                antigravity,
                "get_antigravity_network_check_enabled",
                AsyncMock(return_value=True),
            ),
            patch.object(
                antigravity,
                "check_should_auto_ban",
                AsyncMock(return_value=False),
            ),
        ):
            result = await antigravity._handle_antigravity_403(
                credential_name="account.json",
                credential_data={"access_token": "token"},
                model_name="gemini-test",
                error_text="Forbidden",
            )

        self.assertEqual(result.decision.category, "unknown_403")
        self.assertFalse(result.should_retry)
        self.assertEqual(manager.disabled, [])
        self.assertEqual(manager.rebind_calls, [])

    async def test_unknown_403_never_disables_account_when_auto_ban_enabled(self):
        from src.api import antigravity

        manager = _Fake403CredentialManager(
            {
                "binding_status": "unchecked",
                "eligibility_status": "unchecked",
            }
        )
        with (
            patch.object(antigravity, "credential_manager", manager),
            patch.object(
                antigravity,
                "check_should_auto_ban",
                AsyncMock(return_value=True),
            ),
        ):
            result = await antigravity._handle_antigravity_403(
                credential_name="account.json",
                credential_data={"access_token": "token"},
                model_name="gemini-test",
                error_text="Forbidden",
            )

        self.assertEqual(result.decision.category, "unknown_403")
        self.assertFalse(result.should_retry)
        self.assertEqual(manager.disabled, [])
        self.assertEqual(manager.rebind_calls, [])

    async def test_model_403_only_cools_model_when_auto_ban_enabled(self):
        from src.api import antigravity

        manager = _Fake403CredentialManager(
            {
                "binding_status": "healthy",
                "eligibility_status": "eligible",
            }
        )
        before = time.time()
        with (
            patch.object(antigravity, "credential_manager", manager),
            patch.object(
                antigravity,
                "check_should_auto_ban",
                AsyncMock(return_value=True),
            ),
        ):
            result = await antigravity._handle_antigravity_403(
                credential_name="account.json",
                credential_data={"access_token": "token"},
                model_name="gemini-image",
                error_text="Model gemini-image is not available for this account.",
            )

        self.assertEqual(result.decision.category, "model_forbidden")
        self.assertTrue(result.should_retry)
        self.assertGreaterEqual(
            result.cooldown_until,
            before + antigravity.MODEL_FORBIDDEN_COOLDOWN_SECONDS,
        )
        self.assertEqual(manager.disabled, [])

    async def test_validation_required_cools_down_600s_and_never_bans(self):
        from src.api import antigravity

        manager = _Fake403CredentialManager(
            {
                "binding_status": "healthy",
                "eligibility_status": "eligible",
            }
        )
        before = time.time()
        # 即使自动封禁开关打开，VALIDATION_REQUIRED 也不禁用账号
        with (
            patch.object(antigravity, "credential_manager", manager),
            patch.object(
                antigravity,
                "check_should_auto_ban",
                AsyncMock(return_value=True),
            ),
        ):
            result = await antigravity._handle_antigravity_403(
                credential_name="account.json",
                credential_data={"access_token": "token"},
                model_name="gemini-test",
                error_text=(
                    "Account validation required (VALIDATION_REQUIRED), "
                    "please verify your account."
                ),
            )

        self.assertEqual(result.decision.category, "validation_required")
        self.assertTrue(result.should_retry)
        self.assertIsNotNone(result.cooldown_until)
        self.assertGreaterEqual(
            result.cooldown_until, before + 600
        )
        self.assertLessEqual(result.cooldown_until, time.time() + 600)
        self.assertEqual(manager.disabled, [])
        self.assertEqual(
            manager.health["last_403_category"], "validation_required"
        )
        self.assertEqual(
            manager.health["eligibility_status"], "validation_required"
        )

    async def test_real_load_code_assist_shape_prioritizes_location_block(self):
        from src.antigravity_account_health import check_antigravity_account_health

        storage = _FakeHealthStorage()
        with (
            patch(
                "src.antigravity_account_health.get_async",
                AsyncMock(return_value=_FakeResponse(200, {"ip": "203.0.113.8"})),
            ),
            patch(
                "src.antigravity_account_health.post_async",
                AsyncMock(
                    return_value=_FakeResponse(
                        200,
                        {
                            "allowedTiers": [],
                            "cloudaicompanionProject": "project-1",
                            "currentTier": {"id": "free-tier"},
                            "gcpManaged": False,
                            "ineligibleTiers": [
                                {
                                    "reasonCode": "UNSUPPORTED_LOCATION",
                                    "reasonMessage": (
                                        "Your current account is not eligible for "
                                        "Antigravity, because it is not currently "
                                        "available in your location."
                                    ),
                                }
                            ],
                            "paidTier": {},
                        },
                    )
                ),
            ),
        ):
            result = await check_antigravity_account_health(
                storage=storage,
                credential_name="account.json",
                access_token="token",
                network={"proxy_mode": "direct", "proxy_url": None},
                ip_check_url="https://ip-check.example/json",
                api_base_url="https://daily-cloudcode-pa.googleapis.com",
                checked_at=1000.0,
            )

        self.assertEqual(result["binding_status"], "healthy")
        self.assertEqual(result["eligibility_status"], "geo_blocked")

    async def test_missing_ip_check_url_is_explicit_and_does_not_call_network(self):
        from src.antigravity_account_health import check_antigravity_account_health

        storage = _FakeHealthStorage()
        get_mock = AsyncMock()
        post_mock = AsyncMock()
        with (
            patch("src.antigravity_account_health.get_async", get_mock),
            patch("src.antigravity_account_health.post_async", post_mock),
        ):
            result = await check_antigravity_account_health(
                storage=storage,
                credential_name="account.json",
                access_token="token",
                network={"proxy_mode": "direct", "proxy_url": None},
                ip_check_url="",
                api_base_url="https://daily-cloudcode-pa.googleapis.com",
                checked_at=1000.0,
            )

        self.assertEqual(result["binding_status"], "unchecked")
        self.assertEqual(result["eligibility_status"], "error")
        self.assertIn("ANTIGRAVITY_EGRESS_IP_CHECK_URL", result["eligibility_reason"])
        get_mock.assert_not_awaited()
        post_mock.assert_not_awaited()


class AntigravityNegativeEvidenceTests(unittest.TestCase):
    def assert_negative(self, health, expected):
        from src.antigravity_account_health import (
            account_health_has_negative_evidence,
        )

        self.assertIs(account_health_has_negative_evidence(health), expected)

    def test_explicit_negative_states_block(self):
        self.assert_negative(
            {"binding_status": "proxy_drift", "eligibility_status": "unchecked"},
            True,
        )
        self.assert_negative(
            {"binding_status": "healthy", "eligibility_status": "account_blocked"},
            True,
        )

    def test_geo_blocked_does_not_block(self):
        # geo_blocked 误伤面大，仅作记录，不再作为拦截证据
        self.assert_negative(
            {"binding_status": "healthy", "eligibility_status": "geo_blocked"},
            False,
        )

    def test_detection_failure_states_do_not_block(self):
        self.assert_negative(
            {"binding_status": "proxy_failed", "eligibility_status": "error"},
            False,
        )
        self.assert_negative(
            {"binding_status": "healthy", "eligibility_status": "error"},
            False,
        )
        self.assert_negative(
            {"binding_status": "unchecked", "eligibility_status": "unchecked"},
            False,
        )
        self.assert_negative({}, False)

    def test_healthy_eligible_has_no_negative_evidence(self):
        self.assert_negative(
            {"binding_status": "healthy", "eligibility_status": "eligible"},
            False,
        )


class _FakeGateStorageAdapter:
    def __init__(self, health):
        self.health = health

    async def resolve_credential_network(self, credential_name, mode="antigravity"):
        return {"proxy_mode": "direct", "proxy_url": None}

    async def get_antigravity_account_health(self, credential_name):
        return dict(self.health)


class AntigravityHealthGateTests(unittest.IsolatedAsyncioTestCase):
    def _make_manager(self, health):
        from src.credential_manager import CredentialManager

        manager = CredentialManager()
        manager._initialized = True
        manager._storage_adapter = _FakeGateStorageAdapter(health)
        return manager

    def _patch_config(self):
        return (
            patch(
                "config.get_antigravity_network_check_enabled",
                AsyncMock(return_value=True),
            ),
            patch(
                "config.get_antigravity_network_check_ttl_seconds",
                AsyncMock(return_value=300),
            ),
        )

    async def _ready(self, manager, credential_data=None):
        return await manager.ensure_antigravity_account_ready(
            "account.json",
            credential_data or {"access_token": "token"},
        )

    async def test_fresh_geo_blocked_cache_is_allowed(self):
        # geo_blocked 不再拦截：地区误判不应让有额度的账号不可用
        manager = self._make_manager(
            {
                "binding_status": "healthy",
                "eligibility_status": "geo_blocked",
                "eligibility_checked_at": time.time(),
            }
        )
        enabled_patch, ttl_patch = self._patch_config()
        with enabled_patch, ttl_patch:
            result = await self._ready(manager)
        self.assertIsNotNone(result)
        self.assertEqual(result["access_token"], "token")

    async def test_fresh_proxy_drift_cache_still_blocks(self):
        manager = self._make_manager(
            {
                "binding_status": "proxy_drift",
                "eligibility_status": "unchecked",
                "eligibility_checked_at": time.time(),
            }
        )
        enabled_patch, ttl_patch = self._patch_config()
        with enabled_patch, ttl_patch:
            self.assertIsNone(await self._ready(manager))

    async def test_fresh_proxy_failed_cache_is_allowed(self):
        # echo 429/代理抖动留下的检测故障状态不含账号证据，不应拦截请求
        manager = self._make_manager(
            {
                "binding_status": "proxy_failed",
                "eligibility_status": "error",
                "eligibility_checked_at": time.time(),
            }
        )
        enabled_patch, ttl_patch = self._patch_config()
        with enabled_patch, ttl_patch:
            result = await self._ready(manager)
        self.assertIsNotNone(result)
        self.assertEqual(result["access_token"], "token")

    async def test_fresh_check_with_negative_evidence_blocks(self):
        manager = self._make_manager({})
        enabled_patch, ttl_patch = self._patch_config()
        check_mock = AsyncMock(
            return_value={
                "binding_status": "healthy",
                "eligibility_status": "account_blocked",
            }
        )
        with enabled_patch, ttl_patch, patch.object(
            manager, "check_antigravity_account_health", check_mock
        ):
            self.assertIsNone(await self._ready(manager))

    async def test_fresh_check_inconclusive_is_allowed(self):
        manager = self._make_manager({})
        enabled_patch, ttl_patch = self._patch_config()
        check_mock = AsyncMock(
            return_value={
                "binding_status": "proxy_failed",
                "eligibility_status": "error",
            }
        )
        with enabled_patch, ttl_patch, patch.object(
            manager, "check_antigravity_account_health", check_mock
        ):
            result = await self._ready(manager)
        self.assertIsNotNone(result)


class Antigravity403To503Tests(unittest.TestCase):
    def test_upstream_403_is_rewritten_to_503_with_body_preserved(self):
        from fastapi import Response
        from src.api.antigravity import _upstream_403_to_503

        original = Response(
            content=b'{"error": "forbidden"}',
            status_code=403,
            media_type="application/json",
        )
        masked = _upstream_403_to_503(original)

        self.assertEqual(masked.status_code, 503)
        self.assertEqual(masked.body, b'{"error": "forbidden"}')

    def test_other_statuses_are_untouched(self):
        from fastapi import Response
        from src.api.antigravity import _upstream_403_to_503

        for status in (400, 429, 500):
            original = Response(content="err", status_code=status)
            self.assertIs(_upstream_403_to_503(original), original)


class AntigravityJetskiFingerprintTests(unittest.IsolatedAsyncioTestCase):
    async def _wrap(self, user_email):
        from src.api import antigravity

        state = antigravity.AntigravitySessionState(
            conversation_id="conv",
            trajectory_id="traj",
            session_id="sess",
            step_index=0,
            created_at=0.0,
            last_used_at=0.0,
        )
        with patch.object(
            antigravity, "_get_session_state", AsyncMock(return_value=state)
        ):
            return await antigravity.wrap_cli_request(
                {"contents": []}, "gemini-test", "project-1",
                user_email=user_email,
            )

    async def test_gmail_account_keeps_antigravity_fingerprint(self):
        payload, _ = await self._wrap("someone@gmail.com")

        self.assertEqual(payload["userAgent"], "antigravity")
        self.assertNotIn("metadata", payload["request"])

    async def test_googlemail_account_keeps_antigravity_fingerprint(self):
        payload, _ = await self._wrap("someone@googlemail.com")

        self.assertEqual(payload["userAgent"], "antigravity")
        self.assertNotIn("metadata", payload["request"])

    async def test_missing_email_defaults_to_antigravity_fingerprint(self):
        payload, _ = await self._wrap(None)

        self.assertEqual(payload["userAgent"], "antigravity")
        self.assertNotIn("metadata", payload["request"])

    async def test_non_gmail_account_uses_jetski_fingerprint(self):
        payload, _ = await self._wrap("someone@example.com")

        self.assertEqual(payload["userAgent"], "jetski")
        self.assertEqual(
            payload["request"]["metadata"], {"ideType": "JETSKI"}
        )


class AntigravityHostFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_default_url_returns_three_hosts_in_order(self):
        from config import antigravity_host_candidates

        self.assertEqual(
            antigravity_host_candidates(
                "https://daily-cloudcode-pa.googleapis.com"
            ),
            [
                "https://daily-cloudcode-pa.sandbox.googleapis.com",
                "https://daily-cloudcode-pa.googleapis.com",
                "https://cloudcode-pa.googleapis.com",
            ],
        )

    def test_custom_url_disables_fallback(self):
        from config import antigravity_host_candidates

        self.assertEqual(
            antigravity_host_candidates("https://proxy.example.com"),
            ["https://proxy.example.com"],
        )

    async def test_post_fallback_walks_hosts_on_5xx_and_connection_errors(self):
        from src.api import antigravity

        calls = []

        async def fake_post(**kwargs):
            calls.append(kwargs["url"])
            if len(calls) == 1:
                raise TimeoutError("read timeout")
            if len(calls) == 2:
                return _FakeResponse(503, {"error": "unavailable"})
            return _FakeResponse(200, {"ok": True})

        with (
            patch.object(
                antigravity,
                "get_antigravity_api_url_candidates",
                AsyncMock(
                    return_value=[
                        "https://daily-cloudcode-pa.sandbox.googleapis.com",
                        "https://daily-cloudcode-pa.googleapis.com",
                        "https://cloudcode-pa.googleapis.com",
                    ]
                ),
            ),
            patch.object(antigravity, "post_async", fake_post),
        ):
            response = await antigravity._post_with_host_fallback(
                "/v1internal:fetchAvailableModels", {"h": "v"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            calls,
            [
                "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:fetchAvailableModels",
                "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            ],
        )

    async def test_post_fallback_does_not_retry_non_429_4xx(self):
        from src.api import antigravity

        calls = []

        async def fake_post(**kwargs):
            calls.append(kwargs["url"])
            return _FakeResponse(403, {"error": "forbidden"})

        with (
            patch.object(
                antigravity,
                "get_antigravity_api_url_candidates",
                AsyncMock(
                    return_value=[
                        "https://daily-cloudcode-pa.sandbox.googleapis.com",
                        "https://daily-cloudcode-pa.googleapis.com",
                    ]
                ),
            ),
            patch.object(antigravity, "post_async", fake_post),
        ):
            response = await antigravity._post_with_host_fallback(
                "/v1internal:fetchAvailableModels", {"h": "v"}
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(calls), 1)

    async def test_load_code_assist_falls_back_to_next_host(self):
        from src import google_oauth_api

        calls = []

        async def fake_post(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                raise TimeoutError("read timeout")
            return _FakeResponse(
                200,
                {
                    "currentTier": {"id": "free-tier"},
                    "cloudaicompanionProject": "project-1",
                },
            )

        with patch.object(google_oauth_api, "post_async", fake_post):
            project_id, tier, credits = (
                await google_oauth_api._try_load_code_assist(
                    "https://daily-cloudcode-pa.googleapis.com",
                    {"Authorization": "Bearer token"},
                    proxy_url=None,
                )
            )

        self.assertEqual(project_id, "project-1")
        self.assertEqual(tier, "free")
        self.assertEqual(
            calls,
            [
                "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist",
                "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            ],
        )

    async def test_load_code_assist_custom_url_does_not_fallback(self):
        from src import google_oauth_api

        calls = []

        async def fake_post(url, **kwargs):
            calls.append(url)
            raise TimeoutError("read timeout")

        with patch.object(google_oauth_api, "post_async", fake_post):
            with self.assertRaises(TimeoutError):
                await google_oauth_api._try_load_code_assist(
                    "https://proxy.example.com",
                    {"Authorization": "Bearer token"},
                    proxy_url=None,
                )

        self.assertEqual(
            calls, ["https://proxy.example.com/v1internal:loadCodeAssist"]
        )


class AntigravityQuotaProjectTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_list_uses_stored_project_id(self):
        from src.api import antigravity

        post_mock = AsyncMock(return_value=_FakeResponse(200, {"models": {}}))
        with (
            patch.object(
                antigravity.credential_manager,
                "get_valid_credential",
                AsyncMock(
                    return_value=(
                        "account.json",
                        {
                            "access_token": "token",
                            "project_id": "project-123",
                            "proxy_mode": "direct",
                        },
                    )
                ),
            ),
            patch.object(antigravity, "_post_with_host_fallback", post_mock),
        ):
            result = await antigravity.fetch_available_models()

        self.assertEqual(result, [])
        self.assertEqual(
            post_mock.await_args.kwargs["json_body"],
            {"project": "project-123"},
        )

    async def test_quota_requests_use_stored_project_id(self):
        from src.api import antigravity

        post_mock = AsyncMock(
            side_effect=[
                _FakeResponse(200, {"models": {}}),
                _FakeResponse(200, {"groups": []}),
            ]
        )
        with patch.object(antigravity, "_post_with_host_fallback", post_mock):
            result = await antigravity.fetch_quota_info(
                "token",
                project_id="project-123",
                proxy_url=None,
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(post_mock.await_args_list), 2)
        for call in post_mock.await_args_list:
            self.assertEqual(
                call.kwargs["json_body"],
                {"project": "project-123"},
            )

    async def test_quota_rejects_missing_project_id_without_upstream_request(self):
        from src.api import antigravity

        post_mock = AsyncMock()
        with patch.object(antigravity, "_post_with_host_fallback", post_mock):
            result = await antigravity.fetch_quota_info(
                "token",
                project_id=None,
                proxy_url=None,
            )

        self.assertFalse(result["success"])
        self.assertIn("Project ID", result["error"])
        post_mock.assert_not_awaited()

    async def test_panel_passes_credential_project_id_to_quota_fetch(self):
        from src.panel import creds as creds_panel

        class FakeStorage:
            async def get_credential(self, filename, mode="antigravity"):
                return {
                    "access_token": "token",
                    "refresh_token": None,
                    "client_id": None,
                    "client_secret": None,
                    "project_id": "project-123",
                    "expiry": "2099-01-01T00:00:00+00:00",
                }

            async def resolve_credential_network(self, filename, mode="antigravity"):
                return {"proxy_mode": "direct", "proxy_url": None}

            async def store_credential(self, filename, data, mode="antigravity"):
                return True

            async def get_credential_state(self, filename, mode="antigravity"):
                return {}

        quota_mock = AsyncMock(
            return_value={"success": True, "models": {}, "groups": []}
        )
        with (
            patch.object(
                creds_panel,
                "get_storage_adapter",
                AsyncMock(return_value=FakeStorage()),
            ),
            patch.object(
                creds_panel,
                "proxy_argument_from_network",
                return_value=None,
            ),
            patch.object(creds_panel, "fetch_quota_info", quota_mock),
        ):
            response = await creds_panel.get_credential_quota(
                "account.json",
                token="panel-token",
                mode="antigravity",
            )

        self.assertEqual(response.status_code, 200)
        quota_mock.assert_awaited_once_with(
            "token",
            project_id="project-123",
            proxy_url=None,
        )

    async def test_panel_quota_view_does_not_clear_model_cooldowns(self):
        from src.panel import creds as creds_panel

        class FakeBackend:
            def __init__(self):
                self.cleared = []

            async def set_model_cooldown(
                self, filename, model_name, cooldown_until, mode="antigravity"
            ):
                self.cleared.append((filename, model_name, cooldown_until, mode))

        class FakeStorage:
            def __init__(self):
                self._backend = FakeBackend()

            async def get_credential(self, filename, mode="antigravity"):
                return {
                    "access_token": "token",
                    "refresh_token": None,
                    "client_id": None,
                    "client_secret": None,
                    "project_id": "project-123",
                    "expiry": "2099-01-01T00:00:00+00:00",
                }

            async def resolve_credential_network(self, filename, mode="antigravity"):
                return {"proxy_mode": "direct", "proxy_url": None}

            async def store_credential(self, filename, data, mode="antigravity"):
                return True

            async def get_credential_state(self, filename, mode="antigravity"):
                return {"model_cooldowns": {"gemini-2.5-flash": 9999999999}}

        storage = FakeStorage()
        quota_mock = AsyncMock(
            return_value={
                "success": True,
                "models": {"gemini-2.5-flash": {"remaining": 0.5}},
                "groups": [],
            }
        )
        with (
            patch.object(
                creds_panel,
                "get_storage_adapter",
                AsyncMock(return_value=storage),
            ),
            patch.object(
                creds_panel,
                "proxy_argument_from_network",
                return_value=None,
            ),
            patch.object(creds_panel, "fetch_quota_info", quota_mock),
        ):
            response = await creds_panel.get_credential_quota(
                "account.json",
                token="panel-token",
                mode="antigravity",
            )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage._backend.cleared, [])
        self.assertNotIn("cleared_cooldowns", payload)

    def test_quota_view_does_not_refresh_the_entire_credential_list(self):
        source = Path("front/common.js").read_text(encoding="utf-8")
        start = source.index("async function toggleAntigravityQuotaDetails")
        end = source.index("// 查看报错详情", start)
        quota_view_source = source[start:end]

        self.assertNotIn("refreshAntigravityCredsList()", quota_view_source)


if __name__ == "__main__":
    unittest.main()
