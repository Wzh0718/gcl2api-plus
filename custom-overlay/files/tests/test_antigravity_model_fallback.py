"""Tests for Antigravity model fallback chain on quota exhaustion."""

import json

import pytest
from fastapi import Response

import src.api.antigravity as agy
from src.api.antigravity import (
    CREDITS_EXHAUSTED_MARKER,
    _is_quota_exhausted_429,
    _next_fallback_model,
    non_stream_request,
)


def _quota_429_body() -> str:
    return (
        '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": ['
        '{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "QUOTA_EXHAUSTED", '
        '"metadata": {"quotaResetTimeStamp": "2099-01-01T00:00:00Z", '
        '"quotaResetDelay": "3000s"}}]}}'
    )


def test_is_quota_exhausted_429():
    assert _is_quota_exhausted_429(429, _quota_429_body())
    assert _is_quota_exhausted_429(429, f'{{"error": "{CREDITS_EXHAUSTED_MARKER}"}}')
    assert _is_quota_exhausted_429(429, "Resource has been exhausted (e.g. check quota).")
    # 瞬时限流不触发降级
    assert not _is_quota_exhausted_429(429, '{"error": {"reason": "RATE_LIMIT_EXCEEDED"}}')
    assert not _is_quota_exhausted_429(429, None)
    assert not _is_quota_exhausted_429(403, _quota_429_body())
    assert not _is_quota_exhausted_429(200, _quota_429_body())


def test_next_fallback_model():
    chain = ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-3.6-flash-tiered"]
    assert _next_fallback_model(chain, "gemini-2.5-flash") == "gemini-3.1-flash-lite"
    assert _next_fallback_model(chain, "gemini-3.1-flash-lite") == "gemini-3.6-flash-tiered"
    # 链尾无下一个
    assert _next_fallback_model(chain, "gemini-3.6-flash-tiered") is None
    # 不在链中的模型不降级
    assert _next_fallback_model(chain, "gemini-2.5-pro") is None
    # 空链关闭
    assert _next_fallback_model([], "gemini-2.5-flash") is None


@pytest.mark.asyncio
async def test_fallback_chain_config_parsing(monkeypatch):
    from config import get_antigravity_model_fallback_chain

    monkeypatch.setenv(
        "ANTIGRAVITY_MODEL_FALLBACK_CHAIN",
        " gemini-2.5-flash , gemini-3.1-flash-lite,,gemini-2.5-flash ",
    )
    chain = await get_antigravity_model_fallback_chain()
    assert chain == ["gemini-2.5-flash", "gemini-3.1-flash-lite"]

    # 空串 = 关闭降级
    monkeypatch.setenv("ANTIGRAVITY_MODEL_FALLBACK_CHAIN", "")
    assert await get_antigravity_model_fallback_chain() == []

    # 未配置时使用默认链
    monkeypatch.delenv("ANTIGRAVITY_MODEL_FALLBACK_CHAIN")
    default_chain = await get_antigravity_model_fallback_chain()
    assert default_chain == [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.6-flash-tiered",
        "gemini-3.7-flash-tiered",
    ]


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b""):
        self.status_code = status_code
        self.content = content
        self.body = content
        self.headers = {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="ignore")

    def json(self):
        return json.loads(self.text)


def _patch_common(monkeypatch, chain, max_retries=2):
    monkeypatch.setattr(agy, "get_antigravity_api_url", lambda: _awaitable("http://agy.test"))
    monkeypatch.setattr(agy, "get_antigravity_model_fallback_chain", lambda: _awaitable(chain))
    monkeypatch.setattr(
        agy,
        "get_retry_config",
        lambda: _awaitable({"retry_enabled": True, "max_retries": max_retries, "retry_interval": 0.01}),
    )
    monkeypatch.setattr(agy, "get_auto_ban_error_codes", lambda: _awaitable([]))
    monkeypatch.setattr(agy, "handle_error_with_retry", lambda *a, **k: _awaitable(True))
    monkeypatch.setattr(agy, "record_api_call_error", lambda *a, **k: _awaitable(None))
    monkeypatch.setattr(agy, "record_api_call_success", lambda *a, **k: _awaitable(None))
    monkeypatch.setattr(agy, "_alert_all_accounts_unavailable_if_needed", lambda *a, **k: _awaitable(False))

    class _Recorder:
        async def record(self, **kwargs):
            return None

    monkeypatch.setattr(agy, "get_billing_recorder", lambda: _awaitable(_Recorder()))


async def _awaitable(value):
    return value


@pytest.mark.asyncio
async def test_non_stream_falls_back_to_next_model_on_quota_exhaustion(monkeypatch):
    """首个模型配额耗尽且无可用凭证时，应降级到链上下一个模型重试。"""
    chain = ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-3.6-flash-tiered"]
    _patch_common(monkeypatch, chain)

    cred = ("cred1.json", {"access_token": "token", "project_id": "proj", "user_email": "a@gmail.com"})
    entry_calls = {"done": False}

    async def fake_get_valid_credential(mode=None, model_name=None, **kwargs):
        # 首次（入口）放行 2.5-flash；之后 2.5-flash 号池耗尽，降级目标模型可用
        if model_name == "gemini-2.5-flash":
            if not entry_calls["done"]:
                entry_calls["done"] = True
                return cred
            return None
        return cred

    monkeypatch.setattr(
        agy.credential_manager, "get_valid_credential", fake_get_valid_credential
    )

    sent_models = []

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None):
        sent_models.append(json["model"])
        if json["model"] == "gemini-2.5-flash":
            return _FakeResponse(429, _quota_429_body().encode())
        return _FakeResponse(200, b'{"response": {"candidates": []}}')

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    body = {
        "model": "gemini-2.5-flash",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    }
    resp = await non_stream_request(body)

    assert resp.status_code == 200
    assert sent_models == ["gemini-2.5-flash", "gemini-3.1-flash-lite"]


