import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}"
        self.headers = {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_image_wrapper_uses_image_generation_credit_contract():
    from src.api import antigravity

    state = antigravity.AntigravitySessionState(
        conversation_id="conv",
        trajectory_id="traj",
        session_id="sess",
        step_index=0,
        created_at=0.0,
        last_used_at=0.0,
    )
    with patch.object(antigravity, "_get_session_state", AsyncMock(return_value=state)):
        payload, _ = await antigravity.wrap_cli_request(
            {
                "contents": [{"role": "user", "parts": [{"text": "draw"}]}],
                "generationConfig": {"responseModalities": ["IMAGE"]},
            },
            "gemini-3.1-flash-image",
            "project-1",
            user_email="image@example.com",
        )

    assert payload["requestType"] == "image_gen"
    assert "enabledCreditTypes" not in payload
    assert "sessionId" not in payload["request"]
    assert "labels" not in payload["request"]
    assert "toolConfig" not in payload["request"]


def test_image_headers_match_native_cli_without_quota_project_headers():
    from src.api import antigravity

    headers = antigravity.build_antigravity_headers(
        "redacted",
        image_request=True,
        project_id="project-1",
    )

    assert headers == {
        "User-Agent": antigravity.ANTIGRAVITY_USER_AGENT,
        "Authorization": "Bearer redacted",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }


@pytest.mark.asyncio
async def test_agent_wrapper_keeps_google_one_credit_contract():
    from src.api import antigravity

    state = antigravity.AntigravitySessionState(
        conversation_id="conv",
        trajectory_id="traj",
        session_id="sess",
        step_index=0,
        created_at=0.0,
        last_used_at=0.0,
    )
    with patch.object(antigravity, "_get_session_state", AsyncMock(return_value=state)):
        payload, _ = await antigravity.wrap_cli_request(
            {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
            "gemini-3.1-pro-preview",
            "project-1",
            user_email="agent@example.com",
        )

    assert payload["requestType"] == "agent"
    assert payload["enabledCreditTypes"] == ["GOOGLE_ONE_AI"]


def test_dynamic_image_model_candidates_stay_in_requested_tier():
    from src.api.antigravity import build_dynamic_image_model_candidates

    assert build_dynamic_image_model_candidates("gemini-3.1-flash-image") == [
        "gemini-3.1-flash-image",
        "gemini-3-flash-image",
    ]
    assert build_dynamic_image_model_candidates("gemini-3-pro-image") == [
        "gemini-3-pro-image",
        "gemini-3.1-pro-image",
    ]
    assert "gemini-3.1-flash-image" not in build_dynamic_image_model_candidates(
        "gemini-3-pro-image"
    )


@pytest.mark.asyncio
async def test_dynamic_image_model_uses_account_available_same_tier_alias(monkeypatch):
    from src.api import antigravity

    async def fake_available_models(**kwargs):
        return {"gemini-3-flash-image", "gemini-3.1-pro-image"}

    monkeypatch.setattr(antigravity, "_get_available_models_for_credential", fake_available_models)

    resolved = await antigravity.resolve_dynamic_image_model_for_credential(
        credential_name="ultra.json",
        credential_data={"access_token": "token", "project_id": "project"},
        requested_model="gemini-3.1-flash-image",
    )

    assert resolved == "gemini-3-flash-image"


@pytest.mark.asyncio
async def test_dynamic_image_model_cache_prunes_expired_entries_and_stays_bounded(
    monkeypatch,
):
    from src.api import antigravity

    now = time.monotonic()
    antigravity._image_available_models_cache.clear()
    antigravity._image_available_models_cache.update(
        {
            f"expired-{index}:project": (
                now - antigravity.IMAGE_MODEL_CACHE_TTL_SECONDS - 1,
                {"gemini-3.1-flash-image"},
            )
            for index in range(antigravity.MAX_IMAGE_MODEL_CACHE_ENTRIES + 1)
        }
    )

    upstream = AsyncMock(
        return_value=FakeResponse(
            200,
            {
                "models": {
                    "gemini-3.1-flash-image": {},
                }
            },
        )
    )
    monkeypatch.setattr(antigravity, "post_async", upstream)
    monkeypatch.setattr(
        antigravity,
        "get_antigravity_api_url",
        lambda: async_value("https://daily-cloudcode-pa.googleapis.com"),
    )

    available = await antigravity._get_available_models_for_credential(
        credential_name="current.json",
        credential_data={
            "access_token": "redacted",
            "project_id": "current-project",
        },
    )

    assert available == {"gemini-3.1-flash-image"}
    assert len(antigravity._image_available_models_cache) <= (
        antigravity.MAX_IMAGE_MODEL_CACHE_ENTRIES
    )
    assert all(
        not cache_key.startswith("expired-")
        for cache_key in antigravity._image_available_models_cache
    )
    assert upstream.await_args.kwargs["url"] == (
        "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
    )


@pytest.mark.asyncio
async def test_dynamic_image_model_lookup_stops_after_project_403(
    monkeypatch,
):
    from src.api import antigravity

    antigravity._image_available_models_cache.clear()
    upstream = AsyncMock(
        return_value=FakeResponse(403, {"error": {"message": "project rejected"}})
    )
    monkeypatch.setattr(antigravity, "post_async", upstream)
    monkeypatch.setattr(
        antigravity,
        "get_antigravity_api_url",
        lambda: async_value("https://daily-cloudcode-pa.googleapis.com"),
    )

    available = await antigravity._get_available_models_for_credential(
        credential_name="ultra.json",
        credential_data={
            "access_token": "redacted",
            "project_id": "project-1",
        },
    )

    assert available == set()
    upstream.assert_awaited_once()
    call = upstream.await_args.kwargs
    assert call["url"] == (
        "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
    )
    assert call["json"] == {"project": "project-1"}
    assert "x-goog-user-project" not in call["headers"]


def test_image_account_priority_prefers_least_used_before_tier():
    from src.credential_manager import rank_antigravity_image_candidates

    now = time.time()
    ranked = rank_antigravity_image_candidates(
        {
            "pro.json": {
                "disabled": False,
                "tier": "pro",
                "call_count": 0,
                "rotation_order": 1,
                "last_success": now,
                "model_cooldowns": {},
            },
            "ultra.json": {
                "disabled": False,
                "tier": "ultra",
                "call_count": 4,
                "rotation_order": 0,
                "last_success": now - 100,
                "model_cooldowns": {},
            },
        },
        model_name="gemini-3.1-flash-image",
        runtime_health={},
        now=now,
    )

    assert ranked == ["pro.json", "ultra.json"]


def test_image_account_priority_uses_tier_only_when_usage_is_equal():
    from src.credential_manager import rank_antigravity_image_candidates

    ranked = rank_antigravity_image_candidates(
        {
            "pro.json": {
                "disabled": False,
                "tier": "pro",
                "call_count": 3,
                "rotation_order": 0,
                "model_cooldowns": {},
            },
            "ultra.json": {
                "disabled": False,
                "tier": "ultra",
                "call_count": 3,
                "rotation_order": 1,
                "model_cooldowns": {},
            },
        },
        model_name="gemini-3.1-flash-image",
    )

    assert ranked == ["ultra.json", "pro.json"]


def test_recent_image_success_does_not_create_sticky_routing():
    from src.credential_manager import rank_antigravity_image_candidates

    now = time.time()
    ranked = rank_antigravity_image_candidates(
        {
            "recent.json": {
                "disabled": False,
                "tier": "pro",
                "call_count": 8,
                "rotation_order": 0,
                "model_cooldowns": {},
            },
            "underused.json": {
                "disabled": False,
                "tier": "pro",
                "call_count": 2,
                "rotation_order": 1,
                "model_cooldowns": {},
            },
        },
        model_name="gemini-3.1-flash-image",
        runtime_health={"recent.json": {"last_success_at": now - 1}},
        now=now,
    )

    assert ranked == ["underused.json", "recent.json"]


def test_recent_ultra_capacity_failure_temporarily_promotes_pro():
    from src.credential_manager import rank_antigravity_image_candidates

    now = time.time()
    ranked = rank_antigravity_image_candidates(
        {
            "pro.json": {
                "disabled": False,
                "tier": "pro",
                "last_success": now - 200,
                "model_cooldowns": {},
            },
            "ultra.json": {
                "disabled": False,
                "tier": "utrl",
                "last_success": now - 100,
                "model_cooldowns": {},
            },
        },
        model_name="gemini-3.1-flash-image",
        runtime_health={
            "ultra.json": {
                "last_capacity_failure_at": now - 5,
                "last_success_at": 0,
            }
        },
        now=now,
    )

    assert ranked == ["pro.json", "ultra.json"]


@pytest.mark.asyncio
async def test_image_generate_uses_configured_daily_host_without_host_fallback(
    monkeypatch,
):
    from src.api import antigravity

    calls = []

    async def fake_post(**kwargs):
        calls.append(kwargs["url"])
        return FakeResponse(503, {"error": "capacity unavailable"})

    monkeypatch.setattr(antigravity, "post_async", fake_post)
    monkeypatch.setattr(
        antigravity,
        "get_antigravity_api_url",
        lambda: async_value("https://daily-cloudcode-pa.googleapis.com"),
    )

    result = await antigravity._post_image_generate(
        headers={"Authorization": "Bearer redacted"},
        json_body={"requestType": "image_gen"},
        proxy_url=None,
    )

    assert result.response.status_code == 503
    assert result.host == "https://daily-cloudcode-pa.googleapis.com"
    assert len(result.attempts) == 1
    assert calls == [
        "https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent",
    ]


@pytest.mark.asyncio
async def test_image_generate_respects_custom_target_route_for_account_429(
    monkeypatch,
):
    from src.api import antigravity

    calls = []

    async def fake_post(**kwargs):
        calls.append(kwargs["url"])
        return FakeResponse(429, {"error": "quota"})

    monkeypatch.setattr(antigravity, "post_async", fake_post)
    monkeypatch.setattr(
        antigravity,
        "get_antigravity_api_url",
        lambda: async_value("https://antigravity.invalid/base"),
    )

    result = await antigravity._post_image_generate(
        headers={"Authorization": "Bearer redacted"},
        json_body={"requestType": "image_gen"},
        proxy_url=None,
    )

    assert result.response.status_code == 429
    assert calls == [
        "https://antigravity.invalid/base/v1internal:generateContent",
    ]


@pytest.mark.asyncio
async def test_image_generate_returns_project_403_without_header_retry(monkeypatch):
    from src.api import antigravity

    sent_headers = []

    async def fake_post(**kwargs):
        sent_headers.append(dict(kwargs["headers"]))
        return FakeResponse(403)

    monkeypatch.setattr(antigravity, "post_async", fake_post)
    monkeypatch.setattr(
        antigravity,
        "get_antigravity_api_url",
        lambda: async_value("https://daily-cloudcode-pa.googleapis.com"),
    )

    result = await antigravity._post_image_generate(
        headers={"Authorization": "Bearer redacted"},
        json_body={"requestType": "image_gen"},
        proxy_url=None,
    )

    assert result.response.status_code == 403
    assert len(result.attempts) == 1
    assert sent_headers == [
        {"Authorization": "Bearer redacted"}
    ]


@pytest.mark.asyncio
async def test_image_generate_retries_capacity_using_upstream_retry_info(monkeypatch):
    from src.api import antigravity

    responses = [
        FakeResponse(
            503,
            {
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "2s",
                        },
                        {"reason": "MODEL_CAPACITY_EXHAUSTED"},
                    ]
                }
            },
        ),
        FakeResponse(200, {"response": {"candidates": []}}),
    ]
    upstream = AsyncMock(side_effect=responses)
    sleep = AsyncMock()
    monkeypatch.setattr(antigravity, "post_async", upstream)
    monkeypatch.setattr(antigravity.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        antigravity,
        "get_antigravity_api_url",
        lambda: async_value("https://daily-cloudcode-pa.googleapis.com"),
    )

    result = await antigravity._post_image_generate(
        headers={"Authorization": "Bearer redacted"},
        json_body={"requestType": "image_gen"},
        proxy_url=None,
    )

    assert result.response.status_code == 200
    assert len(result.attempts) == 2
    assert upstream.await_count == 2
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_image_credential_selection_uses_tier_and_exclusions(monkeypatch):
    from src.credential_manager import CredentialManager

    class FakeAdapter:
        async def get_all_credential_states(self, mode):
            assert mode == "antigravity"
            return {
                "pro.json": {
                    "disabled": False,
                    "tier": "pro",
                    "last_success": 20,
                    "model_cooldowns": {},
                    "user_email": "pro@example.com",
                },
                "ultra.json": {
                    "disabled": False,
                    "tier": "ultra",
                    "last_success": 10,
                    "model_cooldowns": {},
                    "user_email": "ultra@example.com",
                },
            }

        async def get_credential(self, filename, mode):
            return {
                "access_token": f"token-{filename}",
                "project_id": f"project-{filename}",
                "expiry": "2999-01-01T00:00:00+00:00",
            }

        async def resolve_credential_network(self, filename, mode):
            return {"proxy_mode": "direct", "proxy_url": None}

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter()
    monkeypatch.setattr(
        manager,
        "ensure_antigravity_account_ready",
        lambda filename, data: async_value(data),
    )

    first = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )
    second = await manager.get_valid_credential(
        mode="antigravity",
        model_name="gemini-3.1-flash-image",
        exclude_filenames={"ultra.json"},
    )

    assert first[0] == "ultra.json"
    assert first[1]["tier"] == "ultra"
    assert second[0] == "pro.json"


