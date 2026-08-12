import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx

from src import dingtalk_alert
from src.dingtalk_alert import (
    all_accounts_are_unavailable,
    build_all_accounts_unavailable_message,
    get_earliest_recovery_timestamp,
    send_all_accounts_unavailable_alert,
)


class DingTalkAlertUnitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dingtalk_alert._reset_alert_throttle_for_tests()

    def test_unattempted_available_account_prevents_false_alert(self):
        states = {
            "failed.json": {"disabled": False, "model_cooldowns": {}},
            "still-available.json": {"disabled": False, "model_cooldowns": {}},
        }

        result = all_accounts_are_unavailable(
            states,
            "gemini-test",
            attempted_credentials={"failed.json"},
        )

        self.assertFalse(result)

    def test_earliest_recovery_uses_enabled_account_model_cooldowns(self):
        now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc).timestamp()
        states = {
            "disabled.json": {
                "disabled": True,
                "model_cooldowns": {"gemini-test": now + 60},
            },
            "later.json": {
                "disabled": False,
                "model_cooldowns": {"gemini-test": now + 7200},
            },
            "earlier.json": {
                "disabled": False,
                "model_cooldowns": {"gemini-test": now + 3600},
            },
        }

        result = get_earliest_recovery_timestamp(states, "gemini-test", now=now)

        self.assertEqual(result, now + 3600)

    def test_message_contains_keyword_and_earliest_shanghai_recovery_time(self):
        now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc).timestamp()
        states = {
            "one.json": {
                "disabled": False,
                "model_cooldowns": {"gemini-test": now + 3600},
            }
        }

        message = build_all_accounts_unavailable_message(
            model_name="gemini-test",
            credential_states=states,
            now=now,
        )

        self.assertIn("Gemini反代", message)
        self.assertIn("所有账号均不可用", message)
        self.assertIn("最快恢复时间：2026-07-27 17:00:00（Asia/Shanghai）", message)

    async def test_sender_only_requires_dingtalk_webhook(self):
        response = httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        fake_post = AsyncMock(return_value=response)
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=test-token"

        with patch.dict(os.environ, {"DINGTALK_WEBHOOK_URL": webhook}, clear=False):
            with patch("src.dingtalk_alert.post_async", fake_post):
                sent = await send_all_accounts_unavailable_alert(
                    model_name="gemini-test",
                    credential_states={"disabled.json": {"disabled": True, "model_cooldowns": {}}},
                    now=datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc).timestamp(),
                )

        self.assertTrue(sent)
        kwargs = fake_post.await_args.kwargs
        self.assertEqual(kwargs["url"], webhook)
        self.assertEqual(kwargs["json"]["msgtype"], "text")
        self.assertIn("Gemini反代", kwargs["json"]["text"]["content"])
        self.assertNotIn("secret", kwargs)

    async def test_sender_rejects_empty_credential_snapshot_without_posting(self):
        fake_post = AsyncMock()
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=test-token"

        with patch.dict(os.environ, {"DINGTALK_WEBHOOK_URL": webhook}, clear=False):
            with patch("src.dingtalk_alert.post_async", fake_post):
                sent = await send_all_accounts_unavailable_alert(
                    model_name="gemini-test",
                    credential_states={},
                )

        self.assertFalse(sent)
        fake_post.assert_not_awaited()

    async def test_sender_suppresses_all_account_alerts_for_one_hour(self):
        response = httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        fake_post = AsyncMock(return_value=response)
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc).timestamp()
        states = {"disabled.json": {"disabled": True, "model_cooldowns": {}}}

        with patch.dict(os.environ, {"DINGTALK_WEBHOOK_URL": webhook}, clear=False):
            with patch("src.dingtalk_alert.post_async", fake_post):
                first = await send_all_accounts_unavailable_alert(
                    model_name="gemini-test",
                    credential_states=states,
                    now=now,
                )
                suppressed = await send_all_accounts_unavailable_alert(
                    model_name="gemini-other-model",
                    credential_states=states,
                    now=now + 3599,
                )
                after_one_hour = await send_all_accounts_unavailable_alert(
                    model_name="gemini-test",
                    credential_states=states,
                    now=now + 3600,
                )

        self.assertTrue(first)
        self.assertFalse(suppressed)
        self.assertTrue(after_one_hour)
        self.assertEqual(fake_post.await_count, 2)

    async def test_failed_delivery_does_not_start_one_hour_cooldown(self):
        responses = [
            httpx.Response(500),
            httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}),
        ]
        fake_post = AsyncMock(side_effect=responses)
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc).timestamp()
        states = {"disabled.json": {"disabled": True, "model_cooldowns": {}}}

        with patch.dict(os.environ, {"DINGTALK_WEBHOOK_URL": webhook}, clear=False):
            with patch("src.dingtalk_alert.post_async", fake_post):
                failed = await send_all_accounts_unavailable_alert(
                    model_name="gemini-test",
                    credential_states=states,
                    now=now,
                )
                retried = await send_all_accounts_unavailable_alert(
                    model_name="gemini-test",
                    credential_states=states,
                    now=now + 1,
                )

        self.assertFalse(failed)
        self.assertTrue(retried)
        self.assertEqual(fake_post.await_count, 2)

    async def test_concurrent_same_model_alerts_only_post_once(self):
        response = httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

        async def delayed_post(**kwargs):
            await asyncio.sleep(0)
            return response

        fake_post = AsyncMock(side_effect=delayed_post)
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc).timestamp()
        states = {"disabled.json": {"disabled": True, "model_cooldowns": {}}}

        with patch.dict(os.environ, {"DINGTALK_WEBHOOK_URL": webhook}, clear=False):
            with patch("src.dingtalk_alert.post_async", fake_post):
                results = await asyncio.gather(
                    send_all_accounts_unavailable_alert(
                        model_name="gemini-test",
                        credential_states=states,
                        now=now,
                    ),
                    send_all_accounts_unavailable_alert(
                        model_name="gemini-test",
                        credential_states=states,
                        now=now,
                    ),
                )

        self.assertEqual(sorted(results), [False, True])
        fake_post.assert_awaited_once()


class AntigravityDingTalkAlertIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_credential_snapshot_never_sends_all_accounts_alert(self):
        from src.api import antigravity

        class FakeCredentialManager:
            async def get_valid_credential(self, **kwargs):
                return None

            async def get_creds_status(self, mode="geminicli"):
                return {}

        alert = AsyncMock(return_value=True)
        with patch.object(antigravity, "credential_manager", FakeCredentialManager()):
            with (
                patch.object(
                    antigravity,
                    "get_antigravity_stream2nostream",
                    AsyncMock(return_value=False),
                ),
                patch.object(
                    antigravity,
                    "send_all_accounts_unavailable_alert",
                    alert,
                ),
            ):
                response = await antigravity.non_stream_request(
                    {"model": "gemini-test", "request": {"contents": []}}
                )

        self.assertEqual(response.status_code, 500)
        alert.assert_not_awaited()

    async def test_available_snapshot_overrides_transient_no_credential_result(self):
        from src.api import antigravity

        class FakeCredentialManager:
            async def get_valid_credential(self, **kwargs):
                return None

            async def get_creds_status(self, mode="geminicli"):
                return {"available.json": {"disabled": False, "model_cooldowns": {}}}

        alert = AsyncMock(return_value=True)
        with patch.object(antigravity, "credential_manager", FakeCredentialManager()):
            with (
                patch.object(
                    antigravity,
                    "get_antigravity_stream2nostream",
                    AsyncMock(return_value=False),
                ),
                patch.object(
                    antigravity,
                    "send_all_accounts_unavailable_alert",
                    alert,
                ),
            ):
                response = await antigravity.non_stream_request(
                    {"model": "gemini-test", "request": {"contents": []}}
                )

        self.assertEqual(response.status_code, 500)
        alert.assert_not_awaited()

    async def test_no_available_account_sends_one_alert(self):
        from src.api import antigravity

        class FakeCredentialManager:
            async def get_valid_credential(self, **kwargs):
                return None

            async def get_creds_status(self, mode="geminicli"):
                self.mode = mode
                return {"one.json": {"disabled": True, "model_cooldowns": {}}}

        fake_manager = FakeCredentialManager()
        alert = AsyncMock(return_value=True)

        with patch.object(antigravity, "credential_manager", fake_manager):
            with patch.object(
                antigravity,
                "get_antigravity_stream2nostream",
                AsyncMock(return_value=False),
            ):
                with patch.object(
                    antigravity,
                    "send_all_accounts_unavailable_alert",
                    alert,
                ):
                    response = await antigravity.non_stream_request(
                        {"model": "gemini-test", "request": {"contents": []}}
                    )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(fake_manager.mode, "antigravity")
        alert.assert_awaited_once()
        self.assertEqual(alert.await_args.kwargs["model_name"], "gemini-test")

    async def test_stream_no_available_account_sends_one_alert(self):
        from src.api import antigravity

        class FakeCredentialManager:
            async def get_valid_credential(self, **kwargs):
                return None

            async def get_creds_status(self, mode="geminicli"):
                return {"one.json": {"disabled": True, "model_cooldowns": {}}}

        alert = AsyncMock(return_value=True)
        with patch.object(antigravity, "credential_manager", FakeCredentialManager()):
            with patch.object(
                antigravity,
                "send_all_accounts_unavailable_alert",
                alert,
            ):
                chunks = [
                    chunk
                    async for chunk in antigravity.stream_request(
                        {"model": "gemini-test", "request": {"contents": []}}
                    )
                ]

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].status_code, 500)
        alert.assert_awaited_once()

    async def test_all_attempted_accounts_return_non_200_sends_one_alert(self):
        from src.api import antigravity

        now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc).timestamp()

        class FakeCredentialManager:
            def __init__(self):
                self.credentials = [
                    (
                        "one.json",
                        {"access_token": "one", "project_id": "project-one"},
                    ),
                    (
                        "two.json",
                        {"access_token": "two", "project_id": "project-two"},
                    ),
                    None,
                ]

            async def get_valid_credential(self, **kwargs):
                return self.credentials.pop(0)

            async def get_creds_status(self, mode="geminicli"):
                return {
                    "one.json": {
                        "disabled": False,
                        "model_cooldowns": {"gemini-test": now + 7200},
                    },
                    "two.json": {
                        "disabled": False,
                        "model_cooldowns": {"gemini-test": now + 3600},
                    },
                }

        responses = [
            httpx.Response(429, content=b'{"error":"quota"}'),
            httpx.Response(429, content=b'{"error":"quota"}'),
        ]

        async def fake_post_async(**kwargs):
            return responses.pop(0)

        class FakeRecorder:
            async def record(self, **kwargs):
                return True

        async def async_value(value):
            return value

        alert = AsyncMock(return_value=True)
        manager = FakeCredentialManager()

        with patch.object(antigravity, "credential_manager", manager):
            with (
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
                    AsyncMock(return_value=[]),
                ),
                patch.object(
                    antigravity,
                    "wrap_cli_request",
                    AsyncMock(return_value=({"project": "project-one"}, "request-1")),
                ),
                patch.object(
                    antigravity,
                    "post_async",
                    fake_post_async,
                ),
                patch.object(
                    antigravity,
                    "handle_error_with_retry",
                    AsyncMock(return_value=True),
                ),
                patch.object(
                    antigravity,
                    "record_api_call_error",
                    AsyncMock(return_value=None),
                ),
                patch.object(
                    antigravity,
                    "get_billing_recorder",
                    AsyncMock(return_value=FakeRecorder()),
                ),
                patch.object(
                    antigravity,
                    "send_all_accounts_unavailable_alert",
                    alert,
                ),
            ):
                response = await antigravity.non_stream_request(
                    {"model": "gemini-test", "request": {"contents": []}}
                )

        self.assertEqual(response.status_code, 429)
        alert.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
