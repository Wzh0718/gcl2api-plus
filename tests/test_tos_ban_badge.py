"""TOS 封禁账号在凭证列表中的标记（账号待申诉）回归测试。"""

import os
import tempfile
import unittest
from unittest.mock import patch

TOS_REASON = (
    '{"error":{"code":403,"message":"This service has been disabled in this account '
    'for violation of Terms of Service. Please submit an appeal to continue using this '
    'product.","status":"PERMISSION_DENIED"}}'
)


class TosBannedSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.storage.sqlite_manager import SQLiteManager

        self.tempdir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CREDENTIALS_DIR": self.tempdir.name}, clear=False)
        self.env.start()
        self.manager = SQLiteManager()
        await self.manager.initialize()

    async def asyncTearDown(self):
        await self.manager.close()
        self.env.stop()
        self.tempdir.cleanup()

    async def test_tos_banned_account_is_flagged(self):
        await self.manager.store_credential(
            "banned.json", {"access_token": "token"}, mode="antigravity"
        )
        await self.manager.update_credential_state(
            "banned.json",
            {"disabled": True, "error_codes": [403]},
            mode="antigravity",
        )
        await self.manager.update_antigravity_account_health(
            "banned.json",
            eligibility_status="error",
            eligibility_reason=TOS_REASON,
            last_403_reason=TOS_REASON,
        )

        summary = await self.manager.get_credentials_summary(
            mode="antigravity", limit=20
        )
        item = next(row for row in summary["items"] if row["filename"] == "banned.json")
        self.assertTrue(item["tos_banned"])

    async def test_normal_account_is_not_flagged(self):
        await self.manager.store_credential(
            "healthy.json", {"access_token": "token"}, mode="antigravity"
        )
        await self.manager.update_antigravity_account_health(
            "healthy.json",
            eligibility_status="eligible",
            eligibility_reason='{"currentTier":{"id":"free-tier"}}',
        )

        summary = await self.manager.get_credentials_summary(
            mode="antigravity", limit=20
        )
        item = next(row for row in summary["items"] if row["filename"] == "healthy.json")
        self.assertFalse(item["tos_banned"])


if __name__ == "__main__":
    unittest.main()