@pytest.mark.asyncio
async def test_image_selection_advances_usage_before_reusing_account(monkeypatch):
    from src.credential_manager import CredentialManager

    class FakeAdapter:
        def __init__(self):
            self.states = {
                "first.json": {
                    "disabled": False,
                    "tier": "pro",
                    "call_count": 0,
                    "rotation_order": 0,
                    "model_cooldowns": {},
                },
                "second.json": {
                    "disabled": False,
                    "tier": "pro",
                    "call_count": 0,
                    "rotation_order": 1,
                    "model_cooldowns": {},
                },
            }

        async def get_all_credential_states(self, mode):
            return {name: dict(state) for name, state in self.states.items()}

        async def mark_credential_selected(
            self, filename, *, mode, expected_call_count
        ):
            state = self.states[filename]
            if state["call_count"] != expected_call_count:
                return False
            state["call_count"] += 1
            return True

        async def get_credential(self, filename, mode):
            return {
                "access_token": f"token-{filename}",
                "project_id": f"project-{filename}",
                "expiry": "2999-01-01T00:00:00+00:00",
            }

        async def resolve_credential_network(self, filename, mode):
            return {"proxy_mode": "direct", "proxy_url": None}

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter()
    monkeypatch.setattr(
        manager,
        "ensure_antigravity_account_ready",
        lambda filename, data: async_value(data),
    )

    first = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )
    await manager.release_image_credential(
        first[0], first[1]["_image_lease_id"]
    )
    second = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )

    assert first[0] == "first.json"
    assert second[0] == "second.json"
    assert manager._storage_adapter.states["first.json"]["call_count"] == 1
    assert manager._storage_adapter.states["second.json"]["call_count"] == 1
    await manager.release_image_credential(
        second[0], second[1]["_image_lease_id"]
    )


