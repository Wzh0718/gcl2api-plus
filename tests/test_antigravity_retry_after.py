"""Retry-After is a cooldown deadline, not a reason to poll the upstream."""

import json
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Response

from src.api import antigravity as agy
from src import antigravity_pacing as pacing


NOW = 1_800_000_000.0


def quota_body(deadline):
    return json.dumps({"error": {"details": [{
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "QUOTA_EXHAUSTED",
        "metadata": {"quotaResetTimeStamp": datetime.fromtimestamp(
            deadline, timezone.utc
        ).isoformat()},
    }]}})


@pytest.fixture
def fixed_time(monkeypatch):
    monkeypatch.setattr(agy.time, "time", lambda: NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Retry-After", "retry-after", "ReTrY-AfTeR"])
async def test_delta_seconds_header_is_case_insensitive(monkeypatch, fixed_time, name):
    fallback = AsyncMock()
    monkeypatch.setattr(pacing, "transient_429_cooldown", fallback)
    assert await agy._resolve_error_cooldown(
        429, None, "account.json", headers={name: " 120 "}
    ) == NOW + 120
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_http_date_header_sets_future_deadline(fixed_time):
    value = format_datetime(datetime.fromtimestamp(NOW + 180, timezone.utc), usegmt=True)
    assert await agy._resolve_error_cooldown(429, None, headers={"Retry-After": value}) == NOW + 180


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "-1", "+1", "1.5", "NaN", "inf", "Infinity", "1e9", "１２", "garbage", "0", "9" * 400])
async def test_invalid_or_nonfuture_header_uses_existing_backoff(monkeypatch, fixed_time, value):
    fallback = lambda name: NOW + 240
    monkeypatch.setattr(pacing, "transient_429_cooldown", fallback)
    assert await agy._resolve_error_cooldown(
        429, None, "account.json", headers={"Retry-After": value}
    ) == NOW + 240


@pytest.mark.asyncio
async def test_past_http_date_uses_existing_backoff(monkeypatch, fixed_time):
    monkeypatch.setattr(pacing, "transient_429_cooldown", lambda name: NOW + 120)
    past = format_datetime(datetime.fromtimestamp(NOW - 1, timezone.utc), usegmt=True)
    assert await agy._resolve_error_cooldown(
        429, None, "account.json", headers={"Retry-After": past}
    ) == NOW + 120


@pytest.mark.asyncio
@pytest.mark.parametrize("body_delay,header_delay", [(300, 120), (120, 300)])
async def test_later_of_body_and_header_deadlines_wins(fixed_time, body_delay, header_delay):
    assert await agy._resolve_error_cooldown(
        429, quota_body(NOW + body_delay), headers={"Retry-After": str(header_delay)}
    ) == NOW + max(body_delay, header_delay)


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), float("-inf"), NOW - 1, NOW])
async def test_invalid_body_deadline_does_not_override_valid_header(monkeypatch, fixed_time, deadline):
    monkeypatch.setattr(agy, "parse_and_log_cooldown", AsyncMock(return_value=deadline))
    assert await agy._resolve_error_cooldown(
        429, "body", headers={"Retry-After": "120"}
    ) == NOW + 120


@pytest.mark.asyncio
async def test_expired_body_without_header_uses_existing_backoff(monkeypatch, fixed_time):
    monkeypatch.setattr(pacing, "transient_429_cooldown", lambda name: NOW + 300)
    assert await agy._resolve_error_cooldown(
        429, quota_body(NOW - 10), "account.json"
    ) == NOW + 300


@pytest.mark.asyncio
async def test_503_preserves_body_cooldown_and_supports_retry_after(fixed_time):
    assert await agy._resolve_error_cooldown(503, None) is None
    assert await agy._resolve_error_cooldown(503, quota_body(NOW + 300)) == NOW + 300
    assert await agy._resolve_error_cooldown(
        503, None, headers={"Retry-After": "120"}
    ) == NOW + 120


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stream", "non_stream", "image"])
async def test_real_request_paths_persist_retry_after_cooldown(monkeypatch, fixed_time, transport):
    """Only external boundaries are mocked; exercise each real cooldown call site."""
    manager = SimpleNamespace(
        get_valid_credential=AsyncMock(return_value=(
            "account.json", {"access_token": "test-token", "project_id": "test-project"}
        )),
        release_image_credential=AsyncMock(),
        record_image_account_outcome=AsyncMock(),
    )
    monkeypatch.setattr(agy, "credential_manager", manager)
    monkeypatch.setattr(agy, "get_antigravity_api_url", AsyncMock(return_value="https://upstream.invalid"))
    monkeypatch.setattr(agy, "get_antigravity_stream2nostream", AsyncMock(return_value=False))
    monkeypatch.setattr(agy, "get_antigravity_model_fallback_chain", AsyncMock(return_value=[]))
    monkeypatch.setattr(agy, "get_retry_config", AsyncMock(return_value={
        "retry_enabled": False, "max_retries": 0, "retry_interval": 0,
    }))
    monkeypatch.setattr(agy, "get_auto_ban_error_codes", AsyncMock(return_value=[]))
    monkeypatch.setattr(agy, "wrap_cli_request", AsyncMock(return_value=({"model": "test-model"}, "request-id")))
    monkeypatch.setattr(agy, "_text_credential_is_available", AsyncMock(return_value=True))
    monkeypatch.setattr(agy, "_alert_all_accounts_unavailable_if_needed", AsyncMock(return_value=False))
    monkeypatch.setattr(agy, "get_billing_recorder", AsyncMock(return_value=SimpleNamespace(record=AsyncMock())))
    monkeypatch.setattr(agy, "resolve_dynamic_image_model_for_credential", AsyncMock(return_value="gemini-3.1-flash-image"))
    monkeypatch.setattr(pacing, "pace_credential", AsyncMock(return_value=0))
    monkeypatch.setattr(pacing, "acquire_credential_slot", AsyncMock(return_value=0))
    monkeypatch.setattr(pacing, "release_credential_slot", lambda name: None)
    errors = AsyncMock()
    monkeypatch.setattr(agy, "record_api_call_error", errors)
    error = b'{"error":{"reason":"RATE_LIMIT_EXCEEDED"}}'
    upstream = SimpleNamespace(status_code=429, content=error, text=error.decode(), headers={"Retry-After": "180"}, json=lambda: json.loads(error))

    async def stream_response(**kwargs):
        yield Response(content=error, status_code=429, headers=upstream.headers)

    monkeypatch.setattr(agy, "stream_post_async", stream_response)
    monkeypatch.setattr(agy, "post_async", AsyncMock(return_value=upstream))
    monkeypatch.setattr(agy, "_post_image_generate", AsyncMock(return_value=SimpleNamespace(
        response=upstream, host="upstream.invalid", attempts=["attempt"]
    )))
    model = "gemini-3.1-flash-image" if transport == "image" else "gemini-test"
    body = {"model": model, "request": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}}
    if transport == "stream":
        responses = [item async for item in agy.stream_request(body)]
        assert responses[-1].status_code == 429
    else:
        response = await agy.non_stream_request(body)
        assert response.status_code == 429
    assert errors.await_count == 1
    assert errors.await_args.args[3] == NOW + 180
