"""Tests for Antigravity model fallback chain on quota exhaustion."""

import asyncio
import json
from types import SimpleNamespace

import src.antigravity_pacing as pacing
import src.storage_adapter as storage

import httpx
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

    # 未配置时默认关闭降级（默认链为空）
    monkeypatch.delenv("ANTIGRAVITY_MODEL_FALLBACK_CHAIN")
    default_chain = await get_antigravity_model_fallback_chain()
    assert default_chain == []


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
    monkeypatch.setattr(agy, "check_should_auto_ban", lambda status: _awaitable(False))
    monkeypatch.setattr(agy, "handle_error_with_retry", lambda *a, **k: _awaitable(True))
    monkeypatch.setattr(agy, "record_api_call_error", lambda *a, **k: _awaitable(None))
    monkeypatch.setattr(agy, "record_api_call_success", lambda *a, **k: _awaitable(None))
    monkeypatch.setattr(agy, "_alert_all_accounts_unavailable_if_needed", lambda *a, **k: _awaitable(False))

    class _Recorder:
        async def record(self, **kwargs):
            return None

    monkeypatch.setattr(agy, "get_billing_recorder", lambda: _awaitable(_Recorder()))
    monkeypatch.setattr(
        storage, "get_storage_adapter",
        lambda: _awaitable(SimpleNamespace(get_credential_state=lambda *a, **k: _awaitable({"disabled": False}))),
    )


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

    assert resp.status_code == 429
    assert json.loads(resp.body)["error"]


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

    assert resp.status_code == 429
    assert json.loads(resp.body)["error"]


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


