"""Tests for AGY CLI 版本动态追踪（src/antigravity_cli_version.py）。"""

import os
import unittest
from unittest.mock import AsyncMock, patch

import src.antigravity_cli_version as cliver
from src.antigravity_cli_version import (
    DEFAULT_CLI_VERSION,
    check_once,
    compare_versions,
    get_check_interval,
    get_effective_version,
    get_env_floor,
    get_manifest_url,
    get_status,
    is_valid_version,
    load_adopted_version,
    reset_adopted_version,
)


def _snapshot_state():
    return {key: cliver._state.get(key) for key in cliver._state}


def _restore_state(snapshot):
    for key in cliver._state:
        cliver._state[key] = snapshot.get(key)


class _StatelessCase(unittest.IsolatedAsyncioTestCase):
    """每个用例前后恢复模块级 _state，避免相互污染。"""

    def setUp(self):
        self._state_snapshot = _snapshot_state()
        for key in cliver._state:
            cliver._state[key] = None if key != "adopted_loaded" else False

    def tearDown(self):
        _restore_state(self._state_snapshot)


class VersionValidationTests(unittest.TestCase):
    def test_accepts_strict_semver(self):
        self.assertTrue(is_valid_version("1.3.1"))
        self.assertTrue(is_valid_version("0.0.0"))
        self.assertTrue(is_valid_version("10.20.30"))

    def test_rejects_malformed_and_injection(self):
        self.assertFalse(is_valid_version("1.2"))
        self.assertFalse(is_valid_version("1.2.3.4"))
        self.assertFalse(is_valid_version("v1.2.3"))
        self.assertFalse(is_valid_version("1.2.3-beta"))
        self.assertFalse(is_valid_version("1.2.3\r\nX-Injected: yes"))
        self.assertFalse(is_valid_version("1.2.3 (aidev_client)"))
        self.assertFalse(is_valid_version(""))
        self.assertFalse(is_valid_version(None))
        self.assertFalse(is_valid_version(123))

    def test_compare_versions(self):
        self.assertEqual(compare_versions("1.3.1", "1.3.1"), 0)
        self.assertEqual(compare_versions("1.3.2", "1.3.1"), 1)
        self.assertEqual(compare_versions("1.3.1", "1.3.2"), -1)
        self.assertEqual(compare_versions("1.10.0", "1.9.9"), 1)
        self.assertEqual(compare_versions("2.0.0", "1.99.99"), 1)


class EnvFloorTests(unittest.TestCase):
    def test_default_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_env_floor(), DEFAULT_CLI_VERSION)

    def test_env_value_used_when_valid(self):
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            self.assertEqual(get_env_floor(), "1.2.7")

    def test_invalid_env_falls_back_to_default(self):
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "bogus"}, clear=True):
            self.assertEqual(get_env_floor(), DEFAULT_CLI_VERSION)


class ManifestUrlTests(unittest.TestCase):
    def test_default_url_composition(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                get_manifest_url(),
                "https://antigravity-cli-auto-updater-974169037036."
                "us-central1.run.app/manifests/linux_amd64.json",
            )

    def test_full_json_override(self):
        with patch.dict(
            os.environ,
            {"ANTIGRAVITY_CLI_MANIFEST_URL": "https://example.com/custom.json"},
            clear=True,
        ):
            self.assertEqual(get_manifest_url(), "https://example.com/custom.json")

    def test_base_override_with_os_arch(self):
        with patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_CLI_MANIFEST_URL": "https://example.com/agy/",
                "ANTIGRAVITY_CLI_OS_TYPE": "darwin",
                "ANTIGRAVITY_CLI_ARCH": "arm64",
            },
            clear=True,
        ):
            self.assertEqual(
                get_manifest_url(), "https://example.com/agy/manifests/darwin_arm64.json"
            )