@pytest.mark.asyncio
async def test_non_stream_no_fallback_when_chain_empty(monkeypatch):
    """降级链为空（关闭）时维持原行为：无可用凭证直接 500。"""
    _patch_common(monkeypatch, [])

    cred = ("cred1.json", {"access_token": "token", "project_id": "proj"})
    entry_calls = {"done": False}

    async def fake_get_valid_credential(mode=None, model_name=None, **kwargs):
        if not entry_calls["done"]:
            entry_calls["done"] = True
            return cred
        return None

    monkeypatch.setattr(
        agy.credential_manager, "get_valid_credential", fake_get_valid_credential
    )

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None):
        return _FakeResponse(429, _quota_429_body().encode())

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    body = {
        "model": "gemini-2.5-flash",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    }
    resp = await non_stream_request(body)

    assert resp.status_code == 500
    assert "无可用凭证" in json.loads(resp.body)["error"]


@pytest.mark.asyncio
async def test_non_stream_no_fallback_on_transient_429(monkeypatch):
    """瞬时限流 429（非配额耗尽）不触发模型降级。"""
    chain = ["gemini-2.5-flash", "gemini-3.1-flash-lite"]
    _patch_common(monkeypatch, chain)

    cred = ("cred1.json", {"access_token": "token", "project_id": "proj"})
    entry_calls = {"done": False}

    async def fake_get_valid_credential(mode=None, model_name=None, **kwargs):
        if not entry_calls["done"]:
            entry_calls["done"] = True
            return cred
        return None

    monkeypatch.setattr(
        agy.credential_manager, "get_valid_credential", fake_get_valid_credential
    )

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None):
        return _FakeResponse(429, b'{"error": {"reason": "RATE_LIMIT_EXCEEDED"}}')

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    body = {
        "model": "gemini-2.5-flash",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    }
    resp = await non_stream_request(body)

    assert resp.status_code == 500
    assert "无可用凭证" in json.loads(resp.body)["error"]


@pytest.mark.asyncio
async def test_non_stream_falls_back_when_retries_exhausted(monkeypatch):
    """重试次数耗尽（max_retries=0）时，配额耗尽 429 也应先走降级链再报错。"""
    chain = ["gemini-2.5-flash", "gemini-3.1-flash-lite"]
    _patch_common(monkeypatch, chain, max_retries=0)

    cred = ("cred1.json", {"access_token": "token", "project_id": "proj"})

    async def fake_get_valid_credential(mode=None, model_name=None, **kwargs):
        return cred

    monkeypatch.setattr(
        agy.credential_manager, "get_valid_credential", fake_get_valid_credential
    )

    sent_models = []

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None):
        sent_models.append(json["model"])
        if json["model"] == "gemini-2.5-flash":
            return _FakeResponse(429, _quota_429_body().encode())
        return _FakeResponse(200, b'{"response": {"candidates": []}}')

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    body = {
        "model": "gemini-2.5-flash",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    }
    resp = await non_stream_request(body)

    assert resp.status_code == 200
    assert sent_models == ["gemini-2.5-flash", "gemini-3.1-flash-lite"]


@pytest.mark.asyncio
async def test_stream_falls_back_when_retries_exhausted(monkeypatch):
    """流式路径：重试耗尽时配额耗尽 429 先降级，链路耗尽才向客户端报错。"""
    chain = ["gemini-2.5-flash", "gemini-3.1-flash-lite"]
    _patch_common(monkeypatch, chain, max_retries=0)

    cred = ("cred1.json", {"access_token": "token", "project_id": "proj"})

    async def fake_get_valid_credential(mode=None, model_name=None, **kwargs):
        return cred

    monkeypatch.setattr(
        agy.credential_manager, "get_valid_credential", fake_get_valid_credential
    )

    sent_models = []

    async def fake_stream_post_async(url=None, body=None, native=False, headers=None, proxy_url=None):
        sent_models.append(body["model"])
        if body["model"] == "gemini-2.5-flash":
            yield Response(content=_quota_429_body().encode(), status_code=429)
        else:
            yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream_post_async)

    body = {
        "model": "gemini-2.5-flash",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    }
    chunks = [item async for item in agy.stream_request(body)]

    assert sent_models == ["gemini-2.5-flash", "gemini-3.1-flash-lite"]
    assert chunks == ['data: {"candidates": []}\n\n']


@pytest.mark.asyncio
async def test_stream_returns_429_only_after_chain_exhausted(monkeypatch):
    """链上所有模型都配额耗尽后，才把 429 返回给客户端。"""
    chain = ["gemini-2.5-flash", "gemini-3.1-flash-lite"]
    _patch_common(monkeypatch, chain, max_retries=0)

    cred = ("cred1.json", {"access_token": "token", "project_id": "proj"})

    async def fake_get_valid_credential(mode=None, model_name=None, **kwargs):
        return cred

    monkeypatch.setattr(
        agy.credential_manager, "get_valid_credential", fake_get_valid_credential
    )

    sent_models = []

    async def fake_stream_post_async(url=None, body=None, native=False, headers=None, proxy_url=None):
        sent_models.append(body["model"])
        yield Response(content=_quota_429_body().encode(), status_code=429)

    monkeypatch.setattr(agy, "stream_post_async", fake_stream_post_async)

    body = {
        "model": "gemini-2.5-flash",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    }
    chunks = [item async for item in agy.stream_request(body)]

    # 链上每个模型都试过一遍之后，才返回最后的 429
    assert sent_models == ["gemini-2.5-flash", "gemini-3.1-flash-lite"]
    assert len(chunks) == 1 and chunks[0].status_code == 429
