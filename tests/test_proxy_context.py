import pytest


@pytest.mark.asyncio
async def test_http_client_proxy_mode_can_force_direct_or_custom(monkeypatch, tmp_path):
    from src.httpx_client import HttpxClientManager

    monkeypatch.setenv("PROXY", "http://global:8080")
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = HttpxClientManager()
    direct = await manager.get_client_kwargs(proxy_url=None)
    custom = await manager.get_client_kwargs(proxy_url="http://account:8080")
    inherited = await manager.get_client_kwargs()
    assert direct["proxy"] is None
    assert custom["proxy"] == "http://account:8080"
    assert inherited["proxy"] == "http://global:8080"


def test_billing_ranges_are_fixed_shanghai_natural_days():
    from datetime import date
    from src.storage.sqlite_manager import SQLiteManager

    assert SQLiteManager._billing_range_dates("7d", date(2026, 7, 27)) == [
        "2026-07-27", "2026-07-26", "2026-07-25", "2026-07-24", "2026-07-23", "2026-07-22", "2026-07-21"
    ]


@pytest.mark.asyncio
async def test_antigravity_retry_switches_token_project_proxy_and_billing_account(monkeypatch):
    import copy
    import httpx
    from src.api import antigravity

    class FakeCredentialManager:
        async def get_valid_credential(self, **kwargs):
            return "one.json", {
                "access_token": "token-one", "project_id": "project-one",
                "proxy_mode": "custom", "proxy_url": "http://proxy-one:8080",
            }

    calls = []
    responses = [
        httpx.Response(429, content=b'{"error":"retry"}'),
        httpx.Response(200, json={"response": {"usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}}}),
    ]

    async def fake_post_async(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return responses.pop(0)

    async def fake_switch(**kwargs):
        assert kwargs["apply_cred_result"](("two.json", {
            "access_token": "token-two", "project_id": "project-two",
            "proxy_mode": "direct", "proxy_url": None,
        }))
        return True, None

    class FakeRecorder:
        def __init__(self):
            self.records = []

        async def record(self, **kwargs):
            self.records.append(kwargs)
            return True

    recorder = FakeRecorder()

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(antigravity, "credential_manager", FakeCredentialManager())
    monkeypatch.setattr(antigravity, "get_antigravity_stream2nostream", lambda: async_value(False))
    monkeypatch.setattr(antigravity, "get_antigravity_api_url", lambda: async_value("https://example.invalid"))
    monkeypatch.setattr(antigravity, "get_retry_config", lambda: async_value({"retry_enabled": True, "max_retries": 1, "retry_interval": 0}))
    monkeypatch.setattr(antigravity, "get_auto_ban_error_codes", lambda: async_value([]))
    monkeypatch.setattr(antigravity, "wrap_cli_request", lambda request, model, project, user_email=None: async_value(({"project": project, "request": request}, "request-1")))
    monkeypatch.setattr(antigravity, "post_async", fake_post_async)
    monkeypatch.setattr(antigravity, "handle_error_with_retry", lambda *args, **kwargs: async_value(True))
    monkeypatch.setattr(antigravity, "_switch_credential_for_retry", fake_switch)
    monkeypatch.setattr(antigravity, "record_api_call_error", noop)
    monkeypatch.setattr(antigravity, "record_api_call_success", noop)
    monkeypatch.setattr(antigravity, "get_billing_recorder", lambda: async_value(recorder))

    response = await antigravity.non_stream_request({"model": "gemini-test", "request": {"contents": []}})

    assert response.status_code == 200
    assert calls[0]["proxy_url"] == "http://proxy-one:8080"
    assert calls[0]["json"]["project"] == "project-one"
    assert calls[0]["headers"]["Authorization"] == "Bearer token-one"
    assert calls[1]["proxy_url"] is None
    assert calls[1]["json"]["project"] == "project-two"
    assert calls[1]["headers"]["Authorization"] == "Bearer token-two"
    assert recorder.records == [{
        "request_id": "request-1", "credential_name": "two.json", "model": "gemini-test",
        "usage_metadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}, "success": True,
        "api_key_id": "env",
    }]


async def async_value(value):
    return value


@pytest.mark.asyncio
async def test_stream_to_non_stream_buffer_limit(monkeypatch):
    from src.api import utils

    async def tiny_limit():
        return 8

    async def stream():
        yield 'data: {"response":{"candidates":[{"content":{"parts":[{"text":"123456789"}]}}]}}'

    monkeypatch.setattr(utils, "get_max_non_stream_buffer_bytes", tiny_limit)
    response = await utils.collect_streaming_response(stream())
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_stream_to_non_stream_minimum_buffer_can_support_large_images(monkeypatch):
    from src.api import utils

    async def tiny_limit():
        return 8

    async def stream():
        yield 'data: {"response":{"candidates":[{"content":{"parts":[{"text":"123456789"}]}}]}}'

    monkeypatch.setattr(utils, "get_max_non_stream_buffer_bytes", tiny_limit)
    response = await utils.collect_streaming_response(
        stream(), minimum_buffer_bytes=16
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_antigravity_image_non_stream_bypasses_stream_collection(monkeypatch):
    from fastapi import Response
    from src.api import antigravity

    captured = {}

    async def fake_image_request(body, headers, api_key_id):
        captured["body"] = body
        captured["headers"] = headers
        captured["api_key_id"] = api_key_id
        return Response(content="{}", status_code=200, media_type="application/json")

    monkeypatch.setattr(
        antigravity,
        "get_antigravity_stream2nostream",
        lambda: async_value(True),
    )
    monkeypatch.setattr(
        antigravity,
        "stream_request",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("image requests must use fixed-length generateContent")
        ),
    )
    monkeypatch.setattr(
        antigravity, "_non_stream_image_request", fake_image_request
    )

    response = await antigravity.non_stream_request(
        {"model": "gemini-3.1-flash-image", "request": {"contents": []}}
    )

    assert response.status_code == 200
    assert captured["body"]["model"] == "gemini-3.1-flash-image"
    assert captured["headers"] is None
    assert captured["api_key_id"] == "env"