@pytest.mark.asyncio
async def test_image_selection_refreshes_after_cross_worker_claim_conflict(
    monkeypatch,
):
    from src.credential_manager import CredentialManager

    class FakeAdapter:
        def __init__(self):
            self.states = {
                "contended.json": {
                    "disabled": False,
                    "tier": "pro",
                    "call_count": 0,
                    "rotation_order": 0,
                    "model_cooldowns": {},
                },
                "available.json": {
                    "disabled": False,
                    "tier": "pro",
                    "call_count": 0,
                    "rotation_order": 1,
                    "model_cooldowns": {},
                },
            }

        async def get_all_credential_states(self, mode):
            return {name: dict(state) for name, state in self.states.items()}

        async def mark_credential_selected(
            self, filename, *, mode, expected_call_count
        ):
            state = self.states[filename]
            if filename == "contended.json" and state["call_count"] == 0:
                state["call_count"] = 1
                return False
            if state["call_count"] != expected_call_count:
                return False
            state["call_count"] += 1
            return True

        async def get_credential(self, filename, mode):
            return {
                "access_token": f"token-{filename}",
                "project_id": f"project-{filename}",
                "expiry": "2999-01-01T00:00:00+00:00",
            }

        async def resolve_credential_network(self, filename, mode):
            return {"proxy_mode": "direct", "proxy_url": None}

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter()
    monkeypatch.setattr(
        manager,
        "ensure_antigravity_account_ready",
        lambda filename, data: async_value(data),
    )

    selected = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )

    assert selected[0] == "available.json"
    await manager.release_image_credential(
        selected[0], selected[1]["_image_lease_id"]
    )