class CheckIntervalTests(unittest.TestCase):
    def test_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_check_interval(), 12 * 3600)

    def test_custom_value(self):
        with patch.dict(
            os.environ, {"ANTIGRAVITY_CLI_VERSION_CHECK_INTERVAL": "3600"}, clear=True
        ):
            self.assertEqual(get_check_interval(), 3600)

    def test_clamped_to_minimum(self):
        with patch.dict(
            os.environ, {"ANTIGRAVITY_CLI_VERSION_CHECK_INTERVAL": "10"}, clear=True
        ):
            self.assertEqual(get_check_interval(), 600)

    def test_invalid_value_uses_default(self):
        with patch.dict(
            os.environ, {"ANTIGRAVITY_CLI_VERSION_CHECK_INTERVAL": "abc"}, clear=True
        ):
            self.assertEqual(get_check_interval(), 12 * 3600)


class EffectiveVersionTests(_StatelessCase):
    async def test_floor_only_when_no_adopted(self):
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            self.assertEqual(get_effective_version(), "1.2.7")

    async def test_adopted_wins_when_newer(self):
        cliver._state["adopted_version"] = "1.3.1"
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            self.assertEqual(get_effective_version(), "1.3.1")
            self.assertEqual(get_status()["source"], "auto-adopted")

    async def test_env_floor_is_floor_not_ceiling_but_wins_when_higher(self):
        cliver._state["adopted_version"] = "1.2.7"
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.3.1"}, clear=True):
            # 保底高于已采纳版本时，保底生效（保底语义）
            self.assertEqual(get_effective_version(), "1.3.1")
            self.assertEqual(get_status()["source"], "env-fallback")

    async def test_default_source_label(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_status()["source"], "default-fallback")


class LoadAdoptedVersionTests(_StatelessCase):
    async def test_loads_valid_persisted_value(self):
        with patch(
            "config.get_config_value", AsyncMock(return_value="1.4.0")
        ):
            await load_adopted_version()
        self.assertEqual(cliver._state["adopted_version"], "1.4.0")
        self.assertTrue(cliver._state["adopted_loaded"])

    async def test_ignores_invalid_persisted_value(self):
        with patch(
            "config.get_config_value", AsyncMock(return_value="not-a-version")
        ):
            await load_adopted_version()
        self.assertIsNone(cliver._state["adopted_version"])

    async def test_tolerates_storage_error(self):
        with patch(
            "config.get_config_value", AsyncMock(side_effect=RuntimeError("db down"))
        ):
            await load_adopted_version()
        self.assertIsNone(cliver._state["adopted_version"])
        self.assertTrue(cliver._state["adopted_loaded"])


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"models": [{}] * 33}
        self.text = ""

    def json(self):
        return self._payload


class _FakeStorage:
    def __init__(self):
        self.config_writes = []
        self.health_writes = []

    async def set_config(self, key, value):
        self.config_writes.append((key, value))
        return True

    async def update_antigravity_account_health(self, filename, **updates):
        # 探测绝不允许写健康状态；被调用即记录供断言
        self.health_writes.append((filename, updates))
        return {}


def _probe_candidates(count=2):
    return [
        (f"cred{i}.json", {"access_token": f"token{i}"}, {"proxy_mode": "direct"})
        for i in range(count)
    ]


