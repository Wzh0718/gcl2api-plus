"""400 must describe the failure without treating a request error as an account ban."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.credential_manager import CredentialManager
from src.api import utils


@pytest.mark.parametrize("body,source,category", [
    ('{"error":{"code":400,"status":"INVALID_ARGUMENT","message":"Invalid request argument"}}', "generation", "invalid_argument"),
    ('{"error":{"code":400,"message":"Model gemini-unknown is not supported"}}', "generation", "model_error"),
    ('{"error":{"code":400,"message":"User location is not supported for the API use"}}', "generation", "geo_blocked"),
    ('{"error":{"code":400,"message":"Account requires verification"}}', "generation", "validation_required"),
    ('{"error":{"code":400,"message":"Account disabled for violation of Terms of Service"}}', "generation", "account_forbidden"),
    ('{"error":"invalid_grant","error_description":"Token has been expired or revoked."}', "oauth", "oauth_invalid_grant"),
    ('{"error":"invalid_client"}', "oauth", "oauth_client_error"),
    ('{"error":"unauthorized_client"}', "oauth", "oauth_client_error"),
    ('{"error":"invalid_request"}', "oauth", "oauth_request_error"),
    ("", "generation", "unknown_400"),
    ("<html>Bad Request</html>", "generation", "unknown_400"),
])
def test_classifies_400_by_body_and_source(body, source, category):
    from src.antigravity_400_classifier import classify_antigravity_400

    result = classify_antigravity_400(body, source=source)

    assert result.category == category
    assert result.source == source
    assert isinstance(result.reason, str) and result.reason
    assert isinstance(result.message, str) and result.message
    if category not in ("account_forbidden", "oauth_invalid_grant"):
        assert result.disable_account is False


def test_generation_does_not_interpret_oauth_words_in_message_as_revocation():
    from src.antigravity_400_classifier import classify_antigravity_400

    result = classify_antigravity_400('{"error":{"status":"INVALID_ARGUMENT","message":"Invalid field: invalid_grant"}}')

    assert result.category == "invalid_argument"
    assert result.disable_account is False


@pytest.mark.asyncio
async def test_configured_400_does_not_disable_or_rotate_antigravity_account(monkeypatch):
    monkeypatch.setattr(utils, "get_auto_ban_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(utils, "get_auto_ban_error_codes", AsyncMock(return_value=[400, 403]))
    manager = SimpleNamespace(set_cred_disabled=AsyncMock())

    retry = await utils.handle_error_with_retry(
        manager, 400, "account.json", True, 0, 3, 0, mode="antigravity"
    )

    assert retry is False
    manager.set_cred_disabled.assert_not_awaited()


@pytest.mark.asyncio
async def test_geminicli_configured_400_retains_existing_policy(monkeypatch):
    monkeypatch.setattr(utils, "get_auto_ban_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(utils, "get_auto_ban_error_codes", AsyncMock(return_value=[400, 403]))
    manager = SimpleNamespace(set_cred_disabled=AsyncMock())

    retry = await utils.handle_error_with_retry(
        manager, 400, "account.json", True, 0, 3, 0, mode="geminicli"
    )

    assert retry is True
    manager.set_cred_disabled.assert_awaited_once_with("account.json", True, mode="geminicli")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_message,category", [
    (None, "unknown_400"),
    ('{"error":{"status":"INVALID_ARGUMENT","message":"Bad input"}}', "invalid_argument"),
    ('{"error":{"message":"Model gemini-unknown is not supported"}}', "model_error"),
])
async def test_400_persists_structured_classification_even_without_response_body(error_message, category):
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = SimpleNamespace(_backend=SimpleNamespace())
    manager.update_credential_state = AsyncMock()

    await manager.record_api_call_result(
        "account.json", False, 400, mode="antigravity", error_message=error_message
    )

    updates = manager.update_credential_state.await_args.args[1]
    assert updates["error_codes"] == [400]
    saved = updates["error_messages"]["400"]
    data = json.loads(saved) if isinstance(saved, str) else saved
    assert data["category"] == category
    assert data["source"] == "generation"
    assert data["reason"]
    assert data["message"]


@pytest.mark.asyncio
async def test_persisted_400_diagnostics_exclude_upstream_secrets_and_request_content():
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = SimpleNamespace(_backend=SimpleNamespace())
    manager.update_credential_state = AsyncMock()
    body = json.dumps({"error": {
        "status": "INVALID_ARGUMENT",
        "message": "Invalid request for private-person@example.com: Bearer ya29.private-token-secret",
        "metadata": {"refresh_token": "1//private-refresh-secret", "project": "private-project-id"},
        "request": {"contents": "private-user-prompt-content"},
    }})

    await manager.record_api_call_result(
        "account.json", False, 400, mode="antigravity", error_message=body
    )

    updates = manager.update_credential_state.await_args.args[1]
    saved = updates["error_messages"]["400"]
    serialized = saved if isinstance(saved, str) else json.dumps(saved)
    for secret in ("private-person@example.com", "ya29.private-token-secret", "1//private-refresh-secret", "private-project-id", "private-user-prompt-content"):
        assert secret not in serialized
    data = json.loads(serialized)
    assert data["category"] == "invalid_argument"


@pytest.mark.parametrize("status", [None, 400, 401, 403])
@pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client", "unknown error"])
def test_client_configuration_or_unknown_refresh_failure_is_not_permanent_account_failure(status, error):
    manager = CredentialManager()

    assert manager._is_permanent_refresh_failure(json.dumps({"error": error}), status) is False


@pytest.mark.parametrize("error", ["invalid_grant", "invalid_refresh_token", "refresh_token_expired"])
def test_explicit_revoked_refresh_authorization_is_permanent(error):
    manager = CredentialManager()

    assert manager._is_permanent_refresh_failure(json.dumps({"error": error}), 400) is True


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_refresh_http_failure_remains_retryable(status):
    manager = CredentialManager()

    assert manager._is_permanent_refresh_failure("temporary upstream failure", status) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_ban_codes", [[400, 403], []])
@pytest.mark.parametrize("transport", ["stream", "non_stream", "image"])
@pytest.mark.parametrize("error_body", [
    b'{"error":{"status":"INVALID_ARGUMENT","message":"Invalid argument"}}',
    b'{"error":{"message":"Model gemini-unknown is not supported"}}',
    b'{"error":{"message":"Unclassified bad request"}}',
])
async def test_real_generation_400_returns_after_one_account_without_disabling(monkeypatch, transport, error_body, auto_ban_codes):
    from fastapi import Response
    from src.api import antigravity as agy
    from src import antigravity_pacing as pacing

    manager = SimpleNamespace(
        get_valid_credential=AsyncMock(return_value=(
            "account.json", {"access_token": "test-token", "project_id": "test-project"}
        )),
        set_cred_disabled=AsyncMock(),
        record_api_call_result=AsyncMock(),
        release_image_credential=AsyncMock(),
        record_image_account_outcome=AsyncMock(),
    )
    monkeypatch.setattr(agy, "credential_manager", manager)
    monkeypatch.setattr(agy, "get_antigravity_api_url", AsyncMock(return_value="https://upstream.invalid"))
    monkeypatch.setattr(agy, "get_antigravity_stream2nostream", AsyncMock(return_value=False))
    monkeypatch.setattr(agy, "get_antigravity_model_fallback_chain", AsyncMock(return_value=[]))
    monkeypatch.setattr(agy, "get_retry_config", AsyncMock(return_value={
        "retry_enabled": True, "max_retries": 3, "retry_interval": 0,
    }))
    monkeypatch.setattr(agy, "get_auto_ban_error_codes", AsyncMock(return_value=auto_ban_codes))
    monkeypatch.setattr(utils, "get_auto_ban_error_codes", AsyncMock(return_value=auto_ban_codes))
    monkeypatch.setattr(utils, "get_auto_ban_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(agy, "wrap_cli_request", AsyncMock(return_value=({"model": "test-model"}, "request-id")))
    monkeypatch.setattr(agy, "_text_credential_is_available", AsyncMock(return_value=True))
    monkeypatch.setattr(agy, "_alert_all_accounts_unavailable_if_needed", AsyncMock(return_value=False))
    monkeypatch.setattr(agy, "get_billing_recorder", AsyncMock(return_value=SimpleNamespace(record=AsyncMock())))
    monkeypatch.setattr(agy, "resolve_dynamic_image_model_for_credential", AsyncMock(return_value="gemini-3.1-flash-image"))
    monkeypatch.setattr(pacing, "pace_credential", AsyncMock(return_value=0))
    monkeypatch.setattr(pacing, "acquire_credential_slot", AsyncMock(return_value=0))
    monkeypatch.setattr(pacing, "release_credential_slot", lambda name: None)
    upstream = SimpleNamespace(status_code=400, content=error_body, text=error_body.decode(), headers={}, json=lambda: json.loads(error_body))
    stream_calls = []

    async def stream_response(**kwargs):
        stream_calls.append(kwargs)
        yield Response(content=error_body, status_code=400)

    post = AsyncMock(return_value=upstream)
    image_post = AsyncMock(return_value=SimpleNamespace(response=upstream, host="upstream.invalid", attempts=["attempt"]))
    monkeypatch.setattr(agy, "stream_post_async", stream_response)
    monkeypatch.setattr(agy, "post_async", post)
    monkeypatch.setattr(agy, "_post_image_generate", image_post)
    model = "gemini-3.1-flash-image" if transport == "image" else "gemini-test"
    body = {"model": model, "request": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}}

    if transport == "stream":
        responses = [item async for item in agy.stream_request(body)]
        response = responses[-1]
        assert len(stream_calls) == 1
    else:
        response = await agy.non_stream_request(body)
        assert (image_post if transport == "image" else post).await_count == 1

    assert response.status_code == 400
    assert response.body == error_body
    assert manager.get_valid_credential.await_count == 1
    manager.set_cred_disabled.assert_not_awaited()
    assert manager.record_api_call_result.await_count == 1
    recorded = manager.record_api_call_result.await_args
    assert recorded.args[:3] == ("account.json", False, 400)
    assert recorded.kwargs["error_message"] == error_body.decode()


@pytest.mark.asyncio
async def test_refresh_invalid_grant_persists_nonempty_oauth_diagnosis_before_disable(monkeypatch):
    from src import credential_manager as module
    from src.google_oauth_api import TokenError

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = SimpleNamespace(_backend=SimpleNamespace())
    manager.update_credential_state = AsyncMock(return_value=True)
    failure = TokenError('{"error":"invalid_grant","error_description":"Token has been expired or revoked."}')
    failure.status_code = 400
    creds = SimpleNamespace(refresh_token="test-refresh", refresh=AsyncMock(side_effect=failure))
    monkeypatch.setattr(module.Credentials, "from_dict", lambda data: creds)

    result = await manager._refresh_token({"proxy_mode": "direct"}, "account.json", mode="antigravity")

    assert result is None
    updates = [call.args[1] for call in manager.update_credential_state.await_args_list]
    diagnosis = next(update["error_messages"]["400"] for update in updates if "error_messages" in update)
    data = json.loads(diagnosis) if isinstance(diagnosis, str) else diagnosis
    assert data["source"] == "oauth"
    assert data["category"] == "oauth_invalid_grant"
    assert data["reason"]
    assert {"disabled": True} in updates


@pytest.mark.asyncio
async def test_oauth_refresh_exception_preserves_status_and_reason_without_tokens(monkeypatch):
    from src import google_oauth_api as oauth

    client = SimpleNamespace(key="test-client", client_id="test-id", client_secret="private-client-secret")
    monkeypatch.setattr(oauth, "get_candidates", lambda preferred: [client])
    monkeypatch.setattr(oauth, "get_oauth_proxy_url", AsyncMock(return_value="https://oauth.invalid"))
    response = SimpleNamespace(status_code=400, text=json.dumps({
        "error": "invalid_grant",
        "error_description": "Rejected private-refresh-token-secret and private-client-secret",
    }))
    monkeypatch.setattr(oauth, "post_async", AsyncMock(return_value=response))

    with pytest.raises(oauth.TokenError) as captured:
        await oauth.refresh_access_token_with_clients("private-refresh-token-secret")

    assert captured.value.status_code == 400
    assert captured.value.error_code == "invalid_grant"
    assert "private-refresh-token-secret" not in str(captured.value)
    assert "private-client-secret" not in str(captured.value)


def test_generation_tos_400_is_diagnostic_only_and_cannot_disable_account():
    from src.antigravity_400_classifier import classify_antigravity_400

    result = classify_antigravity_400('{"error":{"message":"Account disabled for violation of Terms of Service"}}')

    assert result.category == "account_forbidden"
    assert result.disable_account is False


def test_explicit_oauth_client_error_takes_precedence_over_error_description_mentions():
    from src.antigravity_400_classifier import classify_antigravity_400

    body = json.dumps({
        "error": "invalid_client",
        "error_description": "Client configuration rejected; this is not invalid_grant.",
    })
    decision = classify_antigravity_400(body, source="oauth")

    assert decision.category == "oauth_client_error"
    assert decision.disable_account is False
    assert CredentialManager()._is_permanent_refresh_failure(body, 400) is False


@pytest.mark.asyncio
async def test_refresh_uses_final_structured_oauth_error_instead_of_previous_client_summary(monkeypatch):
    from src import credential_manager as module
    from src.google_oauth_api import TokenError

    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = SimpleNamespace(_backend=SimpleNamespace())
    manager.update_credential_state = AsyncMock(return_value=True)
    failure = TokenError(
        'All clients failed: first {"reason":"INVALID_GRANT"}; final {"reason":"INVALID_CLIENT"}',
        status_code=400,
        error_code="invalid_client",
    )
    creds = SimpleNamespace(refresh_token="test-refresh", refresh=AsyncMock(side_effect=failure))
    monkeypatch.setattr(module.Credentials, "from_dict", lambda data: creds)

    result = await manager._refresh_token({"proxy_mode": "direct"}, "account.json", mode="antigravity")

    assert result is None
    updates = [call.args[1] for call in manager.update_credential_state.await_args_list]
    assert not any(update.get("disabled") is True for update in updates)
    diagnosis = next(update["error_messages"]["400"] for update in updates if "error_messages" in update)
    data = json.loads(diagnosis) if isinstance(diagnosis, str) else diagnosis
    assert data["category"] == "oauth_client_error"


def test_400_log_detail_keeps_classification_and_excludes_raw_sensitive_content():
    from src.antigravity_400_classifier import upstream_error_log_detail

    body = json.dumps({"error": {
        "status": "INVALID_ARGUMENT",
        "message": "Bad request private-person@example.com Bearer private-access-token",
        "request": {"contents": "private-user-prompt"},
        "metadata": {"refresh_token": "private-refresh-token"},
    }})
    logged = upstream_error_log_detail(400, body)

    assert "INVALID_ARGUMENT" in logged
    for secret in ("private-person@example.com", "private-access-token", "private-user-prompt", "private-refresh-token"):
        assert secret not in logged