async def _run_text_request(streaming, model):
    body = {"model": model, "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}}
    if streaming:
        chunks = [item async for item in agy.stream_request(body)]
        return chunks[-1] if chunks and isinstance(chunks[-1], Response) else Response(status_code=200)
    return await non_stream_request(body)


def _patch_text_transport(monkeypatch, respond):
    async def post(**kwargs):
        return respond(kwargs["json"]["model"], kwargs["headers"]["Authorization"])

    async def stream(**kwargs):
        response = respond(kwargs["body"]["model"], kwargs["headers"]["Authorization"])
        if response.status_code == 200:
            yield 'data: {"candidates": []}\n\n'
        else:
            yield Response(content=response.content, status_code=response.status_code)

    monkeypatch.setattr(agy, "post_async", post)
    monkeypatch.setattr(agy, "stream_post_async", stream)
    monkeypatch.setattr(agy, "get_antigravity_stream2nostream", lambda: _awaitable(False))


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tail_429_tries_distinct_credentials_then_preserves_429(monkeypatch, streaming):
    """即使时钟越过CD，同一请求也不能绕回已试凭证。"""
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model], max_retries=5)
    selected = []
    sent = []
    sleeps = []
    creds = [(name, {"access_token": name, "project_id": "proj"}) for name in ("a.json", "b.json")]

    async def choose(**kwargs):
        excluded = set(kwargs.get("exclude_filenames") or ())
        selected.append(excluded)
        return next((cred for cred in creds if cred[0] not in excluded), None)

    async def sleep(delay):
        sleeps.append(delay)

    def respond(model, token):
        sent.append((model, token))
        return _FakeResponse(429, b'{"error":{"reason":"RATE_LIMIT_EXCEEDED"}}')

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(agy.asyncio, "sleep", sleep)
    _patch_text_transport(monkeypatch, respond)
    response = await _run_text_request(streaming, model)
    assert sent == [(model, "Bearer a.json"), (model, "Bearer b.json")]
    assert response.status_code == 429
    assert response.body == b'{"error":{"reason":"RATE_LIMIT_EXCEEDED"}}'
    assert {"a.json", "b.json"} in selected
    # 无可用账号时不等待（唯一退避发生在A→B之间）。
    assert len(sleeps) <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_rechecks_cooldown_after_slot_wait_before_sending(monkeypatch, streaming):
    """凭证预选后被其它请求冷却，slot等待结束不能继续发出。"""
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model])
    cooled = set()
    sent = []
    released = []

    async def state(name, **kwargs):
        return {"model_cooldowns": {model: agy.time.time() + 300} if name in cooled else {}}

    async def choose(**kwargs):
        excluded = set(kwargs.get("exclude_filenames") or ())
        for name in ("a.json", "b.json"):
            if name not in excluded and name not in cooled:
                return name, {"access_token": name, "project_id": "proj"}
        return None

    async def acquire(name):
        if name == "a.json":
            cooled.add(name)
        return 0.0

    def respond(model, token):
        sent.append(token)
        return _FakeResponse(200, b'{"response":{"candidates":[]}}')

    monkeypatch.setattr(storage, "get_storage_adapter", lambda: _awaitable(SimpleNamespace(get_credential_state=state)))
    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(pacing, "acquire_credential_slot", acquire)
    monkeypatch.setattr(pacing, "release_credential_slot", released.append)
    _patch_text_transport(monkeypatch, respond)
    response = await _run_text_request(streaming, model)
    assert response.status_code == 200
    assert sent == ["Bearer b.json"]
    assert released == ["a.json", "b.json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_fallback_does_not_reuse_credential_cooled_on_next_model(monkeypatch, streaming):
    first, tail = "gemini-2.5-flash", "gemini-3.1-flash-lite"
    _patch_common(monkeypatch, [first, tail], max_retries=0)
    sent = []

    async def state(name, **kwargs):
        return {"model_cooldowns": {tail: agy.time.time() + 300} if name == "a.json" else {}}

    async def choose(**kwargs):
        name = "a.json" if kwargs["model_name"] == first else "b.json"
        return name, {"access_token": name, "project_id": "proj"}

    def respond(model, token):
        sent.append((model, token))
        return _FakeResponse(429, _quota_429_body().encode()) if model == first else _FakeResponse(200, b'{"response":{"candidates":[]}}')

    monkeypatch.setattr(storage, "get_storage_adapter", lambda: _awaitable(SimpleNamespace(get_credential_state=state)))
    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    _patch_text_transport(monkeypatch, respond)
    response = await _run_text_request(streaming, first)
    assert response.status_code == 200
    assert sent == [(first, "Bearer a.json"), (tail, "Bearer b.json")]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("retry_enabled,max_retries", [(False, 5), (True, 1)])
async def test_tail_429_respects_retry_switch_and_budget(monkeypatch, streaming, retry_enabled, max_retries):
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model], max_retries=max_retries)
    monkeypatch.setattr(agy, "get_retry_config", lambda: _awaitable({
        "retry_enabled": retry_enabled, "max_retries": max_retries, "retry_interval": 0,
    }))
    sent = []

    async def choose(**kwargs):
        excluded = set(kwargs.get("exclude_filenames") or ())
        name = next(name for name in ("a.json", "b.json", "c.json") if name not in excluded)
        return name, {"access_token": name, "project_id": "proj"}

    def respond(model, token):
        sent.append(token)
        return _FakeResponse(429, b'{"error":{"reason":"RATE_LIMIT_EXCEEDED"}}')

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    _patch_text_transport(monkeypatch, respond)
    response = await _run_text_request(streaming, model)
    assert response.status_code == 429
    assert sent == (["Bearer a.json", "Bearer b.json"] if retry_enabled else ["Bearer a.json"])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_429_cooldown_persists_for_later_requests(monkeypatch, streaming):
    """真实record_api_call_error把CD传给存储；后续请求不能重新命中A。"""
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model], max_retries=0)
    states = {name: {"model_cooldowns": {}} for name in ("a.json", "b.json")}
    recorded = []
    sent = []

    async def state(name, **kwargs):
        return states[name]

    async def record(manager, name, status_code, cooldown_until, **kwargs):
        states[name]["model_cooldowns"][kwargs["model_name"]] = cooldown_until
        recorded.append((name, kwargs["model_name"], cooldown_until))

    async def choose(**kwargs):
        for name, state in states.items():
            if state["model_cooldowns"].get(model, 0) <= agy.time.time():
                return name, {"access_token": name, "project_id": "proj"}
        return None

    def respond(model, token):
        sent.append(token)
        return _FakeResponse(429, b'{"error":{"reason":"RATE_LIMIT_EXCEEDED"}}')

    monkeypatch.setattr(storage, "get_storage_adapter", lambda: _awaitable(SimpleNamespace(get_credential_state=state)))
    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(agy, "record_api_call_error", record)
    _patch_text_transport(monkeypatch, respond)
    assert (await _run_text_request(streaming, model)).status_code == 429
    assert (await _run_text_request(streaming, model)).status_code == 429
    assert sent == ["Bearer a.json", "Bearer b.json"]
    assert [(name, model_name) for name, model_name, _ in recorded] == [("a.json", model), ("b.json", model)]
    assert all(until > agy.time.time() for _, _, until in recorded)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_cancellation_during_cooldown_recheck_releases_slot(monkeypatch, streaming):
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model])
    entered = asyncio.Event()
    released = []
    sent = []

    async def state(name, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(storage, "get_storage_adapter", lambda: _awaitable(SimpleNamespace(get_credential_state=state)))
    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", lambda **k: _awaitable(("a.json", {"access_token": "a", "project_id": "proj"})))
    monkeypatch.setattr(pacing, "acquire_credential_slot", lambda name: _awaitable(0))
    monkeypatch.setattr(pacing, "release_credential_slot", released.append)
    _patch_text_transport(monkeypatch, lambda *args: sent.append(args))
    task = asyncio.create_task(_run_text_request(streaming, model))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sent
    assert released == ["a.json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_stale_last_credential_stops_without_wait_or_upstream(monkeypatch, streaming):
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model])
    selected = []
    sent = []
    waits = []

    async def choose(**kwargs):
        selected.append(set(kwargs.get("exclude_filenames") or ()))
        if len(selected) == 1:
            return "a.json", {"access_token": "a", "project_id": "proj"}
        return None

    monkeypatch.setattr(storage, "get_storage_adapter", lambda: _awaitable(SimpleNamespace(
        get_credential_state=lambda *a, **k: _awaitable({"model_cooldowns": {model: agy.time.time() + 300}}),
    )))
    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(agy.asyncio, "sleep", lambda delay: _awaitable(waits.append(delay)))
    _patch_text_transport(monkeypatch, lambda *args: sent.append(args))
    response = await _run_text_request(streaming, model)
    assert response.status_code == 429
    assert selected == [set(), {"a.json"}]
    assert not sent
    assert not waits


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_text_429_keeps_auto_ban_without_sleep_when_pool_exhausted(monkeypatch, streaming):
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model])
    banned, waits = [], []

    async def choose(**kwargs):
        if "a.json" not in (kwargs.get("exclude_filenames") or ()):
            return "a.json", {"access_token": "a", "project_id": "proj"}
        return None

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(agy, "check_should_auto_ban", lambda status: _awaitable(status == 429))
    monkeypatch.setattr(agy.credential_manager, "set_cred_disabled", lambda name, disabled, **kw: _awaitable(banned.append((name, disabled, kw))))
    monkeypatch.setattr(agy.asyncio, "sleep", lambda delay: _awaitable(waits.append(delay)))
    _patch_text_transport(monkeypatch, lambda *args: _FakeResponse(429, b'{"error":"rate limited"}'))
    response = await _run_text_request(streaming, model)
    assert response.status_code == 429
    assert banned == [("a.json", True, {"mode": "antigravity"})]
    assert not waits


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["503", "empty", "network"])
@pytest.mark.parametrize("recovers", [False, True])
async def test_non_429_failure_can_retry_same_only_credential(monkeypatch, streaming, failure, recovers):
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model], max_retries=1)
    sent = []

    async def choose(**kwargs):
        if "a.json" not in (kwargs.get("exclude_filenames") or ()):
            return "a.json", {"access_token": "a", "project_id": "proj"}
        return None

    def respond(*args):
        sent.append(args)
        if len(sent) == 1 or not recovers:
            if failure == "network":
                raise RuntimeError("network failed")
            return _FakeResponse(503, b'{"error":"unavailable"}') if failure == "503" else _FakeResponse(200, b"")
        return _FakeResponse(200, b'{"response":{"candidates":[]}}')

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    _patch_text_transport(monkeypatch, respond)
    if streaming and failure == "empty":
        async def stream(**kwargs):
            response = respond(kwargs["body"]["model"], kwargs["headers"]["Authorization"])
            if response.content:
                yield 'data: {"candidates": []}\n\n'
        monkeypatch.setattr(agy, "stream_post_async", stream)
    response = await _run_text_request(streaming, model)
    assert response.status_code == (200 if recovers else 503 if failure == "503" else 500)
    assert len(sent) == 2
    assert all(token == "Bearer a" for _, token in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("retry_enabled,max_retries", [(False, 5), (True, 0)])
async def test_stale_guard_obeys_retry_switch_and_budget(monkeypatch, streaming, retry_enabled, max_retries):
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model])
    monkeypatch.setattr(agy, "get_retry_config", lambda: _awaitable({
        "retry_enabled": retry_enabled, "max_retries": max_retries, "retry_interval": 0,
    }))
    selected, sent = [], []
    cooled = set()

    async def choose(**kwargs):
        name = "a.json" if not selected else "b.json"
        selected.append(name)
        return name, {"access_token": name, "project_id": "proj"}

    async def acquire(name):
        if name == "a.json":
            cooled.add(name)
        return 0

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(pacing, "acquire_credential_slot", acquire)
    monkeypatch.setattr(storage, "get_storage_adapter", lambda: _awaitable(SimpleNamespace(
        get_credential_state=lambda name, **kw: _awaitable({"model_cooldowns": {model: agy.time.time() + 300} if name in cooled else {}}),
    )))
    _patch_text_transport(monkeypatch, lambda *args: sent.append(args) or _FakeResponse(200, b'{}'))
    response = await _run_text_request(streaming, model)
    assert response.status_code == 429
    assert selected == ["a.json"]
    assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_text_entry_with_no_available_credential_returns_429(monkeypatch, streaming):
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model])
    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", lambda **kw: _awaitable(None))
    _patch_text_transport(monkeypatch, lambda *args: pytest.fail("must not send upstream"))
    assert (await _run_text_request(streaming, model)).status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("stale_state", ["disabled", "cooldown"])