@pytest.mark.asyncio
async def test_explicit_image_request_uses_image_pool_for_non_image_model_alias(
    monkeypatch,
):
    from src.credential_manager import CredentialManager

    class FakeAdapter:
        async def get_all_credential_states(self, mode):
            return {
                "ultra.json": {
                    "disabled": False,
                    "tier": "ultra",
                    "model_cooldowns": {},
                }
            }

        async def get_credential(self, filename, mode):
            return {
                "access_token": "token",
                "project_id": "project",
                "expiry": "2999-01-01T00:00:00+00:00",
            }

        async def resolve_credential_network(self, filename, mode):
            return {"proxy_mode": "direct", "proxy_url": None}

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter()
    monkeypatch.setattr(
        manager,
        "ensure_antigravity_account_ready",
        lambda filename, data: async_value(data),
    )

    selected = await manager.get_valid_credential(
        mode="antigravity",
        model_name="gemini-3.1-flash",
        image_request=True,
    )

    assert selected[0] == "ultra.json"
    assert selected[1]["_image_lease_id"]
    await manager.release_image_credential(
        selected[0], selected[1]["_image_lease_id"]
    )


@pytest.mark.asyncio
async def test_image_credential_selection_reserves_distinct_accounts_until_release(
    monkeypatch,
):
    from src.credential_manager import CredentialManager

    class FakeAdapter:
        async def get_all_credential_states(self, mode):
            return {
                "pro.json": {
                    "disabled": False,
                    "tier": "pro",
                    "model_cooldowns": {},
                },
                "ultra.json": {
                    "disabled": False,
                    "tier": "ultra",
                    "model_cooldowns": {},
                },
            }

        async def get_credential(self, filename, mode):
            return {
                "access_token": f"token-{filename}",
                "project_id": f"project-{filename}",
                "expiry": "2999-01-01T00:00:00+00:00",
            }

        async def resolve_credential_network(self, filename, mode):
            return {"proxy_mode": "direct", "proxy_url": None}

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter()
    monkeypatch.setattr(
        manager,
        "ensure_antigravity_account_ready",
        lambda filename, data: async_value(data),
    )

    first = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )
    second = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )

    assert first[0] == "ultra.json"
    assert second[0] == "pro.json"

    waiting = asyncio.create_task(
        manager.get_valid_credential(
            mode="antigravity", model_name="gemini-3.1-flash-image"
        )
    )
    await asyncio.sleep(0)
    assert not waiting.done()

    released = await manager.release_image_credential(
        first[0], first[1]["_image_lease_id"]
    )
    assert released is True
    third = await asyncio.wait_for(waiting, timeout=1)
    assert third[0] == "ultra.json"

    await manager.release_image_credential(
        second[0], second[1]["_image_lease_id"]
    )
    await manager.release_image_credential(
        third[0], third[1]["_image_lease_id"]
    )


