"""补环境调用（warm_antigravity_environment）契约测试。

官方客户端抓包（antigravity-re/run/capture.log）显示账号激活后会调用
fetchUserInfo 与 listExperiments（带 updateChannel:"stable"）；
warmup 只在登录/检验/健康检查等低频路径触发，且任何失败不得影响主流程。
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_warmup_fires_official_environment_calls():
    from src import google_oauth_api

    calls = []

    async def fake_post(url, json=None, headers=None, timeout=None, proxy_url=None):
        calls.append({"url": url, "json": json, "proxy_url": proxy_url})

        class _Resp:
            status_code = 200

            def json(self):
                return {}

        return _Resp()

    with patch.object(google_oauth_api, "post_async", fake_post):
        await google_oauth_api.warm_antigravity_environment(
            "https://daily-cloudcode-pa.googleapis.com",
            {"Authorization": "Bearer t"},
            "project-1",
            proxy_url=None,
        )

    assert [c["url"] for c in calls] == [
        "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchUserInfo",
        "https://daily-cloudcode-pa.googleapis.com/v1internal:listExperiments",
    ]
    assert calls[0]["json"] == {"project": "project-1"}
    assert calls[1]["json"] == {
        "project": "project-1",
        "metadata": {"updateChannel": "stable"},
    }


@pytest.mark.asyncio
async def test_warmup_swallows_failures_and_skips_empty_project():
    from src import google_oauth_api

    async def failing_post(*args, **kwargs):
        raise TimeoutError("read timeout")

    with patch.object(google_oauth_api, "post_async", failing_post):
        # 不抛异常
        await google_oauth_api.warm_antigravity_environment(
            "https://daily-cloudcode-pa.googleapis.com",
            {},
            "project-1",
            proxy_url=None,
        )

    calls = []

    async def counting_post(*args, **kwargs):
        calls.append(args)

    with patch.object(google_oauth_api, "post_async", counting_post):
        await google_oauth_api.warm_antigravity_environment(
            "https://daily-cloudcode-pa.googleapis.com", {}, "", proxy_url=None
        )
        await google_oauth_api.warm_antigravity_environment(
            "https://daily-cloudcode-pa.googleapis.com", {}, None, proxy_url=None
        )

    assert calls == []


@pytest.mark.asyncio
async def test_fetch_project_id_and_tier_triggers_warmup_on_success():
    from src import google_oauth_api

    warmup_calls = []

    async def fake_load_code_assist(*args, **kwargs):
        return "project-1", "PRO", None

    async def fake_warmup(base_url, headers, project_id, *, proxy_url=None):
        warmup_calls.append((base_url, project_id))

    with (
        patch.object(
            google_oauth_api, "_try_load_code_assist", fake_load_code_assist
        ),
        patch.object(
            google_oauth_api, "warm_antigravity_environment", fake_warmup
        ),
    ):
        project_id, tier = await google_oauth_api.fetch_project_id_and_tier(
            "token", "ua", "https://daily-cloudcode-pa.googleapis.com"
        )

    assert (project_id, tier) == ("project-1", "PRO")
    assert warmup_calls == [
        ("https://daily-cloudcode-pa.googleapis.com", "project-1")
    ]