async def test_stale_guard_maps_previous_validation_403_to_503(monkeypatch, streaming, stale_state):
    """A待验证403后，预选B失效且预算耗尽，最终仍须将403映射为503。"""
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model], max_retries=1)
    selected, sent = [], []
    stale = set()
    error_body = b'{"error":{"reason":"VALIDATION_REQUIRED"}}'

    async def choose(**kwargs):
        name = "a.json" if not selected else "b.json"
        selected.append(name)
        return name, {"access_token": name, "project_id": "proj"}

    async def acquire(name):
        if name == "b.json":
            stale.add(name)
        return 0

    async def state(name, **kwargs):
        if name not in stale:
            return {"disabled": False}
        if stale_state == "disabled":
            return {"disabled": True}
        return {"model_cooldowns": {model: agy.time.time() + 300}}

    def respond(model, token):
        sent.append(token)
        return _FakeResponse(403, error_body)

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(agy.credential_manager, "get_antigravity_account_health", lambda *a, **kw: _awaitable({}))
    monkeypatch.setattr(agy.credential_manager, "update_antigravity_account_health", lambda *a, **kw: _awaitable(None))
    monkeypatch.setattr(pacing, "acquire_credential_slot", acquire)
    monkeypatch.setattr(storage, "get_storage_adapter", lambda: _awaitable(SimpleNamespace(get_credential_state=state)))
    _patch_text_transport(monkeypatch, respond)
    response = await _run_text_request(streaming, model)
    assert response.status_code == 503
    assert response.body == error_body
    assert selected == ["a.json", "b.json"]
    assert sent == ["Bearer a.json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 400])
async def test_non_stream_removes_encoding_headers_from_decoded_upstream_body(monkeypatch, status_code):
    model = "gemini-2.5-flash"
    _patch_common(monkeypatch, [model])
    monkeypatch.setattr(
        agy.credential_manager,
        "get_valid_credential",
        lambda **kw: _awaitable(("a.json", {"access_token": "token", "project_id": "proj"})),
    )
    response_body = (
        b'{"response":{"candidates":[]}}'
        if status_code == 200
        else b'{"error":{"code":400,"status":"INVALID_ARGUMENT"}}'
    )

    async def post(**kwargs):
        response = _FakeResponse(status_code, response_body)
        response.headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Content-Encoding": "gzip",
            "Content-Length": "1234",
            "X-Upstream-Trace-Id": "trace-1",
        }
        return response

    _patch_text_transport(monkeypatch, lambda *args: pytest.fail("unexpected response"))
    monkeypatch.setattr(agy, "post_async", post)

    response = await non_stream_request(
        {"model": model, "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}}
    )

    assert response.status_code == status_code
    assert response.body == response_body
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(response_body))
    assert response.headers["content-type"] == "application/json; charset=UTF-8"
    assert response.headers["x-upstream-trace-id"] == "trace-1"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=response),
        base_url="http://testserver",
    ) as client:
        downstream = await client.post("/antigravity/v1beta/models/model:generateContent")
    assert downstream.status_code == status_code
    assert downstream.json() == json.loads(response_body)
