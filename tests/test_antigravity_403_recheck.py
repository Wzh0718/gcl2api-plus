"""Tests for the periodic 403-banned account recheck service."""

import unittest
from unittest.mock import AsyncMock, patch

from src.antigravity_403_recheck import recheck_disabled_403_accounts


class _FakeCredentialManager:
    def __init__(self, statuses, healths):
        self.statuses = statuses
        self.healths = healths
        self.disabled_calls = []
        self.health_updates = []

    async def get_creds_status(self, mode="geminicli"):
        assert mode == "antigravity"
        return self.statuses

    async def get_antigravity_account_health(self, credential_name):
        return dict(self.healths.get(credential_name, {}))

    async def set_cred_disabled(self, credential_name, disabled, mode="geminicli"):
        self.disabled_calls.append((credential_name, disabled, mode))
        return True

    async def update_antigravity_account_health(self, credential_name, **updates):
        self.health_updates.append((credential_name, updates))
        self.healths.setdefault(credential_name, {}).update(updates)
        return dict(self.healths[credential_name])


class RecheckDisabled403AccountsTests(unittest.IsolatedAsyncioTestCase):
    async def test_rechecks_only_disabled_accounts_with_403_record(self):
        manager = _FakeCredentialManager(
            statuses={
                "banned.json": {"disabled": True},
                "active.json": {"disabled": False},
                "manual.json": {"disabled": True},
            },
            healths={
                "banned.json": {"last_403_category": "account_forbidden"},
                "active.json": {"last_403_category": "account_forbidden"},
                # manual.json 被手动禁用，没有 403 记录，不应复检
                "manual.json": {},
            },
        )
        check_mock = AsyncMock(return_value={"ok": True, "stages": {}})
        with (
            patch(
                "src.antigravity_403_recheck.credential_manager", manager
            ),
            patch(
                "src.antigravity_full_check.run_account_full_check", check_mock
            ),
        ):
            result = await recheck_disabled_403_accounts()

        self.assertEqual(result["checked"], 1)
        check_mock.assert_awaited_once_with("banned.json")

    async def test_passing_recheck_reenables_account_and_clears_403_record(self):
        manager = _FakeCredentialManager(
            statuses={"banned.json": {"disabled": True}},
            healths={"banned.json": {"last_403_category": "unknown_403"}},
        )
        check_mock = AsyncMock(return_value={"ok": True, "stages": {}})
        with (
            patch("src.antigravity_403_recheck.credential_manager", manager),
            patch("src.antigravity_full_check.run_account_full_check", check_mock),
        ):
            result = await recheck_disabled_403_accounts()

        self.assertEqual(result["re_enabled"], ["banned.json"])
        self.assertEqual(
            manager.disabled_calls, [("banned.json", False, "antigravity")]
        )
        self.assertEqual(
            manager.health_updates,
            [
                (
                    "banned.json",
                    {
                        "last_403_category": None,
                        "last_403_reason": None,
                        "last_403_at": None,
                    },
                )
            ],
        )

    async def test_failing_recheck_keeps_account_disabled(self):
        manager = _FakeCredentialManager(
            statuses={"banned.json": {"disabled": True}},
            healths={"banned.json": {"last_403_category": "geo_blocked"}},
        )
        check_mock = AsyncMock(
            return_value={
                "ok": False,
                "stages": {
                    "proxy": {"ok": True},
                    "health": {"ok": False, "eligibility_status": "geo_blocked"},
                    "message": {"ok": False, "skipped": True},
                },
            }
        )
        with (
            patch("src.antigravity_403_recheck.credential_manager", manager),
            patch("src.antigravity_full_check.run_account_full_check", check_mock),
        ):
            result = await recheck_disabled_403_accounts()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["re_enabled"], [])
        self.assertEqual(manager.disabled_calls, [])
        self.assertEqual(manager.health_updates, [])

    async def test_no_candidates_is_a_no_op(self):
        manager = _FakeCredentialManager(
            statuses={"active.json": {"disabled": False}},
            healths={},
        )
        check_mock = AsyncMock()
        with (
            patch("src.antigravity_403_recheck.credential_manager", manager),
            patch("src.antigravity_full_check.run_account_full_check", check_mock),
        ):
            result = await recheck_disabled_403_accounts()

        self.assertEqual(result, {"checked": 0, "re_enabled": []})
        check_mock.assert_not_awaited()

    async def test_check_exception_does_not_break_the_round(self):
        manager = _FakeCredentialManager(
            statuses={
                "bad.json": {"disabled": True},
                "good.json": {"disabled": True},
            },
            healths={
                "bad.json": {"last_403_category": "unknown_403"},
                "good.json": {"last_403_category": "unknown_403"},
            },
        )

        async def fake_check(filename):
            if filename == "bad.json":
                raise ValueError("credential gone")
            return {"ok": True, "stages": {}}

        with (
            patch("src.antigravity_403_recheck.credential_manager", manager),
            patch(
                "src.antigravity_full_check.run_account_full_check",
                AsyncMock(side_effect=fake_check),
            ),
        ):
            result = await recheck_disabled_403_accounts()

        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["re_enabled"], ["good.json"])


if __name__ == "__main__":
    unittest.main()