@pytest.mark.asyncio
async def test_image_credential_release_is_token_safe_and_resettable(monkeypatch):
    from src.credential_manager import CredentialManager

    class FakeAdapter:
        async def get_all_credential_states(self, mode):
            return {
                "ultra.json": {
                    "disabled": False,
                    "tier": "ultra",
                    "model_cooldowns": {},
                }
            }

        async def get_credential(self, filename, mode):
            return {
                "access_token": "token",
                "project_id": "project",
                "expiry": "2999-01-01T00:00:00+00:00",
            }

        async def resolve_credential_network(self, filename, mode):
            return {"proxy_mode": "direct", "proxy_url": None}

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter()
    monkeypatch.setattr(
        manager,
        "ensure_antigravity_account_ready",
        lambda filename, data: async_value(data),
    )

    first = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )
    first_lease_id = first[1]["_image_lease_id"]
    assert await manager.release_image_credential("ultra.json", first_lease_id)

    second = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )
    second_lease_id = second[1]["_image_lease_id"]
    assert second_lease_id != first_lease_id
    assert not await manager.release_image_credential("ultra.json", first_lease_id)

    waiting = asyncio.create_task(
        manager.get_valid_credential(
            mode="antigravity", model_name="gemini-3.1-flash-image"
        )
    )
    await asyncio.sleep(0)
    assert not waiting.done()

    await manager.reset_image_account_leases()
    selected_after_reset = await asyncio.wait_for(waiting, timeout=1)
    assert selected_after_reset[0] == "ultra.json"
    await manager.release_image_credential(
        selected_after_reset[0], selected_after_reset[1]["_image_lease_id"]
    )

    selected_before_close = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )
    await manager.close()
    assert not await manager.release_image_credential(
        selected_before_close[0], selected_before_close[1]["_image_lease_id"]
    )


@pytest.mark.asyncio
async def test_image_credential_selection_prunes_removed_account_health(monkeypatch):
    from src.credential_manager import CredentialManager

    class FakeAdapter:
        async def get_all_credential_states(self, mode):
            return {
                "current.json": {
                    "disabled": False,
                    "tier": "pro",
                    "last_success": 0,
                    "model_cooldowns": {},
                }
            }

        async def get_credential(self, filename, mode):
            return {
                "access_token": "token",
                "project_id": "project",
                "expiry": "2999-01-01T00:00:00+00:00",
            }

        async def resolve_credential_network(self, filename, mode):
            return {"proxy_mode": "direct", "proxy_url": None}

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = FakeAdapter()
    manager._image_runtime_health = {
        "removed.json": {"last_capacity_failure_at": time.time()},
    }
    monkeypatch.setattr(
        manager,
        "ensure_antigravity_account_ready",
        lambda filename, data: async_value(data),
    )

    selected = await manager.get_valid_credential(
        mode="antigravity", model_name="gemini-3.1-flash-image"
    )

    assert selected[0] == "current.json"
    assert "removed.json" not in manager._image_runtime_health