class CheckOnceTests(_StatelessCase):
    def _patch_common(self, storage):
        return (
            patch(
                "src.storage_adapter.get_storage_adapter",
                AsyncMock(return_value=storage),
            ),
            patch(
                "config.get_antigravity_api_url",
                AsyncMock(return_value="https://agy.example.com"),
            ),
            patch(
                "src.proxy_groups.proxy_argument_from_network",
                lambda network: None,
            ),
        )

    async def test_manifest_fetch_failure_keeps_state(self):
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            with patch(
                "src.antigravity_cli_version._fetch_manifest",
                AsyncMock(side_effect=RuntimeError("network down")),
            ):
                status = await check_once()
        self.assertFalse(status["last_check_ok"])
        self.assertIn("network down", status["last_check_error"])
        self.assertEqual(status["current_version"], "1.2.7")

    async def test_invalid_manifest_version_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "src.antigravity_cli_version._fetch_manifest",
                AsyncMock(return_value={"version": "1.9.9\r\nX-Evil: 1"}),
            ):
                status = await check_once()
        self.assertFalse(status["last_check_ok"])
        self.assertIn("非法", status["last_check_error"])
        self.assertEqual(status["current_version"], DEFAULT_CLI_VERSION)

    async def test_older_or_same_manifest_version_skips_probe(self):
        probe_mock = AsyncMock()
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.3.1"}, clear=True):
            with (
                patch(
                    "src.antigravity_cli_version._fetch_manifest",
                    AsyncMock(return_value={"version": "1.2.0"}),
                ),
                patch("src.antigravity_cli_version._probe_version", probe_mock),
            ):
                status = await check_once()
        probe_mock.assert_not_called()
        self.assertTrue(status["last_check_ok"])
        self.assertEqual(status["latest_known"], "1.2.0")
        self.assertIsNone(status["pending_version"])

    async def test_newer_version_without_credentials_stays_pending(self):
        storage = _FakeStorage()
        p1, p2, p3 = self._patch_common(storage)
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            with (
                patch(
                    "src.antigravity_cli_version._fetch_manifest",
                    AsyncMock(return_value={"version": "1.3.1"}),
                ),
                patch(
                    "src.antigravity_cli_version._pick_probe_credentials",
                    AsyncMock(return_value=[]),
                ),
                p1,
                p2,
                p3,
            ):
                status = await check_once()
        self.assertEqual(status["current_version"], "1.2.7")
        self.assertEqual(status["pending_version"], "1.3.1")
        self.assertEqual(status["last_probe_result"], "failed")
        self.assertEqual(storage.config_writes, [])
        self.assertEqual(storage.health_writes, [])

    async def test_newer_version_adopted_after_successful_probe(self):
        storage = _FakeStorage()
        p1, p2, p3 = self._patch_common(storage)
        post_mock = AsyncMock(return_value=_FakeResponse(200, {"models": [{}] * 34}))
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            with (
                patch(
                    "src.antigravity_cli_version._fetch_manifest",
                    AsyncMock(return_value={"version": "1.3.1"}),
                ),
                patch(
                    "src.antigravity_cli_version._pick_probe_credentials",
                    AsyncMock(return_value=_probe_candidates(2)),
                ),
                patch("src.httpx_client.post_async", post_mock),
                p1,
                p2,
                p3,
            ):
                status = await check_once()
        self.assertEqual(status["current_version"], "1.3.1")
        self.assertEqual(status["source"], "auto-adopted")
        self.assertIsNone(status["pending_version"])
        self.assertEqual(status["last_probe_result"], "verified")
        self.assertEqual(
            storage.config_writes,
            [("antigravity_cli_version_adopted", "1.3.1")],
        )
        self.assertEqual(storage.health_writes, [])
        # 探测只打了一个凭证就成功
        self.assertEqual(post_mock.await_count, 1)
        # 探测请求头使用候选版本 UA
        headers = post_mock.await_args.kwargs["headers"]
        self.assertIn("antigravity/cli/1.3.1", headers["User-Agent"])

    async def test_auth_failure_falls_through_to_next_credential(self):
        storage = _FakeStorage()
        p1, p2, p3 = self._patch_common(storage)
        post_mock = AsyncMock(
            side_effect=[_FakeResponse(403, {}), _FakeResponse(200, {"models": []})]
        )
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            with (
                patch(
                    "src.antigravity_cli_version._fetch_manifest",
                    AsyncMock(return_value={"version": "1.3.1"}),
                ),
                patch(
                    "src.antigravity_cli_version._pick_probe_credentials",
                    AsyncMock(return_value=_probe_candidates(2)),
                ),
                patch("src.httpx_client.post_async", post_mock),
                p1,
                p2,
                p3,
            ):
                status = await check_once()
        self.assertEqual(post_mock.await_count, 2)
        self.assertEqual(status["current_version"], "1.3.1")

    async def test_all_probes_failed_keeps_current_version(self):
        storage = _FakeStorage()
        p1, p2, p3 = self._patch_common(storage)
        post_mock = AsyncMock(return_value=_FakeResponse(400, {}))
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            with (
                patch(
                    "src.antigravity_cli_version._fetch_manifest",
                    AsyncMock(return_value={"version": "1.3.1"}),
                ),
                patch(
                    "src.antigravity_cli_version._pick_probe_credentials",
                    AsyncMock(return_value=_probe_candidates(2)),
                ),
                patch("src.httpx_client.post_async", post_mock),
                p1,
                p2,
                p3,
            ):
                status = await check_once()
        self.assertEqual(status["current_version"], "1.2.7")
        self.assertEqual(status["pending_version"], "1.3.1")
        self.assertEqual(status["last_probe_result"], "failed")
        self.assertEqual(storage.config_writes, [])
        self.assertEqual(storage.health_writes, [])

    async def test_probe_exception_counts_as_attempt_failure(self):
        storage = _FakeStorage()
        p1, p2, p3 = self._patch_common(storage)
        post_mock = AsyncMock(side_effect=TimeoutError("slow"))
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            with (
                patch(
                    "src.antigravity_cli_version._fetch_manifest",
                    AsyncMock(return_value={"version": "1.3.1"}),
                ),
                patch(
                    "src.antigravity_cli_version._pick_probe_credentials",
                    AsyncMock(return_value=_probe_candidates(1)),
                ),
                patch("src.httpx_client.post_async", post_mock),
                p1,
                p2,
                p3,
            ):
                status = await check_once()
        self.assertEqual(status["current_version"], "1.2.7")
        self.assertEqual(status["last_probe_result"], "failed")


class ResetAdoptedVersionTests(_StatelessCase):
    async def test_reset_clears_adopted_and_persists_empty(self):
        storage = _FakeStorage()
        cliver._state["adopted_version"] = "1.4.0"
        cliver._state["adopted_loaded"] = True
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            with patch(
                "src.storage_adapter.get_storage_adapter",
                AsyncMock(return_value=storage),
            ):
                await reset_adopted_version()
            self.assertEqual(get_effective_version(), "1.2.7")
        self.assertIsNone(cliver._state["adopted_version"])
        self.assertEqual(
            storage.config_writes, [("antigravity_cli_version_adopted", "")]
        )


class UserAgentBuilderTests(_StatelessCase):
    async def test_build_with_explicit_version(self):
        from src.utils import build_antigravity_user_agent

        ua = build_antigravity_user_agent("9.9.9")
        self.assertEqual(
            ua,
            "antigravity/cli/9.9.9 "
            "(aidev_client; os_type=linux; arch=amd64; auth_method=consumer)",
        )

    async def test_get_user_agent_uses_effective_version(self):
        from src.utils import get_antigravity_user_agent

        cliver._state["adopted_version"] = "1.4.0"
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            self.assertIn("antigravity/cli/1.4.0", get_antigravity_user_agent())

    async def test_oauth_user_agent_follows_effective_version(self):
        from src.utils import get_antigravity_oauth_user_agent

        cliver._state["adopted_version"] = "1.4.0"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("src.utils.ANTIGRAVITY_OAUTH_UA_VERSION", ""),
        ):
            self.assertEqual(
                get_antigravity_oauth_user_agent(),
                "vscode/1.X.X (Antigravity/1.4.0)",
            )

    async def test_oauth_user_agent_env_explicit_override(self):
        from src.utils import get_antigravity_oauth_user_agent

        with patch.dict(
            os.environ,
            {"ANTIGRAVITY_OAUTH_USER_AGENT": "custom-ua/1.0"},
            clear=True,
        ):
            self.assertEqual(get_antigravity_oauth_user_agent(), "custom-ua/1.0")

    async def test_oauth_user_agent_version_pin(self):
        from src.utils import get_antigravity_oauth_user_agent

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("src.utils.ANTIGRAVITY_OAUTH_UA_VERSION", "1.1.1"),
        ):
            self.assertEqual(
                get_antigravity_oauth_user_agent(),
                "vscode/1.X.X (Antigravity/1.1.1)",
            )

    async def test_build_headers_version_override(self):
        from src.api.antigravity import build_antigravity_headers

        headers = build_antigravity_headers("tok", version_override="2.0.0")
        self.assertIn("antigravity/cli/2.0.0", headers["User-Agent"])
        self.assertEqual(headers["Authorization"], "Bearer tok")

    async def test_build_headers_default_uses_effective_version(self):
        from src.api.antigravity import build_antigravity_headers

        cliver._state["adopted_version"] = "1.4.0"
        with patch.dict(os.environ, {"ANTIGRAVITY_CLI_VERSION": "1.2.7"}, clear=True):
            headers = build_antigravity_headers("tok")
        self.assertIn("antigravity/cli/1.4.0", headers["User-Agent"])


if __name__ == "__main__":
    unittest.main()