@pytest.mark.asyncio
async def test_image_non_stream_rotates_capacity_without_account_unavailable_alert(
    monkeypatch,
):
    import httpx

    from src.api import antigravity

    class FakeCredentialManager:
        def __init__(self):
            self.selections = []
            self.outcomes = []
            self.releases = []

        async def get_valid_credential(self, **kwargs):
            excluded = set(kwargs.get("exclude_filenames") or set())
            self.selections.append(excluded)
            if "ultra.json" not in excluded:
                return "ultra.json", {
                    "access_token": "ultra-token",
                    "project_id": "ultra-project",
                    "tier": "ultra",
                    "user_email": "ultra@example.com",
                    "proxy_mode": "direct",
                    "_image_lease_id": "lease-ultra",
                }
            if "pro.json" not in excluded:
                return "pro.json", {
                    "access_token": "pro-token",
                    "project_id": "pro-project",
                    "tier": "pro",
                    "user_email": "pro@example.com",
                    "proxy_mode": "direct",
                    "_image_lease_id": "lease-pro",
                }
            return None

        def record_image_account_outcome(self, credential_name, **kwargs):
            self.outcomes.append((credential_name, kwargs))

        async def release_image_credential(self, credential_name, lease_id):
            self.releases.append((credential_name, lease_id))
            return True

    manager = FakeCredentialManager()
    upstream_calls = []

    async def fake_resolve_dynamic_model(**kwargs):
        return (
            "gemini-3-flash-image"
            if kwargs["credential_name"] == "ultra.json"
            else "gemini-3.1-flash-image"
        )

    async def fake_wrap(request, model, project, user_email=None):
        return {
            "project": project,
            "model": model,
            "request": request,
            "requestType": "image_gen",
        }, f"request-{project}"

    async def fake_image_post(**kwargs):
        upstream_calls.append(kwargs)
        if kwargs["json_body"]["project"] == "ultra-project":
            response = httpx.Response(
                503,
                json={"error": {"details": [{"reason": "MODEL_CAPACITY_EXHAUSTED"}]}},
            )
            return antigravity.ImageUpstreamResult(response, "sandbox", ())
        response = httpx.Response(
            200,
            json={"response": {"candidates": [], "usageMetadata": {}}},
        )
        return antigravity.ImageUpstreamResult(response, "prod", ())

    class FakeRecorder:
        async def record(self, **kwargs):
            return True

    alert = AsyncMock(return_value=True)
    persistent_error = AsyncMock(return_value=None)
    monkeypatch.setattr(antigravity, "credential_manager", manager)
    monkeypatch.setattr(antigravity, "get_antigravity_stream2nostream", lambda: async_value(True))
    monkeypatch.setattr(
        antigravity,
        "get_retry_config",
        lambda: async_value({"retry_enabled": True, "max_retries": 5, "retry_interval": 0}),
    )
    monkeypatch.setattr(antigravity, "get_auto_ban_error_codes", lambda: async_value([]))
    monkeypatch.setattr(
        antigravity,
        "resolve_dynamic_image_model_for_credential",
        fake_resolve_dynamic_model,
    )
    monkeypatch.setattr(antigravity, "wrap_cli_request", fake_wrap)
    monkeypatch.setattr(antigravity, "_post_image_generate", fake_image_post)
    monkeypatch.setattr(antigravity, "record_api_call_error", persistent_error)
    monkeypatch.setattr(antigravity, "record_api_call_success", AsyncMock(return_value=None))
    monkeypatch.setattr(antigravity, "get_billing_recorder", lambda: async_value(FakeRecorder()))
    monkeypatch.setattr(antigravity, "send_all_accounts_unavailable_alert", alert)
    monkeypatch.setattr(
        antigravity,
        "stream_request",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("image used streaming")),
    )

    response = await antigravity.non_stream_request(
        {
            "model": "gemini-3.1-flash-image",
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "draw"}]}],
                "generationConfig": {"responseModalities": ["IMAGE"]},
            },
        }
    )

    assert response.status_code == 200
    assert manager.selections == [set(), {"ultra.json"}]
    assert [call["json_body"]["model"] for call in upstream_calls] == [
        "gemini-3-flash-image",
        "gemini-3.1-flash-image",
    ]
    assert manager.outcomes[0][0] == "ultra.json"
    assert manager.outcomes[0][1]["capacity_failure"] is True
    assert manager.outcomes[1][0] == "pro.json"
    assert manager.outcomes[1][1]["success"] is True
    assert manager.releases == [
        ("ultra.json", "lease-ultra"),
        ("pro.json", "lease-pro"),
    ]
    persistent_error.assert_not_awaited()
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_image_capacity_failures_return_503_without_account_alert(monkeypatch):
    import httpx

    from src.api import antigravity

    class FakeCredentialManager:
        def __init__(self):
            self.outcomes = []
            self.releases = []

        async def get_valid_credential(self, **kwargs):
            excluded = set(kwargs.get("exclude_filenames") or set())
            for filename, tier in (("ultra.json", "ultra"), ("pro.json", "pro")):
                if filename not in excluded:
                    return filename, {
                        "access_token": f"token-{tier}",
                        "project_id": f"project-{tier}",
                        "tier": tier,
                        "proxy_mode": "direct",
                        "_image_lease_id": f"lease-{tier}",
                    }
            return None

        def record_image_account_outcome(self, credential_name, **kwargs):
            self.outcomes.append((credential_name, kwargs))

        async def release_image_credential(self, credential_name, lease_id):
            self.releases.append((credential_name, lease_id))
            return True

    manager = FakeCredentialManager()

    async def fake_image_post(**kwargs):
        response = httpx.Response(
            503,
            json={"error": {"details": [{"reason": "MODEL_CAPACITY_EXHAUSTED"}]}},
        )
        return antigravity.ImageUpstreamResult(response, "prod", ())

    async def fake_wrap(request, model, project, user_email=None):
        return {
            "project": project,
            "model": model,
            "request": request,
            "requestType": "image_gen",
        }, f"request-{project}"

    class FakeRecorder:
        async def record(self, **kwargs):
            return True

    alert = AsyncMock(return_value=True)
    persistent_error = AsyncMock(return_value=None)
    monkeypatch.setattr(antigravity, "credential_manager", manager)
    monkeypatch.setattr(
        antigravity,
        "get_retry_config",
        lambda: async_value({"retry_enabled": True, "max_retries": 1, "retry_interval": 0}),
    )
    monkeypatch.setattr(antigravity, "get_auto_ban_error_codes", lambda: async_value([]))
    monkeypatch.setattr(
        antigravity,
        "resolve_dynamic_image_model_for_credential",
        lambda **kwargs: async_value(kwargs["requested_model"]),
    )
    monkeypatch.setattr(antigravity, "wrap_cli_request", fake_wrap)
    monkeypatch.setattr(antigravity, "_post_image_generate", fake_image_post)
    monkeypatch.setattr(antigravity, "record_api_call_error", persistent_error)
    monkeypatch.setattr(antigravity, "get_billing_recorder", lambda: async_value(FakeRecorder()))
    monkeypatch.setattr(antigravity, "send_all_accounts_unavailable_alert", alert)

    response = await antigravity.non_stream_request(
        {
            "model": "gemini-3.1-flash-image",
            "request": {"contents": [{"role": "user", "parts": [{"text": "draw"}]}]},
        }
    )

    assert response.status_code == 503
    assert [item[0] for item in manager.outcomes] == ["ultra.json", "pro.json"]
    assert all(item[1]["capacity_failure"] for item in manager.outcomes)
    assert manager.releases == [
        ("ultra.json", "lease-ultra"),
        ("pro.json", "lease-pro"),
    ]
    persistent_error.assert_not_awaited()
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_non_stream_releases_account_when_request_is_cancelled(monkeypatch):
    from src.api import antigravity

    class FakeCredentialManager:
        def __init__(self):
            self.releases = []

        async def get_valid_credential(self, **kwargs):
            return "ultra.json", {
                "access_token": "ultra-token",
                "project_id": "ultra-project",
                "tier": "ultra",
                "proxy_mode": "direct",
                "_image_lease_id": "lease-ultra",
            }

        def record_image_account_outcome(self, credential_name, **kwargs):
            return None

        async def release_image_credential(self, credential_name, lease_id):
            self.releases.append((credential_name, lease_id))
            return True

    manager = FakeCredentialManager()
    entered_upstream = asyncio.Event()

    async def fake_image_post(**kwargs):
        entered_upstream.set()
        await asyncio.Event().wait()

    async def fake_wrap(request, model, project, user_email=None):
        return {
            "project": project,
            "model": model,
            "request": request,
            "requestType": "image_gen",
        }, f"request-{project}"

    monkeypatch.setattr(antigravity, "credential_manager", manager)
    monkeypatch.setattr(
        antigravity,
        "get_retry_config",
        lambda: async_value({"retry_enabled": True, "max_retries": 1, "retry_interval": 0}),
    )
    monkeypatch.setattr(antigravity, "get_auto_ban_error_codes", lambda: async_value([]))
    monkeypatch.setattr(
        antigravity,
        "resolve_dynamic_image_model_for_credential",
        lambda **kwargs: async_value(kwargs["requested_model"]),
    )
    monkeypatch.setattr(antigravity, "wrap_cli_request", fake_wrap)
    monkeypatch.setattr(antigravity, "_post_image_generate", fake_image_post)

    task = asyncio.create_task(
        antigravity.non_stream_request(
            {
                "model": "gemini-3.1-flash-image",
                "request": {"contents": [{"role": "user", "parts": [{"text": "draw"}]}]},
            }
        )
    )
    await asyncio.wait_for(entered_upstream.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.releases == [("ultra.json", "lease-ultra")]


@pytest.mark.asyncio
async def test_image_stream_uses_configured_route_and_releases_on_cancel(
    monkeypatch,
):
    from src.api import antigravity

    class FakeCredentialManager:
        def __init__(self):
            self.releases = []

        async def get_valid_credential(self, **kwargs):
            return "ultra.json", {
                "access_token": "ultra-token",
                "project_id": "ultra-project",
                "tier": "ultra",
                "proxy_mode": "direct",
                "_image_lease_id": "lease-ultra",
            }

        async def release_image_credential(self, credential_name, lease_id):
            self.releases.append((credential_name, lease_id))
            return True

    manager = FakeCredentialManager()
    entered_upstream = asyncio.Event()
    upstream_urls = []

    async def fake_stream_post_async(**kwargs):
        upstream_urls.append(kwargs["url"])
        entered_upstream.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    monkeypatch.setattr(antigravity, "credential_manager", manager)
    monkeypatch.setattr(
        antigravity,
        "get_antigravity_api_url",
        lambda: async_value("https://antigravity.invalid"),
    )
    monkeypatch.setattr(
        antigravity,
        "wrap_cli_request",
        lambda *args, **kwargs: async_value(
            ({"project": "ultra-project", "request": {}}, "request-ultra")
        ),
    )
    monkeypatch.setattr(
        antigravity,
        "get_retry_config",
        lambda: async_value({"retry_enabled": True, "max_retries": 0, "retry_interval": 0}),
    )
    monkeypatch.setattr(antigravity, "get_auto_ban_error_codes", lambda: async_value([]))
    monkeypatch.setattr(antigravity, "stream_post_async", fake_stream_post_async)

    async def consume_stream():
        async for _ in antigravity.stream_request(
            {
                "model": "gemini-3.1-flash-image",
                "request": {"contents": [{"role": "user", "parts": [{"text": "draw"}]}]},
            }
        ):
            pass

    task = asyncio.create_task(consume_stream())
    await asyncio.wait_for(entered_upstream.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.releases == [("ultra.json", "lease-ultra")]
    assert upstream_urls == [
        "https://antigravity.invalid/v1internal:streamGenerateContent?alt=sse"
    ]


def test_capacity_exhausted_is_model_level_not_account_unavailable():
    from src.api.antigravity import is_image_model_capacity_exhausted

    assert is_image_model_capacity_exhausted(
        503,
        '{"error":{"details":[{"reason":"MODEL_CAPACITY_EXHAUSTED"}]}}',
    )
    assert not is_image_model_capacity_exhausted(503, "generic backend failure")
    assert not is_image_model_capacity_exhausted(429, "MODEL_CAPACITY_EXHAUSTED")


@pytest.mark.asyncio
async def test_image_outcome_helper_awaits_async_singleton_proxy(monkeypatch):
    from src.api import antigravity

    recorder = AsyncMock(return_value=None)
    manager = type("Manager", (), {"record_image_account_outcome": recorder})()
    monkeypatch.setattr(antigravity, "credential_manager", manager)

    await antigravity._record_image_account_outcome(
        "account.json",
        success=True,
        latency_seconds=1.25,
    )

    recorder.assert_awaited_once_with(
        "account.json",
        success=True,
        capacity_failure=False,
        latency_seconds=1.25,
    )


async def async_value(value):
    return value
